"""
recorder.py — Screen & Audio Recorder Module
=============================================
Records the full desktop (all monitors combined) + system audio using FFmpeg
for video and PyAudioWPatch (WASAPI loopback) for audio.

Architecture:
  - Recording is split into SEGMENTS to support Pause/Resume
  - Each segment: FFmpeg records VIDEO only, _AudioCaptureThread records AUDIO
    to a parallel WAV file.
  - On Pause: both stop → segment pair (mp4+wav) saved
  - On Resume: new segment pair started
  - On Stop: _FinalizeWorker thread concatenates video, concatenates WAV,
    muxes them into the final MP4 (never blocks the Qt main thread!).
  - Final file is logged to SQLite

Audio strategy (Windows):
  1. pyaudiowpatch WASAPI loopback  ← captures ALL Windows audio (speakers)
  2. No audio                         ← last resort if PyAudio unavailable

Signals (all Qt-thread-safe):
  recording_started(client_name)
  recording_stopped(file_path, duration_seconds)
  recording_error(error_message)
  status_changed(short_ui_text)
"""

from __future__ import annotations

import os
import re
import sys
import json
import shutil
import subprocess
import tempfile
import threading
import wave
from datetime import datetime
from typing import Optional

from PySide6.QtCore import QObject, Signal, QThread


# ──────────────────────────────────────────────────────────────────────────────
# Monitor geometry
# ──────────────────────────────────────────────────────────────────────────────

def get_virtual_screen_geometry() -> tuple[int, int, int, int]:
    """
    Return (x, y, width, height) of the combined virtual desktop.
    Tries screeninfo first; falls back to ctypes (always on Windows).
    """
    try:
        import screeninfo
        monitors = screeninfo.get_monitors()
        if monitors:
            x_min = min(m.x for m in monitors)
            y_min = min(m.y for m in monitors)
            x_max = max(m.x + m.width  for m in monitors)
            y_max = max(m.y + m.height for m in monitors)
            return x_min, y_min, x_max - x_min, y_max - y_min
    except Exception:
        pass

    try:
        import ctypes
        u32 = ctypes.windll.user32
        x = u32.GetSystemMetrics(76)   # SM_XVIRTUALSCREEN
        y = u32.GetSystemMetrics(77)   # SM_YVIRTUALSCREEN
        w = u32.GetSystemMetrics(78)   # SM_CXVIRTUALSCREEN
        h = u32.GetSystemMetrics(79)   # SM_CYVIRTUALSCREEN
        if w > 0 and h > 0:
            return x, y, w, h
    except Exception:
        pass

    return 0, 0, 1920, 1080


# ──────────────────────────────────────────────────────────────────────────────
# Audio capture via PyAudioWPatch (WASAPI loopback)
# ──────────────────────────────────────────────────────────────────────────────

def get_wasapi_loopback_device():
    """Return (device_info_dict, PyAudio_instance) for the default WASAPI loopback.
    Returns (None, None) if pyaudiowpatch is unavailable."""
    try:
        import pyaudiowpatch as pyaudio
        p = pyaudio.PyAudio()
        device = p.get_default_wasapi_loopback()
        return device, p
    except Exception as e:
        print(f"[Recorder] WASAPI loopback unavailable: {e}", file=sys.stderr)
        return None, None


class _AudioCaptureThread(threading.Thread):
    """
    Captures WASAPI loopback audio to a WAV file in a background thread.
    Call stop() to signal it to finish, then join().
    """
    def __init__(self, output_path: str):
        super().__init__(daemon=True)
        self.output_path = output_path
        self._stop_event = threading.Event()
        self.ok = False

    def run(self):
        try:
            import pyaudiowpatch as pyaudio
            p = pyaudio.PyAudio()
            device = p.get_default_wasapi_loopback()
            channels = device["maxInputChannels"]
            rate     = int(device["defaultSampleRate"])

            with wave.open(self.output_path, "wb") as wf:
                wf.setnchannels(channels)
                wf.setsampwidth(p.get_sample_size(pyaudio.paInt16))
                wf.setframerate(rate)

                def callback(in_data, frame_count, time_info, status):
                    if self._stop_event.is_set():
                        return (b"\x00" * len(in_data), pyaudio.paComplete)
                    wf.writeframes(in_data)
                    return (None, pyaudio.paContinue)

                stream = p.open(
                    format=pyaudio.paInt16,
                    channels=channels,
                    rate=rate,
                    frames_per_buffer=1024,
                    input=True,
                    input_device_index=device["index"],
                    stream_callback=callback,
                )
                stream.start_stream()
                while not self._stop_event.is_set() and stream.is_active():
                    threading.Event().wait(0.05)
                stream.stop_stream()
                stream.close()

            p.terminate()
            self.ok = os.path.exists(self.output_path) and os.path.getsize(self.output_path) > 44
            if self.ok:
                print(f"[Recorder] Audio WAV saved: {self.output_path}", file=sys.stderr)
        except Exception as e:
            print(f"[Recorder] Audio capture error: {e}", file=sys.stderr)
            self.ok = False

    def stop(self):
        self._stop_event.set()


# ──────────────────────────────────────────────────────────────────────────────
# Misc helpers
# ──────────────────────────────────────────────────────────────────────────────

def get_video_duration(file_path: str) -> int:
    """Use ffprobe to get video duration in whole seconds."""
    ffprobe = shutil.which("ffprobe") or "ffprobe"
    try:
        result = subprocess.run(
            [ffprobe, "-v", "quiet", "-print_format", "json",
             "-show_format", file_path],
            capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=15,
        )
        data = json.loads(result.stdout)
        return int(float(data.get("format", {}).get("duration", 0)))
    except Exception:
        return 0


def _fmt_size(size_bytes: int) -> str:
    if size_bytes < 1024 ** 2:
        return f"{size_bytes / 1024:.0f} KB"
    if size_bytes < 1024 ** 3:
        return f"{size_bytes / 1024 ** 2:.1f} MB"
    return f"{size_bytes / 1024 ** 3:.2f} GB"


# ──────────────────────────────────────────────────────────────────────────────
# Recording state machine
# ──────────────────────────────────────────────────────────────────────────────

class RecordingState:
    IDLE      = "idle"
    RECORDING = "recording"
    PAUSED    = "paused"
    STOPPING  = "stopping"


# ──────────────────────────────────────────────────────────────────────────────
# Segment worker — one FFmpeg + one AudioCaptureThread per segment
# ──────────────────────────────────────────────────────────────────────────────

class _SegmentWorker(QThread):
    """
    Runs FFmpeg (video only) + _AudioCaptureThread in parallel for one segment.
    Emits finished(video_path, audio_wav_path, ok) when done.
    audio_wav_path is empty string if audio capture failed or was unavailable.
    """
    finished = Signal(str, str, bool)  # video_path, audio_path, ok

    def __init__(self, cmd: list[str], segment_path: str, audio_path: str):
        super().__init__()
        self.cmd          = cmd
        self.segment_path = segment_path
        self.audio_path   = audio_path
        self._process: Optional[subprocess.Popen] = None
        self._audio_thread: Optional[_AudioCaptureThread] = None

    def run(self):
        audio_ok  = False
        video_ok  = False
        try:
            # ── Start audio BEFORE FFmpeg so recording begins simultaneously ──
            if self.audio_path:
                self._audio_thread = _AudioCaptureThread(self.audio_path)
                self._audio_thread.start()

            # ── Start FFmpeg video capture ────────────────────────────────────
            self._process = subprocess.Popen(
                self.cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            self._process.wait()
            video_ok = (
                os.path.exists(self.segment_path)
                and os.path.getsize(self.segment_path) > 0
            )
        except Exception as e:
            print(f"[Recorder segment] FFmpeg error: {e}", file=sys.stderr)
            video_ok = False
        finally:
            # ── Always stop audio when video finishes ─────────────────────────
            if self._audio_thread:
                self._audio_thread.stop()
                self._audio_thread.join(timeout=8)
                audio_ok = getattr(self._audio_thread, "ok", False)

        actual_audio = self.audio_path if audio_ok else ""
        self.finished.emit(self.segment_path, actual_audio, video_ok)

    def stop_gracefully(self):
        """Send 'q' to FFmpeg stdin so it saves properly before exiting."""
        if self._process and self._process.poll() is None:
            try:
                self._process.stdin.write("q\n")
                self._process.stdin.flush()
            except OSError:
                pass
            try:
                self._process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._process.terminate()
        # Audio thread stops in run()'s finally block automatically


# ──────────────────────────────────────────────────────────────────────────────
# Finalize worker — runs concat + mux in background (never blocks Qt thread)
# ──────────────────────────────────────────────────────────────────────────────

class _FinalizeWorker(QThread):
    """
    Runs all slow post-processing (ffmpeg concat, mux) in a background thread
    so the Qt main thread stays responsive during finalization.
    """
    done  = Signal(str, int, int)  # final_path, duration_sec, file_size_bytes
    error = Signal(str)

    def __init__(self, segments: list, final_path: str, session_dir: str):
        super().__init__()
        self._segments    = segments      # list of (video_mp4, audio_wav)
        self._final_path  = final_path
        self._session_dir = session_dir

    def run(self):
        ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
        output = self._final_path

        try:
            os.makedirs(os.path.dirname(output), exist_ok=True)
        except Exception:
            pass

        video_paths = [v for v, _ in self._segments]
        audio_paths = [a for _, a in self._segments if a and os.path.exists(a)]
        has_audio   = bool(audio_paths)

        # ── Step 1: concat video segments ─────────────────────────────────────
        if len(video_paths) == 1:
            raw_video = video_paths[0]
        else:
            concat_file = os.path.join(self._session_dir, "concat.txt")
            with open(concat_file, "w", encoding="utf-8") as f:
                for seg in video_paths:
                    f.write(f"file '{seg}'\n")
            raw_video = os.path.join(self._session_dir, "video_concat.mp4")
            try:
                subprocess.run(
                    [ffmpeg, "-y", "-f", "concat", "-safe", "0",
                     "-i", concat_file, "-c", "copy", raw_video],
                    capture_output=True, timeout=180,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
            except Exception as e:
                self.error.emit(f"Ошибка конкатенации видео: {e}")
                return

        if not has_audio:
            # No audio — move video directly to output
            try:
                shutil.move(raw_video, output)
            except Exception as e:
                self.error.emit(f"Ошибка сохранения видео: {e}")
                return
        else:
            # ── Step 2: concat audio WAV files ─────────────────────────────────
            if len(audio_paths) == 1:
                raw_audio = audio_paths[0]
            else:
                audio_list = os.path.join(self._session_dir, "audio_concat.txt")
                with open(audio_list, "w", encoding="utf-8") as f:
                    for ap in audio_paths:
                        f.write(f"file '{ap}'\n")
                raw_audio = os.path.join(self._session_dir, "audio_concat.wav")
                try:
                    subprocess.run(
                        [ffmpeg, "-y", "-f", "concat", "-safe", "0",
                         "-i", audio_list, "-c", "copy", raw_audio],
                        capture_output=True, timeout=60,
                        creationflags=subprocess.CREATE_NO_WINDOW,
                    )
                except Exception as e:
                    print(f"[Recorder] Audio concat error (skip audio): {e}", file=sys.stderr)
                    raw_audio = ""

            # ── Step 3: mux video + audio → final MP4 ──────────────────────────
            if raw_audio and os.path.exists(raw_audio):
                try:
                    subprocess.run(
                        [ffmpeg, "-y",
                         "-i", raw_video,
                         "-i", raw_audio,
                         "-c:v", "copy",
                         "-c:a", "aac", "-b:a", "128k",
                         "-map", "0:v", "-map", "1:a",
                         "-shortest",
                         output],
                        capture_output=True, timeout=300,
                        creationflags=subprocess.CREATE_NO_WINDOW,
                    )
                    print(f"[Recorder] Muxed video+audio -> {output}", file=sys.stderr)
                except Exception as e:
                    print(f"[Recorder] Mux error (video only): {e}", file=sys.stderr)
                    try:
                        shutil.copy2(raw_video, output)
                    except Exception:
                        pass
            else:
                try:
                    shutil.move(raw_video, output)
                except Exception:
                    pass

        # ── Cleanup temp dir ──────────────────────────────────────────────────
        try:
            shutil.rmtree(self._session_dir, ignore_errors=True)
        except Exception:
            pass

        duration  = get_video_duration(output)
        file_size = os.path.getsize(output) if os.path.exists(output) else 0
        self.done.emit(output, duration, file_size)


# ──────────────────────────────────────────────────────────────────────────────
# Main recorder — coordinates segments, exposes public API
# ──────────────────────────────────────────────────────────────────────────────

class ScreenRecorder(QObject):
    """
    Manages multi-segment screen recording with pause/resume support.

    Public API (call from main/Qt thread):
      start_recording(client_name)
      pause_recording()
      resume_recording()
      stop_recording()

    Properties:
      is_recording  — True while active (including paused)
      is_paused     — True while paused
      state         — RecordingState string
    """

    recording_started = Signal(str)       # client_name
    recording_stopped = Signal(str, int)  # final_path, total_duration_seconds
    recording_error   = Signal(str)       # error message
    status_changed    = Signal(str)       # short UI text

    def __init__(self, db, recordings_dir: str = "recordings"):
        super().__init__()
        self.db             = db
        self.recordings_dir = recordings_dir

        self._state          = RecordingState.IDLE
        self._client_name: str      = ""
        self._session_dir: str      = ""
        self._segments: list[tuple[str, str]] = []   # (video_mp4, audio_wav)
        self._final_path: str       = ""
        self._start_time: Optional[datetime] = None
        self._segment_id: Optional[int]      = None
        self._segment_worker: Optional[_SegmentWorker]  = None
        self._finalize_worker: Optional[_FinalizeWorker] = None
        self._has_audio: bool       = False

    # ── Public properties ─────────────────────────────────────────────────────

    @property
    def is_recording(self) -> bool:
        return self._state in (RecordingState.RECORDING, RecordingState.PAUSED)

    @property
    def is_paused(self) -> bool:
        return self._state == RecordingState.PAUSED

    @property
    def state(self) -> str:
        return self._state

    # ── Public API ────────────────────────────────────────────────────────────

    def start_recording(self, client_name: str, segment_id: int = None):
        """Start a fresh recording session. Optionally link to a timer segment_id."""
        if self._state != RecordingState.IDLE:
            self.status_changed.emit("⚠ Сначала остановите текущую запись")
            return

        self._client_name = client_name.strip() or "Без_имени"
        self._segment_id  = segment_id
        self._start_time  = datetime.now()
        self._segments    = []
        self._final_path  = self._build_final_path(self._client_name)

        os.makedirs(os.path.dirname(self._final_path), exist_ok=True)
        self._session_dir = tempfile.mkdtemp(prefix="rec_seg_")

        # Check pyaudiowpatch availability once per session
        dev, pa = get_wasapi_loopback_device()
        if pa:
            pa.terminate()
        self._has_audio = dev is not None
        if self._has_audio:
            print("[Recorder] Audio: WASAPI loopback via pyaudiowpatch ✓", file=sys.stderr)
        else:
            print("[Recorder] Audio: NOT available — video only", file=sys.stderr)

        self._state = RecordingState.RECORDING
        self.recording_started.emit(self._client_name)
        self._launch_segment()

    def pause_recording(self):
        """Pause: stop current FFmpeg + audio segment, keep session open."""
        if self._state != RecordingState.RECORDING:
            return
        self._state = RecordingState.PAUSED
        self.status_changed.emit("⏸ Пауза записи…")
        if self._segment_worker:
            self._segment_worker.stop_gracefully()

    def resume_recording(self):
        """Resume: start a new segment."""
        if self._state != RecordingState.PAUSED:
            return
        self._state = RecordingState.RECORDING
        self.status_changed.emit(f"⏺ Запись: {self._client_name}")
        self._launch_segment()

    def stop_recording(self):
        """Stop: finalize all segments and produce the final file."""
        if self._state not in (RecordingState.RECORDING, RecordingState.PAUSED):
            return
        self._state = RecordingState.STOPPING
        self.status_changed.emit("⏹ Сохранение записи…")
        if self._segment_worker and self._segment_worker.isRunning():
            self._segment_worker.stop_gracefully()
            # _on_segment_done will call _finalize when state == STOPPING
        else:
            self._finalize()

    # ── Internal ──────────────────────────────────────────────────────────────

    def _launch_segment(self):
        """Build FFmpeg command + audio path for the next segment and start worker."""
        seg_index  = len(self._segments)
        seg_path   = os.path.join(self._session_dir, f"segment_{seg_index:03d}.mp4")
        audio_path = (
            os.path.join(self._session_dir, f"audio_{seg_index:03d}.wav")
            if self._has_audio else ""
        )
        cmd = self._build_ffmpeg_command(seg_path)
        if cmd is None:
            self._state = RecordingState.IDLE
            self.recording_error.emit("Не удалось построить FFmpeg-команду.")
            return

        print(f"[Recorder] Starting segment {seg_index}: {' '.join(cmd[:6])}…", file=sys.stderr)

        worker = _SegmentWorker(cmd, seg_path, audio_path)
        worker.finished.connect(self._on_segment_done)
        self._segment_worker = worker
        worker.start()

    def _on_segment_done(self, seg_path: str, audio_path: str, ok: bool):
        """Called (in Qt main thread) when a segment worker finishes."""
        if ok:
            self._segments.append((seg_path, audio_path))
            print(f"[Recorder] Segment OK: {seg_path}  audio={audio_path or 'none'}", file=sys.stderr)
        else:
            print(f"[Recorder] Segment FAILED: {seg_path}", file=sys.stderr)

        if self._state == RecordingState.STOPPING:
            self._finalize()
        elif self._state == RecordingState.PAUSED:
            self.status_changed.emit("⏸ На паузе")
        elif self._state == RecordingState.RECORDING:
            # Segment crashed mid-recording — restart
            self.status_changed.emit("⚠ Сегмент прерван, перезапуск…")
            self._launch_segment()

    def _finalize(self):
        """Start _FinalizeWorker in background (does NOT block the Qt main thread)."""
        if not self._segments:
            self._state = RecordingState.IDLE
            self.recording_error.emit("Нет записанных сегментов. FFmpeg мог не запуститься.")
            return

        worker = _FinalizeWorker(
            segments    = list(self._segments),
            final_path  = self._final_path,
            session_dir = self._session_dir,
        )
        worker.done.connect(self._on_finalize_done)
        worker.error.connect(self._on_finalize_error)
        self._finalize_worker = worker
        worker.start()

    def _on_finalize_done(self, output: str, duration: int, file_size: int):
        """Called when _FinalizeWorker finishes successfully."""
        self.db.add_recording(
            client_name = self._client_name,
            file_path   = os.path.abspath(output),
            duration    = duration,
            file_size   = file_size,
            segment_id  = self._segment_id,
        )
        self._state = RecordingState.IDLE
        self.status_changed.emit(f"✔ Сохранено ({_fmt_size(file_size)}, {duration}с)")
        self.recording_stopped.emit(os.path.abspath(output), duration)

    def _on_finalize_error(self, msg: str):
        """Called when _FinalizeWorker encounters a fatal error."""
        self._state = RecordingState.IDLE
        self.recording_error.emit(msg)

    def _build_ffmpeg_command(self, output_path: str) -> Optional[list[str]]:
        """Build FFmpeg command for VIDEO ONLY (audio captured by pyaudiowpatch)."""
        ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
        if not ffmpeg:
            self.recording_error.emit("ffmpeg не найден в PATH.")
            return None

        x, y, w, h = get_virtual_screen_geometry()
        w = w if w % 2 == 0 else w - 1
        h = h if h % 2 == 0 else h - 1

        print(f"[Recorder] Screen geometry: {w}x{h} at ({x},{y})", file=sys.stderr)

        return [
            ffmpeg, "-y",
            "-f", "gdigrab",
            "-framerate", "15",
            "-offset_x", str(x),
            "-offset_y", str(y),
            "-video_size", f"{w}x{h}",
            "-draw_mouse", "1",
            "-i", "desktop",
            "-map", "0:v",
            "-vcodec", "libx264",
            "-preset", "faster",
            "-crf", "28",
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            output_path,
        ]

    def _build_final_path(self, client_name: str) -> str:
        """recordings/YYYY-MM-DD/HH-MM_ClientName.mp4"""
        now = datetime.now()
        safe = re.sub(r'[\\/:*?"<>|]', "_", client_name)
        date_folder = now.strftime("%Y-%m-%d")
        filename    = now.strftime("%H-%M") + f"_{safe}.mp4"
        return os.path.join(self.recordings_dir, date_folder, filename)

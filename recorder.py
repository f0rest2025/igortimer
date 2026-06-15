"""
recorder.py — Screen & Audio Recorder Module
=============================================
Records the full desktop (all monitors combined) + system audio using FFmpeg
for video and PyAudioWPatch (WASAPI loopback) for audio.

Architecture:
  - Recording is split into SEGMENTS to support Pause/Resume
  - Each segment: FFmpeg records VIDEO only, _AudioCaptureThread records AUDIO
    to a parallel WAV file.
  - On Pause: both processes stop → segment pair (mp4+wav) saved
  - On Resume: new segment pair started
  - On Stop: video segments concatenated, audio WAV files concatenated,
    then ffmpeg muxes final video+audio into one MP4.
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
import struct
import subprocess
import tempfile
import threading
import wave
from datetime import datetime
from pathlib import Path
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

    # ctypes fallback (SM_CXVIRTUALSCREEN covers all monitors including negative coords)
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

    return 0, 0, 1920, 1080  # last resort


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
    """
    def __init__(self, output_path: str):
        super().__init__(daemon=True)
        self.output_path = output_path
        self._stop_event = threading.Event()
        self.ok = False
        self._channels = 2
        self._rate = 48000

    def run(self):
        try:
            import pyaudiowpatch as pyaudio
            p = pyaudio.PyAudio()
            device = p.get_default_wasapi_loopback()
            self._channels = device["maxInputChannels"]
            self._rate = int(device["defaultSampleRate"])
            frames_per_buf = 1024

            with wave.open(self.output_path, "wb") as wf:
                wf.setnchannels(self._channels)
                wf.setsampwidth(p.get_sample_size(pyaudio.paInt16))
                wf.setframerate(self._rate)

                def callback(in_data, frame_count, time_info, status):
                    if self._stop_event.is_set():
                        return (b"\x00" * len(in_data), pyaudio.paComplete)
                    wf.writeframes(in_data)
                    return (None, pyaudio.paContinue)

                stream = p.open(
                    format=pyaudio.paInt16,
                    channels=self._channels,
                    rate=self._rate,
                    frames_per_buffer=frames_per_buf,
                    input=True,
                    input_device_index=device["index"],
                    stream_callback=callback,
                )
                stream.start_stream()
                while not self._stop_event.is_set() and stream.is_active():
                    threading.Event().wait(0.1)
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


# ──────────────────────────────────────────────────────────────────────────────
# Recording state machine
# ──────────────────────────────────────────────────────────────────────────────

class RecordingState:
    IDLE      = "idle"
    RECORDING = "recording"
    PAUSED    = "paused"
    STOPPING  = "stopping"


# ──────────────────────────────────────────────────────────────────────────────
# Segment worker (one FFmpeg VIDEO process + one audio thread per segment)
# ──────────────────────────────────────────────────────────────────────────────

class _SegmentWorker(QThread):
    """
    Runs FFmpeg (video only) + _AudioCaptureThread in parallel for one segment.
    Emits finished(video_segment_path, audio_wav_path, ok) when done.
    """
    finished = Signal(str, str, bool)   # video_path, audio_path, ok

    def __init__(self, cmd: list[str], segment_path: str, audio_path: str):
        super().__init__()
        self.cmd = cmd
        self.segment_path = segment_path
        self.audio_path = audio_path
        self._process: Optional[subprocess.Popen] = None
    def run(self):
        try:
            self._process = subprocess.Popen(
                self.cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True, encoding="utf-8", errors="replace",
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            self._process.wait()
            ok = os.path.exists(self.segment_path) and os.path.getsize(self.segment_path) > 0
        except Exception as e:
            print(f"[Recorder segment] error: {e}", file=sys.stderr)
            ok = False
        self.finished.emit(self.segment_path, ok)

    def stop_gracefully(self):
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
        self.db = db
        self.recordings_dir = recordings_dir

        self._state = RecordingState.IDLE
        self._client_name: str = ""
        self._session_dir: str = ""              # temp dir for segments
        self._segments: list[tuple[str, str]] = []  # (video_mp4, audio_wav) pairs
        self._final_path: str = ""
        self._start_time: Optional[datetime] = None
        self._segment_id: Optional[int] = None   # linked time_segment id
        self._segment_worker: Optional[_SegmentWorker] = None
        self._has_audio: bool = False            # True when pyaudiowpatch is available

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
        self._segment_id = segment_id
        self._start_time = datetime.now()
        self._segments = []
        self._final_path = self._build_final_path(self._client_name)

        # Create temp session dir
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
            print("[Recorder] Audio: NOT available — recording video only", file=sys.stderr)

        self._state = RecordingState.RECORDING
        self.recording_started.emit(self._client_name)
        self._launch_segment()

    def pause_recording(self):
        """Pause: stop current FFmpeg segment, keep session open."""
        if self._state != RecordingState.RECORDING:
            return
        self._state = RecordingState.PAUSED
        self.status_changed.emit("⏸ Пауза записи…")
        if self._segment_worker:
            self._segment_worker.stop_gracefully()
            # segment finished signal will handle the rest

    def resume_recording(self):
        """Resume: start a new FFmpeg segment."""
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
        """Build FFmpeg VIDEO command + audio WAV path for the next segment."""
        seg_index = len(self._segments)
        seg_path = os.path.join(self._session_dir, f"segment_{seg_index:03d}.mp4")
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
        """Called when a segment worker finishes."""
        if ok:
            self._segments.append((seg_path, audio_path))
            print(f"[Recorder] Segment saved: {seg_path}  audio={audio_path or 'none'}", file=sys.stderr)
        else:
            print(f"[Recorder] Segment FAILED: {seg_path}", file=sys.stderr)

        if self._state == RecordingState.STOPPING:
            self._finalize()
        elif self._state == RecordingState.PAUSED:
            self.status_changed.emit("⏸ На паузе")
        elif self._state == RecordingState.RECORDING:
            self.status_changed.emit("⚠ Сегмент прерван, перезапуск…")
            self._launch_segment()

    def _finalize(self):
        """Merge all segments into the final MP4 with audio and log to DB."""
        if not self._segments:
            self._state = RecordingState.IDLE
            self.recording_error.emit("Нет записанных сегментов.")
            return

        ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
        output = self._final_path
        os.makedirs(os.path.dirname(output), exist_ok=True)
        video_paths  = [v for v, _ in self._segments]
        audio_paths  = [a for _, a in self._segments if a and os.path.exists(a)]
        has_audio    = bool(audio_paths)

        # ── Step 1: concat video segments ────────────────────────────────────
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
                self._state = RecordingState.IDLE
                self.recording_error.emit(f"Ошибка конкатенации видео: {e}")
                return

        if not has_audio:
            # No audio — just copy video to output
            import shutil as _sh
            _sh.move(raw_video, output)
        else:
            # ── Step 2: concat audio WAV files ───────────────────────────────
            if len(audio_paths) == 1:
                raw_audio = audio_paths[0]
            else:
                # Concatenate WAV files using ffmpeg concat
                audio_concat_list = os.path.join(self._session_dir, "audio_concat.txt")
                with open(audio_concat_list, "w", encoding="utf-8") as f:
                    for ap in audio_paths:
                        f.write(f"file '{ap}'\n")
                raw_audio = os.path.join(self._session_dir, "audio_concat.wav")
                try:
                    subprocess.run(
                        [ffmpeg, "-y", "-f", "concat", "-safe", "0",
                         "-i", audio_concat_list, "-c", "copy", raw_audio],
                        capture_output=True, timeout=60,
                        creationflags=subprocess.CREATE_NO_WINDOW,
                    )
                except Exception as e:
                    print(f"[Recorder] Audio concat error (will skip audio): {e}", file=sys.stderr)
                    raw_audio = ""

            # ── Step 3: mux video + audio into final MP4 ─────────────────────
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
                    print(f"[Recorder] Mux error (saving video only): {e}", file=sys.stderr)
                    import shutil as _sh
                    _sh.copy2(raw_video, output)
            else:
                import shutil as _sh
                _sh.move(raw_video, output)

        # ── Cleanup temp dir ─────────────────────────────────────────────────
        try:
            shutil.rmtree(self._session_dir, ignore_errors=True)
        except Exception:
            pass

        # Calculate stats
        duration = get_video_duration(output)
        file_size = os.path.getsize(output) if os.path.exists(output) else 0

        # Log to DB
        self.db.add_recording(
            client_name=self._client_name,
            file_path=os.path.abspath(output),
            duration=duration,
            file_size=file_size,
            segment_id=self._segment_id,
        )

        self._state = RecordingState.IDLE
        self.status_changed.emit(f"✔ Сохранено ({_fmt_size(file_size)}, {duration}с)")
        self.recording_stopped.emit(os.path.abspath(output), duration)

    def _build_ffmpeg_command(self, output_path: str) -> Optional[list[str]]:
        """Build FFmpeg command for VIDEO ONLY (audio captured separately by pyaudiowpatch)."""
        ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
        if not ffmpeg:
            self.recording_error.emit("ffmpeg не найден в PATH.")
            return None

        x, y, w, h = get_virtual_screen_geometry()
        w = w if w % 2 == 0 else w - 1
        h = h if h % 2 == 0 else h - 1

        print(f"[Recorder] Screen geometry: {w}x{h} at ({x},{y})", file=sys.stderr)

        cmd = [
            ffmpeg, "-y",
            # Video capture via GDI grab
            "-f", "gdigrab",
            "-framerate", "15",
            "-offset_x", str(x),
            "-offset_y", str(y),
            "-video_size", f"{w}x{h}",
            "-draw_mouse", "1",
            "-i", "desktop",
            # Map only video
            "-map", "0:v",
            # H.264 encoding
            "-vcodec", "libx264",
            "-preset", "faster",
            "-crf", "28",
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            output_path,
        ]
        return cmd

    # ── Path helpers ──────────────────────────────────────────────────────────

    def _build_final_path(self, client_name: str) -> str:
        """recordings/YYYY-MM-DD/HH-MM_ClientName.mp4"""
        now = datetime.now()
        safe = re.sub(r'[\\/:*?"<>|]', "_", client_name)
        date_folder = now.strftime("%Y-%m-%d")
        filename = now.strftime("%H-%M") + f"_{safe}.mp4"
        return os.path.join(self.recordings_dir, date_folder, filename)


def _fmt_size(size_bytes: int) -> str:
    if size_bytes < 1024 ** 2:
        return f"{size_bytes / 1024:.0f} KB"
    if size_bytes < 1024 ** 3:
        return f"{size_bytes / 1024 ** 2:.1f} MB"
    return f"{size_bytes / 1024 ** 3:.2f} GB"

"""
recorder.py — Screen & Audio Recorder Module
=============================================
Records the full desktop (all monitors combined) + system audio + microphone.

Architecture:
  - Recording is split into SEGMENTS to support Pause/Resume
  - Each segment: FFmpeg records VIDEO only; two audio threads run in parallel:
      _LoopbackCaptureThread  — WASAPI loopback (all Windows speaker output)
      _MicCaptureThread       — default microphone input (your voice)
  - On Stop: _FinalizeWorker thread (background):
      1. Concatenates video segments
      2. Concatenates loopback WAVs
      3. Concatenates mic WAVs
      4. Mixes loopback + mic with amix
      5. Muxes video + mixed audio → final MP4
  - Final file is logged to SQLite

Audio strategy:
  Loopback: pyaudiowpatch WASAPI loopback — all Windows audio (browser, calls)
  Mic:      pyaudiowpatch default input   — your microphone voice
  Both use SHARED mode — other apps (WhatsApp, Zoom) can use mic simultaneously.

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
        x = u32.GetSystemMetrics(76)
        y = u32.GetSystemMetrics(77)
        w = u32.GetSystemMetrics(78)
        h = u32.GetSystemMetrics(79)
        if w > 0 and h > 0:
            return x, y, w, h
    except Exception:
        pass

    return 0, 0, 1920, 1080


# ──────────────────────────────────────────────────────────────────────────────
# Audio capture helpers
# ──────────────────────────────────────────────────────────────────────────────

def _pyaudio():
    """Import pyaudiowpatch, raise ImportError if unavailable."""
    import pyaudiowpatch as pa
    return pa


def _open_wav(path: str, channels: int, rate: int, sample_width: int) -> wave.Wave_write:
    wf = wave.open(path, "wb")
    wf.setnchannels(channels)
    wf.setsampwidth(sample_width)
    wf.setframerate(rate)
    return wf


class _LoopbackCaptureThread(threading.Thread):
    """
    Captures ALL Windows audio output (speakers/headphones) via WASAPI loopback.
    Uses SHARED mode — does NOT block other applications from using audio.
    """
    def __init__(self, output_path: str):
        super().__init__(daemon=True)
        self.output_path = output_path
        self._stop_event = threading.Event()
        self.ok = False

    def run(self):
        try:
            pa = _pyaudio()
            p = pa.PyAudio()
            device = p.get_default_wasapi_loopback()
            channels = device["maxInputChannels"]
            rate     = int(device["defaultSampleRate"])

            with _open_wav(self.output_path, channels, rate,
                           p.get_sample_size(pa.paInt16)) as wf:
                def callback(in_data, frame_count, time_info, status):
                    if self._stop_event.is_set():
                        return (b"\x00" * len(in_data), pa.paComplete)
                    wf.writeframes(in_data)
                    return (None, pa.paContinue)

                stream = p.open(
                    format=pa.paInt16,
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
                print(f"[Recorder] Loopback WAV: {self.output_path}", file=sys.stderr)
        except Exception as e:
            print(f"[Recorder] Loopback capture error: {e}", file=sys.stderr)
            self.ok = False

    def stop(self):
        self._stop_event.set()


class _MicCaptureThread(threading.Thread):
    """
    Captures microphone input (your voice) via the default input device.
    Uses SHARED mode — WhatsApp/Zoom/Teams can use the mic at the same time.
    """
    def __init__(self, output_path: str):
        super().__init__(daemon=True)
        self.output_path = output_path
        self._stop_event = threading.Event()
        self.ok = False

    def run(self):
        try:
            pa = _pyaudio()
            p = pa.PyAudio()

            # Get default microphone (standard input, not loopback)
            mic_info = p.get_default_input_device_info()
            channels = min(int(mic_info["maxInputChannels"]), 2)  # max stereo
            rate     = int(mic_info["defaultSampleRate"])

            print(f"[Recorder] Mic device: {mic_info['name']} "
                  f"({channels}ch, {rate}Hz)", file=sys.stderr)

            with _open_wav(self.output_path, channels, rate,
                           p.get_sample_size(pa.paInt16)) as wf:
                def callback(in_data, frame_count, time_info, status):
                    if self._stop_event.is_set():
                        return (b"\x00" * len(in_data), pa.paComplete)
                    wf.writeframes(in_data)
                    return (None, pa.paContinue)

                stream = p.open(
                    format=pa.paInt16,
                    channels=channels,
                    rate=rate,
                    frames_per_buffer=1024,
                    input=True,
                    input_device_index=mic_info["index"],
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
                print(f"[Recorder] Mic WAV: {self.output_path}", file=sys.stderr)
        except Exception as e:
            print(f"[Recorder] Mic capture error: {e}", file=sys.stderr)
            self.ok = False

    def stop(self):
        self._stop_event.set()


# ──────────────────────────────────────────────────────────────────────────────
# Misc helpers
# ──────────────────────────────────────────────────────────────────────────────

def get_wasapi_loopback_device():
    """Return (device_info_dict, PyAudio_instance) for the default WASAPI loopback.
    Returns (None, None) if pyaudiowpatch is unavailable."""
    try:
        pa = _pyaudio()
        p = pa.PyAudio()
        device = p.get_default_wasapi_loopback()
        return device, p
    except Exception as e:
        print(f"[Recorder] WASAPI loopback unavailable: {e}", file=sys.stderr)
        return None, None


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
# Segment worker — FFmpeg video + loopback + mic in parallel
# ──────────────────────────────────────────────────────────────────────────────

class _SegmentWorker(QThread):
    """
    Runs FFmpeg (video) + _LoopbackCaptureThread + _MicCaptureThread in parallel.
    Emits finished(video_path, loopback_wav, mic_wav, ok).
    Empty string means that track was unavailable/failed.
    """
    finished = Signal(str, str, str, bool)  # video, loopback_wav, mic_wav, ok

    def __init__(self, cmd: list[str], seg_path: str,
                 loopback_path: str, mic_path: str):
        super().__init__()
        self.cmd           = cmd
        self.seg_path      = seg_path
        self.loopback_path = loopback_path
        self.mic_path      = mic_path
        self._process: Optional[subprocess.Popen] = None
        self._loopback_t: Optional[_LoopbackCaptureThread] = None
        self._mic_t:      Optional[_MicCaptureThread]      = None

    def run(self):
        video_ok    = False
        loopback_ok = False
        mic_ok      = False
        try:
            # ── Start audio capture threads BEFORE FFmpeg ──────────────────────
            if self.loopback_path:
                self._loopback_t = _LoopbackCaptureThread(self.loopback_path)
                self._loopback_t.start()
            if self.mic_path:
                self._mic_t = _MicCaptureThread(self.mic_path)
                self._mic_t.start()

            # ── FFmpeg video capture ───────────────────────────────────────────
            self._process = subprocess.Popen(
                self.cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True, encoding="utf-8", errors="replace",
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            self._process.wait()
            video_ok = (
                os.path.exists(self.seg_path)
                and os.path.getsize(self.seg_path) > 0
            )
        except Exception as e:
            print(f"[Recorder segment] FFmpeg error: {e}", file=sys.stderr)
        finally:
            # ── Stop audio threads after FFmpeg exits ──────────────────────────
            if self._loopback_t:
                self._loopback_t.stop()
                self._loopback_t.join(timeout=8)
                loopback_ok = getattr(self._loopback_t, "ok", False)
            if self._mic_t:
                self._mic_t.stop()
                self._mic_t.join(timeout=8)
                mic_ok = getattr(self._mic_t, "ok", False)

        actual_loopback = self.loopback_path if loopback_ok else ""
        actual_mic      = self.mic_path      if mic_ok      else ""
        self.finished.emit(self.seg_path, actual_loopback, actual_mic, video_ok)

    def stop_gracefully(self):
        """Send 'q' to FFmpeg so it saves the segment cleanly."""
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
        # Audio threads stop in run()'s finally block automatically


# ──────────────────────────────────────────────────────────────────────────────
# Finalize worker — background thread, never blocks Qt main thread
# ──────────────────────────────────────────────────────────────────────────────

class _FinalizeWorker(QThread):
    """
    Post-processing in a background thread:
      1. Concat video segments
      2. Concat loopback WAVs
      3. Concat mic WAVs
      4. Mix loopback + mic (amix)
      5. Mux video + mixed audio → MP4
    """
    done  = Signal(str, int, int)  # final_path, duration_sec, file_size_bytes
    error = Signal(str)

    def __init__(self, segments: list, final_path: str, session_dir: str):
        super().__init__()
        # segments: list of (video, loopback_wav, mic_wav)
        self._segments    = segments
        self._final_path  = final_path
        self._session_dir = session_dir

    def run(self):
        ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
        output = self._final_path

        try:
            os.makedirs(os.path.dirname(output), exist_ok=True)
        except Exception:
            pass

        video_paths    = [v             for v, _, _   in self._segments]
        loopback_paths = [lb            for _, lb, _  in self._segments
                          if lb and os.path.exists(lb)]
        mic_paths      = [mc            for _, _, mc  in self._segments
                          if mc and os.path.exists(mc)]
        has_loopback   = bool(loopback_paths)
        has_mic        = bool(mic_paths)
        has_audio      = has_loopback or has_mic

        # ── Step 1: concat video ──────────────────────────────────────────────
        if len(video_paths) == 1:
            raw_video = video_paths[0]
        else:
            concat_f = os.path.join(self._session_dir, "concat_video.txt")
            with open(concat_f, "w", encoding="utf-8") as f:
                for seg in video_paths:
                    f.write(f"file '{seg}'\n")
            raw_video = os.path.join(self._session_dir, "video_concat.mp4")
            try:
                subprocess.run(
                    [ffmpeg, "-y", "-f", "concat", "-safe", "0",
                     "-i", concat_f, "-c", "copy", raw_video],
                    capture_output=True, timeout=180,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
            except Exception as e:
                self.error.emit(f"Ошибка конкатенации видео: {e}")
                return

        if not has_audio:
            _safe_move(raw_video, output)
            self._emit_done(output)
            return

        # ── Step 2: concat loopback WAVs ──────────────────────────────────────
        raw_loopback = self._concat_wavs(ffmpeg, loopback_paths, "loopback_concat.wav")

        # ── Step 3: concat mic WAVs ───────────────────────────────────────────
        raw_mic = self._concat_wavs(ffmpeg, mic_paths, "mic_concat.wav")

        # ── Step 4: mix loopback + mic ────────────────────────────────────────
        raw_audio = self._mix_audio(ffmpeg, raw_loopback, raw_mic)

        # ── Step 5: mux video + mixed audio ──────────────────────────────────
        if raw_audio and os.path.exists(raw_audio):
            try:
                subprocess.run(
                    [ffmpeg, "-y",
                     "-i", raw_video,
                     "-i", raw_audio,
                     "-c:v", "copy",
                     "-c:a", "aac", "-b:a", "192k",
                     "-map", "0:v", "-map", "1:a",
                     "-shortest",
                     output],
                    capture_output=True, timeout=300,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                print(f"[Recorder] Final: {output}", file=sys.stderr)
            except Exception as e:
                print(f"[Recorder] Mux error (video only): {e}", file=sys.stderr)
                _safe_move(raw_video, output)
        else:
            _safe_move(raw_video, output)

        # ── Cleanup ───────────────────────────────────────────────────────────
        try:
            shutil.rmtree(self._session_dir, ignore_errors=True)
        except Exception:
            pass

        self._emit_done(output)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _concat_wavs(self, ffmpeg: str, paths: list[str], out_name: str) -> str:
        """Concatenate a list of WAV files. Returns path or empty string."""
        if not paths:
            return ""
        if len(paths) == 1:
            return paths[0]
        concat_f = os.path.join(self._session_dir, out_name + ".txt")
        with open(concat_f, "w", encoding="utf-8") as f:
            for p in paths:
                f.write(f"file '{p}'\n")
        out = os.path.join(self._session_dir, out_name)
        try:
            subprocess.run(
                [ffmpeg, "-y", "-f", "concat", "-safe", "0",
                 "-i", concat_f, "-c", "copy", out],
                capture_output=True, timeout=60,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            return out if os.path.exists(out) else ""
        except Exception as e:
            print(f"[Recorder] WAV concat error: {e}", file=sys.stderr)
            return ""

    def _mix_audio(self, ffmpeg: str, loopback: str, mic: str) -> str:
        """Mix loopback + mic WAVs with amix. Returns path of mixed WAV."""
        lb_ok  = loopback and os.path.exists(loopback)
        mic_ok = mic      and os.path.exists(mic)

        if lb_ok and mic_ok:
            mixed = os.path.join(self._session_dir, "mixed.wav")
            try:
                subprocess.run(
                    [ffmpeg, "-y",
                     "-i", loopback,
                     "-i", mic,
                     "-filter_complex",
                     "[0:a][1:a]amix=inputs=2:duration=longest:dropout_transition=0[aout]",
                     "-map", "[aout]",
                     "-c:a", "pcm_s16le",
                     mixed],
                    capture_output=True, timeout=120,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                if os.path.exists(mixed) and os.path.getsize(mixed) > 44:
                    print("[Recorder] Audio mixed: loopback + mic ✓", file=sys.stderr)
                    return mixed
            except Exception as e:
                print(f"[Recorder] amix error: {e}", file=sys.stderr)
            # fallback: loopback only
            return loopback
        elif lb_ok:
            print("[Recorder] Audio: loopback only (no mic)", file=sys.stderr)
            return loopback
        elif mic_ok:
            print("[Recorder] Audio: mic only (no loopback)", file=sys.stderr)
            return mic
        return ""

    def _emit_done(self, output: str):
        duration  = get_video_duration(output)
        file_size = os.path.getsize(output) if os.path.exists(output) else 0
        self.done.emit(output, duration, file_size)


def _safe_move(src: str, dst: str):
    try:
        shutil.move(src, dst)
    except Exception as e:
        print(f"[Recorder] move {src} -> {dst} error: {e}", file=sys.stderr)


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
        self._client_name    = ""
        self._session_dir    = ""
        # Each segment: (video_mp4, loopback_wav, mic_wav)
        self._segments: list[tuple[str, str, str]] = []
        self._final_path     = ""
        self._start_time: Optional[datetime] = None
        self._segment_id: Optional[int]      = None
        self._segment_worker: Optional[_SegmentWorker]   = None
        self._finalize_worker: Optional[_FinalizeWorker] = None
        self._has_loopback   = False
        self._has_mic        = False

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

        # ── Probe audio availability once per session ─────────────────────────
        self._has_loopback = False
        self._has_mic      = False
        try:
            pa = _pyaudio()
            p  = pa.PyAudio()
            try:
                p.get_default_wasapi_loopback()
                self._has_loopback = True
                print("[Recorder] Loopback: WASAPI ✓", file=sys.stderr)
            except Exception as e:
                print(f"[Recorder] Loopback: unavailable ({e})", file=sys.stderr)
            try:
                info = p.get_default_input_device_info()
                if info and int(info.get("maxInputChannels", 0)) > 0:
                    self._has_mic = True
                    print(f"[Recorder] Mic: {info['name']} ✓", file=sys.stderr)
            except Exception as e:
                print(f"[Recorder] Mic: unavailable ({e})", file=sys.stderr)
            p.terminate()
        except Exception as e:
            print(f"[Recorder] pyaudiowpatch unavailable: {e}", file=sys.stderr)

        if not self._has_loopback and not self._has_mic:
            print("[Recorder] No audio — video only", file=sys.stderr)

        self._state = RecordingState.RECORDING
        self.recording_started.emit(self._client_name)
        self._launch_segment()

    def pause_recording(self):
        if self._state != RecordingState.RECORDING:
            return
        self._state = RecordingState.PAUSED
        self.status_changed.emit("⏸ Пауза записи…")
        if self._segment_worker:
            self._segment_worker.stop_gracefully()

    def resume_recording(self):
        if self._state != RecordingState.PAUSED:
            return
        self._state = RecordingState.RECORDING
        self.status_changed.emit(f"⏺ Запись: {self._client_name}")
        self._launch_segment()

    def stop_recording(self):
        if self._state not in (RecordingState.RECORDING, RecordingState.PAUSED):
            return
        self._state = RecordingState.STOPPING
        self.status_changed.emit("⏹ Сохранение записи…")
        if self._segment_worker and self._segment_worker.isRunning():
            self._segment_worker.stop_gracefully()
        else:
            self._finalize()

    # ── Internal ──────────────────────────────────────────────────────────────

    def _launch_segment(self):
        idx = len(self._segments)

        seg_path      = os.path.join(self._session_dir, f"segment_{idx:03d}.mp4")
        loopback_path = (
            os.path.join(self._session_dir, f"loopback_{idx:03d}.wav")
            if self._has_loopback else ""
        )
        mic_path = (
            os.path.join(self._session_dir, f"mic_{idx:03d}.wav")
            if self._has_mic else ""
        )

        cmd = self._build_ffmpeg_command(seg_path)
        if cmd is None:
            self._state = RecordingState.IDLE
            self.recording_error.emit("Не удалось построить FFmpeg-команду.")
            return

        print(f"[Recorder] Segment {idx}: {' '.join(cmd[:6])}…", file=sys.stderr)

        worker = _SegmentWorker(cmd, seg_path, loopback_path, mic_path)
        worker.finished.connect(self._on_segment_done)
        self._segment_worker = worker
        worker.start()

    def _on_segment_done(self, seg_path: str, loopback_wav: str,
                         mic_wav: str, ok: bool):
        if ok:
            self._segments.append((seg_path, loopback_wav, mic_wav))
            print(
                f"[Recorder] Segment OK: {seg_path} | "
                f"lb={loopback_wav or 'none'} | mic={mic_wav or 'none'}",
                file=sys.stderr,
            )
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
        self._state = RecordingState.IDLE
        self.recording_error.emit(msg)

    def _build_ffmpeg_command(self, output_path: str) -> Optional[list[str]]:
        """VIDEO ONLY — audio captured separately by pyaudiowpatch."""
        ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
        if not ffmpeg:
            self.recording_error.emit("ffmpeg не найден в PATH.")
            return None

        x, y, w, h = get_virtual_screen_geometry()
        w = w if w % 2 == 0 else w - 1
        h = h if h % 2 == 0 else h - 1

        print(f"[Recorder] Screen: {w}x{h} at ({x},{y})", file=sys.stderr)

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
        now = datetime.now()
        safe = re.sub(r'[\\/:*?"<>|]', "_", client_name)
        return os.path.join(
            self.recordings_dir,
            now.strftime("%Y-%m-%d"),
            now.strftime("%H-%M") + f"_{safe}.mp4",
        )

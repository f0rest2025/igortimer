"""
recorder.py — Screen & Audio Recorder Module
=============================================
Records the full desktop (all monitors combined) + system audio using FFmpeg.

Architecture:
  - Recording is split into SEGMENTS to support Pause/Resume
  - On Pause: current FFmpeg process is stopped gracefully → segment saved
  - On Resume: new FFmpeg process started → new segment file
  - On Stop: all segments concatenated into one final MP4 via ffmpeg concat
  - Final file is logged to SQLite

Audio strategy (priority order, Windows):
  1. WASAPI loopback  (-f wasapi -loopback 1)      ← captures ALL Windows audio
  2. dshow Stereo Mix (-f dshow -i audio="...")     ← fallback for older setups
  3. No audio                                        ← last resort, still records

Microphone: optional bonus. Added as a second audio track and mixed in.
If mic is absent/muted — recording continues without interruption.

Signals (all Qt-thread-safe):
  recording_started(client_name)
  recording_stopped(file_path, duration_seconds)
  recording_error(error_message)
  status_changed(short_ui_text)

Usage from main thread:
  recorder.start_recording("Client Name")
  recorder.pause_recording()
  recorder.resume_recording()
  recorder.stop_recording()
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
# Audio device probing
# ──────────────────────────────────────────────────────────────────────────────

def list_dshow_audio_devices() -> list[str]:
    """Return list of DirectShow audio device names available on this system."""
    ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
    try:
        result = subprocess.run(
            [ffmpeg, "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
            capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=10,
        )
        output = result.stderr
        devices: list[str] = []
        in_audio = False
        for line in output.splitlines():
            if "DirectShow audio devices" in line:
                in_audio = True
                continue
            if in_audio:
                m = re.search(r'"([^"]+)"', line)
                if m and "Alternative name" not in line:
                    devices.append(m.group(1))
        return devices
    except Exception as e:
        print(f"[Recorder] dshow probe error: {e}", file=sys.stderr)
        return []


def test_wasapi_loopback() -> bool:
    """Quick-check whether FFmpeg can open WASAPI loopback on this machine."""
    ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
    try:
        result = subprocess.run(
            [ffmpeg, "-f", "wasapi", "-loopback", "1",
             "-i", "default", "-t", "0.1", "-f", "null", "-"],
            capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=8,
        )
        # If it doesn't say "Error" in the first line about opening device it's OK
        return "avformat_open_input" not in result.stderr and result.returncode in (0, 1)
    except Exception:
        return False


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
# Segment worker (one FFmpeg process per segment)
# ──────────────────────────────────────────────────────────────────────────────

class _SegmentWorker(QThread):
    """
    Runs a single FFmpeg process for one recording segment.
    Emits finished(segment_path, ok) when done.
    """
    finished = Signal(str, bool)

    def __init__(self, cmd: list[str], segment_path: str):
        super().__init__()
        self.cmd = cmd
        self.segment_path = segment_path
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
        self._session_dir: str = ""          # temp dir for segments
        self._segments: list[str] = []       # completed segment paths
        self._final_path: str = ""
        self._start_time: Optional[datetime] = None
        self._segment_worker: Optional[_SegmentWorker] = None

        # Audio config cached at session start
        self._audio_cmd_parts: list[str] = []
        self._audio_map_parts: list[str] = []

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

    def start_recording(self, client_name: str):
        """Start a fresh recording session."""
        if self._state != RecordingState.IDLE:
            self.status_changed.emit("⚠ Сначала остановите текущую запись")
            return

        self._client_name = client_name.strip() or "Без_имени"
        self._start_time = datetime.now()
        self._segments = []
        self._final_path = self._build_final_path(self._client_name)

        # Create temp session dir for segment files
        os.makedirs(os.path.dirname(self._final_path), exist_ok=True)
        self._session_dir = tempfile.mkdtemp(prefix="rec_seg_")

        # Probe and cache audio devices ONCE per session
        self._probe_audio()

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
        """Build FFmpeg command for the next segment and start worker."""
        seg_index = len(self._segments)
        seg_path = os.path.join(self._session_dir, f"segment_{seg_index:03d}.mp4")
        cmd = self._build_ffmpeg_command(seg_path)

        if cmd is None:
            self._state = RecordingState.IDLE
            self.recording_error.emit("Не удалось построить FFmpeg-команду.")
            return

        print(f"[Recorder] Starting segment {seg_index}: {' '.join(cmd[:6])}…", file=sys.stderr)

        worker = _SegmentWorker(cmd, seg_path)
        worker.finished.connect(self._on_segment_done)
        self._segment_worker = worker
        worker.start()

    def _on_segment_done(self, seg_path: str, ok: bool):
        """Called when a segment worker finishes."""
        if ok:
            self._segments.append(seg_path)
            print(f"[Recorder] Segment saved: {seg_path}", file=sys.stderr)
        else:
            print(f"[Recorder] Segment FAILED: {seg_path}", file=sys.stderr)

        if self._state == RecordingState.STOPPING:
            self._finalize()
        elif self._state == RecordingState.PAUSED:
            self.status_changed.emit("⏸ На паузе")
        # If RECORDING: shouldn't happen (process ended unexpectedly)
        elif self._state == RecordingState.RECORDING:
            # Segment crashed mid-recording — try to restart
            self.status_changed.emit("⚠ Сегмент прерван, перезапуск…")
            self._launch_segment()

    def _finalize(self):
        """Concatenate all segments into the final file and log to DB."""
        if not self._segments:
            self._state = RecordingState.IDLE
            self.recording_error.emit("Нет записанных сегментов. Возможно, FFmpeg не запустился.")
            return

        output = self._final_path
        os.makedirs(os.path.dirname(output), exist_ok=True)

        if len(self._segments) == 1:
            # Single segment — just move it
            import shutil as _sh
            _sh.move(self._segments[0], output)
        else:
            # Write concat list file
            concat_file = os.path.join(self._session_dir, "concat.txt")
            with open(concat_file, "w", encoding="utf-8") as f:
                for seg in self._segments:
                    f.write(f"file '{seg}'\n")

            ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
            concat_cmd = [
                ffmpeg, "-y",
                "-f", "concat", "-safe", "0",
                "-i", concat_file,
                "-c", "copy",
                output,
            ]
            try:
                subprocess.run(
                    concat_cmd,
                    capture_output=True, timeout=120,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
            except Exception as e:
                self._state = RecordingState.IDLE
                self.recording_error.emit(f"Ошибка конкатенации: {e}")
                return

        # Cleanup temp dir
        try:
            import shutil as _sh2
            _sh2.rmtree(self._session_dir, ignore_errors=True)
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
        )

        self._state = RecordingState.IDLE
        self.status_changed.emit(f"✔ Сохранено ({_fmt_size(file_size)}, {duration}с)")
        self.recording_stopped.emit(os.path.abspath(output), duration)

    # ── FFmpeg command builder ─────────────────────────────────────────────────

    def _probe_audio(self):
        """
        Determine the best audio capture strategy.
        Sets self._audio_cmd_parts and self._audio_map_parts.

        Priority:
          1. WASAPI loopback  → ALL Windows audio, no driver needed
          2. dshow Stereo Mix → common loopback device
          3. No audio (silent)

        Mic: always try; mixed in as a bonus if available.
        """
        ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
        audio_inputs = []   # list of "-f ... -i ..." arg groups
        audio_indices = []  # 1-based input indices (0 = video)

        # --- System audio ---
        system_ok = False

        # Try WASAPI loopback first
        if test_wasapi_loopback():
            audio_inputs.append(["-f", "wasapi", "-loopback", "1", "-i", "default"])
            audio_indices.append(len(audio_inputs))  # 1
            system_ok = True
            print("[Recorder] Audio: WASAPI loopback ✓", file=sys.stderr)
        else:
            # Try dshow Stereo Mix
            dshow_devs = list_dshow_audio_devices()
            loopback = _find_loopback_device(dshow_devs)
            if loopback:
                audio_inputs.append(["-f", "dshow", "-i", f"audio={loopback}"])
                audio_indices.append(len(audio_inputs))
                system_ok = True
                print(f"[Recorder] Audio: dshow Stereo Mix '{loopback}' ✓", file=sys.stderr)
            else:
                print("[Recorder] Audio: no system audio device found ⚠", file=sys.stderr)

        if not system_ok:
            print("[Recorder] Recording will be SILENT.", file=sys.stderr)

        # --- Microphone (optional) ---
        dshow_devs_fresh = list_dshow_audio_devices()
        mic = _find_mic_device(dshow_devs_fresh)
        if mic:
            audio_inputs.append(["-f", "dshow", "-i", f"audio={mic}"])
            audio_indices.append(len(audio_inputs))
            print(f"[Recorder] Mic: '{mic}' ✓", file=sys.stderr)
        else:
            print("[Recorder] Mic: not found or muted (will record system audio only)", file=sys.stderr)

        # Build -map args
        cmd_parts = []
        for parts in audio_inputs:
            cmd_parts.extend(parts)

        map_parts = ["-map", "0:v"]
        if len(audio_indices) == 2:
            map_parts += [
                "-filter_complex",
                f"[{audio_indices[0]}:a][{audio_indices[1]}:a]amix=inputs=2:duration=longest:dropout_transition=2[aout]",
                "-map", "[aout]",
            ]
        elif len(audio_indices) == 1:
            map_parts += ["-map", f"{audio_indices[0]}:a"]

        self._audio_cmd_parts = cmd_parts
        self._audio_map_parts = map_parts

    def _build_ffmpeg_command(self, output_path: str) -> Optional[list[str]]:
        ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
        if not ffmpeg:
            self.recording_error.emit("ffmpeg не найден в PATH.")
            return None

        x, y, w, h = get_virtual_screen_geometry()
        w = w if w % 2 == 0 else w - 1
        h = h if h % 2 == 0 else h - 1

        print(f"[Recorder] Screen geometry: {w}x{h} at ({x},{y})", file=sys.stderr)

        cmd = [ffmpeg, "-y"]

        # Video input
        cmd += [
            "-f", "gdigrab",
            "-framerate", "15",
            "-offset_x", str(x),
            "-offset_y", str(y),
            "-video_size", f"{w}x{h}",
            "-draw_mouse", "1",
            "-i", "desktop",
        ]

        # Audio inputs (pre-built in _probe_audio)
        cmd += self._audio_cmd_parts

        # Mapping
        cmd += self._audio_map_parts

        # Video codec: H.264, CRF 28, faster preset
        cmd += [
            "-vcodec", "libx264",
            "-preset", "faster",
            "-crf", "28",
            "-pix_fmt", "yuv420p",
        ]

        # Audio codec (only if we have audio tracks)
        if self._audio_cmd_parts:
            cmd += ["-acodec", "aac", "-b:a", "128k"]

        cmd += ["-movflags", "+faststart", output_path]
        return cmd

    # ── Path helpers ──────────────────────────────────────────────────────────

    def _build_final_path(self, client_name: str) -> str:
        """recordings/YYYY-MM-DD/HH-MM_ClientName.mp4"""
        now = datetime.now()
        safe = re.sub(r'[\\/:*?"<>|]', "_", client_name)
        date_folder = now.strftime("%Y-%m-%d")
        filename = now.strftime("%H-%M") + f"_{safe}.mp4"
        return os.path.join(self.recordings_dir, date_folder, filename)


# ──────────────────────────────────────────────────────────────────────────────
# Free helpers
# ──────────────────────────────────────────────────────────────────────────────

def _find_loopback_device(devices: list[str]) -> str:
    keywords = ["stereo mix", "what u hear", "loopback",
                "воспроизведение со", "wave out", "sum"]
    for d in devices:
        dl = d.lower()
        if any(kw in dl for kw in keywords):
            return d
    return ""


def _find_mic_device(devices: list[str]) -> str:
    loopback_kw = ["stereo mix", "what u hear", "loopback", "воспроизведение"]
    mic_kw = ["microphone", "микрофон", "mic", "input", "headset"]
    for d in devices:
        dl = d.lower()
        if any(kw in dl for kw in loopback_kw):
            continue
        if any(kw in dl for kw in mic_kw):
            return d
    return ""


def _fmt_size(size_bytes: int) -> str:
    if size_bytes < 1024 ** 2:
        return f"{size_bytes / 1024:.0f} KB"
    if size_bytes < 1024 ** 3:
        return f"{size_bytes / 1024 ** 2:.1f} MB"
    return f"{size_bytes / 1024 ** 3:.2f} GB"

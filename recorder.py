"""
recorder.py  ―  Screen & Audio Recorder
========================================
Records all monitors (gdigrab virtual desktop) + system audio (WASAPI
loopback) + microphone (default input) into one MP4 per session.

Session lifecycle
-----------------
  start_recording()
      │  probe audio devices (background thread)
      │  create temp dir
      └─► launch_segment()
              │  FFmpeg  → segment_NNN.mp4          (video)
              │  Loopback thread → loopback_NNN.wav (speaker output)
              └─ Mic thread     → mic_NNN.wav       (microphone)
  pause_recording() → stop_gracefully() → _on_segment_done
  resume_recording() → launch_segment()
  stop_recording()  → stop_gracefully() → _on_segment_done → _finalize()
      │  _FinalizeWorker (QThread, never blocks UI):
      │      concat video segments
      │      concat loopback WAVs
      │      concat mic WAVs
      │      amix loopback + mic  → mixed.wav
      │      ffmpeg mux video + mixed.wav → YYYY-MM-DD/HH-MM_Client.mp4
      └─► _on_finalize_done → DB log + signals

Qt signals (emitted from Qt thread, safe to connect to UI slots)
-----------------------------------------------------------------
  recording_started(client_name: str)
  recording_stopped(file_path: str, duration_sec: int)
  recording_error(message: str)
  status_changed(short_text: str)
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import wave
from datetime import datetime
from typing import Optional

from PySide6.QtCore import QObject, QThread, Qt, QMetaObject, Signal, Slot



# ──────────────────────────────────────────────────────────────────────────────
# Helpers: screen geometry
# ──────────────────────────────────────────────────────────────────────────────

def get_virtual_screen_geometry() -> tuple[int, int, int, int]:
    """Return (x, y, w, h) of the combined virtual desktop (all monitors)."""
    try:
        import screeninfo
        mons = screeninfo.get_monitors()
        if mons:
            x0 = min(m.x for m in mons)
            y0 = min(m.y for m in mons)
            x1 = max(m.x + m.width  for m in mons)
            y1 = max(m.y + m.height for m in mons)
            return x0, y0, x1 - x0, y1 - y0
    except Exception:
        pass
    try:
        import ctypes
        u = ctypes.windll.user32
        x = u.GetSystemMetrics(76)
        y = u.GetSystemMetrics(77)
        w = u.GetSystemMetrics(78)
        h = u.GetSystemMetrics(79)
        if w > 0 and h > 0:
            return x, y, w, h
    except Exception:
        pass
    return 0, 0, 1920, 1080


def _even(n: int) -> int:
    return n if n % 2 == 0 else n - 1


# ──────────────────────────────────────────────────────────────────────────────
# Helpers: audio
# ──────────────────────────────────────────────────────────────────────────────

def _import_pa():
    """Return pyaudiowpatch module or raise ImportError."""
    import pyaudiowpatch as pa
    return pa


def _write_wav_blocking(
    output_path: str,
    stop_event: threading.Event,
    device_index: int,
    channels: int,
    rate: int,
    is_loopback: bool,
):
    """
    Blocking audio capture loop. Run inside a daemon Thread.
    Opens PyAudio stream in callback mode; writes PCM frames to a WAV file.
    Returns True on success, False on error.
    """
    try:
        pa = _import_pa()
        p  = pa.PyAudio()

        open_kwargs = dict(
            format            = pa.paInt16,
            channels          = channels,
            rate              = rate,
            frames_per_buffer = 1024,
            input             = True,
            input_device_index= device_index,
        )
        # pyaudiowpatch: loopback streams need as_loopback flag
        if is_loopback:
            open_kwargs["as_loopback"] = True

        wf = wave.open(output_path, "wb")
        wf.setnchannels(channels)
        wf.setsampwidth(p.get_sample_size(pa.paInt16))
        wf.setframerate(rate)

        frames_written = [0]
        errors = [0]

        def _cb(in_data, frame_count, time_info, status_flags):
            if stop_event.is_set():
                return (None, pa.paComplete)
            if in_data:
                wf.writeframes(in_data)
                frames_written[0] += frame_count
            else:
                errors[0] += 1
            return (None, pa.paContinue)

        stream = p.open(stream_callback=_cb, **open_kwargs)
        stream.start_stream()
        while not stop_event.is_set() and stream.is_active():
            threading.Event().wait(0.05)
        stream.stop_stream()
        stream.close()
        wf.close()
        p.terminate()

        ok = (
            frames_written[0] > 0
            and os.path.exists(output_path)
            and os.path.getsize(output_path) > 44  # > WAV header
        )
        label = "loopback" if is_loopback else "mic"
        if ok:
            print(f"[Recorder] {label} OK → {output_path} "
                  f"({frames_written[0]} frames)", file=sys.stderr)
        else:
            print(f"[Recorder] {label} empty/failed "
                  f"(frames={frames_written[0]}, errors={errors[0]})", file=sys.stderr)
        return ok

    except Exception as exc:
        label = "loopback" if is_loopback else "mic"
        print(f"[Recorder] {label} exception: {exc}", file=sys.stderr)
        return False


class _AudioThread(threading.Thread):
    """Generic daemon thread that captures audio to a WAV file."""

    def __init__(self, path: str, device_index: int, channels: int,
                 rate: int, is_loopback: bool):
        super().__init__(daemon=True)
        self.path         = path
        self._device_idx  = device_index
        self._channels    = channels
        self._rate        = rate
        self._is_loopback = is_loopback
        self._stop        = threading.Event()
        self.ok           = False

    def run(self):
        self.ok = _write_wav_blocking(
            self.path, self._stop,
            self._device_idx, self._channels, self._rate, self._is_loopback,
        )

    def stop(self):
        self._stop.set()


def _probe_audio() -> tuple[Optional[dict], Optional[dict]]:
    """
    Probe available audio devices.
    Returns (loopback_info, mic_info); each is a dict with keys
    {index, channels, rate} or None if not available.
    """
    loopback_info = None
    mic_info      = None
    try:
        pa = _import_pa()
        p  = pa.PyAudio()
        try:
            dev = p.get_default_wasapi_loopback()
            loopback_info = {
                "index"   : int(dev["index"]),
                "channels": int(dev["maxInputChannels"]),
                "rate"    : int(dev["defaultSampleRate"]),
            }
            print(f"[Recorder] Loopback: {dev['name']} "
                  f"({loopback_info['channels']}ch, {loopback_info['rate']}Hz)",
                  file=sys.stderr)
        except Exception as e:
            print(f"[Recorder] Loopback: unavailable ({e})", file=sys.stderr)

        try:
            dev = p.get_default_input_device_info()
            ch  = min(int(dev.get("maxInputChannels", 0)), 2)
            if ch > 0:
                mic_info = {
                    "index"   : int(dev["index"]),
                    "channels": ch,
                    "rate"    : int(dev["defaultSampleRate"]),
                }
                print(f"[Recorder] Mic: {dev['name']} "
                      f"({ch}ch, {mic_info['rate']}Hz)", file=sys.stderr)
        except Exception as e:
            print(f"[Recorder] Mic: unavailable ({e})", file=sys.stderr)

        p.terminate()
    except Exception as e:
        print(f"[Recorder] pyaudiowpatch error: {e}", file=sys.stderr)

    return loopback_info, mic_info


# ──────────────────────────────────────────────────────────────────────────────
# Helpers: misc
# ──────────────────────────────────────────────────────────────────────────────

def get_video_duration(path: str) -> int:
    """Return video duration in whole seconds via ffprobe."""
    ffprobe = shutil.which("ffprobe") or "ffprobe"
    try:
        r = subprocess.run(
            [ffprobe, "-v", "quiet", "-print_format", "json",
             "-show_format", path],
            capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=15,
        )
        return int(float(json.loads(r.stdout).get("format", {}).get("duration", 0)))
    except Exception:
        return 0


def _fmt_size(b: int) -> str:
    if b < 1 << 20:
        return f"{b / 1024:.0f} KB"
    if b < 1 << 30:
        return f"{b / 1048576:.1f} MB"
    return f"{b / 1073741824:.2f} GB"


def _run(args: list[str], timeout: int = 120) -> bool:
    """Run a subprocess silently. Returns True if exit code == 0."""
    try:
        r = subprocess.run(
            args,
            capture_output=True,
            timeout=timeout,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if r.returncode != 0 and r.stderr:
            print(f"[Recorder] ffmpeg stderr: {r.stderr[-400:]}", file=sys.stderr)
        return r.returncode == 0
    except Exception as e:
        print(f"[Recorder] subprocess error: {e}", file=sys.stderr)
        return False


# ──────────────────────────────────────────────────────────────────────────────
# State
# ──────────────────────────────────────────────────────────────────────────────

class _S:
    IDLE      = "idle"
    RECORDING = "recording"
    PAUSED    = "paused"
    STOPPING  = "stopping"


# ──────────────────────────────────────────────────────────────────────────────
# _SegmentWorker — one FFmpeg process + two audio threads per segment
# ──────────────────────────────────────────────────────────────────────────────

class _SegmentWorker(QThread):
    """
    Runs in a background QThread.
    Starts FFmpeg (video-only) and up to two audio threads simultaneously.
    Emits `finished` when everything has stopped.
    """
    # video_path, loopback_wav, mic_wav, video_ok
    finished = Signal(str, str, str, bool)

    def __init__(
        self,
        ffmpeg_cmd: list[str],
        seg_path:      str,
        loopback_path: str,           # empty string = skip
        mic_path:      str,           # empty string = skip
        loopback_info: Optional[dict],
        mic_info:      Optional[dict],
    ):
        super().__init__()
        self._cmd          = ffmpeg_cmd
        self._seg_path     = seg_path
        self._lb_path      = loopback_path
        self._mic_path     = mic_path
        self._lb_info      = loopback_info
        self._mic_info     = mic_info
        self._proc: Optional[subprocess.Popen] = None
        self._lb_t:  Optional[_AudioThread]    = None
        self._mic_t: Optional[_AudioThread]    = None

    # ── public ────────────────────────────────────────────────────────────────

    def stop_gracefully(self):
        """Send 'q' to FFmpeg stdin (non-blocking). Audio stops in run()."""
        proc = self._proc
        if proc and proc.poll() is None:
            try:
                proc.stdin.write("q\n")
                proc.stdin.flush()
            except OSError:
                pass

    # ── QThread.run ───────────────────────────────────────────────────────────

    def run(self):
        video_ok = False
        try:
            # Start audio BEFORE ffmpeg so timestamps align
            if self._lb_path and self._lb_info:
                self._lb_t = _AudioThread(
                    self._lb_path,
                    self._lb_info["index"],
                    self._lb_info["channels"],
                    self._lb_info["rate"],
                    is_loopback=True,
                )
                self._lb_t.start()

            if self._mic_path and self._mic_info:
                self._mic_t = _AudioThread(
                    self._mic_path,
                    self._mic_info["index"],
                    self._mic_info["channels"],
                    self._mic_info["rate"],
                    is_loopback=False,
                )
                self._mic_t.start()

            # Run FFmpeg
            self._proc = subprocess.Popen(
                self._cmd,
                stdin  = subprocess.PIPE,
                stdout = subprocess.DEVNULL,
                stderr = subprocess.DEVNULL,
                text   = True,
                encoding = "utf-8",
                errors   = "replace",
                creationflags = subprocess.CREATE_NO_WINDOW,
            )
            self._proc.wait()
            video_ok = (
                os.path.exists(self._seg_path)
                and os.path.getsize(self._seg_path) > 0
            )
        except Exception as exc:
            print(f"[Recorder] FFmpeg error: {exc}", file=sys.stderr)
        finally:
            # Stop audio threads (they check stop event every 50 ms)
            if self._lb_t:
                self._lb_t.stop()
                self._lb_t.join(timeout=10)
            if self._mic_t:
                self._mic_t.stop()
                self._mic_t.join(timeout=10)

        lb_out  = self._lb_path  if (self._lb_t  and self._lb_t.ok)  else ""
        mic_out = self._mic_path if (self._mic_t and self._mic_t.ok) else ""
        self.finished.emit(self._seg_path, lb_out, mic_out, video_ok)


# ──────────────────────────────────────────────────────────────────────────────
# _FinalizeWorker — background post-processing; never blocks the UI thread
# ──────────────────────────────────────────────────────────────────────────────

class _FinalizeWorker(QThread):
    """
    Concatenates video segments, concatenates + mixes audio WAVs,
    muxes everything into the final MP4. All heavy I/O in a background thread.
    """
    done  = Signal(str, int, int)   # final_path, duration_sec, file_size
    error = Signal(str)

    def __init__(
        self,
        segments:    list[tuple[str, str, str]],   # (video, lb_wav, mic_wav)
        final_path:  str,
        session_dir: str,
    ):
        super().__init__()
        self._segs    = segments
        self._out     = final_path
        self._tmp     = session_dir

    def run(self):
        ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
        out    = self._out

        try:
            os.makedirs(os.path.dirname(out), exist_ok=True)
        except Exception:
            pass

        vids = [v for v, _, _ in self._segs if v and os.path.exists(v)]
        lbs  = [l for _, l, _ in self._segs if l and os.path.exists(l)]
        mics = [m for _, _, m in self._segs if m and os.path.exists(m)]

        if not vids:
            self.error.emit("Нет видеофайлов для сохранения.")
            return

        # ── 1. Concat video ───────────────────────────────────────────────────
        raw_video = self._concat_media(ffmpeg, vids, "concat_video.mp4")
        if not raw_video:
            self.error.emit("Ошибка сборки видео.")
            return

        # ── 2. Concat loopback WAVs ────────────────────────────────────────────
        raw_lb = self._concat_media(ffmpeg, lbs, "concat_lb.wav") if lbs else ""

        # ── 3. Concat mic WAVs ────────────────────────────────────────────────
        raw_mic = self._concat_media(ffmpeg, mics, "concat_mic.wav") if mics else ""

        # ── 4. Mix audio ──────────────────────────────────────────────────────
        raw_audio = self._mix(ffmpeg, raw_lb, raw_mic)

        # ── 5. Mux video + audio → final MP4 ─────────────────────────────────
        if raw_audio and os.path.exists(raw_audio):
            ok = _run([
                ffmpeg, "-y",
                "-i", raw_video,
                "-i", raw_audio,
                "-c:v", "copy",
                "-c:a", "aac", "-b:a", "192k",
                "-map", "0:v:0",
                "-map", "1:a:0",
                "-shortest",
                out,
            ], timeout=600)
            if not ok:
                print("[Recorder] Mux failed, saving video-only", file=sys.stderr)
                shutil.copy2(raw_video, out)
        else:
            print("[Recorder] No audio — saving video-only", file=sys.stderr)
            try:
                shutil.move(raw_video, out)
            except Exception:
                shutil.copy2(raw_video, out)

        # ── Cleanup temp dir ──────────────────────────────────────────────────
        try:
            shutil.rmtree(self._tmp, ignore_errors=True)
        except Exception:
            pass

        if not os.path.exists(out):
            self.error.emit("Файл записи не был создан.")
            return

        duration  = get_video_duration(out)
        file_size = os.path.getsize(out)
        self.done.emit(out, duration, file_size)

    # ── helpers ───────────────────────────────────────────────────────────────

    def _concat_media(self, ffmpeg: str, paths: list[str], out_name: str) -> str:
        """Concat list of same-codec media files. Returns output path or ''."""
        if not paths:
            return ""
        if len(paths) == 1 and os.path.exists(paths[0]):
            return paths[0]
        lst = os.path.join(self._tmp, out_name + ".txt")
        out = os.path.join(self._tmp, out_name)
        with open(lst, "w", encoding="utf-8") as f:
            for p in paths:
                # Escape single quotes for ffmpeg concat list
                escaped = p.replace("'", "'\\''")
                f.write(f"file '{escaped}'\n")
        ok = _run([
            ffmpeg, "-y",
            "-f", "concat", "-safe", "0",
            "-i", lst,
            "-c", "copy",
            out,
        ], timeout=180)
        return out if (ok and os.path.exists(out)) else (paths[0] if paths else "")

    def _mix(self, ffmpeg: str, lb: str, mic: str) -> str:
        """Mix loopback + mic with amix. Falls back gracefully."""
        lb_ok  = bool(lb  and os.path.exists(lb))
        mic_ok = bool(mic and os.path.exists(mic))

        if lb_ok and mic_ok:
            mixed = os.path.join(self._tmp, "mixed.wav")
            ok = _run([
                ffmpeg, "-y",
                "-i", lb, "-i", mic,
                "-filter_complex",
                "[0:a][1:a]amix=inputs=2:duration=longest:dropout_transition=0[a]",
                "-map", "[a]",
                "-c:a", "pcm_s16le",
                mixed,
            ], timeout=180)
            if ok and os.path.exists(mixed) and os.path.getsize(mixed) > 44:
                print("[Recorder] Audio: loopback + mic mixed ✓", file=sys.stderr)
                return mixed
            print("[Recorder] amix failed, falling back to loopback", file=sys.stderr)
            return lb
        if lb_ok:
            print("[Recorder] Audio: loopback only", file=sys.stderr)
            return lb
        if mic_ok:
            print("[Recorder] Audio: mic only", file=sys.stderr)
            return mic
        return ""


# ──────────────────────────────────────────────────────────────────────────────
# ScreenRecorder — public API
# ──────────────────────────────────────────────────────────────────────────────

class ScreenRecorder(QObject):
    """
    Thread-safe screen recorder. All public methods are safe to call from the
    Qt main thread. Signals are delivered in the Qt main thread.

    Usage:
        recorder = ScreenRecorder(db, recordings_dir="recordings")
        recorder.start_recording("Client Name", segment_id=42)
        recorder.pause_recording()
        recorder.resume_recording()
        recorder.stop_recording()
    """

    recording_started = Signal(str)       # client_name
    recording_stopped = Signal(str, int)  # file_path, duration_sec
    recording_error   = Signal(str)       # error message
    status_changed    = Signal(str)       # short status for UI label

    def __init__(self, db, recordings_dir: str = "recordings"):
        super().__init__()
        self.db             = db
        self.recordings_dir = recordings_dir

        self._state: str                         = _S.IDLE
        self._client_name: str                   = ""
        self._segment_id:  Optional[int]         = None
        self._session_dir: str                   = ""
        self._final_path:  str                   = ""
        self._segments: list[tuple[str, str, str]] = []   # (video, lb, mic)

        self._seg_worker: Optional[_SegmentWorker]   = None
        self._fin_worker: Optional[_FinalizeWorker]  = None

        # Audio device info — populated once per session in a background thread
        self._lb_info:  Optional[dict] = None
        self._mic_info: Optional[dict] = None

    # ── properties ────────────────────────────────────────────────────────────

    @property
    def is_recording(self) -> bool:
        return self._state in (_S.RECORDING, _S.PAUSED)

    @property
    def is_paused(self) -> bool:
        return self._state == _S.PAUSED

    @property
    def state(self) -> str:
        return self._state

    # ── public API ────────────────────────────────────────────────────────────

    def start_recording(self, client_name: str, segment_id: int = None):
        """Start a new recording session (silently ignored if already active)."""
        if self._state != _S.IDLE:
            self.status_changed.emit("⚠ Сначала остановите текущую запись")
            return

        self._client_name = (client_name or "").strip() or "Без_имени"
        self._segment_id  = segment_id
        self._segments    = []
        self._final_path  = self._make_final_path(self._client_name)
        self._session_dir = tempfile.mkdtemp(prefix="rec_")

        try:
            os.makedirs(os.path.dirname(self._final_path), exist_ok=True)
        except Exception as e:
            self.recording_error.emit(f"Не удалось создать папку: {e}")
            return

        def _probe_then_start():
            lb, mic = _probe_audio()
            self._lb_info  = lb
            self._mic_info = mic
            # Post _begin_recording back to the Qt thread
            QMetaObject.invokeMethod(self, "_begin_recording",
                                     Qt.ConnectionType.QueuedConnection)

        threading.Thread(target=_probe_then_start, daemon=True).start()

    def pause_recording(self):
        if self._state != _S.RECORDING:
            return
        self._state = _S.PAUSED
        self.status_changed.emit("⏸ Пауза…")
        if self._seg_worker:
            self._seg_worker.stop_gracefully()
        # _on_segment_done will handle the rest

    def resume_recording(self):
        if self._state != _S.PAUSED:
            return
        self._state = _S.RECORDING
        self.status_changed.emit(f"⏺ {self._client_name}")
        self._launch_segment()

    def stop_recording(self):
        if self._state not in (_S.RECORDING, _S.PAUSED):
            return
        self._state = _S.STOPPING
        self.status_changed.emit("⏹ Сохраняю…")
        if self._seg_worker and self._seg_worker.isRunning():
            self._seg_worker.stop_gracefully()
            # _on_segment_done → _finalize
        else:
            self._finalize()

    # ── private slots (called from Qt thread) ─────────────────────────────────

    @Slot()
    def _begin_recording(self):
        """Called in Qt thread after audio probe completes."""
        if self._state != _S.IDLE:
            return
        self._state = _S.RECORDING
        self.recording_started.emit(self._client_name)
        self._launch_segment()

    def _launch_segment(self):
        idx = len(self._segments)
        seg   = os.path.join(self._session_dir, f"seg_{idx:03d}.mp4")
        lb    = os.path.join(self._session_dir, f"lb_{idx:03d}.wav")  if self._lb_info  else ""
        mic   = os.path.join(self._session_dir, f"mic_{idx:03d}.wav") if self._mic_info else ""

        cmd = self._build_ffmpeg_cmd(seg)
        if not cmd:
            self._state = _S.IDLE
            self.recording_error.emit("ffmpeg не найден в PATH.")
            return

        print(f"[Recorder] start segment {idx}: {seg}", file=sys.stderr)
        w = _SegmentWorker(cmd, seg, lb, mic, self._lb_info, self._mic_info)
        w.finished.connect(self._on_segment_done)
        self._seg_worker = w
        w.start()

    def _on_segment_done(self, seg: str, lb_wav: str, mic_wav: str, ok: bool):
        if ok:
            self._segments.append((seg, lb_wav, mic_wav))
            print(f"[Recorder] segment done — video ok, "
                  f"lb={bool(lb_wav)}, mic={bool(mic_wav)}", file=sys.stderr)
        else:
            # Video failed; still try to keep audio if we have previous segments
            print(f"[Recorder] segment FAILED: {seg}", file=sys.stderr)

        if self._state == _S.STOPPING:
            self._finalize()
        elif self._state == _S.PAUSED:
            self.status_changed.emit("⏸ На паузе")
        elif self._state == _S.RECORDING:
            # Unexpected crash of FFmpeg mid-recording → restart
            self.status_changed.emit("⚠ Перезапуск…")
            self._launch_segment()

    def _finalize(self):
        if not self._segments:
            self._state = _S.IDLE
            self.recording_error.emit(
                "Нет записанных сегментов.\n"
                "Убедитесь, что ffmpeg установлен и доступен в PATH."
            )
            return

        w = _FinalizeWorker(
            segments    = list(self._segments),
            final_path  = self._final_path,
            session_dir = self._session_dir,
        )
        w.done.connect(self._on_finalize_done)
        w.error.connect(self._on_finalize_error)
        self._fin_worker = w
        w.start()

    def _on_finalize_done(self, path: str, duration: int, size: int):
        try:
            self.db.add_recording(
                client_name = self._client_name,
                file_path   = os.path.abspath(path),
                duration    = duration,
                file_size   = size,
                segment_id  = self._segment_id,
            )
        except Exception as e:
            print(f"[Recorder] DB write error: {e}", file=sys.stderr)
        self._state = _S.IDLE
        self.status_changed.emit(f"✔ Сохранено ({_fmt_size(size)}, {duration}с)")
        self.recording_stopped.emit(os.path.abspath(path), duration)

    def _on_finalize_error(self, msg: str):
        self._state = _S.IDLE
        self.recording_error.emit(msg)

    # ── FFmpeg command ─────────────────────────────────────────────────────────

    def _build_ffmpeg_cmd(self, output: str) -> list[str]:
        ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
        if not os.path.isabs(ffmpeg) and not shutil.which(ffmpeg):
            return []
        x, y, w, h = get_virtual_screen_geometry()
        w, h = _even(w), _even(h)
        print(f"[Recorder] screen {w}x{h}+{x}+{y}", file=sys.stderr)
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
            "-c:v", "libx264",
            "-preset", "faster",
            "-crf", "28",
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            output,
        ]

    # ── path builder ──────────────────────────────────────────────────────────

    def _make_final_path(self, client_name: str) -> str:
        now  = datetime.now()
        safe = re.sub(r'[\\/:*?"<>|]', "_", client_name)
        return os.path.join(
            self.recordings_dir,
            now.strftime("%Y-%m-%d"),
            now.strftime("%H-%M") + f"_{safe}.mp4",
        )

import sys
import os
import subprocess
from PySide6.QtWidgets import QApplication, QMessageBox
from PySide6.QtCore import Qt

from assets import app_icon
from db import Database
from floating_timer import FloatingTimer
from idle_detector import IdleDetector
from hotkeys import GlobalHotkeyManager
from journal_window import JournalWindow
from settings_window import SettingsWindow
from recorder import ScreenRecorder
from recorder_dialog import RecorderDialog

def main():
    # 1. Initialize App
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setWindowIcon(app_icon())

    # 2. Database Setup
    base_dir = os.path.dirname(os.path.abspath(__file__))
    data_dir = os.path.join(base_dir, "data")
    if not os.path.exists(data_dir):
        os.makedirs(data_dir)
    
    db_path = os.path.join(data_dir, "tracker.db")
    db = Database(db_path)

    # Add demo data if empty
    if not db.get_clients():
        db.add_client("Демо Клиент", "Тестовый клиент для первого запуска", 2000)
        db.add_client("Разработка ПО", "Внутренний проект", 0)

    # 3. Main Windows
    hotkey_manager = None
    settings_window = SettingsWindow(db)

    def show_settings():
        settings_window.show()
        settings_window.raise_()
        settings_window.activateWindow()

    timer_window = FloatingTimer(db, on_open_settings=show_settings)
    journal_window = JournalWindow(db)

    # --- Screen Recorder ---
    _default_rec_dir = os.path.join(base_dir, "recordings")
    recordings_dir = db.get_setting("recordings_dir", "") or _default_rec_dir
    recorder = ScreenRecorder(db, recordings_dir=recordings_dir)

    def on_recording_started(client_name: str):
        timer_window.set_recording_status(True, client_name)

    def on_recording_stopped(file_path: str, duration: int):
        timer_window.set_recording_status(False)

    def on_recording_error(msg: str):
        timer_window.set_recording_status(False)
        QMessageBox.warning(
            None, "Ошибка записи",
            f"Не удалось записать экран:\n\n{msg}"
        )

    def on_recorder_status(msg: str):
        timer_window.show_recorder_status(msg)

    recorder.recording_started.connect(on_recording_started)
    recorder.recording_stopped.connect(on_recording_stopped)
    recorder.recording_error.connect(on_recording_error)
    recorder.status_changed.connect(on_recorder_status)

    # --- Auto-sync: timer ↔ recorder ---
    def on_work_started(client_name: str, segment_id: int):
        """Timer just started → silently start recording (no dialog)."""
        if not recorder.is_recording:
            recorder.start_recording(client_name, segment_id=segment_id)

    def on_work_paused():
        """Timer paused → pause recording."""
        if recorder.is_recording and not recorder.is_paused:
            recorder.pause_recording()

    def on_work_resumed(client_name: str, segment_id: int):
        """Timer resumed → resume recording (or start fresh if was stopped)."""
        if recorder.is_paused:
            # Update segment_id to the new resume segment
            recorder._segment_id = segment_id
            recorder.resume_recording()
        elif not recorder.is_recording:
            recorder.start_recording(client_name, segment_id=segment_id)

    def on_work_stopped():
        """Timer stopped → stop and save recording."""
        if recorder.is_recording:
            recorder.stop_recording()

    timer_window.work_started.connect(on_work_started)
    timer_window.work_paused.connect(on_work_paused)
    timer_window.work_resumed.connect(on_work_resumed)
    timer_window.work_stopped.connect(on_work_stopped)

    _cleanup_done = [False]   # guard against double cleanup

    # 4. Crash Recovery Check
    open_seg = db.get_open_segment()
    if open_seg:
        msg = f"Обнаружена незавершенная сессия:\nКлиент: {open_seg['client_name']}\nНачало: {open_seg['start_at']}\n\nПродолжить?"
        reply = QMessageBox.question(None, "Восстановление", msg, QMessageBox.Yes | QMessageBox.No)
        if reply == QMessageBox.Yes:
            timer_window.current_client_id = open_seg['client_id']
            timer_window.current_segment_id = open_seg['id']
            timer_window.client_label.setText(open_seg['client_name'] or "N/A")
            timer_window.client_label.show()
            timer_window.client_input.hide()
            timer_window.btn_toggle.setText("Ⅱ")
            timer_window.status_label.setText("Восстановлено")
            
            # Calculate elapsed
            from datetime import datetime
            start_dt = datetime.fromisoformat(open_seg['start_at'])
            timer_window.seconds_elapsed = int((datetime.now() - start_dt).total_seconds())
            timer_window.display_timer.start(1000)
        else:
            db.end_segment(open_seg['id'], note="Завершено при восстановлении")

    # Position timer top right
    screen = app.primaryScreen().geometry()
    timer_window.move(screen.width() - timer_window.width() - 50, 100)
    timer_window.show()

    # 4. Idle Detection
    threshold = int(db.get_setting('idle_threshold', '300'))
    idle_thread = IdleDetector(threshold_seconds=threshold)
    idle_thread.start()

    # 5. Hotkeys Integration
    def handle_hotkey(action):
        if action == 'toggle_timer':
            timer_window.toggle_start_stop()
        elif action == 'pause_timer':
            timer_window.toggle_pause_resume()
        elif action == 'add_note':
            timer_window.add_note()
        elif action == 'add_reminder':
            timer_window.add_reminder_dialog()
        elif action == 'change_client':
            timer_window.pick_client()
        elif action == 'open_journal':
            journal_window.show()
            journal_window.raise_()
            journal_window.activateWindow()
        elif action == 'toggle_recording':
            if recorder.is_recording:
                # Already recording — show hint instead of accidental stop
                timer_window.show_recorder_status("⚠ Используйте Ctrl+Alt+E для остановки")
            else:
                # Pre-fill with current timer client name if active
                prefill = ""
                if timer_window.current_client_id:
                    prefill = timer_window.client_label.text()
                dlg = RecorderDialog(db, current_client_name=prefill)
                if dlg.exec():
                    recorder.start_recording(dlg.client_name)
        elif action == 'pause_recording':
            if recorder.is_paused:
                recorder.resume_recording()
                timer_window.set_recording_status(True, recorder._client_name)
            elif recorder.is_recording:
                recorder.pause_recording()
                timer_window.set_recording_status(True, recorder._client_name, paused=True)
            else:
                timer_window.show_recorder_status("Запись не активна")
        elif action == 'stop_recording':
            if recorder.is_recording:
                recorder.stop_recording()
            else:
                timer_window.show_recorder_status("Запись не активна")
        elif action == 'open_recordings':
            _open_streamlit(base_dir)
        elif action.startswith('switch_client_'):
            idx = int(action.split('_')[-1]) - 1
            clients = db.get_clients()
            if idx < len(clients):
                client = clients[idx]
                timer_window.current_client_id = client['id']
                timer_window.client_label.setText(client['name'])
                timer_window.client_label.show()
                timer_window.client_input.hide()
                if timer_window.current_segment_id:
                    timer_window.stop_work()
                    timer_window.toggle_work()

    def _open_streamlit(base_dir: str):
        """Launch Streamlit search app in background and open browser."""
        app_script = os.path.join(base_dir, "app.py")
        if not os.path.exists(app_script):
            QMessageBox.information(None, "Streamlit", "app.py не найден. Убедитесь, что app.py находится рядом с main.py.")
            return
        try:
            subprocess.Popen(
                [sys.executable, "-m", "streamlit", "run", app_script,
                 "--server.headless", "false"],
                creationflags=subprocess.CREATE_NO_WINDOW,
                cwd=base_dir,
            )
        except Exception as e:
            QMessageBox.warning(None, "Streamlit", f"Не удалось запустить Streamlit:\n{e}")

    def start_hotkey_manager():
        nonlocal hotkey_manager

        if hotkey_manager:
            hotkey_manager.stop()
            hotkey_manager.wait(1000)

        hotkey_manager = GlobalHotkeyManager(db.get_hotkeys())
        hotkey_manager.hotkey_triggered.connect(handle_hotkey)
        hotkey_manager.start()

    def restart_hotkey_manager():
        start_hotkey_manager()

    settings_window.hotkeys_changed.connect(restart_hotkey_manager)
    settings_window.opacity_changed.connect(timer_window.apply_opacity)
    settings_window.color_changed.connect(timer_window.apply_app_color)
    start_hotkey_manager()

    # 6. Final cleanup on exit
    def cleanup():
        if _cleanup_done[0]:
            return
        _cleanup_done[0] = True
        print("Останавливаю потоки и сохраняю данные...")
        idle_thread.stop()
        if hotkey_manager:
            hotkey_manager.stop()
        # Disconnect timer→recorder sync BEFORE stop_work to prevent
        # on_work_stopped from triggering a second stop_recording()
        try:
            timer_window.work_stopped.disconnect(on_work_stopped)
        except Exception:
            pass
        if recorder.is_recording:
            recorder.stop_recording()
        timer_window.stop_work()

    app.aboutToQuit.connect(cleanup)

    # 7. Run
    sys.exit(app.exec())

if __name__ == "__main__":
    main()

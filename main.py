import sys
import os
from PySide6.QtWidgets import QApplication, QMessageBox
from PySide6.QtCore import Qt

from assets import app_icon
from db import Database
from floating_timer import FloatingTimer
from idle_detector import IdleDetector
from hotkeys import GlobalHotkeyManager
from journal_window import JournalWindow
from settings_window import SettingsWindow

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
    start_hotkey_manager()

    # 6. Final cleanup on exit
    def cleanup():
        print("Stopping threads and saving data...")
        idle_thread.stop()
        if hotkey_manager:
            hotkey_manager.stop()
        timer_window.stop_work()

    app.aboutToQuit.connect(cleanup)

    # 7. Run
    sys.exit(app.exec())

if __name__ == "__main__":
    main()

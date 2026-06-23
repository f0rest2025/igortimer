from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, 
    QFrame, QMenu, QSystemTrayIcon, QStyle, QInputDialog,
    QLineEdit, QCompleter
)
from PySide6.QtCore import Qt, QTimer, QPoint, Signal, QStringListModel
from PySide6.QtGui import QAction, QColor, QPalette
import subprocess, sys, os, socket

from assets import app_icon

class FloatingTimer(QWidget):
    """
    A small, semi-transparent window that stays on top of all other windows.
    """
    # Work-state signals for auto-sync with recorder
    work_started  = Signal(str, int)   # client_name, segment_id
    work_paused   = Signal()
    work_resumed  = Signal(str, int)   # client_name, segment_id
    work_stopped  = Signal()
    def __init__(self, db, on_open_settings=None):
        super().__init__()
        self.db = db
        self.on_open_settings = on_open_settings
        self.current_segment_id = None
        self.current_client_id = None
        self.seconds_elapsed = 0
        self.is_paused = False
        
        self.init_ui()
        self.load_settings()
        self.update_client_list()
        
        # Main timer for display updates
        self.display_timer = QTimer(self)
        self.display_timer.timeout.connect(self.update_tick)
        
        self.drag_pos = None

    def init_ui(self):
        self.setWindowIcon(app_icon())

        # Window properties
        self.setWindowFlags(
            Qt.WindowStaysOnTopHint | 
            Qt.FramelessWindowHint | 
            Qt.Tool | # Hides from taskbar
            Qt.SubWindow
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        
        self.apply_app_color("#4ade80")

        # Layout
        self.main_layout = QVBoxLayout(self)
        self.main_layout.setContentsMargins(0, 0, 0, 0)
        
        self.frame = QFrame()
        self.frame.setObjectName("MainFrame")
        self.frame_layout = QVBoxLayout(self.frame)
        self.frame_layout.setContentsMargins(10, 8, 10, 8)
        self.frame_layout.setSpacing(2)

        # Header: Client Input/Label
        self.client_container = QWidget()
        self.client_container_layout = QVBoxLayout(self.client_container)
        self.client_container_layout.setContentsMargins(0, 0, 0, 0)
        
        self.client_input = QLineEdit()
        self.client_input.setPlaceholderText("Клиент (Поиск/Новый)...")
        self.client_input.returnPressed.connect(self.handle_client_input)
        
        self.client_label = QLabel("Нет клиента")
        self.client_label.setObjectName("ClientLabel")
        self.client_label.setToolTip("Нажмите, чтобы сменить клиента")
        self.client_label.setCursor(Qt.PointingHandCursor)
        self.client_label.mousePressEvent = lambda e: self.enable_client_input()
        
        self.client_container_layout.addWidget(self.client_input)
        self.client_container_layout.addWidget(self.client_label)
        self.client_label.hide() # Input is visible by default if no client
        
        self.frame_layout.addWidget(self.client_container)

        # Body: Timer
        self.time_label = QLabel("00:00:00")
        self.time_label.setObjectName("TimeLabel")
        self.frame_layout.addWidget(self.time_label)

        # Status
        self.status_label = QLabel("Ожидание")
        self.status_label.setObjectName("StatusLabel")
        self.frame_layout.addWidget(self.status_label)

        # Controls (Initially hidden or compact)
        self.controls_widget = QWidget()
        self.controls_layout = QHBoxLayout(self.controls_widget)
        self.controls_layout.setContentsMargins(0, 5, 0, 0)
        self.controls_layout.setSpacing(5)

        self.btn_toggle = QPushButton("▶")
        self.btn_toggle.setToolTip("Старт/Пауза")
        self.btn_toggle.setObjectName("ActionBtn")
        self.btn_toggle.clicked.connect(self.toggle_work)

        self.btn_stop = QPushButton("◼")
        self.btn_stop.setToolTip("Стоп")
        self.btn_stop.clicked.connect(self.stop_work)

        self.btn_note = QPushButton("✎")
        self.btn_note.setToolTip("Добавить заметку")
        self.btn_note.clicked.connect(self.add_note)

        self.btn_menu = QPushButton("⋮")
        self.btn_menu.setToolTip("Меню")
        self.btn_menu.clicked.connect(self.show_context_menu)

        self.controls_layout.addWidget(self.btn_toggle)
        self.controls_layout.addWidget(self.btn_stop)
        self.controls_layout.addWidget(self.btn_note)

        # REC dot — inline, between note and menu, always present, hidden when idle
        self.rec_dot = QLabel("⏺")
        self.rec_dot.setObjectName("RecDot")
        self.rec_dot.setFixedWidth(18)
        self.rec_dot.setAlignment(Qt.AlignCenter)
        self.rec_dot.setToolTip("Запись активна")
        self.rec_dot.hide()
        self.controls_layout.addWidget(self.rec_dot)

        self.controls_layout.addWidget(self.btn_menu)
        
        self.frame_layout.addWidget(self.controls_widget)
        
        # Reminders Section
        self.reminder_label = QLabel("")
        self.reminder_label.setObjectName("ReminderLabel")
        self.reminder_label.setWordWrap(True)
        self.reminder_label.setAlignment(Qt.AlignCenter)
        self.reminder_label.setCursor(Qt.PointingHandCursor)
        self.reminder_label.mousePressEvent = self.dismiss_reminder
        self.reminder_label.hide()
        self.frame_layout.addWidget(self.reminder_label)

        self.main_layout.addWidget(self.frame)

        self.setFixedSize(160, 140)  # Fixed forever — REC dot is inline, no height jump
        self.active_reminder = None
        self._rec_blink_state = False
        self._rec_blink_timer = QTimer(self)
        self._rec_blink_timer.timeout.connect(self._blink_rec)

        # Reminder Check Timer
        self.reminder_check_timer = QTimer(self)
        self.reminder_check_timer.timeout.connect(self.check_reminders)
        self.reminder_check_timer.start(5000)

    def apply_app_color(self, color):
        accent = QColor(color)
        if not accent.isValid():
            accent = QColor("#4ade80")

        accent_hex = accent.name()
        accent_soft = f"rgba({accent.red()}, {accent.green()}, {accent.blue()}, 36)"

        self.setStyleSheet(f"""
            #MainFrame {{
                background-color: rgba(30, 30, 30, 220);
                border: 1px solid #444;
                border-radius: 8px;
            }}
            QLabel {{
                color: #e0e0e0;
                font-family: 'Segoe UI', sans-serif;
            }}
            QLineEdit {{
                background-color: rgba(255, 255, 255, 0.1);
                border: 1px solid #555;
                color: white;
                border-radius: 4px;
                padding: 2px 5px;
                font-size: 11px;
            }}
            #TimeLabel {{
                font-size: 20px;
                font-weight: bold;
                color: {accent_hex};
                margin-bottom: 2px;
            }}
            #ClientLabel {{
                font-size: 11px;
                color: #94a3b8;
                font-weight: 600;
            }}
            #StatusLabel {{
                font-size: 10px;
                color: #64748b;
                font-style: italic;
            }}
            #ReminderLabel {{
                font-size: 11px;
                color: {accent_hex};
                background-color: {accent_soft};
                border-radius: 4px;
                padding: 4px 6px;
                margin-top: 4px;
                font-weight: 500;
            }}
            QPushButton {{
                background-color: transparent;
                border: none;
                color: #cbd5e1;
                font-size: 14px;
                padding: 4px;
                border-radius: 4px;
            }}
            QPushButton:hover {{
                background-color: rgba(255, 255, 255, 0.1);
            }}
            QPushButton#ActionBtn {{
                color: {accent_hex};
                font-weight: bold;
            }}
            #RecDot {{
                font-size: 11px;
                color: #ff4444;
                font-weight: bold;
                padding: 0px 1px;
            }}
        """)

    def load_settings(self):
        try:
            opacity = float(self.db.get_setting('opacity', '0.8'))
        except (TypeError, ValueError):
            opacity = 0.8
        self.apply_opacity(opacity)
        self.apply_app_color(self.db.get_setting('app_color', '#4ade80'))

    def apply_opacity(self, opacity):
        self.setWindowOpacity(max(0.3, min(1.0, float(opacity))))

    def update_client_list(self):
        clients = self.db.get_clients()
        client_names = [c['name'] for c in clients]
        
        completer = QCompleter(client_names, self)
        completer.setCaseSensitivity(Qt.CaseInsensitive)
        completer.setFilterMode(Qt.MatchContains)
        self.client_input.setCompleter(completer)

    def enable_client_input(self):
        if self.current_segment_id:
            reply = QInputDialog.getText(self, "Смена клиента", "Текущая сессия будет завершена. Продолжить?", QLineEdit.Normal, "")
            if not reply[1]: return
            self.stop_work()

        self.client_label.hide()
        self.client_input.show()
        self.client_input.setFocus()
        self.client_input.selectAll()

    def handle_client_input(self):
        name = self.client_input.text().strip()
        if not name: return

        clients = self.db.get_clients()
        client = next((c for c in clients if c['name'].lower() == name.lower()), None)
        
        if client:
            self.current_client_id = client['id']
            name = client['name']
        else:
            # Create new client automatically or ask?
            # User wants to "write and choose", implied creation of new if doesn't exist.
            self.current_client_id = self.db.add_client(name)
            self.update_client_list()

        self.client_label.setText(name)
        self.client_input.hide()
        self.client_label.show()
        
        # Optionally auto-start if user pressed Enter
        self.toggle_work()

    def update_tick(self):
        if self.current_segment_id and not self.is_paused:
            self.seconds_elapsed += 1
            h = self.seconds_elapsed // 3600
            m = (self.seconds_elapsed % 3600) // 60
            s = self.seconds_elapsed % 60
            self.time_label.setText(f"{h:02}:{m:02}:{s:02}")

    def toggle_work(self):
        if not self.current_client_id:
            self.enable_client_input()
            return

        if self.current_segment_id:
            if self.is_paused:
                # Resume
                self.db.end_segment(self.current_segment_id)
                self.current_segment_id = self.db.start_segment(self.current_client_id, "Работа", "work")
                self.is_paused = False
                self.btn_toggle.setText("Ⅱ")
                self.status_label.setText("В процессе")
                client_name = self.client_label.text()
                self.work_resumed.emit(client_name, self.current_segment_id)
            else:
                # Pause
                self.db.end_segment(self.current_segment_id)
                self.current_segment_id = self.db.start_segment(self.current_client_id, "Пауза", "pause")
                self.is_paused = True
                self.btn_toggle.setText("▶")
                self.status_label.setText("Пауза")
                self.work_paused.emit()
        else:
            # New Start
            self.current_segment_id = self.db.start_segment(self.current_client_id, "Работа", "work")
            self.is_paused = False
            self.btn_toggle.setText("Ⅱ")
            self.status_label.setText("В процессе")
            self.display_timer.start(1000)
            client_name = self.client_label.text()
            self.work_started.emit(client_name, self.current_segment_id)

    def toggle_start_stop(self):
        if self.current_segment_id:
            self.stop_work()
        else:
            self.toggle_work()

    def toggle_pause_resume(self):
        if self.current_segment_id:
            self.toggle_work()

    def stop_work(self):
        if self.current_segment_id:
            self.db.end_segment(self.current_segment_id)
        self.current_segment_id = None
        self.display_timer.stop()
        self.btn_toggle.setText("▶")
        self.status_label.setText("Завершено")
        self.seconds_elapsed = 0
        self.time_label.setText("00:00:00")
        self.work_stopped.emit()

    def pick_client(self):
        # Override pick_client to use our new inline system
        self.enable_client_input()

    def add_note(self):
        if not self.current_segment_id: return
        note, ok = QInputDialog.getText(self, "Заметка", "Добавьте комментарий к текущему сегменту:")
        if ok:
            # We update the current segment Note field
            self.db.conn.execute("UPDATE time_segments SET note = ? WHERE id = ?", (note, self.current_segment_id))
            self.db.conn.commit()

    def add_reminder_dialog(self):
        text, ok = QInputDialog.getText(self, "Новое напоминание", "Формат: Текст ЧЧ:ММ (или просто текст)")
        if not ok or not text.strip(): return

        # Simple parser for "Text HH:MM"
        parts = text.rsplit(' ', 1)
        remind_time_str = ""
        reminder_text = text
        
        if len(parts) == 2 and ':' in parts[1]:
            reminder_text = parts[0]
            remind_time_str = parts[1]

        from datetime import datetime, timedelta
        now = datetime.now()
        
        if remind_time_str:
            try:
                h, m = map(int, remind_time_str.split(':'))
                remind_at = now.replace(hour=h, minute=m, second=0, microsecond=0)
                if remind_at < now:
                    remind_at += timedelta(days=1)
            except:
                remind_at = now + timedelta(minutes=5)
        else:
            remind_at = now + timedelta(minutes=5)

        self.db.add_reminder(reminder_text, remind_at.isoformat())
        self.status_label.setText("Напомню!")

    def check_reminders(self):
        if self.active_reminder: return

        reminders = self.db.get_pending_reminders()
        if reminders:
            self.active_reminder = reminders[0]
            self.reminder_label.setText(f"🔔 {self.active_reminder['text']}")
            self.reminder_label.show()
            self.setFixedSize(160, 180) # Expand
            self.status_label.setText("!!!")

    def dismiss_reminder(self, event):
        if self.active_reminder:
            self.db.mark_reminder_done(self.active_reminder['id'])
            self.active_reminder = None
            self.reminder_label.hide()
            self.setFixedSize(160, 140) # Shrink back
            self.status_label.setText("Готов")

    # --- Recording Status ---

    def set_recording_status(self, is_recording: bool, client_name: str = "", paused: bool = False):
        """Update the inline REC dot. Window size never changes."""
        if is_recording:
            self.rec_dot.show()
            if paused:
                # Steady amber dot — paused
                self.rec_dot.setText("⏸")
                self.rec_dot.setStyleSheet("color: #f59e0b;")
                self.rec_dot.setToolTip(f"Пауза записи: {client_name}")
                self._rec_blink_timer.stop()
                self._rec_blink_state = True
            else:
                # Blinking red dot — recording
                self.rec_dot.setText("⏺")
                self.rec_dot.setStyleSheet("color: #ff4444;")
                self.rec_dot.setToolTip(f"Запись: {client_name}")
                self._rec_blink_timer.start(800)
        else:
            self._rec_blink_timer.stop()
            self.rec_dot.hide()

    def show_recorder_status(self, msg: str):
        """Show a transient recorder status in the status label."""
        self.status_label.setText(msg)

    def _blink_rec(self):
        """Toggle REC dot opacity for blinking effect."""
        self._rec_blink_state = not self._rec_blink_state
        self.rec_dot.setVisible(self._rec_blink_state)


    def show_context_menu(self):
        menu = QMenu(self)
        accent = QColor(self.db.get_setting('app_color', '#4ade80'))
        if not accent.isValid():
            accent = QColor("#4ade80")
        accent_brightness = accent.red() * 0.299 + accent.green() * 0.587 + accent.blue() * 0.114
        accent_text = "#111111" if accent_brightness > 160 else "#ffffff"
        menu.setStyleSheet(f"""
            QMenu {{
                background-color: #2d2d2d;
                color: white;
                border: 1px solid #444;
            }}
            QMenu::item:selected {{
                background-color: {accent.name()};
                color: {accent_text};
            }}
        """)
        
        change_client_act = QAction("Сменить клиента", self)
        change_client_act.triggered.connect(self.enable_client_input)
        
        add_reminder_act = QAction("Установить напоминание", self)
        add_reminder_act.triggered.connect(self.add_reminder_dialog)

        journal_act = QAction("Открыть журнал", self)
        journal_act.triggered.connect(self.show_journal)

        recordings_act = QAction("🔍 Поиск записей (Streamlit)", self)
        recordings_act.triggered.connect(self.open_recordings_search)

        settings_act = QAction("Настройки", self)
        settings_act.triggered.connect(self.open_settings)
        
        exit_act = QAction("Выйти из приложения", self)
        exit_act.triggered.connect(self.quit_app)

        menu.addAction(change_client_act)
        menu.addAction(add_reminder_act)
        menu.addAction(journal_act)
        menu.addAction(recordings_act)
        menu.addAction(settings_act)
        menu.addSeparator()
        menu.addAction(exit_act)
        
        menu.exec(self.mapToGlobal(self.btn_menu.pos()))

    def show_journal(self):
        from PySide6.QtWidgets import QApplication
        from journal_window import JournalWindow
        for widget in QApplication.topLevelWidgets():
            if isinstance(widget, JournalWindow):
                widget.show()
                widget.raise_()
                widget.activateWindow()
                return

    def open_settings(self):
        if self.on_open_settings:
            self.on_open_settings()

    def open_recordings_search(self):
        """Open Streamlit recordings UI. Ensures only ONE instance is running."""
        import os, sys, subprocess, socket, webbrowser, threading, time

        base_dir   = os.path.dirname(os.path.abspath(__file__))
        app_script = os.path.join(base_dir, "app.py")
        url        = "http://localhost:8501"

        def _port_open(port: int) -> bool:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(0.4)
                return s.connect_ex(("127.0.0.1", port)) == 0

        if _port_open(8501):
            webbrowser.open(url)
            return

        if not os.path.exists(app_script):
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.information(self, "Поиск записей", "app.py не найден.")
            return

        try:
            proc = subprocess.Popen(
                [sys.executable, "-m", "streamlit", "run", app_script,
                 "--server.headless", "true",
                 "--server.port", "8501",
                 "--browser.gatherUsageStats", "false"],
                cwd=base_dir,
                # No CREATE_NO_WINDOW so errors can be seen if needed
            )
        except Exception as e:
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.warning(self, "Streamlit", f"Ошибка запуска: {e}")
            return

        # Poll until port is ready (max 20 seconds)
        def _wait_and_open():
            deadline = time.time() + 20
            while time.time() < deadline:
                if _port_open(8501):
                    webbrowser.open(url)
                    return
                time.sleep(0.5)
            # Timed out — try anyway
            webbrowser.open(url)

        threading.Thread(target=_wait_and_open, daemon=True).start()


    def quit_app(self):
        from PySide6.QtWidgets import QApplication
        QApplication.quit()

    # --- Dragging Logic ---
    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.drag_pos = event.globalPosition().toPoint()

    def mouseMoveEvent(self, event):
        if self.drag_pos:
            delta = event.globalPosition().toPoint() - self.drag_pos
            self.move(self.pos() + delta)
            self.drag_pos = event.globalPosition().toPoint()

    def mouseReleaseEvent(self, event):
        self.drag_pos = None

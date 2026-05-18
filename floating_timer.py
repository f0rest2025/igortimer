from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, 
    QFrame, QMenu, QSystemTrayIcon, QStyle, QInputDialog,
    QLineEdit, QCompleter
)
from PySide6.QtCore import Qt, QTimer, QPoint, Signal, QStringListModel
from PySide6.QtGui import QAction, QColor, QPalette

from assets import app_icon

class FloatingTimer(QWidget):
    """
    A small, semi-transparent window that stays on top of all other windows.
    """
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
        
        # Stylesheet
        self.setStyleSheet("""
            #MainFrame {
                background-color: rgba(30, 30, 30, 220);
                border: 1px solid #444;
                border-radius: 8px;
            }
            QLabel {
                color: #e0e0e0;
                font-family: 'Segoe UI', sans-serif;
            }
            QLineEdit {
                background-color: rgba(255, 255, 255, 0.1);
                border: 1px solid #555;
                color: white;
                border-radius: 4px;
                padding: 2px 5px;
                font-size: 11px;
            }
            #TimeLabel {
                font-size: 20px;
                font-weight: bold;
                color: #4ade80; /* Light green */
                margin-bottom: 2px;
            }
            #ClientLabel {
                font-size: 11px;
                color: #94a3b8;
                font-weight: 600;
            }
            #StatusLabel {
                font-size: 10px;
                color: #64748b;
                font-style: italic;
            }
            #ReminderLabel {
                font-size: 11px;
                color: #fbbf24;
                background-color: rgba(251, 191, 36, 0.1);
                border-radius: 4px;
                padding: 4px 6px;
                margin-top: 4px;
                font-weight: 500;
            }
            QPushButton {
                background-color: transparent;
                border: none;
                color: #cbd5e1;
                font-size: 14px;
                padding: 4px;
                border-radius: 4px;
            }
            QPushButton:hover {
                background-color: rgba(255, 255, 255, 0.1);
            }
            QPushButton#ActionBtn {
                color: #4ade80;
                font-weight: bold;
            }
        """)

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

        self.setFixedSize(160, 140) # Slightly larger default
        self.active_reminder = None

        # Reminder Check Timer
        self.reminder_check_timer = QTimer(self)
        self.reminder_check_timer.timeout.connect(self.check_reminders)
        self.reminder_check_timer.start(5000)

    def load_settings(self):
        opacity = float(self.db.get_setting('opacity', '0.8'))
        self.setWindowOpacity(opacity)

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
            else:
                # Pause
                self.db.end_segment(self.current_segment_id)
                self.current_segment_id = self.db.start_segment(self.current_client_id, "Пауза", "pause")
                self.is_paused = True
                self.btn_toggle.setText("▶")
                self.status_label.setText("Пауза")
        else:
            # New Start
            self.current_segment_id = self.db.start_segment(self.current_client_id, "Работа", "work")
            self.is_paused = False
            self.btn_toggle.setText("Ⅱ")
            self.status_label.setText("В процессе")
            self.display_timer.start(1000)

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

    def show_context_menu(self):
        menu = QMenu(self)
        menu.setStyleSheet("QMenu { background-color: #2d2d2d; color: white; border: 1px solid #444; }")
        
        change_client_act = QAction("Сменить клиента", self)
        change_client_act.triggered.connect(self.enable_client_input)
        
        add_reminder_act = QAction("Установить напоминание", self)
        add_reminder_act.triggered.connect(self.add_reminder_dialog)

        journal_act = QAction("Открыть журнал", self)
        journal_act.triggered.connect(self.show_journal)

        settings_act = QAction("Настройки", self)
        settings_act.triggered.connect(self.open_settings)
        
        exit_act = QAction("Выйти из приложения", self)
        exit_act.triggered.connect(self.quit_app)

        menu.addAction(change_client_act)
        menu.addAction(add_reminder_act)
        menu.addAction(journal_act)
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

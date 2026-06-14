"""
recorder_dialog.py — Dialog for starting a screen recording session.
=====================================================================
Shows a small, stylish PySide6 window where the user types the client
name or ticket number. Integrates with the existing FloatingTimer:
if a client session is already active, its name is pre-filled.
"""

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QLineEdit, QCompleter, QFrame,
)
from PySide6.QtCore import Qt, QStringListModel
from PySide6.QtGui import QKeyEvent, QColor


class RecorderDialog(QDialog):
    """
    A minimal, dark-themed dialog.
    Call .exec() — returns QDialog.Accepted or QDialog.Rejected.
    After acceptance, read .client_name for the entered value.
    """

    def __init__(self, db, current_client_name: str = "", parent=None):
        super().__init__(parent)
        self.db = db
        self.client_name: str = current_client_name

        self.setWindowTitle("Начать запись")
        self.setWindowFlags(
            Qt.Dialog |
            Qt.WindowStaysOnTopHint |
            Qt.FramelessWindowHint
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setMinimumWidth(340)

        self._init_ui(current_client_name)
        self._apply_styles()

    # ------------------------------------------------------------------

    def _init_ui(self, prefill: str):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        self.frame = QFrame()
        self.frame.setObjectName("RecFrame")
        layout = QVBoxLayout(self.frame)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)

        # ---- Title row ----
        title_row = QHBoxLayout()
        dot = QLabel("⏺")
        dot.setObjectName("RecDot")
        title = QLabel("Новая запись экрана")
        title.setObjectName("RecTitle")
        title_row.addWidget(dot)
        title_row.addWidget(title)
        title_row.addStretch()

        close_btn = QPushButton("✕")
        close_btn.setObjectName("CloseBtn")
        close_btn.setFixedSize(22, 22)
        close_btn.clicked.connect(self.reject)
        title_row.addWidget(close_btn)
        layout.addLayout(title_row)

        # ---- Hint ----
        hint = QLabel("Введите имя клиента или номер тикета:")
        hint.setObjectName("HintLabel")
        layout.addWidget(hint)

        # ---- Input ----
        self.name_input = QLineEdit(prefill)
        self.name_input.setObjectName("NameInput")
        self.name_input.setPlaceholderText("Клиент / Тикет #1234…")
        self.name_input.selectAll()
        self.name_input.returnPressed.connect(self._accept)
        layout.addWidget(self.name_input)

        # Autocomplete from existing clients
        try:
            clients = self.db.get_clients()
            names = [c['name'] for c in clients]
            # Also add names from recordings
            recordings = self.db.get_recordings(limit=200)
            rec_names = list({r['client_name'] for r in recordings})
            all_names = list(dict.fromkeys(names + rec_names))  # deduplicate
            completer = QCompleter(all_names, self)
            completer.setCaseSensitivity(Qt.CaseInsensitive)
            completer.setFilterMode(Qt.MatchContains)
            self.name_input.setCompleter(completer)
        except Exception:
            pass

        # ---- Buttons ----
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)

        self.cancel_btn = QPushButton("Отмена")
        self.cancel_btn.setObjectName("CancelBtn")
        self.cancel_btn.clicked.connect(self.reject)

        self.start_btn = QPushButton("⏺  Начать запись")
        self.start_btn.setObjectName("StartBtn")
        self.start_btn.clicked.connect(self._accept)
        self.start_btn.setDefault(True)

        btn_row.addWidget(self.cancel_btn)
        btn_row.addStretch()
        btn_row.addWidget(self.start_btn)
        layout.addLayout(btn_row)

        outer.addWidget(self.frame)
        self.name_input.setFocus()

    def _accept(self):
        name = self.name_input.text().strip()
        if not name:
            self.name_input.setPlaceholderText("⚠ Введите имя или тикет!")
            self.name_input.setFocus()
            return
        self.client_name = name
        self.accept()

    def keyPressEvent(self, event: QKeyEvent):
        if event.key() == Qt.Key_Escape:
            self.reject()
        else:
            super().keyPressEvent(event)

    # ------------------------------------------------------------------
    # Drag support (frameless window)
    # ------------------------------------------------------------------

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._drag_pos = event.globalPosition().toPoint()

    def mouseMoveEvent(self, event):
        if hasattr(self, '_drag_pos') and self._drag_pos:
            delta = event.globalPosition().toPoint() - self._drag_pos
            self.move(self.pos() + delta)
            self._drag_pos = event.globalPosition().toPoint()

    def mouseReleaseEvent(self, event):
        self._drag_pos = None

    # ------------------------------------------------------------------

    def _apply_styles(self):
        self.setStyleSheet("""
            #RecFrame {
                background-color: rgba(22, 22, 28, 245);
                border: 1px solid #333;
                border-radius: 12px;
            }

            #RecDot {
                color: #ff4444;
                font-size: 18px;
                margin-right: 4px;
            }

            #RecTitle {
                color: #f0f0f0;
                font-family: 'Segoe UI', sans-serif;
                font-size: 14px;
                font-weight: 600;
            }

            #HintLabel {
                color: #8a8a9a;
                font-family: 'Segoe UI', sans-serif;
                font-size: 11px;
            }

            #NameInput {
                background-color: rgba(255,255,255,0.07);
                border: 1px solid #444;
                border-radius: 7px;
                color: #f0f0f0;
                font-family: 'Segoe UI', sans-serif;
                font-size: 13px;
                padding: 8px 12px;
            }

            #NameInput:focus {
                border: 1px solid #e84040;
                background-color: rgba(232, 64, 64, 0.06);
            }

            #StartBtn {
                background-color: #c0392b;
                color: white;
                border: none;
                border-radius: 7px;
                font-family: 'Segoe UI', sans-serif;
                font-size: 12px;
                font-weight: 600;
                padding: 8px 18px;
            }

            #StartBtn:hover { background-color: #e74c3c; }
            #StartBtn:pressed { background-color: #922b21; }

            #CancelBtn {
                background-color: rgba(255,255,255,0.06);
                color: #9a9aaa;
                border: 1px solid #3a3a4a;
                border-radius: 7px;
                font-family: 'Segoe UI', sans-serif;
                font-size: 12px;
                padding: 8px 14px;
            }

            #CancelBtn:hover {
                background-color: rgba(255,255,255,0.1);
                color: #d0d0e0;
            }

            #CloseBtn {
                background: transparent;
                color: #555;
                border: none;
                font-size: 13px;
                border-radius: 4px;
            }

            #CloseBtn:hover { color: #e74c3c; }

            QCompleter, QAbstractItemView {
                background-color: #1e1e2a;
                color: #e0e0f0;
                border: 1px solid #3a3a4a;
                selection-background-color: #c0392b;
            }
        """)

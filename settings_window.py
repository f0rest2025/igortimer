from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSlider,
    QVBoxLayout,
)

from assets import app_icon
from hotkey_definitions import (
    HOTKEY_ACTIONS,
    format_hotkey_for_display,
)


MODIFIER_KEYS = {
    Qt.Key_Control,
    Qt.Key_Alt,
    Qt.Key_Shift,
    Qt.Key_Meta,
}


SPECIAL_KEYS = {
    Qt.Key_Space: "<space>",
    Qt.Key_Return: "<enter>",
    Qt.Key_Enter: "<enter>",
    Qt.Key_Tab: "<tab>",
    Qt.Key_Escape: "<esc>",
    Qt.Key_Backspace: "<backspace>",
    Qt.Key_Delete: "<delete>",
    Qt.Key_Home: "<home>",
    Qt.Key_End: "<end>",
    Qt.Key_PageUp: "<page_up>",
    Qt.Key_PageDown: "<page_down>",
    Qt.Key_Left: "<left>",
    Qt.Key_Right: "<right>",
    Qt.Key_Up: "<up>",
    Qt.Key_Down: "<down>",
}


class SettingsWindow(QDialog):
    hotkeys_changed = Signal()
    opacity_changed = Signal(float)

    def __init__(self, db):
        super().__init__()
        self.db = db
        self.recording_action = None
        self.hotkey_value_labels = {}
        self.record_buttons = {}
        self.opacity_value_label = None
        self.opacity_slider = None

        self.setWindowTitle("Настройки")
        self.setWindowIcon(app_icon())
        self.resize(640, 620)
        self.setModal(False)

        self.init_ui()
        self.refresh_opacity()
        self.refresh_hotkey_labels()

    def init_ui(self):
        root_layout = QVBoxLayout(self)

        window_title = QLabel("Окно")
        window_title.setStyleSheet("font-size: 18px; font-weight: 600;")
        root_layout.addWidget(window_title)

        window_frame = QFrame()
        window_frame.setFrameShape(QFrame.StyledPanel)
        window_layout = QGridLayout(window_frame)
        window_layout.setColumnStretch(1, 1)

        window_layout.addWidget(QLabel("Прозрачность окна"), 0, 0)

        self.opacity_slider = QSlider(Qt.Horizontal)
        self.opacity_slider.setRange(30, 100)
        self.opacity_slider.setSingleStep(5)
        self.opacity_slider.setPageStep(10)
        self.opacity_slider.valueChanged.connect(self.handle_opacity_changed)
        window_layout.addWidget(self.opacity_slider, 0, 1)

        self.opacity_value_label = QLabel()
        self.opacity_value_label.setMinimumWidth(48)
        self.opacity_value_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        window_layout.addWidget(self.opacity_value_label, 0, 2)

        root_layout.addWidget(window_frame)

        title = QLabel("Горячие клавиши")
        title.setStyleSheet("font-size: 18px; font-weight: 600;")
        root_layout.addWidget(title)

        help_label = QLabel(
            "Для изменения нажмите «Записать сочетание», затем нажмите нужные клавиши."
        )
        help_label.setWordWrap(True)
        root_layout.addWidget(help_label)

        frame = QFrame()
        frame.setFrameShape(QFrame.StyledPanel)
        grid = QGridLayout(frame)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 0)
        grid.setColumnStretch(2, 0)

        grid.addWidget(QLabel("Действие"), 0, 0)
        grid.addWidget(QLabel("Сочетание"), 0, 1)
        grid.addWidget(QLabel(""), 0, 2)

        for row, (action, label) in enumerate(HOTKEY_ACTIONS, start=1):
            action_label = QLabel(label)
            hotkey_label = QLabel()
            hotkey_label.setAlignment(Qt.AlignCenter)
            record_button = QPushButton("Записать сочетание")
            record_button.clicked.connect(lambda checked=False, current_action=action: self.start_recording(current_action))

            self.hotkey_value_labels[action] = hotkey_label
            self.record_buttons[action] = record_button

            grid.addWidget(action_label, row, 0)
            grid.addWidget(hotkey_label, row, 1)
            grid.addWidget(record_button, row, 2)

        hotkeys_scroll = QScrollArea()
        hotkeys_scroll.setWidgetResizable(True)
        hotkeys_scroll.setFrameShape(QFrame.NoFrame)
        hotkeys_scroll.setWidget(frame)
        root_layout.addWidget(hotkeys_scroll, 1)

        buttons_layout = QHBoxLayout()
        buttons_layout.addStretch()

        reset_button = QPushButton("Сбросить горячие клавиши по умолчанию")
        reset_button.clicked.connect(self.reset_hotkeys)
        buttons_layout.addWidget(reset_button)

        close_button = QPushButton("Закрыть")
        close_button.clicked.connect(self.close)
        buttons_layout.addWidget(close_button)

        root_layout.addLayout(buttons_layout)

    def refresh_opacity(self):
        opacity = self.current_opacity()
        value = int(round(opacity * 100))
        self.opacity_slider.blockSignals(True)
        self.opacity_slider.setValue(value)
        self.opacity_slider.blockSignals(False)
        self.opacity_value_label.setText(f"{value}%")

    def current_opacity(self):
        try:
            opacity = float(self.db.get_setting("opacity", "0.8"))
        except (TypeError, ValueError):
            opacity = 0.8
        return max(0.3, min(1.0, opacity))

    def handle_opacity_changed(self, value):
        opacity = value / 100
        self.opacity_value_label.setText(f"{value}%")
        self.db.set_setting("opacity", f"{opacity:.2f}")
        self.opacity_changed.emit(opacity)

    def refresh_hotkey_labels(self):
        hotkeys = self.db.get_hotkeys()
        for action, _ in HOTKEY_ACTIONS:
            self.hotkey_value_labels[action].setText(
                format_hotkey_for_display(hotkeys[action])
            )

    def start_recording(self, action):
        self.recording_action = action
        for current_action, button in self.record_buttons.items():
            button.setEnabled(current_action == action)

        self.record_buttons[action].setText("Нажмите сочетание...")
        self.grabKeyboard()
        self.setFocus(Qt.OtherFocusReason)
        self.activateWindow()

    def keyPressEvent(self, event: QKeyEvent):
        if not self.recording_action:
            return super().keyPressEvent(event)

        if event.key() in MODIFIER_KEYS:
            return

        hotkey = self.event_to_hotkey(event)
        if not hotkey:
            QMessageBox.warning(
                self,
                "Горячие клавиши",
                "Не удалось распознать сочетание. Попробуйте другое.",
            )
            self.finish_recording()
            return

        conflict_action = self.db.find_hotkey_conflict(
            hotkey,
            exclude_action=self.recording_action,
        )
        if conflict_action:
            conflict_label = dict(HOTKEY_ACTIONS)[conflict_action]
            QMessageBox.warning(
                self,
                "Конфликт горячих клавиш",
                f"Сочетание уже назначено для действия «{conflict_label}».",
            )
            self.finish_recording()
            return

        self.db.set_hotkey(self.recording_action, hotkey)
        self.refresh_hotkey_labels()
        self.hotkeys_changed.emit()
        self.finish_recording()

    def event_to_hotkey(self, event: QKeyEvent):
        modifiers = []
        active_modifiers = event.modifiers()

        if active_modifiers & Qt.ControlModifier:
            modifiers.append("<ctrl>")
        if active_modifiers & Qt.AltModifier:
            modifiers.append("<alt>")
        if active_modifiers & Qt.ShiftModifier:
            modifiers.append("<shift>")
        if active_modifiers & Qt.MetaModifier:
            modifiers.append("<cmd>")

        key_code = event.key()
        key = SPECIAL_KEYS.get(key_code)
        if not key:
            if Qt.Key_A <= key_code <= Qt.Key_Z:
                key = chr(key_code).lower()
            elif Qt.Key_0 <= key_code <= Qt.Key_9:
                key = str(key_code - Qt.Key_0)
            elif Qt.Key_F1 <= key_code <= Qt.Key_F35:
                key = f"<f{key_code - Qt.Key_F1 + 1}>"

        if not key:
            return None

        return "+".join([*modifiers, key])

    def finish_recording(self):
        if self.recording_action:
            self.record_buttons[self.recording_action].setText("Записать сочетание")

        self.releaseKeyboard()
        self.recording_action = None
        for button in self.record_buttons.values():
            button.setEnabled(True)

    def reset_hotkeys(self):
        reply = QMessageBox.question(
            self,
            "Сброс горячих клавиш",
            "Вернуть все сочетания по умолчанию?",
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        self.db.reset_hotkeys_to_default()
        self.refresh_hotkey_labels()
        self.hotkeys_changed.emit()

    def closeEvent(self, event):
        if self.recording_action:
            self.finish_recording()
        super().closeEvent(event)

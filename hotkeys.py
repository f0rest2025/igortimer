from pynput import keyboard
from PySide6.QtCore import QThread, Signal

from hotkey_definitions import DEFAULT_HOTKEYS

class GlobalHotkeyManager(QThread):
    """
    Listens for global system-wide hotkeys using pynput.
    Connects these hotkeys to application actions.
    """
    hotkey_triggered = Signal(str) # Key code like 'toggle_timer', 'stop_timer', etc.

    def __init__(self, hotkeys=None):
        super().__init__()
        self._running = True
        self.listener = None
        self.hotkeys = hotkeys or DEFAULT_HOTKEYS.copy()

    def run(self):
        hotkeys = {
            hotkey: lambda current_action=action: self.hotkey_triggered.emit(current_action)
            for action, hotkey in self.hotkeys.items()
        }

        with keyboard.GlobalHotKeys(hotkeys) as self.listener:
            self.listener.join()

    def stop(self):
        if self.listener:
            self.listener.stop()
        self._running = False

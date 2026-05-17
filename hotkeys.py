from pynput import keyboard
from PySide6.QtCore import QThread, Signal

class GlobalHotkeyManager(QThread):
    """
    Listens for global system-wide hotkeys using pynput.
    Connects these hotkeys to application actions.
    """
    hotkey_triggered = Signal(str) # Key code like 'toggle_timer', 'stop_timer', etc.

    def __init__(self):
        super().__init__()
        self._running = True
        self.listener = None

    def run(self):
        # Define hotkey mappings
        hotkeys = {
            '<ctrl>+<alt>+s': lambda: self.hotkey_triggered.emit('toggle_timer'),
            '<ctrl>+<alt>+<space>': lambda: self.hotkey_triggered.emit('pause_timer'),
            '<ctrl>+<alt>+n': lambda: self.hotkey_triggered.emit('add_note'),
            '<ctrl>+<alt>+r': lambda: self.hotkey_triggered.emit('add_reminder'),
            '<ctrl>+<alt>+c': lambda: self.hotkey_triggered.emit('change_client'),
            '<ctrl>+<alt>+j': lambda: self.hotkey_triggered.emit('open_journal')
        }
        
        # Add quick client switches 1-9
        for i in range(1, 10):
            hotkeys[f'<ctrl>+<alt>+{i}'] = lambda x=i: self.hotkey_triggered.emit(f'switch_client_{x}')

        with keyboard.GlobalHotKeys(hotkeys) as self.listener:
            self.listener.join()

    def stop(self):
        if self.listener:
            self.listener.stop()
        self._running = False

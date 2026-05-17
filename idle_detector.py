import time
from PySide6.QtCore import QThread, Signal
from pynput import mouse, keyboard

class IdleDetector(QThread):
    """
    Tracks user inactivity using global mouse and keyboard listeners.
    """
    idle_started = Signal(int) # Seconds of idle
    activity_resumed = Signal()

    def __init__(self, threshold_seconds=300):
        super().__init__()
        self.threshold = threshold_seconds
        self.last_activity_time = time.time()
        self.is_idle = False
        self._running = True

    def on_activity(self, *args):
        now = time.time()
        if self.is_idle:
            self.activity_resumed.emit()
            self.is_idle = False
        self.last_activity_time = now

    def stop(self):
        self._running = False

    def run(self):
        # Create listeners
        mouse_listener = mouse.Listener(
            on_move=self.on_activity, 
            on_click=self.on_activity, 
            on_scroll=self.on_activity
        )
        key_listener = keyboard.Listener(on_press=self.on_activity)
        
        mouse_listener.start()
        key_listener.start()

        while self._running:
            current_idle = time.time() - self.last_activity_time
            if current_idle >= self.threshold and not self.is_idle:
                self.is_idle = True
                self.idle_started.emit(int(current_idle))
            
            time.sleep(1)
        
        mouse_listener.stop()
        key_listener.stop()

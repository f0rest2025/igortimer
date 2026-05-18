import sqlite3
import os
from datetime import datetime

from hotkey_definitions import DEFAULT_HOTKEYS, setting_key_for_action

class Database:
    def __init__(self, db_path="data/tracker.db"):
        self.db_path = db_path
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.create_tables()

    def create_tables(self):
        cursor = self.conn.cursor()
        
        # Clients table
        cursor.execute('''CREATE TABLE IF NOT EXISTS clients (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            comment TEXT,
            rate REAL DEFAULT 0,
            is_active INTEGER DEFAULT 1,
            last_used TIMESTAMP
        )''')
        
        # Time segments (work, pause, idle)
        cursor.execute('''CREATE TABLE IF NOT EXISTS time_segments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id INTEGER,
            task TEXT,
            start_at TIMESTAMP NOT NULL,
            end_at TIMESTAMP,
            status TEXT NOT NULL, -- 'work', 'pause', 'idle', 'disputed'
            note TEXT,
            FOREIGN KEY(client_id) REFERENCES clients(id)
        )''')
        
        # Settings
        cursor.execute('''CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )''')

        # Reminders table
        cursor.execute('''CREATE TABLE IF NOT EXISTS reminders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            text TEXT NOT NULL,
            remind_at TIMESTAMP NOT NULL,
            is_done INTEGER DEFAULT 0
        )''')
        
        # Initialize default settings
        default_settings = [
            ('opacity', '0.8'),
            ('idle_threshold', '300'), # 5 minutes
            ('always_on_top', '1'),
            ('rounding_mode', 'none'),
            ('theme', 'dark')
        ]
        default_settings.extend(
            (setting_key_for_action(action), hotkey)
            for action, hotkey in DEFAULT_HOTKEYS.items()
        )
        cursor.executemany("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", default_settings)
        
        self.conn.commit()

    # --- Client Methods ---
    def get_clients(self, active_only=True):
        query = "SELECT * FROM clients"
        if active_only:
            query += " WHERE is_active = 1"
        query += " ORDER BY last_used DESC, name ASC"
        return self.conn.execute(query).fetchall()

    def add_client(self, name, comment="", rate=0):
        cursor = self.conn.cursor()
        cursor.execute(
            "INSERT INTO clients (name, comment, rate, last_used) VALUES (?, ?, ?, ?)",
            (name, comment, rate, datetime.now().isoformat())
        )
        self.conn.commit()
        return cursor.lastrowid

    def update_client_usage(self, client_id):
        self.conn.execute(
            "UPDATE clients SET last_used = ? WHERE id = ?",
            (datetime.now().isoformat(), client_id)
        )
        self.conn.commit()

    # --- Segment Methods ---
    def start_segment(self, client_id, task, status="work"):
        now = datetime.now().isoformat()
        cursor = self.conn.cursor()
        cursor.execute(
            "INSERT INTO time_segments (client_id, task, start_at, status) VALUES (?, ?, ?, ?)",
            (client_id, task, now, status)
        )
        self.conn.commit()
        if client_id:
            self.update_client_usage(client_id)
        return cursor.lastrowid

    def end_segment(self, segment_id, note=None):
        now = datetime.now().isoformat()
        if note:
            self.conn.execute(
                "UPDATE time_segments SET end_at = ?, note = ? WHERE id = ?",
                (now, note, segment_id)
            )
        else:
            self.conn.execute(
                "UPDATE time_segments SET end_at = ? WHERE id = ?",
                (now, segment_id)
            )
        self.conn.commit()

    def get_segments(self, start_date=None, end_date=None):
        query = """
            SELECT t.*, c.name as client_name 
            FROM time_segments t 
            LEFT JOIN clients c ON t.client_id = c.id
        """
        params = []
        if start_date and end_date:
            query += " WHERE date(t.start_at) BETWEEN ? AND ?"
            params = [start_date, end_date]
        query += " ORDER BY t.start_at DESC"
        return self.conn.execute(query, params).fetchall()

    # --- Settings ---
    def get_setting(self, key, default=None):
        res = self.conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return res['value'] if res else default

    def set_setting(self, key, value):
        self.conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(value)))
        self.conn.commit()

    # --- Hotkeys ---
    def get_hotkeys(self):
        return {
            action: self.get_setting(setting_key_for_action(action), default)
            for action, default in DEFAULT_HOTKEYS.items()
        }

    def set_hotkey(self, action, hotkey):
        self.set_setting(setting_key_for_action(action), hotkey)

    def find_hotkey_conflict(self, hotkey, exclude_action=None):
        for action, assigned_hotkey in self.get_hotkeys().items():
            if action != exclude_action and assigned_hotkey == hotkey:
                return action
        return None

    def reset_hotkeys_to_default(self):
        for action, hotkey in DEFAULT_HOTKEYS.items():
            self.set_hotkey(action, hotkey)

    def get_open_segment(self):
        """Finds any segment that was started but not finished (e.g. after a crash)."""
        query = """
            SELECT t.*, c.name as client_name 
            FROM time_segments t 
            LEFT JOIN clients c ON t.client_id = c.id
            WHERE t.end_at IS NULL
            ORDER BY t.start_at DESC LIMIT 1
        """
        return self.conn.execute(query).fetchone()

    # --- Reminder Methods ---
    def add_reminder(self, text, remind_at):
        self.conn.execute(
            "INSERT INTO reminders (text, remind_at) VALUES (?, ?)",
            (text, remind_at)
        )
        self.conn.commit()

    def get_pending_reminders(self):
        now = datetime.now().isoformat()
        return self.conn.execute(
            "SELECT * FROM reminders WHERE is_done = 0 AND remind_at <= ? ORDER BY remind_at ASC",
            (now,)
        ).fetchall()

    def mark_reminder_done(self, reminder_id):
        self.conn.execute("UPDATE reminders SET is_done = 1 WHERE id = ?", (reminder_id,))
        self.conn.commit()

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

        # Recordings of screen sessions
        cursor.execute('''CREATE TABLE IF NOT EXISTS recordings (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            client_name TEXT NOT NULL,
            date_time   DATETIME NOT NULL,
            file_path   TEXT NOT NULL,
            duration    INTEGER DEFAULT 0,
            file_size   INTEGER DEFAULT 0,
            segment_id  INTEGER
        )''')
        # Add segment_id to existing DBs that were created before v0.2.1
        try:
            cursor.execute("ALTER TABLE recordings ADD COLUMN segment_id INTEGER")
            self.conn.commit()
        except Exception:
            pass  # Column already exists
        
        # Initialize default settings
        default_settings = [
            ('opacity', '0.8'),
            ('app_color', '#4ade80'),
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

    # --- Recording Methods ---
    def add_recording(self, client_name: str, file_path: str,
                      duration: int = 0, file_size: int = 0,
                      segment_id: int = None) -> int:
        """Save a completed screen recording to the database."""
        now = datetime.now().isoformat()
        cursor = self.conn.cursor()
        cursor.execute(
            "INSERT INTO recordings (client_name, date_time, file_path, duration, file_size, segment_id) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (client_name, now, file_path, duration, file_size, segment_id)
        )
        self.conn.commit()
        return cursor.lastrowid

    def get_recordings(self, limit: int = 500) -> list:
        """Return all recordings ordered newest-first."""
        return self.conn.execute(
            "SELECT * FROM recordings ORDER BY date_time DESC LIMIT ?",
            (limit,)
        ).fetchall()

    def search_recordings(self, query: str) -> list:
        """Search recordings by client name (case-insensitive substring)."""
        return self.conn.execute(
            "SELECT * FROM recordings WHERE LOWER(client_name) LIKE LOWER(?) ORDER BY date_time DESC",
            (f"%{query}%",)
        ).fetchall()

    def delete_recording(self, recording_id: int):
        """Remove a recording entry from the database (does NOT delete the file)."""
        self.conn.execute("DELETE FROM recordings WHERE id = ?", (recording_id,))
        self.conn.commit()

    def get_recordings_for_segment(self, segment_id: int) -> list:
        """Return recordings that are linked to a specific time segment."""
        return self.conn.execute(
            "SELECT * FROM recordings WHERE segment_id = ? ORDER BY date_time ASC",
            (segment_id,)
        ).fetchall()

    def get_recordings_for_client_on_date(self, client_name: str, date_str: str) -> list:
        """Return recordings for a client on a specific date (YYYY-MM-DD)."""
        return self.conn.execute(
            "SELECT * FROM recordings WHERE client_name = ? AND date(date_time) = ? ORDER BY date_time ASC",
            (client_name, date_str)
        ).fetchall()

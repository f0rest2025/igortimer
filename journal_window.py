import os
import subprocess
from datetime import datetime

from PySide6.QtCore import Qt, QDate
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QApplication, QDateEdit, QFileDialog, QHBoxLayout, QHeaderView,
    QLabel, QMainWindow, QMessageBox, QPushButton, QTreeWidget,
    QTreeWidgetItem, QVBoxLayout, QWidget,
)

from assets import app_icon
from export_excel import export_to_excel


class JournalWindow(QMainWindow):
    """
    Main data exploration window. Shows all time segments grouped by client.
    Column 7 (REC) shows a ▶ play button for any recording linked to the segment.
    """

    def __init__(self, db):
        super().__init__()
        self.db = db
        self.setWindowTitle("Журнал времени")
        self.setWindowIcon(app_icon())
        self.resize(1100, 720)
        self.init_ui()
        self.load_data()

    # ─────────────────────────────────────────────────────────────────
    # UI construction
    # ─────────────────────────────────────────────────────────────────

    def init_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        # ── Toolbar ──────────────────────────────────────────────────
        toolbar = QHBoxLayout()
        toolbar.addWidget(QLabel("Период с:"))
        self.date_from = QDateEdit(QDate.currentDate().addDays(-7))
        self.date_from.setCalendarPopup(True)
        toolbar.addWidget(self.date_from)

        toolbar.addWidget(QLabel("по:"))
        self.date_to = QDateEdit(QDate.currentDate())
        self.date_to.setCalendarPopup(True)
        toolbar.addWidget(self.date_to)

        btn_refresh = QPushButton("Обновить")
        btn_refresh.clicked.connect(self.load_data)
        toolbar.addWidget(btn_refresh)

        toolbar.addStretch()

        btn_export = QPushButton("Экспорт в Excel")
        btn_export.clicked.connect(self.export_data)
        toolbar.addWidget(btn_export)

        root.addLayout(toolbar)

        # ── Tree ─────────────────────────────────────────────────────
        self.tree = QTreeWidget()
        self.tree.setColumnCount(8)
        self.tree.setHeaderLabels([
            "Клиент / Дата", "Задача", "Начало", "Конец",
            "Длительность", "Статус", "Заметка", "🎬 Запись",
        ])
        hdr = self.tree.header()
        hdr.setSectionResizeMode(QHeaderView.Interactive)
        hdr.setStretchLastSection(False)
        hdr.setSectionResizeMode(6, QHeaderView.Stretch)   # Заметка растягивается
        hdr.resizeSection(7, 90)                           # REC колонка фиксированная

        self.tree.setStyleSheet("""
            QTreeWidget {
                background-color: #1e1e1e;
                color: #e0e0e0;
            }
            QHeaderView::section {
                background-color: #2d2d2d;
                color: white;
                padding: 4px;
                border: 1px solid #444;
            }
            QPushButton#recBtn {
                background: transparent;
                color: #4ade80;
                border: 1px solid #4ade80;
                border-radius: 3px;
                padding: 1px 6px;
                font-size: 12px;
            }
            QPushButton#recBtn:hover {
                background: rgba(74,222,128,0.15);
            }
        """)

        root.addWidget(self.tree)

    # ─────────────────────────────────────────────────────────────────
    # Data loading
    # ─────────────────────────────────────────────────────────────────

    def load_data(self):
        start_date = self.date_from.date().toString("yyyy-MM-dd")
        end_date   = self.date_to.date().toString("yyyy-MM-dd")
        segments   = self.db.get_segments(start_date, end_date)

        self.tree.clear()

        # Group by client
        client_groups: dict[str, list] = {}
        for seg in segments:
            key = seg["client_name"] or "Без клиента"
            client_groups.setdefault(key, []).append(seg)

        for client_name, group in client_groups.items():
            # ── Total for client ──────────────────────────────────────
            total_sec = 0
            for seg in group:
                s = datetime.fromisoformat(seg["start_at"])
                e = datetime.fromisoformat(seg["end_at"]) if seg["end_at"] else datetime.now()
                total_sec += int((e - s).total_seconds())

            h, rem = divmod(total_sec, 3600)
            m, s   = divmod(rem, 60)

            parent = QTreeWidgetItem(self.tree)
            parent.setText(0, client_name)
            parent.setText(4, f"{h:02}:{m:02}:{s:02} (Всего)")
            parent.setData(0, Qt.UserRole, client_name)

            bold = QFont()
            bold.setBold(True)
            for col in range(8):
                parent.setFont(col, bold)
                parent.setBackground(col, Qt.darkGray)

            # ── Children ──────────────────────────────────────────────
            for seg in group:
                s_dt = datetime.fromisoformat(seg["start_at"])
                e_dt = datetime.fromisoformat(seg["end_at"]) if seg["end_at"] else datetime.now()
                dur  = int((e_dt - s_dt).total_seconds())
                dh, dr = divmod(dur, 3600)
                dm, ds = divmod(dr, 60)

                child = QTreeWidgetItem(parent)
                child.setText(0, s_dt.strftime("%d.%m.%Y"))
                child.setText(1, seg["task"])
                child.setText(2, s_dt.strftime("%H:%M:%S"))
                child.setText(3, e_dt.strftime("%H:%M:%S") if seg["end_at"] else "…")
                child.setText(4, f"{dh:02}:{dm:02}:{ds:02}")
                child.setText(5, seg["status"])
                child.setText(6, seg["note"] or "")

                # ── REC button ────────────────────────────────────────
                seg_id = seg["id"]
                recs = self.db.get_recordings_for_segment(seg_id)

                # Fallback: search by client + date if no direct link yet
                if not recs:
                    date_str = s_dt.strftime("%Y-%m-%d")
                    recs = self.db.get_recordings_for_client_on_date(
                        seg["client_name"] or "", date_str
                    )

                if recs:
                    rec = recs[0]  # show button for the first/only recording
                    file_path = rec["file_path"]

                    btn = QPushButton(f"▶ {_fmt_dur(rec['duration'])}")
                    btn.setObjectName("recBtn")
                    btn.setToolTip(f"Открыть запись:\n{file_path}")
                    btn.clicked.connect(
                        lambda _checked=False, fp=file_path: self._open_video(fp)
                    )
                    self.tree.setItemWidget(child, 7, btn)
                else:
                    child.setText(7, "—")

        self.tree.expandAll()
        for i in range(7):
            self.tree.resizeColumnToContents(i)

    # ─────────────────────────────────────────────────────────────────
    # Actions
    # ─────────────────────────────────────────────────────────────────

    def _open_video(self, file_path: str):
        """Open the video file with the system default player."""
        if not os.path.exists(file_path):
            QMessageBox.warning(
                self, "Файл не найден",
                f"Видеофайл не найден:\n{file_path}\n\n"
                "Возможно, он был перемещён или удалён."
            )
            return
        try:
            os.startfile(file_path)          # Windows: opens with default player
        except Exception as e:
            # Fallback
            subprocess.Popen(["explorer", "/select,", file_path])

    def export_data(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Сохранить отчет", "report.xlsx", "Excel Files (*.xlsx)"
        )
        if path:
            try:
                export_to_excel(self.db, path)
                QMessageBox.information(self, "Успех", f"Отчет сохранен в {path}")
                os.startfile(os.path.dirname(path))
            except Exception as e:
                QMessageBox.critical(self, "Ошибка", f"Не удалось сохранить: {e}")


# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────

def _fmt_dur(seconds: int) -> str:
    """Format seconds as MM:SS or HH:MM."""
    if seconds <= 0:
        return ""
    h, r = divmod(seconds, 3600)
    m, s = divmod(r, 60)
    if h:
        return f"{h}:{m:02}"
    return f"{m}:{s:02}"

from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QTreeWidget, 
    QTreeWidgetItem, QHeaderView, QPushButton, QHBoxLayout,
    QDateEdit, QLabel, QFileDialog, QMessageBox
)
from PySide6.QtCore import Qt, QDate
from export_excel import export_to_excel
import os
from datetime import datetime

from assets import app_icon

class JournalWindow(QMainWindow):
    """
    Main data exploration window. Shows all time segments grouped by client.
    """
    def __init__(self, db):
        super().__init__()
        self.db = db
        self.setWindowTitle("Журнал времени (Сгруппированный)")
        self.setWindowIcon(app_icon())
        self.resize(1000, 700)
        
        self.init_ui()
        self.load_data()

    def init_ui(self):
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        layout = QVBoxLayout(central_widget)

        # Filters and Toolbar
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

        layout.addLayout(toolbar)

        # Tree Widget for Grouping
        self.tree = QTreeWidget()
        self.tree.setColumnCount(7)
        self.tree.setHeaderLabels([
            "Клиент / Дата", "Задача", "Начало", "Конец", "Длительность", "Статус", "Заметка"
        ])
        self.tree.header().setSectionResizeMode(QHeaderView.Interactive)
        self.tree.header().setStretchLastSection(True)
        self.tree.setStyleSheet("""
            QTreeWidget { background-color: #1e1e1e; color: #e0e0e0; }
            QHeaderView::section { background-color: #2d2d2d; color: white; padding: 4px; border: 1px solid #444; }
        """)
        
        layout.addWidget(self.tree)

    def load_data(self):
        start_date = self.date_from.date().toString("yyyy-MM-dd")
        end_date = self.date_to.date().toString("yyyy-MM-dd")
        
        segments = self.db.get_segments(start_date, end_date)
        
        self.tree.clear()
        
        # Grouping logic
        client_groups = {}
        for seg in segments:
            c_name = seg['client_name'] or "Без клиента"
            if c_name not in client_groups:
                client_groups[c_name] = []
            client_groups[c_name].append(seg)

        for client_name, group in client_groups.items():
            # Calculate total duration for client
            total_seconds = 0
            for seg in group:
                s_dt = datetime.fromisoformat(seg['start_at'])
                e_dt = datetime.fromisoformat(seg['end_at']) if seg['end_at'] else datetime.now()
                total_seconds += int((e_dt - s_dt).total_seconds())
            
            h = total_seconds // 3600
            m = (total_seconds % 3600) // 60
            s = total_seconds % 60
            
            # Create Parent Item
            parent = QTreeWidgetItem(self.tree)
            parent.setText(0, client_name)
            parent.setText(4, f"{h:02}:{m:02}:{s:02} (Всего)")
            parent.setData(0, Qt.UserRole, client_name)
            
            # Bold fonts for parents
            font = parent.font(0)
            font.setBold(True)
            for col in range(7):
                parent.setFont(col, font)
                parent.setBackground(col, Qt.darkGray)

            # Add children
            for seg in group:
                s_dt = datetime.fromisoformat(seg['start_at'])
                e_dt = datetime.fromisoformat(seg['end_at']) if seg['end_at'] else datetime.now()
                duration = int((e_dt - s_dt).total_seconds())
                sh = duration // 3600
                sm = (duration % 3600) // 60
                ss = duration % 60
                
                child = QTreeWidgetItem(parent)
                child.setText(0, s_dt.strftime("%d.%m.%Y"))
                child.setText(1, seg['task'])
                child.setText(2, s_dt.strftime("%H:%M:%S"))
                child.setText(3, e_dt.strftime("%H:%M:%S") if seg['end_at'] else "...")
                child.setText(4, f"{sh:02}:{sm:02}:{ss:02}")
                child.setText(5, seg['status'])
                child.setText(6, seg['note'] or "")

        self.tree.expandAll()
        for i in range(7):
            self.tree.resizeColumnToContents(i)

    def export_data(self):
        path, _ = QFileDialog.getSaveFileName(self, "Сохранить отчет", "report.xlsx", "Excel Files (*.xlsx)")
        if path:
            try:
                export_to_excel(self.db, path)
                QMessageBox.information(self, "Успех", f"Отчет сохранен в {path}")
                os.startfile(os.path.dirname(path))
            except Exception as e:
                QMessageBox.critical(self, "Ошибка", f"Не удалось сохранить файл: {str(e)}")

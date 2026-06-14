"""
app.py — Streamlit Web Interface for Screen Recordings
=======================================================
Run with:  streamlit run app.py

Features:
  - Search recordings by client name
  - Table view sorted newest-first
  - Inline HTML5 video player (click any row to watch)
  - Daily/weekly statistics
  - Delete individual records from DB
"""

import os
import sys
import sqlite3
import base64
from datetime import datetime, timedelta
from pathlib import Path

import streamlit as st

# ──────────────────────────────────────────────────────────────────────────────
# Config
# ──────────────────────────────────────────────────────────────────────────────

BASE_DIR = Path(__file__).parent
DB_PATH  = BASE_DIR / "data" / "tracker.db"

st.set_page_config(
    page_title="IgorTimer — Записи экрана",
    page_icon="🎬",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ──────────────────────────────────────────────────────────────────────────────
# Custom CSS
# ──────────────────────────────────────────────────────────────────────────────

st.markdown("""
<style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap');

    html, body, [class*="css"] {
        font-family: 'Inter', sans-serif;
    }

    /* Dark background */
    .stApp {
        background: linear-gradient(135deg, #0d0d14 0%, #12121e 60%, #0a0a12 100%);
    }

    /* Sidebar */
    [data-testid="stSidebar"] {
        background-color: #111118;
        border-right: 1px solid #222235;
    }

    /* Main title area */
    .title-bar {
        display: flex;
        align-items: center;
        gap: 14px;
        margin-bottom: 24px;
        padding-bottom: 16px;
        border-bottom: 1px solid #222235;
    }
    .title-bar h1 {
        margin: 0;
        font-size: 26px;
        font-weight: 700;
        background: linear-gradient(90deg, #ff4444 0%, #ff8080 100%);
        -webkit-background-clip: text;
        -webkit-text-fill-color: transparent;
        background-clip: text;
    }
    .rec-dot {
        width: 12px; height: 12px;
        border-radius: 50%;
        background: #ff4444;
        animation: blink 1.2s ease-in-out infinite;
        flex-shrink: 0;
    }
    @keyframes blink {
        0%, 100% { opacity: 1; }
        50% { opacity: 0.2; }
    }

    /* Stat cards */
    .stat-card {
        background: rgba(255,255,255,0.04);
        border: 1px solid rgba(255,255,255,0.07);
        border-radius: 12px;
        padding: 16px 20px;
        text-align: center;
    }
    .stat-card .value {
        font-size: 28px;
        font-weight: 700;
        color: #ff6060;
        line-height: 1.1;
    }
    .stat-card .label {
        font-size: 11px;
        color: #666680;
        margin-top: 4px;
        text-transform: uppercase;
        letter-spacing: 0.05em;
    }

    /* Recording table rows */
    .rec-row {
        display: flex;
        align-items: center;
        gap: 10px;
        padding: 11px 14px;
        margin-bottom: 6px;
        background: rgba(255,255,255,0.03);
        border: 1px solid rgba(255,255,255,0.06);
        border-radius: 10px;
        cursor: pointer;
        transition: background 0.15s, border-color 0.15s;
    }
    .rec-row:hover {
        background: rgba(255, 68, 68, 0.08);
        border-color: rgba(255, 68, 68, 0.25);
    }
    .rec-row .client {
        flex: 1;
        font-weight: 600;
        font-size: 13px;
        color: #e0e0f0;
    }
    .rec-row .date {
        font-size: 11px;
        color: #5a5a7a;
        white-space: nowrap;
    }
    .rec-row .duration {
        font-size: 11px;
        color: #7a7a9a;
        white-space: nowrap;
    }
    .rec-row .size-badge {
        font-size: 10px;
        background: rgba(255,255,255,0.07);
        color: #8888aa;
        padding: 2px 7px;
        border-radius: 4px;
        white-space: nowrap;
    }

    /* Video player container */
    .video-wrapper {
        border-radius: 12px;
        overflow: hidden;
        border: 1px solid #2a2a40;
        background: #000;
    }

    /* Streamlit native overrides */
    div[data-testid="stTextInput"] input {
        background: rgba(255,255,255,0.05) !important;
        border: 1px solid #2a2a40 !important;
        border-radius: 8px !important;
        color: #e0e0f0 !important;
    }
    div[data-testid="stTextInput"] input:focus {
        border-color: #ff4444 !important;
        box-shadow: 0 0 0 3px rgba(255,68,68,0.15) !important;
    }

    button[data-testid="baseButton-secondary"] {
        border-radius: 8px !important;
    }

    .stButton > button {
        border-radius: 8px !important;
        font-family: 'Inter', sans-serif !important;
    }

    /* Hide default Streamlit footer & menu */
    #MainMenu, footer { visibility: hidden; }
</style>
""", unsafe_allow_html=True)


# ──────────────────────────────────────────────────────────────────────────────
# Database helpers
# ──────────────────────────────────────────────────────────────────────────────

@st.cache_resource
def get_connection():
    if not DB_PATH.exists():
        st.error(f"База данных не найдена: `{DB_PATH}`\nЗапустите `python main.py` хотя бы один раз.")
        st.stop()
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def fetch_recordings(search: str = "") -> list[sqlite3.Row]:
    conn = get_connection()
    if search.strip():
        rows = conn.execute(
            "SELECT * FROM recordings WHERE LOWER(client_name) LIKE LOWER(?) ORDER BY date_time DESC",
            (f"%{search.strip()}%",)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM recordings ORDER BY date_time DESC"
        ).fetchall()
    return rows


def delete_recording(rec_id: int):
    conn = get_connection()
    conn.execute("DELETE FROM recordings WHERE id = ?", (rec_id,))
    conn.commit()
    st.cache_data.clear()


def get_stats() -> dict:
    conn = get_connection()
    today = datetime.now().date().isoformat()
    week_start = (datetime.now() - timedelta(days=7)).date().isoformat()

    total = conn.execute("SELECT COUNT(*), COALESCE(SUM(duration),0), COALESCE(SUM(file_size),0) FROM recordings").fetchone()
    today_r = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(duration),0) FROM recordings WHERE date(date_time) = ?", (today,)
    ).fetchone()
    week_r = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(duration),0) FROM recordings WHERE date(date_time) >= ?", (week_start,)
    ).fetchone()

    return {
        "total_count":    total[0],
        "total_duration": total[1],
        "total_size":     total[2],
        "today_count":    today_r[0],
        "today_duration": today_r[1],
        "week_count":     week_r[0],
        "week_duration":  week_r[1],
    }


# ──────────────────────────────────────────────────────────────────────────────
# Formatting helpers
# ──────────────────────────────────────────────────────────────────────────────

def fmt_duration(seconds: int) -> str:
    if seconds <= 0:
        return "—"
    h, rem = divmod(int(seconds), 3600)
    m, s   = divmod(rem, 60)
    if h:
        return f"{h}ч {m:02}м"
    if m:
        return f"{m}м {s:02}с"
    return f"{s}с"


def fmt_size(size_bytes: int) -> str:
    if size_bytes <= 0:
        return "—"
    if size_bytes < 1024 ** 2:
        return f"{size_bytes / 1024:.1f} KB"
    if size_bytes < 1024 ** 3:
        return f"{size_bytes / 1024 ** 2:.1f} MB"
    return f"{size_bytes / 1024 ** 3:.2f} GB"


def fmt_datetime(dt_str: str) -> str:
    try:
        dt = datetime.fromisoformat(dt_str)
        return dt.strftime("%d.%m.%Y  %H:%M")
    except Exception:
        return dt_str


def video_player_html(file_path: str) -> str:
    """Return an HTML5 video tag with base64-encoded video for inline playback."""
    path = Path(file_path)
    if not path.exists():
        return f"<p style='color:#ff6060'>Файл не найден:<br><code>{file_path}</code></p>"

    # For large files stream via file:// is better than base64
    # Streamlit's st.video() works great for local files
    return None  # Signal to use st.video()


# ──────────────────────────────────────────────────────────────────────────────
# Sidebar
# ──────────────────────────────────────────────────────────────────────────────

with st.sidebar:
    st.markdown("### 🎬 IgorTimer")
    st.markdown("**Записи экрана**")
    st.divider()

    search_query = st.text_input(
        "🔍 Поиск по клиенту",
        placeholder="Имя клиента или тикет…",
        key="search"
    )

    st.divider()

    # Date range filter
    st.markdown("**Фильтр по дате**")
    today = datetime.now().date()
    date_from = st.date_input("С", value=today - timedelta(days=30), key="dfrom")
    date_to   = st.date_input("По", value=today, key="dto")

    st.divider()

    if st.button("🔄 Обновить", use_container_width=True):
        st.cache_data.clear()
        st.rerun()

    st.divider()
    st.caption("Горячие клавиши:")
    st.caption("⏺ Запись:  **Ctrl+Alt+W**")
    st.caption("🔍 Поиск:  **Ctrl+Alt+F**")


# ──────────────────────────────────────────────────────────────────────────────
# Main content
# ──────────────────────────────────────────────────────────────────────────────

st.markdown("""
<div class="title-bar">
  <div class="rec-dot"></div>
  <h1>Записи экрана</h1>
</div>
""", unsafe_allow_html=True)

# ---- Stats row ----
stats = get_stats()
col1, col2, col3, col4 = st.columns(4)

with col1:
    st.markdown(f"""
    <div class="stat-card">
        <div class="value">{stats['total_count']}</div>
        <div class="label">Всего записей</div>
    </div>""", unsafe_allow_html=True)

with col2:
    st.markdown(f"""
    <div class="stat-card">
        <div class="value">{fmt_duration(stats['total_duration'])}</div>
        <div class="label">Общая длительность</div>
    </div>""", unsafe_allow_html=True)

with col3:
    st.markdown(f"""
    <div class="stat-card">
        <div class="value">{stats['today_count']}</div>
        <div class="label">Сегодня записей</div>
    </div>""", unsafe_allow_html=True)

with col4:
    st.markdown(f"""
    <div class="stat-card">
        <div class="value">{fmt_size(stats['total_size'])}</div>
        <div class="label">Занято на диске</div>
    </div>""", unsafe_allow_html=True)

st.markdown("<br>", unsafe_allow_html=True)

# ---- Load recordings ----
recordings = fetch_recordings(search_query)

# Apply date filter
filtered = []
for r in recordings:
    try:
        rec_date = datetime.fromisoformat(r['date_time']).date()
        if date_from <= rec_date <= date_to:
            filtered.append(r)
    except Exception:
        filtered.append(r)

recordings = filtered

# ── Two-column layout: list + player ──────────────────────────────────────────
list_col, player_col = st.columns([1, 1.3], gap="large")

with list_col:
    st.markdown(f"#### Найдено: {len(recordings)}")

    if not recordings:
        if search_query:
            st.info(f'Нет записей по запросу «{search_query}»')
        else:
            st.info("Записей пока нет. Нажмите **Ctrl+Alt+W** чтобы начать запись.")
    else:
        # Session state for selected recording
        if "selected_rec_id" not in st.session_state:
            st.session_state.selected_rec_id = None

        for rec in recordings:
            rec_id   = rec['id']
            is_sel   = st.session_state.selected_rec_id == rec_id

            with st.container():
                cols = st.columns([3, 2, 1, 0.7])
                with cols[0]:
                    label = f"**{rec['client_name']}**"
                    if is_sel:
                        label = f"▶ {label}"
                    if st.button(label, key=f"rec_{rec_id}", use_container_width=True):
                        st.session_state.selected_rec_id = rec_id
                        st.rerun()
                with cols[1]:
                    st.caption(fmt_datetime(rec['date_time']))
                with cols[2]:
                    st.caption(fmt_duration(rec['duration']))
                with cols[3]:
                    st.caption(fmt_size(rec['file_size']))

        # Delete button for selected
        if st.session_state.selected_rec_id:
            st.divider()
            if st.button("🗑 Удалить запись из БД", type="secondary", use_container_width=True):
                delete_recording(st.session_state.selected_rec_id)
                st.session_state.selected_rec_id = None
                st.rerun()

with player_col:
    sel_id = st.session_state.get("selected_rec_id")
    if sel_id is None:
        st.markdown("""
        <div style="
            height: 280px;
            display: flex; align-items: center; justify-content: center;
            border: 1px dashed #2a2a40; border-radius: 12px;
            color: #3a3a5a; font-size: 14px; flex-direction: column; gap: 8px;
        ">
            <span style="font-size:36px">▶</span>
            Выберите запись слева для просмотра
        </div>
        """, unsafe_allow_html=True)
    else:
        sel_rec = next((r for r in recordings if r['id'] == sel_id), None)
        if sel_rec is None:
            # Maybe filtered out — find directly
            conn = get_connection()
            sel_rec = conn.execute("SELECT * FROM recordings WHERE id=?", (sel_id,)).fetchone()

        if sel_rec:
            fp = sel_rec['file_path']
            st.markdown(f"##### {sel_rec['client_name']}")
            st.caption(
                f"📅 {fmt_datetime(sel_rec['date_time'])}  |  "
                f"⏱ {fmt_duration(sel_rec['duration'])}  |  "
                f"💾 {fmt_size(sel_rec['file_size'])}"
            )
            st.caption(f"`{fp}`")

            if Path(fp).exists():
                with open(fp, "rb") as f:
                    video_bytes = f.read()
                st.video(video_bytes, format="video/mp4")
            else:
                st.error(f"❌ Файл не найден:\n`{fp}`\n\nВозможно, он был перемещён или удалён.")

            # Open in system player button
            if Path(fp).exists():
                st.markdown(f"""
                <a href="file:///{fp.replace(os.sep, '/')}" target="_blank"
                   style="display:inline-block; margin-top:8px; padding:7px 14px;
                          background:rgba(255,255,255,0.06); color:#8888cc;
                          border:1px solid #2a2a40; border-radius:8px;
                          text-decoration:none; font-size:12px;">
                   🖥 Открыть в системном плеере
                </a>
                """, unsafe_allow_html=True)


# ──────────────────────────────────────────────────────────────────────────────
# Bottom: Full table view (collapsible)
# ──────────────────────────────────────────────────────────────────────────────
with st.expander("📋 Полная таблица всех записей", expanded=False):
    if recordings:
        import pandas as pd
        data = [
            {
                "ID":       r['id'],
                "Клиент":   r['client_name'],
                "Дата/время": fmt_datetime(r['date_time']),
                "Длительность": fmt_duration(r['duration']),
                "Размер":   fmt_size(r['file_size']),
                "Файл":     r['file_path'],
            }
            for r in recordings
        ]
        df = pd.DataFrame(data)
        st.dataframe(df, use_container_width=True, hide_index=True)
    else:
        st.info("Нет записей.")

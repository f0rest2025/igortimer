HOTKEY_ACTIONS = [
    ("toggle_timer", "Старт / стоп таймера"),
    ("pause_timer", "Пауза / продолжить таймер"),
    ("add_note", "Добавить заметку"),
    ("add_reminder", "Добавить напоминание"),
    ("change_client", "Сменить клиента"),
    ("open_journal", "Открыть журнал"),
    # --- Запись экрана ---
    ("toggle_recording", "Запись: Старт новой сессии"),
    ("pause_recording",  "Запись: Пауза / продолжить"),
    ("stop_recording",   "Запись: Остановить и сохранить"),
    ("open_recordings",  "Запись: Открыть поиск"),
]

HOTKEY_ACTIONS.extend(
    (f"switch_client_{index}", f"Быстрый клиент {index}")
    for index in range(1, 10)
)

DEFAULT_HOTKEYS = {
    "toggle_timer":    "<ctrl>+<alt>+s",
    "pause_timer":     "<ctrl>+<alt>+<space>",
    "add_note":        "<ctrl>+<alt>+n",
    "add_reminder":    "<ctrl>+<alt>+r",
    "change_client":   "<ctrl>+<alt>+c",
    "open_journal":    "<ctrl>+<alt>+j",
    # Recording
    "toggle_recording": "<ctrl>+<alt>+w",
    "pause_recording":  "<ctrl>+<alt>+p",
    "stop_recording":   "<ctrl>+<alt>+e",
    "open_recordings":  "<ctrl>+<alt>+f",
    **{
        f"switch_client_{index}": f"<ctrl>+<alt>+{index}"
        for index in range(1, 10)
    },
}

HOTKEY_SETTING_PREFIX = "hotkey_"


def setting_key_for_action(action):
    return f"{HOTKEY_SETTING_PREFIX}{action}"


def format_hotkey_for_display(hotkey):
    parts = hotkey.split("+")
    display_parts = []

    replacements = {
        "<ctrl>": "Ctrl",
        "<alt>": "Alt",
        "<shift>": "Shift",
        "<cmd>": "Meta",
        "<space>": "Space",
        "<enter>": "Enter",
        "<tab>": "Tab",
        "<esc>": "Esc",
        "<backspace>": "Backspace",
        "<delete>": "Delete",
        "<home>": "Home",
        "<end>": "End",
        "<page_up>": "Page Up",
        "<page_down>": "Page Down",
        "<left>": "Left",
        "<right>": "Right",
        "<up>": "Up",
        "<down>": "Down",
    }

    for part in parts:
        display_parts.append(replacements.get(part, part.upper() if len(part) == 1 else part))

    return "+".join(display_parts)

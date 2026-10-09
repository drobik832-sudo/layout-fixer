#!/usr/bin/env python3
"""Сквозной тест автозамены: запущенный демон + реальное приложение.

Как проверяем:

1. запускается gnome-terminal, внутри которого ``cat > FILE`` — всё, что в него
   напечатают, оказывается в файле;
2. текст печатается инъекцией с **другим именем устройства**: демон принимает
   такие нажатия за настоящую клавиатуру (свою виртуальную он игнорирует);
3. демон видит в буфере слово не в той раскладке, стирает его Backspace'ом,
   переключает раскладку и печатает заново;
4. читаем файл — там должно оказаться исправленное слово.

Требуется запущенный демон в режиме ``fix``.

Запуск:  .venv/bin/python scripts/e2e_fix_test.py
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from evdev import ecodes as e  # noqa: E402

from layout_fixer.config import Config  # noqa: E402
from layout_fixer.injector import Injector  # noqa: E402
from layout_fixer.layout_state import LayoutState, configured_actuator, get_actuator  # noqa: E402
from layout_fixer.xkb import LayoutTables  # noqa: E402

RESULT_FILE = Path("/tmp/layout-fixer-e2e.txt")
TEST_DEVICE_NAME = "E2E test keyboard (layout-fixer)"
FOCUS_TIMEOUT = 12.0
DEVICE_DISCOVERY_WAIT = 3.5

WRONG = "ghbdtn"  # набрано в английской раскладке
RIGHT = "привет"  # что должно оказаться в файле


def session_env() -> dict[str, str]:
    """Чистое окружение рабочей сессии (у snap-терминала VS Code оно испорчено)."""
    try:
        pid = subprocess.check_output(["pgrep", "-x", "gsd-media-keys"], text=True).split()[0]
    except (subprocess.CalledProcessError, IndexError):
        print("НЕ НАЙДЕН gsd-media-keys — не могу получить окружение сессии")
        raise SystemExit(2)
    raw = Path(f"/proc/{pid}/environ").read_bytes()
    return {
        key.decode(): value.decode("utf-8", "replace")
        for key, _, value in (item.partition(b"=") for item in raw.split(b"\0"))
        if key
    }


def terminal_focused() -> bool:
    """Активно ли окно gnome-terminal (иначе инъекция уйдёт в чужое окно)."""
    try:
        import gi

        gi.require_version("Atspi", "2.0")
        from gi.repository import Atspi
    except Exception:
        return False
    desktop = Atspi.get_desktop(0)
    for i in range(desktop.get_child_count()):
        app = desktop.get_child_at_index(i)
        if app is None:
            continue
        try:
            if "terminal" not in (app.get_name() or "").lower():
                continue
            for j in range(app.get_child_count()):
                window = app.get_child_at_index(j)
                if window is not None and window.get_state_set().contains(Atspi.StateType.ACTIVE):
                    return True
        except Exception:
            continue
    return False


def main() -> int:
    config = Config.load()
    if config.mode != "fix":
        print(f"Демон в режиме {config.mode!r} — тест бессмысленен.")
        print("Включить автозамену:  layout-fixer set-mode fix")
        return 2
    print(f"режим демона: {config.mode!r}, актуатор: {config.actuator}")

    RESULT_FILE.unlink(missing_ok=True)
    tables = LayoutTables(["us", "ru"])
    actuator = configured_actuator() or get_actuator(config.actuator)

    # 1. Система должна быть в английской раскладке: тогда инжектированные
    #    физические клавиши слова дают в приложении латинскую «абракадабру».
    with Injector(tables=tables) as system_injector:
        state = LayoutState(tables.layouts, actuator, injector=system_injector, settle=0.25)
        print(f"1. текущая раскладка: {state.refresh()!r}", end="")
        if state.ensure("us"):
            print(f" -> переключил на {state.current!r}")
        else:
            print(" (уже us)")
    time.sleep(0.5)

    # 2. Приёмник текста: терминал, который пишет весь ввод в файл.
    print(f"2. запускаю терминал-приёмник (cat > {RESULT_FILE})")
    terminal = subprocess.Popen(
        ["/usr/bin/gnome-terminal", "--window", "--", "/bin/bash", "-c", f"cat > {RESULT_FILE}"],
        env=session_env(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + FOCUS_TIMEOUT
    while time.monotonic() < deadline:
        if terminal_focused():
            break
        time.sleep(0.4)
    else:
        print("   ОТМЕНА: окно терминала не получило фокус — не рискую печатать в чужое окно")
        terminal.terminate()
        return 2
    print("   окно терминала в фокусе")
    time.sleep(0.8)

    # 3. Печатаем «не то» слово с устройства, которое демон считает настоящей
    #    клавиатурой. Дальше демон должен сработать сам.
    print(f"3. печатаю {WRONG!r} + пробел (демон должен исправить на {RIGHT!r})")
    with Injector(name=TEST_DEVICE_NAME, tables=tables, key_delay=0.01) as test_keyboard:
        print(f"   жду {DEVICE_DISCOVERY_WAIT} с, чтобы демон заметил новое устройство...")
        time.sleep(DEVICE_DISCOVERY_WAIT)
        # Изоляция от живого набора: в буфере демона могут остаться символы,
        # которые пользователь набирал до теста. Одиночный Ctrl буфер очищает,
        # так что тест не склеится с чужой буквой.
        test_keyboard.tap(e.KEY_LEFTCTRL)
        time.sleep(0.3)
        test_keyboard.type_text(WRONG, "us")
        test_keyboard.tap(e.KEY_SPACE)
        time.sleep(2.5)  # время демону на исправление
        # Enter нужен только чтобы `cat` записал строку в файл.
        test_keyboard.tap(e.KEY_ENTER)

    time.sleep(1.0)
    content = RESULT_FILE.read_text(encoding="utf-8", errors="replace") if RESULT_FILE.exists() else ""
    lines = [line.strip() for line in content.splitlines() if line.strip()]
    terminal.terminate()

    print(f"\nсодержимое файла: {content!r}")
    expected = RIGHT
    got = lines[0] if lines else None
    if got == expected:
        print(f"[OK  ] демон исправил раскладку сам: {WRONG!r} -> {got!r}")
        return 0
    print(f"[FAIL] ожидалось {expected!r}, получено {got!r}")
    print("Проверьте лог демона:  journalctl --user -u layout-fixer --since '1 min ago'")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

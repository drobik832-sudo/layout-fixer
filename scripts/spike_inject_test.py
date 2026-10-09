#!/usr/bin/env python3
"""Спайк: объективная проверка инъекции клавиш — без «посмотри глазами».

Как это работает: запускается обычный gnome-terminal, внутри которого выполняется
``cat > FILE``. Всё, что мы инжектируем, попадает в cat и оказывается в файле.
Результат читается с диска, поэтому проверка не зависит ни от AT-SPI, ни от
человека.

Проверяются все три примитива:
  1. печать текста в английской раскладке;
  2. переключение раскладки + печать кириллицы;
  3. Backspace (стирание уже набранного).

Запуск:  .venv/bin/python scripts/spike_inject_test.py
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from evdev import ecodes as e  # noqa: E402

from layout_fixer.injector import Injector  # noqa: E402
from layout_fixer.layout_state import (  # noqa: E402
    LayoutState,
    configured_actuator,
    get_actuator,
    xkb_layout_ids,
)
from layout_fixer.xkb import LayoutTables  # noqa: E402

RESULT_FILE = Path("/tmp/layout-fixer-inject-test.txt")
FOCUS_TIMEOUT = 12.0
# Для теста берём паузу после переключения раскладки с запасом.
SETTLE = 0.25


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


def terminal_window_active() -> bool:
    """Активно ли окно gnome-terminal (проверка через AT-SPI)."""
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
        except Exception:
            continue
        try:
            count = app.get_child_count()
        except Exception:
            continue
        for j in range(count):
            window = app.get_child_at_index(j)
            if window is None:
                continue
            try:
                if window.get_state_set().contains(Atspi.StateType.ACTIVE):
                    return True
            except Exception:
                continue
    return False


def read_result() -> list[str]:
    if not RESULT_FILE.exists():
        return []
    return RESULT_FILE.read_text(encoding="utf-8", errors="replace").splitlines()


def main() -> int:
    RESULT_FILE.unlink(missing_ok=True)
    env = session_env()

    print("1. запускаю gnome-terminal, который пишет весь ввод в файл:")
    print(f"   {RESULT_FILE}")
    terminal = subprocess.Popen(
        ["/usr/bin/gnome-terminal", "--window", "--", "/bin/bash", "-c", f"cat > {RESULT_FILE}"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    print("2. жду, пока окно терминала станет активным...")
    deadline = time.monotonic() + FOCUS_TIMEOUT
    while time.monotonic() < deadline:
        if terminal_window_active():
            break
        time.sleep(0.4)
    else:
        print("   ОТМЕНА: не удалось подтвердить фокус окна терминала.")
        print("   Инъекция не выполнена — текст ушёл бы в другое окно.")
        return 2
    print("   окно терминала в фокусе\n")
    time.sleep(0.6)

    tables = LayoutTables(xkb_layout_ids() or ["us", "ru"])
    actuator = configured_actuator() or get_actuator("leftalt")
    state = LayoutState(tables.layouts, actuator=actuator, settle=SETTLE)

    with Injector(tables=tables) as injector:
        state.injector = injector
        print(f"3. текущая раскладка по mru-sources: {state.refresh()!r}")

        print("4. печатаю 'ghbdtn' + Enter в раскладке us")
        if state.ensure("us"):
            print(f"   переключил раскладку на {state.current!r}")
        injector.type_text("ghbdtn", "us")
        injector.tap(e.KEY_ENTER)

        print("5. переключаю раскладку и печатаю 'привет' + Enter")
        if state.ensure("ru"):
            print(f"   переключил раскладку на {state.current!r}")
        injector.type_text("привет", "ru")
        injector.tap(e.KEY_ENTER)

        print("6. печатаю 'zzz', стираю 3 символа Backspace, печатаю 'ok' + Enter")
        if state.ensure("us"):
            print(f"   переключил раскладку на {state.current!r}")
        injector.type_text("zzz", "us")
        injector.backspace(3)
        injector.type_text("ok", "us")
        injector.tap(e.KEY_ENTER)

    time.sleep(1.0)
    lines = read_result()
    print(f"\nсодержимое файла: {lines!r}\n")

    expected = ["ghbdtn", "привет", "ok"]
    titles = [
        "печать в раскладке us",
        "смена раскладки + печать кириллицы",
        "Backspace (стёр 3 символа из 'zzz')",
    ]
    ok = True
    for index, (title, want) in enumerate(zip(titles, expected)):
        got = lines[index] if index < len(lines) else None
        passed = got == want
        ok = ok and passed
        print(f"   [{'OK  ' if passed else 'FAIL'}] {title}: ожидалось {want!r}, получено {got!r}")

    print()
    if ok:
        print("ИТОГ: инъекция работает полностью — печать, Backspace, смена раскладки.")
        print("Этап 1 (спайк) закрыт.")
    else:
        print("ИТОГ: есть расхождения — смотри значения выше.")
        print("Частые причины: окно потеряло фокус между шагами; раскладка не успела")
        print("переключиться (увеличь SETTLE); приложение игнорирует виртуальную клавиатуру.")

    terminal.terminate()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""CLI layout-fixer: диагностика и ручные операции.

На этапе спайка команды служат для проверки трёх примитивов:
инъекция клавиш, переключение раскладки и чтение клавиатуры с трекингом.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import re
import subprocess
import sys
from pathlib import Path

from evdev import ecodes as e

from . import __version__, gnome_settings
from .config import CONFIG_PATH, MODES, Config
from .daemon import Daemon
from .decisions_log import DEFAULT_PATH as DECISIONS_LOG_PATH
from .decisions_log import DecisionLog, summarize
from .detector import Detector
from .gnome_settings import read_mru, read_sources, read_xkb_options, xkb_layout_ids
from .hunspell import SpellCheckers
from .injector import VIRTUAL_DEVICE_NAME, Injector, InjectorError
from .keys import MODIFIER_KEYS, ModifierState
from .layout_state import (
    ACTUATORS,
    DEFAULT_ACTUATOR,
    Actuator,
    LayoutState,
    configured_actuator,
    get_actuator,
)
from .reader import KeyboardReader, find_keyboards
from .xkb import LEVELS, LayoutTables

log = logging.getLogger("layout_fixer")

# Опции xkb, которые переключают раскладку по клавише (нас интересует ровно одна).
GROUP_TOGGLE_RE = re.compile(r"^grp:.*_toggle$")
MODE_LINE_RE = re.compile(r"^mode\s*=.*$", re.MULTILINE)
BACKUP_PATH = Path.home() / ".config" / "layout-fixer" / "xkb-options.backup"
SERVICE = "layout-fixer"


# --- вспомогательное ------------------------------------------------------
def _default_layouts() -> list[str]:
    layouts = xkb_layout_ids()
    return layouts or ["us", "ru"]


def _make_injector(tables: LayoutTables, key_delay: float) -> Injector:
    return Injector(key_delay=key_delay, tables=tables)


def _actuator(name: str | None = None) -> Actuator:
    """Актуатор: явно указанный, иначе — реально настроенный в системе."""
    if name:
        return get_actuator(name)
    return configured_actuator() or get_actuator(DEFAULT_ACTUATOR)


def _word_preview(tables: LayoutTables, layout: str, word: list[str]) -> str:
    text = "".join(word)
    if not text:
        return ""
    other = [item for item in tables.layouts if item != layout]
    if not other:
        return f"слово: {text!r}"
    converted = tables.convert(text, layout, other[0])
    return f"слово: {text!r} -> в {other[0]!r}: {converted!r}"


# --- команды --------------------------------------------------------------
def cmd_explain(args: argparse.Namespace) -> int:
    """Разбор решения по одному слову — для настройки порогов."""
    layouts = args.layouts.split(",") if args.layouts else _default_layouts()
    config = Config()
    tables = LayoutTables(layouts)
    spell = SpellCheckers(layouts)
    detector = Detector(tables, spell, config)
    layout = args.layout or layouts[0]
    print(f"настройки: мин. длина {config.min_word_length}, порог биграмм {config.bigram_margin}")
    print(detector.explain(args.word, layout))
    return 0


def _service_is_active() -> bool:
    result = subprocess.run(
        ["systemctl", "--user", "is-active", SERVICE], capture_output=True, text=True
    )
    return result.stdout.strip() == "active"


def cmd_report(args: argparse.Namespace) -> int:
    """Отчёт по журналу решений: что демон видел и что исправил."""
    path = Path(args.file) if args.file else DECISIONS_LOG_PATH
    if args.clear:
        if path.exists():
            path.unlink()
            print(f"журнал очищен: {path}")
        else:
            print(f"журнала нет: {path}")
        return 0

    entries = DecisionLog.read(path, days=args.days)
    print(f"Файл: {path}")
    if not path.exists():
        print("Журнал ещё не создан — демон не запускался или не проверял слова.")
        return 0
    print(summarize(entries, top=args.top, tail=args.tail))
    return 0


def cmd_set_mode(args: argparse.Namespace) -> int:
    """Переключить режим работы: observe (только лог) <-> fix (реально исправлять)."""
    if not CONFIG_PATH.exists():
        Config.load(CONFIG_PATH)  # создаст файл со значениями по умолчанию
    text = CONFIG_PATH.read_text(encoding="utf-8")
    found = MODE_LINE_RE.search(text)
    old_mode = found.group(0).split("=", 1)[1].strip().strip('"') if found else "(не задан)"

    updated = MODE_LINE_RE.sub(f'mode = "{args.mode}"', text, count=1)
    if updated == text and (found is None or old_mode != args.mode):
        updated = f'mode = "{args.mode}"\n' + text
    CONFIG_PATH.write_text(updated, encoding="utf-8")
    print(f"режим: {old_mode} -> {args.mode}  ({CONFIG_PATH})")

    if args.mode == "fix":
        print("ВНИМАНИЕ: демон будет сам перепечатывать неверные слова.")
        print("  пауза на ходу:  systemctl --user kill -s SIGUSR1 layout-fixer")
        print("  вернуть наблюдение:  layout-fixer set-mode observe")

    if _service_is_active():
        subprocess.run(["systemctl", "--user", "restart", SERVICE], check=False, capture_output=True)
        print("сервис перезапущен — новый режим уже действует")
    else:
        print("сервис не запущен — режим применится при следующем запуске")
    return 0


def cmd_actuator(args: argparse.Namespace) -> int:
    """Показать/сменить/вернуть клавишу-актуатор переключения раскладки.

    Актуатор — техническая клавиша, которой демон переключает раскладку.
    Держать на ней Alt плохо: Alt участвует в комбинациях (Alt+Tab, Ctrl+Alt+T)
    и xkb-опция переключает язык прямо внутри них.
    """
    current = read_xkb_options()

    if args.action == "show":
        actuator = configured_actuator()
        print(f"xkb-options: {current}")
        print(f"актуатор: {actuator if actuator else 'не настроен'}")
        if BACKUP_PATH.exists():
            print(f"бэкап исходных опций: {BACKUP_PATH}")
        return 0

    if args.action == "restore":
        if not BACKUP_PATH.exists():
            print(f"бэкап не найден: {BACKUP_PATH}", file=sys.stderr)
            return 2
        original = [line for line in BACKUP_PATH.read_text(encoding="utf-8").splitlines() if line]
        gnome_settings.settings().set_strv("xkb-options", original)
        print(f"восстановлены исходные опции: {original}")
        return 0

    target = get_actuator(args.name)
    kept = [option for option in current if not GROUP_TOGGLE_RE.match(option)]
    updated = kept + [target.xkb_option]

    print(f"было:  {current}")
    print(f"будет: {updated}")
    print(f"актуатор: {target}")
    if args.dry_run:
        print("это только показ — ничего не изменено (уберите --dry-run)")
        return 0

    if not BACKUP_PATH.exists():
        BACKUP_PATH.parent.mkdir(parents=True, exist_ok=True)
        BACKUP_PATH.write_text("\n".join(current) + "\n", encoding="utf-8")
        print(f"сохранил исходные опции в {BACKUP_PATH}")

    gnome_settings.settings().set_strv("xkb-options", updated)
    print("\nготово. Если что-то не так:")
    print("  layout-fixer actuator restore")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    """Запустить демон (по умолчанию в безопасном режиме наблюдения)."""
    # Демону нужен INFO по умолчанию: именно на этом уровне пишутся решения
    # по словам, а в режиме наблюдения это единственный полезный вывод.
    logging.getLogger().setLevel(logging.DEBUG if args.verbose else logging.INFO)

    config = Config.load(args.config) if args.config else Config.load()
    if args.mode:
        config.mode = args.mode
    if args.actuator:
        config.actuator = args.actuator
    if args.trigger:
        config.trigger = args.trigger
    if args.layouts:
        config.layouts = args.layouts.split(",")
    problems = config.validate()
    for problem in problems:
        print(f"ошибка настроек: {problem}", file=sys.stderr)
    if problems:
        return 2

    daemon = Daemon(config)
    try:
        asyncio.run(daemon.run())
    except KeyboardInterrupt:
        return 0
    return 0


def cmd_devices(args: argparse.Namespace) -> int:
    devices = find_keyboards()
    if not devices:
        print("клавиатуры не найдены (проверьте права на /dev/input/event*)")
        return 1
    for device in devices:
        keys = device.capabilities().get(e.EV_KEY, [])
        print(f"{device.path}  {device.name}")
        print(f"    буквы: {'да' if e.KEY_A in keys else 'нет'}, всего клавиш: {len(keys)}")
    print(f"\nвиртуальная клавиатура будет называться: {VIRTUAL_DEVICE_NAME!r}")
    return 0


def cmd_state(args: argparse.Namespace) -> int:
    print("настройки GNOME:")
    print(f"  sources:     {read_sources()}")
    print(f"  mru-sources: {read_mru()}")
    print(f"  xkb-options: {read_xkb_options()}")
    actuator = configured_actuator()
    print(f"\nактуатор в системе: {actuator if actuator else 'не найден'}")
    layouts = _default_layouts()
    state = LayoutState(layouts, actuator or get_actuator(DEFAULT_ACTUATOR))
    print(f"\nтекущая раскладка (по mru-sources): {state.refresh()!r}")
    print("\nдоступные актуаторы:")
    for name, item in ACTUATORS.items():
        mark = "*" if actuator and item.name == actuator.name else " "
        print(f" {mark} {name:12} {item.xkb_option:22} {item.description}")
    return 0


def cmd_keymap(args: argparse.Namespace) -> int:
    tables = LayoutTables([args.layout])
    table = tables.table(args.layout)
    print(f"раскладка {args.layout!r}: {len(table)} клавиш")
    for code in range(e.KEY_GRAVE, e.KEY_SLASH + 1):
        levels = table.get(code)
        if not levels:
            continue
        name = e.KEY[code].replace("KEY_", "")
        plain = levels.get((False, False), "")
        shift = levels.get((True, False), "")
        altgr = levels.get((False, True), "")
        print(f"  {name:12} {plain!r:7} shift={shift!r:7} altgr={altgr!r}")
    return 0


def cmd_convert(args: argparse.Namespace) -> int:
    layouts = args.layouts.split(",")
    tables = LayoutTables(layouts)
    source, target = args.source, args.target
    if source is None or target is None:
        source, target = layouts[0], layouts[1 % len(layouts)]
    result = tables.convert(args.text, source, target)
    if result is None:
        print(f"не удалось перевести {args.text!r} из {source!r} в {target!r}")
        return 1
    print(f"{source} -> {target}: {args.text!r} => {result!r}")
    return 0


def cmd_type(args: argparse.Namespace) -> int:
    tables = LayoutTables([args.layout])
    with _make_injector(tables, args.key_delay) as injector:
        if args.ensure:
            layouts = _default_layouts()
            actuator = _actuator(args.actuator)
            state = LayoutState(layouts, actuator, injector=injector)
            state.refresh()
            if state.ensure(args.layout):
                print(f"[раскладка переключена: {state.current}]")
        skipped = injector.type_text(args.text, args.layout)
    print(f"напечатано {args.text!r} в раскладке {args.layout!r}")
    if skipped:
        print(f"не удалось напечатать символы: {skipped!r}")
    return 0


def cmd_backspace(args: argparse.Namespace) -> int:
    with _make_injector(LayoutTables(["us"]), args.key_delay) as injector:
        injector.backspace(args.count)
    print(f"Backspace x{args.count}")
    return 0


def cmd_key(args: argparse.Namespace) -> int:
    name = args.name.upper()
    code = getattr(e, f"KEY_{name}", None)
    if code is None:
        print(f"неизвестная клавиша {args.name!r}")
        return 2
    with _make_injector(LayoutTables(["us"]), args.key_delay) as injector:
        injector.tap(code)
    print(f"нажата {name}")
    return 0


def cmd_switch(args: argparse.Namespace) -> int:
    layouts = _default_layouts()
    actuator = _actuator(args.actuator)
    with _make_injector(LayoutTables(layouts), args.key_delay) as injector:
        state = LayoutState(layouts, actuator, injector=injector)
        before = state.refresh()
        after = state.toggle()
    print(f"{before} --({actuator.name})--> {after}")
    print("проверьте индикатор раскладки в системе: он должен смениться")
    return 0


def cmd_sniff(args: argparse.Namespace) -> int:
    try:
        return asyncio.run(_sniff(args))
    except KeyboardInterrupt:
        return 0


async def _sniff(args: argparse.Namespace) -> int:
    layouts = args.layouts.split(",")
    tables = LayoutTables(layouts)
    actuator = _actuator(args.actuator)
    state = LayoutState(layouts, actuator)
    state.refresh()
    mods = ModifierState()
    word: list[str] = []

    def flush() -> None:
        if word:
            print(f"           {_word_preview(tables, state.current, word)}")
            word.clear()

    def on_event(dev, event) -> None:
        code, value = event.code, event.value
        if state.handle_event(code, value):
            print(f"\n[РАСКЛАДКА] {actuator.name} -> теперь считаем {state.current!r}")
            return
        mods.update(code, value)
        if value not in (1, 2) or code in MODIFIER_KEYS:
            return
        if code == e.KEY_BACKSPACE:
            if word:
                word.pop()
            print(f"[BACKSPACE] осталось: {''.join(word)!r}")
            return
        char = tables.decode(state.current, code, shift=mods.shift, altgr=mods.altgr, caps=mods.caps)
        name = e.KEY[code].replace("KEY_", "")
        if code in (e.KEY_SPACE, e.KEY_ENTER, e.KEY_TAB) or char is None:
            if word:
                flush()
            print(f"[{name}]")
            return
        word.append(char)
        print(f"{name:12} -> {char!r}    ({''.join(word)!r})")

    print(f"раскладки: {layouts}, актуатор: {actuator.name}")
    print(f"считаем текущей раскладку: {state.current!r} (по mru-sources)")
    print("Ctrl+C — выход. Печатайте: покажу, что вижу.\n")

    reader = KeyboardReader(on_event)
    try:
        await reader.run()
    finally:
        await reader.stop_async()
    return 0


# --- разбор аргументов ----------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="layout-fixer",
        description="Автоисправление раскладки клавиатуры для GNOME/Wayland",
    )
    parser.add_argument("--version", action="version", version=f"layout-fixer {__version__}")
    parser.add_argument("-v", "--verbose", action="count", default=0, help="подробный лог")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("report", help="отчёт по журналу решений демона")
    p.add_argument("--days", type=int, help="только за последние N дней")
    p.add_argument("--top", type=int, default=10, help="сколько слов показать в топе (по умолчанию 10)")
    p.add_argument("--tail", type=int, default=10, help="сколько последних записей показать (0 = не показывать)")
    p.add_argument("--file", help="другой путь к журналу")
    p.add_argument("--clear", action="store_true", help="очистить журнал")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("set-mode", help="режим работы: observe (лог) или fix (исправлять)")
    p.add_argument("mode", choices=list(MODES))
    p.set_defaults(func=cmd_set_mode)

    p = sub.add_parser("actuator", help="клавиша-актуатор переключения раскладки")
    asp = p.add_subparsers(dest="action", required=True)
    asp.add_parser("show", help="показать текущие xkb-опции и актуатор")
    asp.add_parser("restore", help="вернуть исходные xkb-опции из бэкапа")
    ap = asp.add_parser("set", help="назначить актуатор")
    ap.add_argument("name", choices=list(ACTUATORS))
    ap.add_argument("--dry-run", action="store_true", help="только показать, что изменится")
    p.set_defaults(func=cmd_actuator)

    p = sub.add_parser("run", help="запустить демон (по умолчанию только наблюдение)")
    p.add_argument("--mode", choices=list(MODES), help="observe (лог) или fix (исправлять)")
    p.add_argument("--actuator", choices=list(ACTUATORS), help="клавиша переключения раскладки")
    p.add_argument("--trigger", choices=["alt_tap", "none"], help="как переключать раскладку пользователю")
    p.add_argument("--layouts", help="список раскладок через запятую, напр. us,ru")
    p.add_argument("--config", help="путь к файлу настроек")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("explain", help="разобрать решение по слову (для настройки порогов)")
    p.add_argument("word")
    p.add_argument("--layout", help="раскладка, в которой набрано слово (по умолчанию первая)")
    p.add_argument("--layouts", help="список раскладок через запятую")
    p.set_defaults(func=cmd_explain)

    p = sub.add_parser("devices", help="показать клавиатуры, доступные для чтения")
    p.set_defaults(func=cmd_devices)

    p = sub.add_parser("state", help="показать состояние раскладок и актуатора")
    p.set_defaults(func=cmd_state)

    p = sub.add_parser("keymap", help="показать таблицу символов раскладки")
    p.add_argument("--layout", default="ru")
    p.set_defaults(func=cmd_keymap)

    p = sub.add_parser("convert", help="перевести текст из одной раскладки в другую")
    p.add_argument("text")
    p.add_argument("--layouts", default="us,ru")
    p.add_argument("--source")
    p.add_argument("--target")
    p.set_defaults(func=cmd_convert)

    p = sub.add_parser("type", help="напечатать текст инъекцией (проверка uinput)")
    p.add_argument("text")
    p.add_argument("--layout", default="us")
    p.add_argument("--ensure", action="store_true", help="сначала переключить раскладку системы")
    p.add_argument("--actuator", choices=list(ACTUATORS), help="по умолчанию — из системы")
    p.add_argument("--key-delay", type=float, default=0.002)
    p.set_defaults(func=cmd_type)

    p = sub.add_parser("backspace", help="отправить Backspace N раз")
    p.add_argument("count", type=int, nargs="?", default=1)
    p.add_argument("--key-delay", type=float, default=0.002)
    p.set_defaults(func=cmd_backspace)

    p = sub.add_parser("key", help="нажать одну клавишу (например leftalt)")
    p.add_argument("name")
    p.add_argument("--key-delay", type=float, default=0.002)
    p.set_defaults(func=cmd_key)

    p = sub.add_parser("switch", help="переключить раскладку инъекцией актуатора")
    p.add_argument("--actuator", choices=list(ACTUATORS), help="по умолчанию — из системы")
    p.add_argument("--key-delay", type=float, default=0.002)
    p.set_defaults(func=cmd_switch)

    p = sub.add_parser("sniff", help="слушать клавиатуру: декодировать символы и следить за раскладкой")
    p.add_argument("--layouts", default="us,ru")
    p.add_argument("--actuator", choices=list(ACTUATORS), help="по умолчанию — из системы")
    p.set_defaults(func=cmd_sniff)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose > 1 else (logging.INFO if args.verbose else logging.WARNING),
        format="%(levelname)s %(name)s: %(message)s",
    )
    try:
        return int(args.func(args))
    except InjectorError as exc:
        print(f"ошибка: {exc}", file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        return 130

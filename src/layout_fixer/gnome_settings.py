"""Доступ к настройкам GNOME и подписка на их изменения.

Читаем через ``Gio.Settings`` (то есть напрямую в dconf), без запуска
``gsettings`` в подпроцессе. Это и быстрее, и не требует парсинга текста.

Напоминание из разведки: достоверно прочитать текущую раскладку можно только
через ``mru-sources`` (первый элемент = активная раскладка). Ключ ``current``
устарел и игнорируется, ``Shell.Eval`` заблокирован.
"""

from __future__ import annotations

import logging
import subprocess
import time
import threading
from collections.abc import Callable
from typing import Any

from gi.repository import Gio, GLib

log = logging.getLogger(__name__)

SCHEMA = "org.gnome.desktop.input-sources"

_settings: Gio.Settings | None = None
_settings_lock = threading.Lock()


def settings() -> Gio.Settings:
    """Общий объект настроек (Gio.Settings потокобезопасен для чтения)."""
    global _settings
    with _settings_lock:
        if _settings is None:
            _settings = Gio.Settings.new(SCHEMA)
        return _settings


def _sources_from(variant: Any) -> list[tuple[str, str]]:
    return [(str(kind), str(name)) for kind, name in variant.unpack()]


def read_sources() -> list[tuple[str, str]]:
    return _sources_from(settings().get_value("sources"))


def read_mru() -> list[tuple[str, str]]:
    return _sources_from(settings().get_value("mru-sources"))


def read_xkb_options() -> list[str]:
    return [str(item) for item in settings().get_value("xkb-options").unpack()]


def xkb_layout_ids(sources: list[tuple[str, str]] | None = None) -> list[str]:
    """Идентификаторы раскладок xkb по порядку — порядок равен порядку групп в xkb."""
    items = sources if sources is not None else read_sources()
    return [name for kind, name in items if kind == "xkb"]


def mru_layouts() -> list[str]:
    """Раскладки из mru-sources; первый элемент — текущая."""
    return [name for kind, name in read_mru() if kind == "xkb"]


class SettingsWatcher:
    """Подписка на изменения ключа настроек через GLib.

    Колбэк вызывается **в потоке GLib**. Если подписчик работает на asyncio,
    его нужно обернуть в ``loop.call_soon_threadsafe``.
    """

    def __init__(self, key: str, on_change: Callable[[Any], None]):
        self.key = key
        self._on_change = on_change
        self._settings = Gio.Settings.new(SCHEMA)
        self._loop: GLib.MainLoop | None = None
        self._thread: threading.Thread | None = None
        self._handler_id: int | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._handler_id = self._settings.connect(f"changed::{self.key}", self._changed)
        self._loop = GLib.MainLoop()
        self._thread = threading.Thread(target=self._run, name=f"watch:{self.key}", daemon=True)
        self._thread.start()
        log.debug("подписка на %s запущена", self.key)

    def _run(self) -> None:
        assert self._loop is not None
        try:
            self._loop.run()
        except Exception:  # pragma: no cover - защита потока от падения
            log.exception("поток наблюдения за %s упал", self.key)

    def _changed(self, _settings: Gio.Settings, _key: str) -> None:
        try:
            value = _settings.get_value(self.key)
        except Exception:
            log.exception("не удалось прочитать %s", self.key)
            return
        try:
            self._on_change(value)
        except Exception:
            log.exception("ошибка в обработчике изменения %s", self.key)

    def stop(self) -> None:
        if self._handler_id is not None:
            self._settings.disconnect(self._handler_id)
            self._handler_id = None
        if self._loop is not None:
            self._loop.quit()
            self._loop = None
        self._thread = None


class MruWatcher(SettingsWatcher):
    """Подписка на смену текущей раскладки (через ``mru-sources``).

    Нужна, чтобы демон оставался в синхроне, если раскладку переключили мимо
    него: клик по индикатору, переключение самой системой и т.п.
    """

    def __init__(self, on_layout: Callable[[str], None]):
        super().__init__("mru-sources", self._dispatch)
        self._on_layout = on_layout
        self._last: str | None = None

    def _dispatch(self, variant: Any) -> None:
        layouts = [name for kind, name in _sources_from(variant) if kind == "xkb"]
        if not layouts:
            return
        current = layouts[0]
        if current == self._last:
            return
        log.debug("mru-sources изменён: %s -> %s", self._last, current)
        self._last = current
        self._on_layout(current)

_groups_cache = []
_led_cache = None

# Насколько свежим считаем прочитанный индикатор. Короткое окно нужно, чтобы
# ensure() не платил за два чтения подряд; сбрасывается при каждом нашем
# переключении (см. invalidate_xkb_cache), поэтому после него читаем заново.
LED_CACHE_TTL_S = 0.12


def invalidate_xkb_cache() -> None:
    """Сбросить кеш: вызывать после любого нашего переключения раскладки."""
    global _led_cache
    _led_cache = None


def xkb_groups(refresh: bool = False) -> list[str]:
    """Список групп XKB ровно в том виде, в каком их держит X-сервер.

    Кешируется: он меняется только когда кто-то пересобирает список, а читать
    его — значит породить ``setxkbmap -query`` (17 мс) на каждой правке.
    """
    global _groups_cache
    if _groups_cache and not refresh:
        return _groups_cache
    try:
        out = subprocess.run(
            ["setxkbmap", "-query"], capture_output=True, text=True, timeout=3
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return _groups_cache
    groups: list[str] = []
    for line in out.splitlines():
        if line.startswith("layout:"):
            groups = [p for p in line.split(":", 1)[1].strip().split(",") if p]
            break
    if groups:
        _groups_cache = groups
    return _groups_cache


def _led_mask() -> int | None:
    global _led_cache
    now = time.monotonic()
    if _led_cache is not None and now - _led_cache[0] < LED_CACHE_TTL_S:
        return _led_cache[1]
    try:
        out = subprocess.run(
            ["xset", "q"], capture_output=True, text=True, timeout=3
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for line in out.splitlines():
        if "LED mask:" in line:
            try:
                mask = int(line.split("LED mask:")[1].split()[0], 16)
            except (IndexError, ValueError):
                return None
            _led_cache = (now, mask)
            return mask
    return None


def xkb_group_index(layouts: list[str]) -> int | None:
    """Индекс активной группы XKB, прочитанный у X-сервера.

    ``mru-sources`` обновляется с задержкой 26–329 мс, и в это окно демон
    успевал поверить устаревшему значению. Индикатор группы у X-сервера — это
    состояние самого сервера, без задержки.

    Работает только когда число групп равно числу раскладок. Если групп больше
    (дубль ``ru`` от виртуальной клавиатуры), индикатор показывает лишь первые
    две группы и отвечает не на тот вопрос — тогда возвращается None, и
    вызывающий код обязан привести список в порядок, а не угадывать.
    """
    groups = xkb_groups()
    if len(groups) != len(layouts) or len(layouts) < 2:
        return None
    mask = _led_mask()
    if mask is None:
        return None
    return 1 if mask & (1 << 12) else 0

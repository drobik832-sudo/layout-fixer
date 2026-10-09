"""Инъекция клавиш через ``/dev/uinput``.

На Wayland приложение не может отправить «виртуальное» нажатие клавиши в другое
приложение: протокол virtual-keyboard в Mutter не реализован, а ``ydotool`` /
``wtype`` в системе отсутствуют. Единственный рабочий путь — создать собственное
виртуальное устройство ввода на уровне ядра.

Требуется доступ на запись к ``/dev/uinput`` (см. ``udev/99-layout-fixer-uinput.rules``).
"""

from __future__ import annotations

import contextlib
import time

from evdev import InputEvent, UInput, ecodes as e

from .xkb import LayoutTables

VIRTUAL_DEVICE_NAME = "layout-fixer virtual keyboard"

# Клавиши, которые умеет изображать виртуальная клавиатура. Ровно столько,
# сколько нужно: печать текста, Backspace, служебные и модификаторы.
#
# ВАЖНО: uinput молча игнорирует события для клавиш, не объявленных здесь,
# поэтому список обязан покрывать все клавиши обеих раскладок — в том числе
# «,», «.» и «/»: это буквы б, ю и точка в русской раскладке.
# Проверяется тестом TestInjectorCoverage.
INJECT_KEYS: list[int] = sorted(
    {
        e.KEY_ESC,
        e.KEY_GRAVE,
        e.KEY_MINUS,
        e.KEY_EQUAL,
        e.KEY_COMMA,
        e.KEY_DOT,
        e.KEY_SLASH,
        e.KEY_BACKSPACE,
        e.KEY_TAB,
        e.KEY_LEFTBRACE,
        e.KEY_RIGHTBRACE,
        e.KEY_BACKSLASH,
        e.KEY_ENTER,
        e.KEY_SEMICOLON,
        e.KEY_APOSTROPHE,
        e.KEY_SPACE,
        e.KEY_102ND,
        e.KEY_MENU,
        e.KEY_CAPSLOCK,
        e.KEY_SCROLLLOCK,
        e.KEY_NUMLOCK,
        e.KEY_LEFTCTRL,
        e.KEY_LEFTSHIFT,
        e.KEY_LEFTALT,
        e.KEY_LEFTMETA,
        e.KEY_RIGHTCTRL,
        e.KEY_RIGHTSHIFT,
        e.KEY_RIGHTALT,
        e.KEY_RIGHTMETA,
        e.KEY_HOME,
        e.KEY_END,
        e.KEY_INSERT,
        e.KEY_DELETE,
        e.KEY_PAGEUP,
        e.KEY_PAGEDOWN,
        e.KEY_LEFT,
        e.KEY_RIGHT,
        e.KEY_UP,
        e.KEY_DOWN,
        *range(e.KEY_1, e.KEY_0 + 1),  # цифры основного блока
        *range(e.KEY_Q, e.KEY_P + 1),
        *range(e.KEY_A, e.KEY_L + 1),
        *range(e.KEY_Z, e.KEY_M + 1),
    }
)


class InjectorError(RuntimeError):
    """Не удалось создать виртуальное устройство (обычно — нет прав на /dev/uinput)."""


class Injector:
    """Виртуальная клавиатура: печать текста, Backspace, одиночные нажатия."""

    def __init__(
        self,
        name: str = VIRTUAL_DEVICE_NAME,
        key_delay: float = 0.002,
        tables: LayoutTables | None = None,
    ):
        self.name = name
        self.key_delay = key_delay
        self.tables = tables
        try:
            self._ui = UInput(
                {e.EV_KEY: INJECT_KEYS},
                name=name,
                vendor=0x1234,
                product=0x5678,
                version=1,
                bustype=e.BUS_USB,
                phys="layout-fixer",
            )
        except PermissionError as exc:  # pragma: no cover - зависит от прав в системе
            raise InjectorError(
                "нет доступа к /dev/uinput. Установите udev-правило:\n"
                "  sudo cp udev/99-layout-fixer-uinput.rules /etc/udev/rules.d/\n"
                "  sudo udevadm control --reload-rules && sudo udevadm trigger --subsystem-match=misc"
            ) from exc
        self.path = self._ui.device.path

    # --- низкий уровень -------------------------------------------------
    def _write(self, code: int, value: int) -> None:
        self._ui.write_event(InputEvent(0, 0, e.EV_KEY, code, value))

    def key_down(self, code: int) -> None:
        self._write(code, 1)
        self._ui.syn()

    def key_up(self, code: int) -> None:
        self._write(code, 0)
        self._ui.syn()

    def tap(self, code: int, shift: bool = False, altgr: bool = False) -> None:
        """Одиночное нажатие клавиши (при необходимости — с модификаторами).

        Отпускание клавиш обязательно происходит даже если ``syn()`` или
        ``sleep()`` прервутся исключением: залипший на виртуальной клавиатуре
        Shift переводит весь дальнейший ввод и держится до перезапуска демона.
        """
        mods = []
        if shift:
            mods.append(e.KEY_LEFTSHIFT)
        if altgr:
            mods.append(e.KEY_RIGHTALT)

        for mod in mods:
            self._write(mod, 1)
        self._write(code, 1)
        try:
            self._ui.syn()
            time.sleep(self.key_delay)
        finally:
            with contextlib.suppress(Exception):
                self._write(code, 0)
                for mod in reversed(mods):
                    self._write(mod, 0)
                self._ui.syn()
        time.sleep(self.key_delay)

    # --- прикладной уровень ---------------------------------------------
    def type_char(self, code: int, shift: bool = False, altgr: bool = False) -> None:
        self.tap(code, shift=shift, altgr=altgr)

    def type_text(self, text: str, layout: str) -> list[str]:
        """Напечатать текст так, как его выдаёт раскладка ``layout``.

        Возвращает список символов, которые напечатать не удалось.
        """
        if self.tables is None:
            raise RuntimeError("для печати текста нужен LayoutTables (передайте tables=...)")
        skipped: list[str] = []
        for char in text:
            key = self.tables.to_key(layout, char)
            if key is None:
                skipped.append(char)
                continue
            code, shift, altgr = key
            self.type_char(code, shift=shift, altgr=altgr)
        return skipped

    def backspace(self, count: int = 1) -> None:
        for _ in range(max(0, count)):
            self.tap(e.KEY_BACKSPACE)

    def close(self) -> None:
        try:
            self._ui.close()
        except Exception:
            pass

    def __enter__(self) -> "Injector":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

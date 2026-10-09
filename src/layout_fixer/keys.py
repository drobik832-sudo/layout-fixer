"""Состояние модификаторов из потока evdev-событий.

evdev отдаёт «сырые» коды клавиш и ничего не знает о раскладке и модификаторах,
поэтому Shift / Caps Lock / AltGr приходится отслеживать самостоятельно — иначе
невозможно понять, какой именно символ напечатался.
"""

from __future__ import annotations

from evdev import ecodes as e

SHIFT_KEYS = frozenset({e.KEY_LEFTSHIFT, e.KEY_RIGHTSHIFT})
CTRL_KEYS = frozenset({e.KEY_LEFTCTRL, e.KEY_RIGHTCTRL})
ALT_KEYS = frozenset({e.KEY_LEFTALT, e.KEY_RIGHTALT})
META_KEYS = frozenset({e.KEY_LEFTMETA, e.KEY_RIGHTMETA})
ALTGR_KEYS = frozenset({e.KEY_RIGHTALT})

CAPS_KEY = e.KEY_CAPSLOCK
MODIFIER_KEYS = SHIFT_KEYS | CTRL_KEYS | ALT_KEYS | META_KEYS | {CAPS_KEY}


class ModifierState:
    """Какие модификаторы зажаты прямо сейчас (и включён ли Caps Lock)."""

    def __init__(self) -> None:
        self._pressed: set[int] = set()
        self.caps = False

    def update(self, code: int, value: int) -> None:
        """Обновить состояние по событию клавиши (value: 0 — отпускание, 1 — нажатие, 2 — автоповтор)."""
        if value == 1:
            if code == CAPS_KEY:
                self.caps = not self.caps
                return
            self._pressed.add(code)
        elif value == 0:
            self._pressed.discard(code)

    def reset(self) -> None:
        self._pressed.clear()

    @property
    def shift(self) -> bool:
        return bool(self._pressed & SHIFT_KEYS)

    @property
    def ctrl(self) -> bool:
        return bool(self._pressed & CTRL_KEYS)

    @property
    def alt(self) -> bool:
        return bool(self._pressed & ALT_KEYS)

    @property
    def altgr(self) -> bool:
        return bool(self._pressed & ALTGR_KEYS)

    @property
    def meta(self) -> bool:
        return bool(self._pressed & META_KEYS)

    @property
    def command(self) -> bool:
        """Зажат любой «командный» модификатор — значит это горячая клавиша, а не текст."""
        return self.ctrl or self.alt or self.meta

    @property
    def any_pressed(self) -> bool:
        return bool(self._pressed)

    def other_modifiers(self, exclude: int | None = None) -> bool:
        """Зажат ли какой-нибудь модификатор, кроме ``exclude``.

        Нужно, чтобы отличить одиночный тап Alt от комбинации: ``Shift+Alt``
        иначе проходил бы как чистое нажатие и переключал раскладку.
        """
        modifiers = MODIFIER_KEYS - {exclude} if exclude is not None else MODIFIER_KEYS
        return bool(self._pressed & modifiers)

    def __repr__(self) -> str:  # pragma: no cover - отладка
        pressed = sorted(e.KEY[int(code)] for code in self._pressed)
        return f"ModifierState(shift={self.shift}, ctrl={self.ctrl}, alt={self.alt}, caps={self.caps}, pressed={pressed})"

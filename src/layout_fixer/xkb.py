"""Таблицы соответствия «evdev-код клавиши ↔ символ» для раскладок.

Таблицы НЕ написаны руками, а сгенерированы через ``libxkbcommon`` из тех же
файлов ``xkeyboard-config``, которые читает Mutter. Поэтому маппинг совпадает с
реальным поведением системы: варианты раскладок, Shift, AltGr, Caps Lock.

Почему через ctypes, а не через готовую библиотеку: ``xkbcli`` и python-биндинги
в системе отсутствуют, а ``libxkbcommon.so.0`` есть (зависимость Mutter).
"""

from __future__ import annotations

import ctypes
import ctypes.util
from typing import Iterable

from evdev import ecodes as e

# xkbcommon оперирует XKB-кодами клавиш = evdev-код + 8 (историческая конвенция X11).
EVDEV_TO_XKB = 8

XKB_KEY_UP = 0
XKB_KEY_DOWN = 1
XKB_CONTEXT_NO_FLAGS = 0
XKB_KEYMAP_COMPILE_NO_FLAGS = 0

# Уровни символов: (Shift, AltGr)
Level = tuple[bool, bool]
LEVELS: tuple[Level, ...] = ((False, False), (True, False), (False, True), (True, True))

# Клавиши цифрового блока: дают те же символы, что и основной блок, и мешают
# обратному маппингу (символ -> клавиша), поэтому в обратной таблице их нет.
NUMPAD_CODES = frozenset(
    value for name, value in vars(e).items() if name.startswith("KEY_KP") and isinstance(value, int)
)


class _RuleNames(ctypes.Structure):
    _fields_ = [
        ("rules", ctypes.c_char_p),
        ("model", ctypes.c_char_p),
        ("layout", ctypes.c_char_p),
        ("variant", ctypes.c_char_p),
        ("options", ctypes.c_char_p),
    ]


def _load_lib() -> ctypes.CDLL:
    path = ctypes.util.find_library("xkbcommon") or "libxkbcommon.so.0"
    lib = ctypes.CDLL(path)

    lib.xkb_context_new.restype = ctypes.c_void_p
    lib.xkb_context_new.argtypes = [ctypes.c_int]
    lib.xkb_context_unref.argtypes = [ctypes.c_void_p]

    lib.xkb_keymap_new_from_names.restype = ctypes.c_void_p
    lib.xkb_keymap_new_from_names.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(_RuleNames),
        ctypes.c_int,
    ]
    lib.xkb_keymap_unref.argtypes = [ctypes.c_void_p]

    lib.xkb_state_new.restype = ctypes.c_void_p
    lib.xkb_state_new.argtypes = [ctypes.c_void_p]
    lib.xkb_state_unref.argtypes = [ctypes.c_void_p]

    lib.xkb_state_key_get_utf8.restype = ctypes.c_int
    lib.xkb_state_key_get_utf8.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_char_p,
        ctypes.c_size_t,
    ]

    lib.xkb_state_update_key.restype = ctypes.c_int
    lib.xkb_state_update_key.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int]

    return lib


_lib = _load_lib()
_ctx = _lib.xkb_context_new(XKB_CONTEXT_NO_FLAGS)
if not _ctx:  # pragma: no cover - сломанное окружение
    raise RuntimeError("libxkbcommon: не удалось создать контекст")


def _enc(value: str | None) -> bytes | None:
    return value.encode() if value else None


class Keymap:
    """Скомпилированная раскладка + состояния модификаторов для чтения символов."""

    def __init__(self, layout: str, variant: str | None = None, options: str | None = None):
        self.layout = layout
        self.variant = variant
        names = _RuleNames(None, None, _enc(layout), _enc(variant), _enc(options))
        self._km = _lib.xkb_keymap_new_from_names(_ctx, ctypes.byref(names), XKB_KEYMAP_COMPILE_NO_FLAGS)
        if not self._km:
            raise ValueError(f"не удалось скомпилировать раскладку {layout!r} (вариант {variant!r})")

        self._states: dict[Level, int] = {}
        for shift, altgr in LEVELS:
            state = _lib.xkb_state_new(self._km)
            if shift:
                _lib.xkb_state_update_key(state, e.KEY_LEFTSHIFT + EVDEV_TO_XKB, XKB_KEY_DOWN)
            if altgr:
                _lib.xkb_state_update_key(state, e.KEY_RIGHTALT + EVDEV_TO_XKB, XKB_KEY_DOWN)
            self._states[(shift, altgr)] = state

    def _utf8(self, state: int, code: int) -> str:
        buf = ctypes.create_string_buffer(32)
        size = _lib.xkb_state_key_get_utf8(state, code + EVDEV_TO_XKB, buf, len(buf))
        if size <= 0:
            return ""
        return buf.value.decode("utf-8", "replace")

    def char(self, code: int, shift: bool = False, altgr: bool = False, caps: bool = False) -> str | None:
        """Символ, который система напечатает для данной физической клавиши."""
        level = (bool(shift), bool(altgr))
        if level not in self._states:
            level = (bool(shift), False)
        text = self._utf8(self._states[level], code)
        if not text:
            return None
        if len(text) == 1 and text.isalpha():
            # Caps Lock действует как «инверсия Shift» для букв (в т.ч. кириллицы)
            text = text.upper() if (caps != shift) else text.lower()
        return text

    def table(self) -> dict[int, dict[Level, str]]:
        """Полная таблица: код клавиши -> уровень -> символ (пустые уровни опущены)."""
        result: dict[int, dict[Level, str]] = {}
        for code in range(1, 256):
            if code in NUMPAD_CODES:
                continue
            per_level: dict[Level, str] = {}
            for level in LEVELS:
                text = self._utf8(self._states[level], code)
                if text:
                    per_level[level] = text
            if per_level:
                result[code] = per_level
        return result

    def close(self) -> None:
        for state in self._states.values():
            _lib.xkb_state_unref(state)
        self._states.clear()
        if self._km:
            _lib.xkb_keymap_unref(self._km)
            self._km = None

    def __enter__(self) -> "Keymap":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


class LayoutTables:
    """Набор раскладок с прямым (клавиша -> символ) и обратным (символ -> клавиша) доступом."""

    def __init__(self, layouts: Iterable[str], options: str | None = None):
        self._keymaps: dict[str, Keymap] = {}
        self._tables: dict[str, dict[int, dict[Level, str]]] = {}
        self._reverse: dict[str, dict[str, tuple[int, bool, bool]]] = {}
        self._word_chars: frozenset[str] | None = None
        for layout in layouts:
            keymap = Keymap(layout, options=options)
            table = keymap.table()
            self._keymaps[layout] = keymap
            self._tables[layout] = table
            self._reverse[layout] = self._build_reverse(table)

    @staticmethod
    def _build_reverse(table: dict[int, dict[Level, str]]) -> dict[str, tuple[int, bool, bool]]:
        reverse: dict[str, tuple[int, bool, bool]] = {}
        for code, levels in sorted(table.items()):
            for (shift, altgr) in LEVELS:
                char = levels.get((shift, altgr))
                if not char or len(char) != 1:
                    continue
                # Первое найденное значение выигрывает: коды отсортированы по
                # возрастанию, поэтому предпочитается основной блок клавиш.
                reverse.setdefault(char, (code, shift, altgr))
        return reverse

    @property
    def layouts(self) -> list[str]:
        return list(self._keymaps)

    @property
    def word_characters(self) -> frozenset[str]:
        """Символы, которые считаются частью слова хотя бы в одной раскладке.

        Зачем: в русской раскладке буквы б, ю, ж, э, х, ъ, ё стоят на клавишах,
        которые в латинской дают запятую, точку, точку с запятой и т. п. Человек,
        печатающий по-русски с включённой английской раскладкой, получает слова
        вида «j,yjdktybt» (обновление). Такие символы нельзя считать разделителями,
        иначе половина слов не распознаётся.

        Цифры и «настоящая» пунктуация (пробел, /, -, =) сюда не попадают: их нет
        ни в одной раскладке на буквенных клавишах.
        """
        if self._word_chars is None:
            groups: dict[tuple[int, Level], list[str]] = {}
            for table in self._tables.values():
                for code, levels in table.items():
                    for level, char in levels.items():
                        if len(char) == 1:
                            groups.setdefault((code, level), []).append(char)
            chars: set[str] = set()
            for variants in groups.values():
                # Если на этой клавише хоть в одной раскладке буква — значит
                # во всех остальных это тоже часть слова.
                if any(char.isalpha() for char in variants):
                    chars.update(variants)
            self._word_chars = frozenset(chars)
        return self._word_chars

    def table(self, layout: str) -> dict[int, dict[Level, str]]:
        return self._tables[layout]

    def decode(
        self,
        layout: str,
        code: int,
        shift: bool = False,
        altgr: bool = False,
        caps: bool = False,
    ) -> str | None:
        keymap = self._keymaps.get(layout)
        if keymap is None:
            return None
        return keymap.char(code, shift=shift, altgr=altgr, caps=caps)

    def to_key(self, layout: str, char: str) -> tuple[int, bool, bool] | None:
        """Символ -> (код клавиши, нужен Shift, нужен AltGr) или None, если набрать нельзя."""
        return self._reverse.get(layout, {}).get(char)

    def convert(self, text: str, source: str, target: str) -> str | None:
        """Перевести текст из одной раскладки в другую «по клавишам».

        Например, 'ghbdtn' (us) -> 'привет' (ru). Возвращает None, если хотя бы
        один символ набрать в исходной раскладке невозможно.
        """
        out: list[str] = []
        for char in text:
            key = self.to_key(source, char)
            if key is None:
                return None
            code, shift, altgr = key
            decoded = self.decode(target, code, shift=shift, altgr=altgr)
            if decoded is None:
                return None
            out.append(decoded)
        return "".join(out)

    def close(self) -> None:
        for keymap in self._keymaps.values():
            keymap.close()
        self._keymaps.clear()

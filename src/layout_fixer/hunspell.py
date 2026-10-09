"""Проверка орфографии через ``libhunspell`` (ctypes).

Почему через ctypes, а не через CLI: в системе есть ``libhunspell-1.7.so.0`` и
полные словари (``ru_RU``, ``en_US``), но нет ни утилиты ``hunspell``, ни
python-биндингов. Через библиотеку мы получаем проверку **с учётом аффиксов**
(падежи, склонения, приставки) — то есть «маму» распознаётся как правильное
слово, хотя в ``.dic`` лежит только основа «мама». Наивная проверка по списку
основ давала бы ложные срабатывания на каждом косвенном падеже.
"""

from __future__ import annotations

import ctypes
import ctypes.util
from pathlib import Path

DICT_DIR = Path("/usr/share/hunspell")

# Соответствие «раскладка -> словарь». Раскладка us/en — английский словарь.
LAYOUT_DICTS = {
    "us": "en_US",
    "en": "en_US",
    "gb": "en_US",
    "ru": "ru_RU",
    "ua": "ru_RU",
}


def _load_lib() -> ctypes.CDLL:
    path = ctypes.util.find_library("hunspell") or "libhunspell-1.7.so.0"
    lib = ctypes.CDLL(path)

    lib.Hunspell_create.restype = ctypes.c_void_p
    lib.Hunspell_create.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
    lib.Hunspell_destroy.argtypes = [ctypes.c_void_p]

    lib.Hunspell_spell.restype = ctypes.c_int
    lib.Hunspell_spell.argtypes = [ctypes.c_void_p, ctypes.c_char_p]

    lib.Hunspell_stem.restype = ctypes.c_int
    lib.Hunspell_stem.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.POINTER(ctypes.c_char_p)),
        ctypes.c_char_p,
    ]

    lib.Hunspell_free_list.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.POINTER(ctypes.c_char_p)),
        ctypes.c_int,
    ]

    lib.Hunspell_add.restype = ctypes.c_int
    lib.Hunspell_add.argtypes = [ctypes.c_void_p, ctypes.c_char_p]

    lib.Hunspell_remove.restype = ctypes.c_int
    lib.Hunspell_remove.argtypes = [ctypes.c_void_p, ctypes.c_char_p]

    return lib


_lib = _load_lib()


class SpellCheckError(RuntimeError):
    """Не удалось загрузить словарь."""


class SpellChecker:
    """Словарь одного языка (с аффиксами и поддержкой пользовательских слов)."""

    def __init__(self, dictionary: str, dict_dir: Path = DICT_DIR):
        self.dictionary = dictionary
        aff = dict_dir / f"{dictionary}.aff"
        dic = dict_dir / f"{dictionary}.dic"
        if not aff.exists() or not dic.exists():
            raise SpellCheckError(f"словарь {dictionary!r} не найден в {dict_dir}")
        self._aff = str(aff).encode()
        self._dic = str(dic).encode()
        self._handle = _lib.Hunspell_create(self._aff, self._dic)
        if not self._handle:
            raise SpellCheckError(f"hunspell не смог открыть словарь {dictionary!r}")
        self._known_cache: dict[str, bool] = {}

    @classmethod
    def for_layout(cls, layout: str) -> "SpellChecker | None":
        """Словарь для раскладки или None, если словаря нет."""
        name = LAYOUT_DICTS.get(layout)
        if name is None:
            return None
        return cls(name)

    def known(self, word: str) -> bool:
        """Есть ли слово в словаре (с учётом словоформ)."""
        if not word:
            return False
        cached = self._known_cache.get(word)
        if cached is not None:
            return cached
        # Hunspell не любит слова с апострофами/дефисами в нашем контексте —
        # такие проверяем «как есть», это нормально.
        result = bool(_lib.Hunspell_spell(self._handle, word.encode("utf-8")))
        self._known_cache[word] = result
        return result

    def stems(self, word: str) -> list[str]:
        """Основы слова (полезно для эвристик)."""
        pointer = ctypes.POINTER(ctypes.c_char_p)()
        count = _lib.Hunspell_stem(self._handle, ctypes.byref(pointer), word.encode("utf-8"))
        if count <= 0:
            return []
        try:
            return [pointer[i].decode("utf-8", "replace") for i in range(count) if pointer[i]]
        finally:
            _lib.Hunspell_free_list(self._handle, ctypes.byref(pointer), count)

    def add(self, word: str) -> None:
        """Добавить слово в словарь на время работы (кнопка «не исправлять это слово»)."""
        _lib.Hunspell_add(self._handle, word.encode("utf-8"))
        self._known_cache.pop(word, None)

    def remove(self, word: str) -> None:
        _lib.Hunspell_remove(self._handle, word.encode("utf-8"))
        self._known_cache.pop(word, None)

    def close(self) -> None:
        if self._handle:
            _lib.Hunspell_destroy(self._handle)
            self._handle = None

    def __enter__(self) -> "SpellChecker":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


class SpellCheckers:
    """Набор словарей по раскладкам (ленивая загрузка)."""

    def __init__(self, layouts: list[str]):
        self._checkers: dict[str, SpellChecker | None] = {layout: None for layout in layouts}
        self._loaded = False

    def load(self) -> None:
        for layout in self._checkers:
            try:
                self._checkers[layout] = SpellChecker.for_layout(layout)
            except SpellCheckError:
                self._checkers[layout] = None
        self._loaded = True

    def for_layout(self, layout: str) -> SpellChecker | None:
        if not self._loaded:
            self.load()
        return self._checkers.get(layout)

    def known(self, layout: str, word: str) -> bool:
        checker = self.for_layout(layout)
        return bool(checker and checker.known(word))

    def add(self, layout: str, word: str) -> None:
        """Дописать слово в словарь раскладки (словарь hunspell неполон).

        Нужен для слов, которых нет в системном ``ru_RU``, но которые вы
        набираете каждый день: «перевёт», «столово». Без этого такие слова
        невозможно исправить — детектор их не узнаёт, а включать биграммы
        ради этого опасно: на истории они дали бы на каждый правильный ответ
        примерно столько же мусора.
        """
        checker = self.for_layout(layout)
        if checker is not None:
            checker.add(word)

    def close(self) -> None:
        seen: set[int] = set()
        for checker in self._checkers.values():
            if checker is not None and id(checker) not in seen:
                seen.add(id(checker))
                checker.close()

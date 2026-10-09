"""Буфер текущего слова.

Задача: в любой момент знать, какое слово набрано непосредственно перед
курсором и в какой раскладке. Из этого следует всё остальное — что стирать
Backspace'ом и что печатать заново.

Ключевое ограничение: мы не знаем содержимое чужого текстового поля. Значит
буфер нужно **сбрасывать** при любом действии, которое может сдвинуть курсор
или изменить текст мимо нас: стрелки, Home/End, мышь, горячие клавиши,
переключение окна. Иначе автозамена может стереть не то.
"""

from __future__ import annotations

from dataclasses import dataclass

# Разделители, после которых исправлять НЕЛЬЗЯ: эффект уже наступил (сообщение
# отправлено, команда выполнена, вкладка переключена) и отменить его нечем.
#
# Всё остальное — цифры, «/», «(», «@» и прочее — стирается и печатается заново
# без побочных эффектов, поэтому исправлять можно. Раньше здесь стоял белый
# список «безопасных» разделителей, и любой символ вне него (а это и «/», и
# цифры) отменял правку: за 12 отмен 4 были напрасными.
UNSAFE_SEPARATORS = frozenset({"\n", "\r", "\t", "\x1b"})

# Клавиши, которые гарантированно ломают наше представление о тексте перед курсором.
CARET_BREAKING = frozenset(
    {
        "KEY_LEFT",
        "KEY_RIGHT",
        "KEY_UP",
        "KEY_DOWN",
        "KEY_HOME",
        "KEY_END",
        "KEY_PAGEUP",
        "KEY_PAGEDOWN",
        "KEY_INSERT",
        "KEY_DELETE",
        "KEY_PRIOR",
        "KEY_NEXT",
    }
)


@dataclass(frozen=True)
class FinishedWord:
    """Слово, после которого пользователь ввёл разделитель."""

    text: str
    separator: str
    layout: str
    mutable: bool

    @property
    def separator_safe(self) -> bool:
        """Разделитель можно стереть и напечатать заново без побочных эффектов."""
        return self.separator not in UNSAFE_SEPARATORS

    @property
    def separator_unsafe(self) -> bool:
        """Enter/Tab/Escape: эффект уже наступил, отменить его нечем."""
        return self.separator in UNSAFE_SEPARATORS

    def __str__(self) -> str:
        return f"{self.text!r}+{self.separator!r} (раскладка {self.layout})"


class WordBuffer:
    """Набор символов последнего слова и признак «пользователь его правил»."""

    def __init__(self, max_length: int = 64):
        self.max_length = max_length
        self._chars: list[str] = []
        # Символы, которые могут оказаться как буквой, так и разделителем
        # (например «,» — это запятая, но и русская буква «б»). Держим их
        # отдельно, пока не станет ясно, что слово продолжается.
        self._pending: list[str] = []
        self._edited = False

    @property
    def text(self) -> str:
        return "".join(self._chars)

    @property
    def pending(self) -> str:
        """Отложенные символы, которые ещё неизвестно — буквы они или разделители."""
        return "".join(self._pending)

    @property
    def edited(self) -> bool:
        """Пользователь стирал символы в этом слове — исправлять не будем."""
        return self._edited

    @property
    def overflowed(self) -> bool:
        """Слово слишком длинное — вероятно, это не слово (URL, base64, случайный ввод)."""
        return len(self._chars) > self.max_length

    def __bool__(self) -> bool:
        return bool(self._chars)

    def __len__(self) -> int:
        return len(self._chars)

    def append(self, char: str) -> None:
        if len(self._chars) <= self.max_length:
            self._chars.append(char)

    def append_ambiguous(self, char: str) -> None:
        """Запомнить символ, который может быть буквой, а может — разделителем."""
        self._pending.append(char)

    def commit_pending(self) -> None:
        """Слово продолжается — отложенные символы оказались в нём буквами."""
        if self._pending:
            self._chars.extend(self._pending)
            self._pending.clear()

    def take_pending(self) -> str:
        """Забрать и очистить отложенные символы (для передачи наружу)."""
        if not self._pending:
            return ""
        text = "".join(self._pending)
        self._pending.clear()
        return text

    def backspace(self) -> bool:
        """Учесть Backspace. True — если было что стирать.

        Признак «пользователь правил слово» выставляем только когда действительно
        что-то стёрли. Иначе Backspace по пустому буферу (например сразу после
        автозамены, когда пользователь убирает введённое демоном слово) навсегда
        выключал бы автоправку для следующего слова.
        """
        if self._pending:
            self._pending.pop()
            self._edited = True
            return True
        if self._chars:
            self._chars.pop()
            self._edited = True
            return True
        return False

    def finish(self, separator: str, layout: str) -> FinishedWord | None:
        """Завершить слово разделителем и вернуть его (None, если слова не было)."""
        if not self._chars:
            self.reset()
            return None
        finished = FinishedWord(
            text=self.text,
            separator=separator,
            layout=layout,
            mutable=not self._edited,
        )
        self.reset()
        return finished

    def replace_with(self, text: str, layout: str) -> None:
        """Заменить содержимое буфера (после автозамены слово считается новым)."""
        self._chars = list(text)
        self._edited = False

    def reset(self) -> None:
        self._chars.clear()
        self._pending.clear()
        self._edited = False

    def __repr__(self) -> str:  # pragma: no cover - отладка
        return f"WordBuffer({self.text!r}, pending={self.pending!r}, edited={self._edited})"


def is_word_char(char: str) -> bool:
    """Является ли символ частью слова (буква)."""
    return bool(char) and len(char) == 1 and char.isalpha()


def is_digit(char: str) -> bool:
    return bool(char) and len(char) == 1 and char.isdigit()

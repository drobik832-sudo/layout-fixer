"""Определение «слово набрано не в той раскладке».

Логика намеренно консервативная: лучше пропустить ошибку, чем испортить
правильный текст. Решения принимаются в таком порядке:

1. слово есть в словаре текущей раскладки -> не трогаем;
2. слова нет в текущей, но есть в другой раскладке -> исправляем (сильный сигнал),
   если биграммы не говорят против (``bigram_veto``);
3. слова нет ни там, ни там -> сравниваем «похожесть на слово» по биграммам
   из словарей; исправляем только при заметном перевесе (порог ``bigram_margin``).

Дополнительно отбрасываются: слова с цифрами, слишком короткие/длинные,
аббревиатуры из списка исключений и слова, которые пользователь правил руками.
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .hunspell import SpellCheckers
from .xkb import LayoutTables

log = logging.getLogger(__name__)

# Границы слова участвуют в модели как обычные «символы» — так «ghbdtn»
# получает низкую оценку в русской модели (странные начала/концы слов).
BOUNDARY = "\x00"

# Сглаживание: чтобы нелогичные биграммы не давали -бесконечность.
SMOOTHING = 0.1

# Флаги аффиксов в .dic отделяются косой чертой: «мама/1е» -> «мама».
_AFFIX_FLAGS = re.compile(r"/.*$")
_NON_LETTERS = re.compile(r"[^\w]", re.UNICODE)

# Ниже этой длины слово не даёт улик: одна буква одинаково похожа на любой язык,
# поэтому «d» -> «в» по биграммам не подтверждается (перевес отрицательный).
MIN_EVIDENCE_LENGTH = 2

# Модели биграмм не меняются после сборки и зависят только от файла словаря,
# поэтому переиспользуются между экземплярами Detector: иначе каждый запуск
# демона и каждый автотест платили бы ~1.8 с на разбор .dic.
_BIGRAM_CACHE: dict[str, "BigramModel"] = {}


@dataclass(frozen=True)
class Decision:
    """Решение по одному слову."""

    word: str
    layout: str
    fix: bool
    confidence: float
    reason: str
    converted: str | None = None
    target_layout: str | None = None

    def __str__(self) -> str:
        if not self.fix:
            return f"{self.word!r} ({self.layout}): оставляю — {self.reason}"
        return (
            f"{self.word!r} ({self.layout}) -> {self.converted!r} "
            f"({self.target_layout}), уверенность {self.confidence:.2f} — {self.reason}"
        )


class BigramModel:
    """Оценка «похожести на слово» по частотам биграмм из словаря языка."""

    def __init__(self) -> None:
        self._counts: dict[str, int] = {}
        self._total = 0
        self._vocabulary = 0

    def add_word(self, word: str) -> None:
        text = f"{BOUNDARY}{word}{BOUNDARY}"
        for index in range(len(text) - 1):
            bigram = text[index : index + 2]
            self._counts[bigram] = self._counts.get(bigram, 0) + 1
            self._total += 1
        self._vocabulary = len(self._counts)

    @classmethod
    def from_dictionary(cls, path: Path) -> "BigramModel":
        model = cls()
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as exc:
            log.warning("не удалось прочитать словарь %s: %s", path, exc)
            return model
        # Первая строка .dic — количество слов, не слово.
        for line in lines[1:]:
            word = _AFFIX_FLAGS.sub("", line.strip())
            if len(word) < 2 or not word.isalpha():
                continue
            model.add_word(word.lower())
        log.debug("модель биграмм %s: %d биграмм по словарю", path.name, model._vocabulary)
        return model

    def score(self, text: str) -> float:
        """Средний логарифм вероятности биграмм: больше — «более похоже на слово»."""
        if not text:
            return float("-inf")
        sample = f"{BOUNDARY}{text.lower()}{BOUNDARY}"
        denominator = self._total + SMOOTHING * max(self._vocabulary, 1)
        total = 0.0
        count = 0
        for index in range(len(sample) - 1):
            bigram = sample[index : index + 2]
            probability = (self._counts.get(bigram, 0) + SMOOTHING) / denominator
            total += math.log(probability)
            count += 1
        return total / count if count else float("-inf")

    def __bool__(self) -> bool:
        return self._total > 0


class Detector:
    """Решает, надо ли исправлять слово, и во что именно."""

    def __init__(self, tables: LayoutTables, spell: SpellCheckers, config: Config):
        self.tables = tables
        self.spell = spell
        self.config = config
        self._ignore = {word.casefold() for word in config.ignore_words}

    # --- модель биграмм ---------------------------------------------------
    def _bigram(self, layout: str) -> BigramModel:
        model = _BIGRAM_CACHE.get(layout)
        if model is not None:
            return model
        checker = self.spell.for_layout(layout)
        if checker is None:
            _BIGRAM_CACHE[layout] = BigramModel()
            return _BIGRAM_CACHE[layout]
        path = Path("/usr/share/hunspell") / f"{checker.dictionary}.dic"
        log.info("загружаю модель биграмм для %r (%s)...", layout, path.name)
        _BIGRAM_CACHE[layout] = BigramModel.from_dictionary(path)
        return _BIGRAM_CACHE[layout]

    def preload_bigrams(self) -> None:
        """Заранее собрать модели биграмм для всех раскладок.

        Нужно и при ``use_bigram``, и для вето: иначе первая же правка
        подождёт ~1.8 с прямо посреди набора текста.
        """
        for layout in self.tables.layouts:
            self._bigram(layout)

    # --- основная логика --------------------------------------------------
    def decide(self, word: str, layout: str) -> Decision:
        """Оценить слово, набранное в раскладке ``layout``."""
        def no(reason: str) -> Decision:
            return Decision(word, layout, fix=False, confidence=0.0, reason=reason)

        short = len(word) < self.config.min_word_length
        if short and (
            not self.config.allow_short_words or len(word) < MIN_EVIDENCE_LENGTH
        ):
            return no("слишком короткое")
        if len(word) > self.config.max_word_length:
            return no("слишком длинное")
        # Слово может содержать символы, которые буквы только в другой раскладке
        # («j,yjdktybt» = «обновление»), поэтому проверяем не только isalpha.
        word_chars = self.tables.word_characters
        if any(not (char.isalpha() or char in word_chars) for char in word):
            return no("не только буквы")
        if word.casefold() in self._ignore:
            return no("слово в списке исключений")

        targets = [item for item in self.tables.layouts if item != layout]
        if not targets:
            return no("не с чем сравнивать — в системе одна раскладка")
        target = targets[0]

        converted = self.tables.convert(word, layout, target)
        if converted is None:
            return no("нет клавиш для конверсии")

        here = self.spell.known(layout, word.lower())
        there = self.spell.known(target, converted.lower())

        if here and not there:
            return no("слово есть в словаре текущей раскладки")
        if here and there:
            return no("слово есть в словарях обеих раскладок")
        if not here and there:
            # Обратное направление проверяем ПЕРВЫМ, до проверки длины: иначе
            # двухбуквенное «рф» -> «ha» прошло бы по мягкому правилу коротких
            # слов и обошло бы запрет.
            if target != self._primary_layout():
                reverse = self._decide_reverse(word, converted, layout, target)
                if reverse is not None:
                    return reverse
                return no("обратное направление: биграммы не подтверждают")
            if short:
                short_decision = self._decide_short(word, converted, layout, target)
                if short_decision is not None:
                    return short_decision
                return no("короткое слово, биграммы не подтверждают другой раскладку")
            vetoed = self._bigram_veto(word, converted, layout, target)
            if vetoed is not None:
                return no(vetoed)
            return Decision(
                word,
                layout,
                fix=True,
                confidence=0.95,
                reason="нет в словаре этой раскладки, зато есть слово в другой",
                converted=converted,
                target_layout=target,
            )

        # Ни там, ни там: решает модель биграмм (если включена).
        if not self.config.use_bigram:
            return no("нет ни в одном словаре, модель биграмм выключена")
        here_model = self._bigram(layout)
        there_model = self._bigram(target)
        if not here_model or not there_model:
            return no("нет ни в одном словаре, модели биграмм недоступны")
        score_here = here_model.score(word)
        score_there = there_model.score(converted)
        margin = score_there - score_here
        if margin >= self.config.bigram_margin:
            return Decision(
                word,
                layout,
                fix=True,
                confidence=0.6,
                reason=f"похоже на слово в другой раскладке (перевес биграмм {margin:.2f})",
                converted=converted,
                target_layout=target,
            )
        return no(f"нигде не найдено, перевес биграмм недостаточен ({margin:.2f})")

    def _primary_layout(self) -> str:
        """Раскладка, В которую исправляем.

        Из ``target_layout``, иначе последняя в списке системных раскладок. Всё
        остальное — обратное направление, и оно требует поддержки биграмм.
        """
        if self.config.target_layout:
            return self.config.target_layout
        return self.tables.layouts[-1] if self.tables.layouts else ""

    def _decide_reverse(
        self, word: str, converted: str, layout: str, target: str
    ) -> Decision | None:
        """Правка ОБРАТНОГО направления — только если биграммы её подтверждают.

        Словарь здесь обманывает: «рф» -> «ha» и «уда» -> «elf» словарём
        подтверждены, а вы их не набирали. Замер за два дня: нужные правки
        (латиница под русской раскладкой) давали перевес +3.21…+5.50,
        ложные — +2.35…-0.52, поэтому порог стоит между ними.
        """
        here_model = self._bigram(layout)
        there_model = self._bigram(target)
        if not here_model or not there_model:
            return None
        margin = there_model.score(converted) - here_model.score(word)
        if margin < self.config.reverse_bigram_margin:
            log.debug(
                "обратное направление %r (%s -> %s): перевес %.2f — не подтверждено",
                word, layout, target, margin,
            )
            return None
        log.debug("обратное направление %r: перевес %.2f", word, margin)
        return Decision(
            word,
            layout,
            fix=True,
            confidence=0.7,
            reason=f"обратное направление подтверждено биграммами (перевес {margin:.2f})",
            converted=converted,
            target_layout=target,
        )

    def _decide_short(
        self, word: str, converted: str, layout: str, target: str
    ) -> Decision | None:
        """Решение по слову короче ``min_word_length`` — предлоги и союзы.

        «на», «не», «но», «за», «по», «из» — самые частые русские слова и самые
        частые опечатки: в неверной раскладке это «yf», «yt», «yj». Раньше они
        отсекались порогом длины и не исправлялись никогда.

        Одного словаря мало: «ide» -> «шву» тоже подтверждено словарём. Поэтому
        требуем биграммы, но с мягким порогом: у двухбуквенного слова всего три
        биграммы против одиннадцати у длинного, и перевес в 2.5 для него недостижим.
        Достаточно, чтобы целевая раскладка выглядела хоть немного вероятнее.
        """
        here_model = self._bigram(layout)
        there_model = self._bigram(target)
        if not here_model or not there_model:
            return None
        margin = there_model.score(converted) - here_model.score(word)
        if margin <= 0:
            log.debug("короткое слово %r (%s): перевес биграмм %.2f — не подтверждено", word, layout, margin)
            return None
        log.debug("короткое слово %r (%s): перевес биграмм %.2f", word, layout, margin)
        return Decision(
            word,
            layout,
            fix=True,
            confidence=0.6,
            reason=f"короткое слово, подтверждено биграммами (перевес {margin:.2f})",
            converted=converted,
            target_layout=target,
        )

    def _bigram_veto(
        self, word: str, converted: str, layout: str, target: str
    ) -> str | None:
        """Отменить словарную правку, если биграммы явно против неё.

        Правило №2 смотрит только в словари и модель не спрашивает, поэтому
        короткий латинский токен исправляется безусловно: «ide» не в ``en_US``,
        а «шву» в ``ru_RU`` есть — значит «переключить». На деле биграммы были
        против (us -5.145 против ru -7.896): «ide» выглядит куда больше
        английским словом, чем «шву» русским.

        Возвращает причину отмены или ``None``, если править можно.
        """
        if not self.config.bigram_veto:
            return None
        here_model = self._bigram(layout)
        there_model = self._bigram(target)
        if not here_model or not there_model:
            return None
        margin = here_model.score(word) - there_model.score(converted)
        if margin >= self.config.bigram_margin:
            return (
                f"биграммы говорят в пользу текущей раскладки (перевес {margin:.2f})"
            )
        return None

    def explain(self, word: str, layout: str) -> str:
        """Разбор слова по шагам — для отладки и настройки порогов."""
        lines = [f"Слово {word!r} в раскладке {layout!r}:"]
        others = [item for item in self.tables.layouts if item != layout]
        if not others:
            lines.append("  в системе одна раскладка — сравнивать не с чем")
        else:
            other = others[0]
            converted = self.tables.convert(word, layout, other)
            lines.append(f"  конверсия в {other!r}: {converted!r}")
            lines.append(f"  слово в словаре {layout!r}: {self.spell.known(layout, word.lower())}")
            if converted:
                lines.append(
                    f"  конверсия в словаре {other!r}: {self.spell.known(other, converted.lower())}"
                )
            here_model = self._bigram(layout)
            there_model = self._bigram(other)
            if here_model and there_model and converted:
                lines.append(
                    f"  оценка биграмм: {layout!r} {here_model.score(word):.3f} "
                    f"vs {other!r} {there_model.score(converted):.3f}"
                )
        lines.append(f"  решение: {self.decide(word, layout)}")
        return "\n".join(lines)

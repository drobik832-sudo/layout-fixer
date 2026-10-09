"""Применение исправления: стереть слово и напечатать его в другой раскладке.

Последовательность ровно такая, какую ждёт пользователь:
    1. Backspace на длину набранного слова (плюс разделитель, если он уже ушёл в приложение);
    2. переключение раскладки (если нужно) с паузой на обработку компоновщиком;
    3. печать правильного слова и разделителя;
    4. возврат раскладки в ту, в которой пользователь печатал.

Важно: разделитель уже доставлено приложению (мы реагируем на нажатие пробела),
поэтому его приходится стирать и печатать заново. Запрещены только Enter, Tab и
Escape — у них есть побочный эффект, который нечем отменить. Остальное
(цифры, «/», «(» и прочее) стирается и печатается заново безопасно.
"""

from __future__ import annotations

import logging

from .buffer import UNSAFE_SEPARATORS
from .config import Config
from .detector import Decision
from .injector import Injector
from .keys import ModifierState
from .layout_state import LayoutState
from .xkb import LayoutTables

log = logging.getLogger(__name__)


class Fixer:
    """Исполнитель: превращает решение детектора в реальные нажатия."""

    def __init__(
        self,
        injector: Injector,
        tables: LayoutTables,
        state: LayoutState,
        config: Config,
        mods: ModifierState | None = None,
    ):
        self.injector = injector
        self.tables = tables
        self.state = state
        self.config = config
        # Состояние клавиатуры нужно в момент инъекции, а не в момент решения:
        # между нием пользователь успевает нажать Shift или включить Caps Lock.
        self.mods = mods
        self.fixes = 0

    def _separator_to_type(
        self, separator: str, source_layout: str, target_layout: str
    ) -> str | None:
        """Разделитель в том виде, в каком его можно напечатать в целевой раскладке.

        Символа «&» в русской раскладке вообще нет: на этой клавише в русской
        «?». Если вы нажали русский «?», а раскладка была английская, то после
        переключения печатать надо «?» — ровно как слово. Без этого правка
        отменяется и слова с вопросительным знаком не исправляются никогда.

        ``None`` — разделитель нечем напечатать ни так, ни иначе.
        """
        if not separator:
            return ""
        if self.tables.to_key(target_layout, separator) is not None:
            return separator
        converted = self.tables.convert(separator, source_layout, target_layout)
        if converted and self.tables.to_key(target_layout, converted) is not None:
            # Только если в целевой раскладке это тоже не буква. «[» в русской
            # раскладке даёт «Х», но пользователь нажал «[» как знак, и
            # подставлять ему букву нельзя. А «&» -> «?» — тот же знак на
            # другой клавише, вот это подставляем.
            if converted.isalpha():
                return None
            log.debug(
                "разделитель %r в %r не существует, печатаю %r",
                separator, target_layout, converted,
            )
            return converted
        return None

    def _modifier_problem(self) -> str | None:
        """Почему сейчас нельзя печатать: инъекция идёт в «чистую» клавиатуру.

        Решение принимается по ``word.lower()``, поэтому зажатый Shift или
        включённый Caps Lock молча испортят регистр: демон решил напечатать
        «привет», а на экране окажется «ПРИВЕТ». Ctrl/Alt/Super опаснее —
        ими Backspace и буквы превращаются в горячие клавиши.
        """
        if self.mods is None:
            return None
        if self.mods.caps:
            return "включён Caps Lock — инъекция изменит регистр"
        if self.mods.shift:
            return "зажат Shift — инъекция изменит регистр"
        if self.mods.command:
            return "зажат Ctrl/Alt/Super — инъекция станет горячей клавишей"
        return None

    def is_modifier_block(self, reason: str) -> bool:
        """Отказ вызван ЗАЖАТЫМ модификатором — его можно переждать.

        Caps Lock сюда не входит: его не отпускают, а переключают, так что ждать
        нечего. Остальные отказы (Enter, непечатаемый разделитель) переждать
        нельзя, их надо записывать как отмену сразу.
        """
        return "зажат" in reason

    def can_apply(self, decision: Decision, separator: str) -> tuple[bool, str]:
        """Можно ли применить исправление (безопасность важнее полноты)."""
        if not decision.fix or not decision.converted or not decision.target_layout:
            return False, "решение не требует исправления"
        problem = self._modifier_problem()
        if problem is not None:
            return False, problem
        if separator and separator in UNSAFE_SEPARATORS:
            return False, f"разделитель {separator!r} — Enter/Tab/Escape, эффект уже наступил"
        if len(separator) > 1:
            # apply() стирает ровно len(separator) символов Backspace'ом.
            return False, f"разделитель {separator!r} — не один символ, стереть его нечем"
        if decision.target_layout not in self.tables.layouts:
            return False, f"раскладка {decision.target_layout!r} недоступна"
        for char in decision.converted:
            if self.tables.to_key(decision.target_layout, char) is None:
                return False, f"символ {char!r} нельзя напечатать в {decision.target_layout!r}"
        if self._separator_to_type(separator, decision.layout, decision.target_layout) is None:
            return False, f"разделитель {separator!r} нельзя напечатать в {decision.target_layout!r}"
        return True, ""

    def apply(self, decision: Decision, separator: str) -> bool:
        """Стереть слово с разделителем и напечатать исправленный вариант."""
        allowed, why = self.can_apply(decision, separator)
        if not allowed:
            log.info("исправление отменено: %s", why)
            return False

        assert decision.converted and decision.target_layout  # проверено в can_apply
        separator = self._separator_to_type(
            separator, decision.layout, decision.target_layout
        )
        assert separator is not None
        # Раскладка, в которой пользователь печатал: возвращаем её только если
        # включено restore_layout, по умолчанию остаёмся в целевой.
        original = self.state.current
        # Стираем: слово + разделитель (разделитель уже ушёл в приложение).
        self.injector.backspace(len(decision.word) + len(separator))
        switched = self.state.ensure(decision.target_layout)
        skipped = self.injector.type_text(decision.converted + separator, decision.target_layout)
        if skipped:
            log.warning("не удалось напечатать символы: %s", skipped)
        if switched and self.config.restore_layout:
            self.state.ensure(original)
            log.debug("раскладка возвращена в %s", original)
        self.fixes += 1
        log.info(
            "исправлено: %r (%s) -> %r (%s)",
            decision.word,
            decision.layout,
            decision.converted,
            decision.target_layout,
        )
        return True

"""Конфигурация демона: файл ``~/.config/layout-fixer/config.toml``.

Все значения по умолчанию подобраны консервативно: демон сначала только
наблюдает и пишет в лог, что бы он исправил. Автозамена включается осознанно.
"""

from __future__ import annotations

import logging
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

log = logging.getLogger(__name__)

CONFIG_PATH = Path.home() / ".config" / "layout-fixer" / "config.toml"

# Режим работы:
#   observe — только логировать решения (безопасно, для оценки ложных срабатываний);
#   fix     — реально исправлять раскладку.
MODES = ("observe", "fix")

DEFAULT_CONFIG_TEXT = """\
# layout-fixer — автоисправление раскладки клавиатуры
#
# mode:
#   "observe" — только писать в лог, что БЫЛО БЫ исправлено (безопасный режим);
#   "fix"     — реально исправлять.
mode = "observe"

# Пустой список = взять раскладки из настроек GNOME.
layouts = []

# Клавиша, которой демон переключает раскладку (xkb-опция grp:*_toggle).
# Должна быть настроена в xkb-options — см. scripts/setup-actuator.sh
actuator = "scrolllock"

# Как пользователь переключает раскладку сам.
#   "alt_tap" — одиночный тап левого Alt (но НЕ внутри комбинаций);
#   "none"    — не перехватывать, только системные средства.
trigger = "alt_tap"
alt_tap_max_ms = 350

# Исправлять слово по паузе в наборе, а не только по разделителю. Демон ждёт
# паузу idle_fix_ms и правит слово под курсором, даже если вы не нажали
# пробел. Это единственный способ закрыть случай «слово + Enter»: к моменту,
# когда Enter дойдёт до приложения, слово уже должно быть правильным.
# 9 отмен из 13 за всю историю были именно из-за Enter.
#
# Цена: при медленном наборе пауза может случиться в середине слова — тогда
# демон разберёт его на части и не исправит. 0 = выключить (только разделитель).
idle_fix_ms = 600

# Пороги детектора.
min_word_length = 3
max_word_length = 32
# Проверять слова короче min_word_length («yf» -> «на», «yt» -> «не»). Это самые
# частые русские слова и самые частые опечатки, но и самые опасные: «ide» ->
# «шву» тоже подтверждено словарём. Поэтому для коротких слов требуется ещё и
# поддержка биграмм, просто с мягким порогом — на двух буквах перевес в
# bigram_margin недостижим. Слова короче двух букв не трогаем вовсе.
allow_short_words = true

# Раскладка, В которую исправляем. Пусто = в обе стороны (старое поведение).
# Задайте целевой раскладкой, чтобы обратное направление отключилось: если
# русская раскладка включена сознательно, демон не должен переписывать
# набранное в латиницу. Замер за два дня: us->ru 24 правки и одно ложное
# срабатывание, ru->us 6 правок и два ложных.
target_layout = ""

# Обратное направление (латиница, набранная в русской раскладке) по умолчанию
# разрешено, но требует поддержки биграмм. Правила словаря тут недостаточно:
# «рф» -> «ha» тоже подтверждено словарём, а это не то, что вы набирали.
# Замер за два дня: нужные правки давали перевес +3.21…+5.50, ложные +2.35…-0.52.
# Порог 3.0 попадает в разрыв между ними. Меньше — осторожнее (меньше правок).
reverse_bigram_margin = 3.0
# Разрешать исправление слов, которых НЕТ ни в одном словаре, «по похожести»
# (биграммы). Поймает имена и сленг в неверной раскладке, но может ошибиться:
# абракадабра превратится в другую абракадабру. По умолчанию выключено —
# исправляем только то, что подтверждено словарём.
use_bigram = false
# Порог перевеса биграмм (имеет смысл только при use_bigram = true):
# больше порог — меньше ложных срабатываний, но и меньше находок.
bigram_margin = 2.5
# Спрашивать биграммы перед правкой, подтверждённой словарём. Правило «нет в
# текущем словаре, но есть в другом» само по себе слепо: короткий латинский
# токен вроде «ide» превращался в «шву», хотя биграммы были против
# (us -5.145 против ru -7.896). Вето срабатывает, когда текущая раскладка
# выигрывает у другой на bigram_margin. Не требует use_bigram.
bigram_veto = true

# Тайминги инъекции (мс).
key_delay_ms = 2
settle_ms = 80

# Возвращать раскладку после исправления: демон переключает её, чтобы напечатать
# слово правильно, и возвращает ту, что была до правки. Иначе после каждой
# исправленной опечатки приходится жать Alt, чтобы продолжить в той раскладке,
# в которой вы печатали. false — оставлять раскладку на целевой.
restore_layout = true

# Устройства ввода, чьи события не считать пользовательским вводом. Синтетика
# (AI-инструменты печатают через ydotool) не должна ни попадать в детектор, ни
# вызывать переключение раскладки.
ignore_devices = ["ydotoold virtual device"]

# Интервал периодической проверки раскладки (сек). 0 = выключить.
layout_refresh_interval = 2

# Слова, которых нет в системном словаре hunspell, но которые вы набираете.
# Системный ru_RU неполон: «перевёт», «столово», «рдай» в нём отсутствуют,
# поэтому такие слова демон определить не может и не исправляет. Дописываем
# их сами — точнее биграмм, которые на этой же истории дали бы на каждый
# верный ответ столько же мусора.
extra_words = []

# Слова, которые никогда не исправлять (сленг, имена, аббревиатуры).
ignore_words = []


# Журнал решений: по нему команда `layout-fixer report` строит отчёт.
# Пусто = ~/.local/state/layout-fixer/decisions.log
# Отключить совсем: decisions_log = "-"
decisions_log = ""

# Писать ли в журнал сам текст слов (можно выключить ради приватности).
log_words = true
"""


@dataclass
class Config:
    mode: str = "observe"
    layouts: list[str] = field(default_factory=list)
    actuator: str = "scrolllock"
    trigger: str = "alt_tap"
    alt_tap_max_ms: int = 350
    idle_fix_ms: int = 600
    min_word_length: int = 3
    max_word_length: int = 32
    allow_short_words: bool = True
    target_layout: str = ""
    reverse_bigram_margin: float = 3.0
    use_bigram: bool = False
    bigram_margin: float = 2.5
    bigram_veto: bool = True
    key_delay_ms: int = 2
    settle_ms: int = 80
    restore_layout: bool = False
    ignore_devices: list[str] = field(default_factory=lambda: ["ydotoold virtual device"])
    layout_refresh_interval: int = 2
    ignore_words: list[str] = field(default_factory=list)
    extra_words: list[str] = field(default_factory=list)
    decisions_log: str = ""
    log_words: bool = True

    # --- производные значения -------------------------------------------
    @property
    def key_delay(self) -> float:
        return self.key_delay_ms / 1000.0

    @property
    def settle(self) -> float:
        return self.settle_ms / 1000.0

    @property
    def alt_tap_max(self) -> float:
        return self.alt_tap_max_ms / 1000.0

    @property
    def idle_fix(self) -> float:
        """Пауза в наборе, после которой слово можно исправлять (0 — выключено)."""
        return self.idle_fix_ms / 1000.0

    @property
    def layout_refresh(self) -> float:
        return float(self.layout_refresh_interval)

    @property
    def fixing_enabled(self) -> bool:
        return self.mode == "fix"

    def validate(self) -> list[str]:
        """Проверить значения; вернуть список проблем (пустой = всё хорошо)."""
        problems: list[str] = []
        if self.mode not in MODES:
            problems.append(f"mode={self.mode!r}, допустимо: {', '.join(MODES)}")
        if self.trigger not in ("alt_tap", "none"):
            problems.append(f"trigger={self.trigger!r}, допустимо: alt_tap, none")
        if self.min_word_length < 1:
            problems.append("min_word_length должен быть >= 1")
        if self.max_word_length < self.min_word_length:
            problems.append("max_word_length меньше min_word_length")
        if self.settle_ms < 0 or self.key_delay_ms < 0:
            problems.append("тайминги не могут быть отрицательными")
        if self.layout_refresh_interval < 0:
            problems.append("layout_refresh_interval не может быть отрицательным")
        return problems

    @classmethod
    def load(cls, path: Path = CONFIG_PATH) -> "Config":
        """Прочитать конфиг; при отсутствии — создать файл с комментариями."""
        if not path.exists():
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(DEFAULT_CONFIG_TEXT, encoding="utf-8")
                log.info("создан файл конфигурации: %s", path)
            except OSError as exc:
                log.warning("не удалось создать %s: %s (использую значения по умолчанию)", path, exc)
            return cls()

        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            log.error("не удалось прочитать %s: %s (использую значения по умолчанию)", path, exc)
            return cls()

        known = {item.name for item in fields(cls)}
        unknown = set(data) - known
        if unknown:
            log.warning("в конфиге неизвестные ключи: %s", ", ".join(sorted(unknown)))

        config = cls(**{key: value for key, value in data.items() if key in known})
        problems = config.validate()
        for problem in problems:
            log.error("конфиг: %s", problem)
        return config

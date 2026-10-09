"""Состояние текущей раскладки и её переключение.

ПОЧЕМУ ДЕМОН ВЕДЁТ СОСТОЯНИЕ САМ
------------------------------
В GNOME 46 на Wayland нет способа достоверно прочитать активную раскладку
снаружи (проверено на этой машине):

* ключ ``org.gnome.desktop.input-sources current`` помечен DEPRECATED и
  игнорируется («This key is deprecated and ignored»);
* ``org.gnome.Shell.Eval`` возвращает ``(false, '')`` — unsafe mode выключен;
* ``ibus engine`` не отражает раскладку Shell (возвращал ``xkb:us::eng``,
  когда ``mru-sources`` показывал ``ru``);
* AT-SPI не выставляет ``checked``/``selected`` у пунктов меню раскладок,
  поэтому активную раскладку этим способом не определить.

ЧТО ПОДТВЕРДИЛОСЬ НА ПРАКТИКЕ (2026-09-16)
-------------------------------------------
``mru-sources`` действительно отслеживает активную раскладку: после инъекции
левого Alt (актуатор ``grp:lalt_toggle``) значение сменилось с
``[('xkb','us'), ('xkb','ru')]`` на ``[('xkb','ru'), ('xkb','us')]``, то есть
первый элемент списка = текущая раскладка. Значит есть и чтение, и — через
D-Bus-сигнал ``changed::mru-sources`` — канал синхронизации на случай, если
раскладку переключили мимо демона (клик по индикатору и т.п.).

Переключение выполняется инъекцией «актуатора» — клавиши, на которую заведена
xkb-опция ``grp:*_toggle`` (семантика ``LockGroup(group=+1)``: циклический
переход к следующей группе — ровно как в списке ``sources``).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from evdev import ecodes as e

from .gnome_settings import mru_layouts, read_xkb_options, xkb_group_index
from .injector import Injector

log = logging.getLogger(__name__)

# Пауза после переключения раскладки: компоновщик обрабатывает смену группы
# асинхронно, и без этой паузы первый символ может уйти в старую раскладку.
SETTLE_DELAY = 0.08

# Сколько после переключения не верить mru-sources: замерено 93–487 мс
# (медиана 272), берём 700 мс с запасом.
MRU_STALE_DELAY = 0.7


@dataclass(frozen=True)
class Actuator:
    """Клавиша, на которую заведено переключение раскладки в xkb."""

    name: str
    code: int
    xkb_option: str
    description: str = ""

    def __str__(self) -> str:
        return f"{self.name} ({e.KEY[self.code]}, xkb-опция {self.xkb_option})"


# Кандидаты в актуаторы. Все опции существуют в xkeyboard-config этой системы
# (проверено по /usr/share/X11/xkb/rules/base.lst) и имеют семантику «toggle».
ACTUATORS: dict[str, Actuator] = {
    "leftalt": Actuator("leftalt", e.KEY_LEFTALT, "grp:lalt_toggle", "левый Alt (текущая настройка системы)"),
    "scrolllock": Actuator("scrolllock", e.KEY_SCROLLLOCK, "grp:sclk_toggle", "Scroll Lock"),
    "menu": Actuator("menu", e.KEY_MENU, "grp:menu_toggle", "клавиша Menu"),
    "rightctrl": Actuator("rightctrl", e.KEY_RIGHTCTRL, "grp:rctrl_toggle", "правый Ctrl"),
    "capslock": Actuator("capslock", e.KEY_CAPSLOCK, "grp:caps_toggle", "Caps Lock"),
}

DEFAULT_ACTUATOR = "leftalt"


def get_actuator(name: str) -> Actuator:
    try:
        return ACTUATORS[name]
    except KeyError:
        raise ValueError(f"неизвестный актуатор {name!r}; доступны: {', '.join(ACTUATORS)}") from None


def configured_actuator() -> Actuator | None:
    """Какой актуатор настроен в xkb-опциях системы (если настроен).

    Опций ``grp:*_toggle`` в системе может быть несколько, и это нормально:
    каждая привязывает СВОЮ клавишу к переключению раскладки, они не спорят
    друг с другом. Так что ``sclk_toggle`` (демон подставляет Scroll Lock) и
    ``lalt_toggle`` (пользователь жмёт Alt) спокойно живут вместе, а
    ``configured_actuator`` возвращает первое по порядку ``ACTUATORS``.

    Кого на самом деле переключает раскладку — проверяет
    ``Daemon._log_actuator_mismatch``: важно, чтобы актуатор ИЗ КОНФИГА был
    среди настроенных, а не совпадал с первым.
    """
    options = set(read_xkb_options())
    for actuator in ACTUATORS.values():
        if actuator.xkb_option in options:
            return actuator
    return None


def all_configured_actuators() -> list[Actuator]:
    """Все актуаторы, которые реально настроены в системе (может быть несколько)."""
    options = set(read_xkb_options())
    return [actuator for actuator in ACTUATORS.values() if actuator.xkb_option in options]


class LayoutState:
    """Текущая раскладка: счётчик демона + переключение инъекцией актуатора."""

    def __init__(
        self,
        layouts: list[str],
        actuator: Actuator,
        injector: Injector | None = None,
        current: str | None = None,
        settle: float = SETTLE_DELAY,
        tracked_actuator_codes: set[int] | None = None,
        mru_stale: float = MRU_STALE_DELAY,
        group_reader: Callable[[list[str]], int | None] | None = None,
    ):
        if not layouts:
            raise ValueError("список раскладок пуст")
        self.layouts = layouts
        self.actuator = actuator  # актуатор, которым демон переключает сам
        if tracked_actuator_codes is not None:
            self._actuator_codes = tracked_actuator_codes
        else:
            self._actuator_codes = {a.code for a in all_configured_actuators()}  # все актуаторы в системе
            if not self._actuator_codes:
                # Фоллбэк: если в xkb-options ничего нет, считаем что переключает заданный актуатор
                self._actuator_codes = {actuator.code}
        self.injector = injector
        self.settle = settle
        # Сколько после переключения не верить mru-sources (см. refresh).
        self.mru_stale = mru_stale
        self._mru_ignore_until = 0.0
        # Источник истины для refresh()/ensure(). По умолчанию — реальный
        # X-сервер; тесты подменяют, чтобы не зависеть от машины, где идут тесты.
        self._group_reader = group_reader or xkb_group_index
        if current is not None and current not in layouts:
            raise ValueError(f"раскладка {current!r} отсутствует в списке {layouts}")
        self._index = layouts.index(current) if current is not None else 0

    # --- состояние ------------------------------------------------------
    @property
    def current(self) -> str:
        return self.layouts[self._index]

    @property
    def other(self) -> str:
        """Следующая раскладка (для двух раскладок — «вторая»)."""
        return self.layouts[(self._index + 1) % len(self.layouts)]

    def set_current(self, layout: str) -> None:
        """Задать раскладку БЕЗ инъекции — только для синхронизации состояния."""
        self._index = self.layouts.index(layout)

    def refresh(self) -> str:
        """Привести состояние в соответствие с реальной раскладкой.

        Источник истины — индикатор группы у X-сервера (см.
        ``xkb_group_index``), а не ``mru-sources``: тот обновляется с задержкой
        26–329 мс, и в это окно демон успевал поверить устаревшему значению.

        Раньше тут стоял запрет доверять ``mru-sources`` 700 мс после
        переключения. Он лечил симптом, но ломал настоящее переключение:
        клик по индикатору раскладки не даёт клавиши, и пока демон «ждал»,
        он не замечал, что человек ушёл в английскую, — и разбирал следующие
        слова не в той раскладке.
        """
        index = self._group_reader(self.layouts)
        if index is not None:
            self.set_current(self.layouts[index])
            return self.current
        try:
            mru = mru_layouts()
        except Exception as exc:
            log.warning("не удалось прочитать mru-sources: %s", exc)
            return self.current
        for layout in mru:
            if layout in self.layouts:
                if layout != self.current:
                    log.info("синхронизация раскладки: %s -> %s", self.current, layout)
                self.set_current(layout)
                break
        return self.current

    def _mark_switched(self) -> None:
        """Пометить, что только что было переключение: mru-sources ещё устарел."""
        self._mru_ignore_until = time.monotonic() + self.mru_stale

    def mru_stale_guard_active(self) -> bool:
        """Текущий момент находится в окне, когда mru-sources гарантированно устарел."""
        return time.monotonic() < self._mru_ignore_until

    # --- переключение ---------------------------------------------------
    def toggle(self) -> str:
        """Переключить раскладку в системе (инъекцией актуатора) и в своём состоянии."""
        if self.injector is None:
            raise RuntimeError("для переключения раскладки нужен Injector")
        log.debug("переключение раскладки инъекцией %s: %s -> ...", self.actuator.name, self.current)
        self.injector.tap(self.actuator.code)
        if self.settle:
            time.sleep(self.settle)
        self._index = (self._index + 1) % len(self.layouts)
        log.debug("раскладка переключена: %s", self.current)
        self._mark_switched()
        return self.current

    def toggle_local(self) -> str:
        """Учесть переключение, сделанное пользователем вручную (без инъекции)."""
        self._index = (self._index + 1) % len(self.layouts)
        self._mark_switched()
        return self.current

    def ensure(self, layout: str) -> bool:
        """Гарантировать нужную раскладку. True — если пришлось переключить.

        Перед каждым шагом сверяемся с реальной группой XKB, а не со своим
        индексом. Индекс — это bookkeeping, и он расходился с реальностью:
        демон считал, что работает в английской, нажимал ScrollLock, и русская
        раскладка уезжала в ту же английскую вместо того, чтобы остаться.
        Из-за этого правка печаталась в чужой раскладке, а после неё оставалось
        не то, что ожидалось.

        Теперь решение принимается по факту: сначала спрашиваем X-сервер,
        переключаем только если он действительно не там, и после шага снова
        сверяемся. Расхождение самоочищается за один вызов.
        """
        if layout not in self.layouts:
            raise ValueError(f"раскладка {layout!r} отсутствует в списке {self.layouts}")
        self.refresh()
        if self.current == layout:
            return False
        # Переключаем по одному шагу: xkb-опция с LockGroup(+1) умеет только
        # «следующая группа», произвольный переход не поддерживается.
        for _ in range(len(self.layouts)):
            self.toggle()
            self.refresh()
            if self.current == layout:
                return True
        log.warning(
            "не удалось переключиться на %r (после %d шагов в %r)",
            layout, len(self.layouts), self.current,
        )
        return False

    def handle_event(self, code: int, value: int) -> bool:
        """Учесть нажатие актуатора самой рукой пользователя (value=1 — нажатие).

        Возвращает True, если событие относится к актуатору.
        Отслеживаем ЛЮБУЮ клавишу, у которой в xkb-options есть grp:*_toggle.
        """
        if code not in self._actuator_codes or value != 1:
            return False
        self.toggle_local()
        return True

    def __str__(self) -> str:
        return f"{self.current} (из {', '.join(self.layouts)}), актуатор: {self.actuator.name}"

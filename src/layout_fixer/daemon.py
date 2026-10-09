"""Демон layout-fixer: читает клавиатуру, распознаёт неверную раскладку, исправляет.

Устройство:

    evdev (чтение)         -> слова набираются в буфере
    детектор (словари)     -> решение «это не та раскладка»
    uinput (инъекция)      -> стереть и напечатать заново
    mru-sources (GLib)     -> синхронизация, если раскладку сменили мимо нас

Два режима (см. config.py):
    observe — только писать в лог, что БЫЛО БЫ исправлено;
    fix     — реально исправлять.

Обратите внимание на обработку Alt: одиночный тап переключает раскладку, но
комбинация (Alt+Tab) — нет. Это то, чего не умеет xkb-опция ``grp:lalt_toggle``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from evdev import ecodes as e

from .buffer import CARET_BREAKING, UNSAFE_SEPARATORS, WordBuffer
from .config import Config
from .decisions_log import DEFAULT_PATH as DECISIONS_LOG_PATH
from .decisions_log import DecisionLog
from .detector import Decision, Detector
from .fixer import Fixer
from .gnome_settings import (
    MruWatcher,
    read_xkb_options,
    xkb_group_index,
    xkb_layout_ids,
)
from .hunspell import SpellCheckers
from .injector import VIRTUAL_DEVICE_NAME, Injector
from .keys import MODIFIER_KEYS, ModifierState
from .layout_state import Actuator, LayoutState, get_actuator, all_configured_actuators
from .reader import KeyboardReader, PointerWatcher
from .x11focus import ActiveWindow
from .xkb import LayoutTables

log = logging.getLogger(__name__)

# Клавиши-разделители, после которых слово точно закончилось.
WORD_SEPARATORS_UNSAFE = {
    e.KEY_ENTER: "\n",
    e.KEY_KPENTER: "\n",
    e.KEY_TAB: "\t",
}


REVERT_WINDOW_S = 30.0

# Клик мышью делает позицию курсора ненадёжной, но только пока вы не отпечатали
# после него целое слово. Дальше курсор точно после слова и клик не мешает.
CARET_CLICK_WINDOW_S = 5.0


@dataclass
class _InjectedWord:
    """Исправление, только что введённое на экран, — ждём, не сотрёт ли его пользователь."""

    decision: Decision
    text: str  # что появилось на экране, вместе с разделителем
    word_length: int  # сколько символов занимает само слово
    at: float
    erased: int = 0


class Daemon:
    """Связывает чтение клавиатуры, детектор и исполнителя в один цикл."""

    def __init__(self, config: Config, injector: Injector | None = None):
        self.config = config
        layouts = config.layouts or xkb_layout_ids() or ["us", "ru"]
        self.tables = LayoutTables(layouts)
        self.spell = SpellCheckers(self.tables.layouts)
        # Дописываем недостающие слова до Detector: он держит ссылку на spell.
        self._load_extra_words(config)
        self.detector = Detector(self.tables, self.spell, config)
        # Вето по биграммам работает и при выключенном use_bigram, как и проверка
        # коротких слов, а первая же словарная правка иначе ждала бы сборку .dic
        # посреди набора текста.
        if config.use_bigram or config.bigram_veto or config.allow_short_words:
            self.detector.preload_bigrams()

        actuator = get_actuator(config.actuator)
        # Инъектор можно подменить (тесты подставляют записывающий вместо uinput).
        self.injector = injector or Injector(key_delay=config.key_delay, tables=self.tables)
        # Отслеживаем нажатия ЛЮБОГО системного актуатора (grp:*_toggle), а не только своего.
        tracked_codes = {a.code for a in all_configured_actuators()}
        if not tracked_codes:
            tracked_codes = {actuator.code}
        self.state = LayoutState(
            self.tables.layouts,
            actuator,
            injector=self.injector,
            settle=config.settle,
            tracked_actuator_codes=tracked_codes,
            normalize_groups=self._normalize_and_read_group,
        )
        # Наша виртуальная клавиатура появляется в системе, и xf86-input-evdev
        # на каждое новое устройство добавляет группу XKB. Список раздувается
        # до «us, ru, ru», и ScrollLock начинает циклить
        # us -> ru -> ru(дубль) -> us: нажатие из русской выглядит как «ничего
        # не произошло», а индикатор группы отвечает не на тот вопрос. Демон
        # читает раскладку неверно, жмёт ScrollLock не вовремя, и русская
        # раскладка уезжает в английскую. Поэтому дубль снимается сразу после
        # создания инжектора, а не выясняется потом по таймеру.
        self._normalize_and_read_group()

        self.mods = ModifierState()
        self.fixer = Fixer(self.injector, self.tables, self.state, config, mods=self.mods)

        self.buffer = WordBuffer(max_length=config.max_word_length + 8)
        self.decisions = self._make_decision_log(config)

        self._loop: asyncio.AbstractEventLoop | None = None
        self._paused = False
        self._alt_press: float | None = None
        self._alt_used = False
        self._revert: _InjectedWord | None = None
        self._caret_maybe_moved = False
        self._caret_click_at = 0.0
        self._last_char_at = 0.0
        self._pending_fix: tuple[Decision, str] | None = None
        self._buffer_changed_at = time.monotonic()
        self._focus = ActiveWindow()
        self._stats = {
            "слов": 0,
            "решений исправлять": 0,
            "исправлено": 0,
            "исправлено по паузе": 0,
            "отменено": 0,
            "стёрто пользователем": 0,
        }

    def _load_extra_words(self, config: Config) -> None:
        """Дописать в словари слова, которых в hunspell нет.

        Слово идёт только в те раскладки, в которых его можно набрать: русские
        буквы в ``en_US`` не появляются, а дописать их туда бессмысленно —
        и вредно, потому что детектор решит, что слово есть в обеих раскладках.
        """
        if not config.extra_words:
            return
        for layout in self.tables.layouts:
            checker = self.spell.for_layout(layout)
            if checker is None:
                continue
            added = [
                word
                for word in config.extra_words
                if self._typeable(layout, word) and not checker.known(word)
            ]
            for word in added:
                checker.add(word)
            if added:
                log.info("дописал в словарь %r: %s", layout, ", ".join(added))

    def _typeable(self, layout: str, word: str) -> bool:
        """Можно ли это слово вообще набрать в раскладке."""
        return all(self.tables.to_key(layout, char) is not None for char in word)

    @staticmethod
    def _make_decision_log(config: Config) -> DecisionLog:
        """Журнал решений: путь из конфига, пусто — значение по умолчанию, "-" — выключить."""
        if config.decisions_log.strip() == "-":
            return DecisionLog(enabled=False)
        path = Path(config.decisions_log.strip()) if config.decisions_log.strip() else DECISIONS_LOG_PATH
        return DecisionLog(path=path, keep_words=config.log_words)

    # --- запуск и остановка ----------------------------------------------
    def _restore_layout_groups(self) -> None:
        """Убрать лишнюю группу, которую добавляет наша же виртуальная клавиатура.

        xf86-input-evdev пересобирает раскладку при появлении нового устройства
        ввода и добавляет группу. Со списком «us, ru, ru» переключение циклит
        us -> ru -> ru(ничего) -> us, и примерно каждое второе нажатие выглядит
        как «ничего не произошло». Это причина жалобы, а не отдельная поломка.

        Возвращаем ровно тот список, который задан в GNOME.
        """
        layouts = xkb_layout_ids()
        if len(layouts) < 2:
            return
        try:
            probe = subprocess.run(
                ["setxkbmap", "-query"], capture_output=True, text=True, timeout=5
            ).stdout
            current = next(
                (line.split(":", 1)[1].strip()
                 for line in probe.splitlines() if line.startswith("layout:")),
                "",
            )
            wanted = ",".join(layouts)
            if current == wanted:
                return
            log.warning(
                "в XKB %d групп (%s), а в GNOME %s — убираю лишние",
                len(current.split(",")) if current else 0, current or "?",
                wanted,
            )
            options = read_xkb_options()
            subprocess.run(
                ["setxkbmap", "-layout", wanted, "-option", ",".join(options)],
                capture_output=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            log.info("не удалось привести список раскладок в порядок: %s", exc)

    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._restore_layout_groups()
        self._log_startup()

        reader = KeyboardReader(
            self._handle_event,
            exclude_names=(VIRTUAL_DEVICE_NAME, *self.config.ignore_devices),
            on_device_change=self._on_device_change,
        )
        ignored = (VIRTUAL_DEVICE_NAME, *self.config.ignore_devices)
        watcher = MruWatcher(self._on_system_layout)
        watcher.start()
        self._install_signals(reader)

        self.state.refresh()
        log.info("текущая раскладка: %r", self.state.current)

        refresh_task = asyncio.create_task(self._layout_refresh_loop(), name="layout-refresh")
        idle_task = asyncio.create_task(self._idle_loop(), name="idle-fix")
        pointer = PointerWatcher(self._on_pointer_click, exclude_names=ignored)
        pointer_task = asyncio.create_task(pointer.run(), name="mouse")
        groups_task = asyncio.create_task(self._layout_group_watch(), name="xkb-groups")

        try:
            await reader.run()
        finally:
            for task in (refresh_task, idle_task, pointer_task, groups_task):
                task.cancel()
            for task in (refresh_task, idle_task, pointer_task, groups_task):
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            pointer.close()
            watcher.stop()
            await reader.stop_async()
            self._shutdown()

    async def _layout_refresh_loop(self) -> None:
        """Периодически синхронизировать раскладку с системой (фоллбэк для mru-sources)."""
        interval = self.config.layout_refresh
        if interval <= 0:
            log.debug("периодическая синхронизация раскладки отключена (layout_refresh_interval <= 0)")
            return
        while True:
            try:
                await asyncio.sleep(interval)
                if self._paused:
                    continue
                before = self.state.current
                self.state.refresh()
                if self.state.current != before:
                    log.info("периодическая синхронизация: %s -> %s", before, self.state.current)
                    self._reset_word()
            except asyncio.CancelledError:
                break
            except Exception:
                log.exception("ошибка в цикле обновления раскладки")

    def _normalize_and_read_group(self) -> int | None:
        """Привести список групп к числу раскладок и вернуть активный индекс."""
        self._restore_layout_groups()
        return xkb_group_index(self.tables.layouts)

    def _on_device_change(self) -> None:
        """Подключилось новое устройство ввода — снять лишнюю группу XKB сразу.

        xf86-input-evdev добавляет группу на каждое новое устройство. На этой
        машине виртуальных клавиатур две (ydotoold и наша), поэтому список
        раздувается до «us, ru, ru», и ScrollLock циклит
        us -> ru -> ru(ничего) -> us: нажатие из русской выглядит как
        «ничего не произошло», ровно через раз.

        Раньше это правилось таймером раз в 30 секунд, и пользователь успевал
        попасть в сломанное окно — жалоба «переключение не работает» повторялась.
        Теперь реакция идёт по событию, а таймер оставлен как фоллбэк.
        """
        try:
            self._restore_layout_groups()
        except Exception:
            log.exception("не удалось убрать лишние группы после подключения устройства")

    async def _layout_group_watch(self) -> None:
        """Раз в полминуты снимать лишние группы XKB.

        Разово при старте мало: лишняя группа возвращается, когда появляется
        любое новое устройство ввода — например ydotoold у AI-инструментов. Со
        списком «us, ru, ru» переключение циклит us -> ru -> ru(ничего) -> us,
        и демон после каждой правки получает от GNOME раскладку, противоположную
        той, в которую сам переключился.
        """
        while True:
            # 5 с, а не 30: между проверками пользователь успевает попасть в
            # окно с дубликатом и нажать «впустую». Замерено на этой машине:
            # при us,ru,ru нажатие из ru уводит во вторую ru, и для пользователя
            # это «переключение не работает» — ровно через раз.
            await asyncio.sleep(5)
            if self._paused:
                continue
            try:
                self._restore_layout_groups()
            except Exception:
                log.exception("ошибка при проверке списка раскладок")

    async def _idle_loop(self) -> None:
        """Исправлять слово по паузе в наборе, а не только по разделителю.

        Так делают xneur и Punto Switcher: пользователь замолчал — слово под
        курсором можно трогать. Это единственный способ закрыть случай
        «слово + Enter»: к моменту, когда Enter дойдёт до приложения, слово
        уже должно быть правильным. 9 отмен из 13 за всю историю были
        именно из-за Enter.

        Разделителя на экране в этот момент ещё нет, поэтому передаём пустую
        строку: стирать и печатать нужно только само слово.
        """
        interval = self.config.idle_fix
        if interval <= 0:
            log.info("исправление по паузе выключено (idle_fix_ms <= 0)")
            return
        log.info("исправление по паузе: %d мс", self.config.idle_fix_ms)
        try:
            while True:
                await asyncio.sleep(0.05)
                if self._paused or not self.buffer or self.mods.any_pressed:
                    continue
                if time.monotonic() - self._buffer_changed_at < interval:
                    continue
                # Сбрасываем отметку заранее: если правка не выйдет, повторять
                # её каждые 50 мс незачем.
                self._buffer_changed_at = time.monotonic()
                self._finish_word("")
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("ошибка в цикле исправления по паузе")

    def _log_startup(self) -> None:
        """Вся стартовая информация — через logging: print буферизуется в сервисе."""
        actuator = self.state.actuator
        log.info("layout-fixer: раскладки %s, режим %r", self.tables.layouts, self.config.mode)
        log.info("  актуатор (из конфига): %s", actuator)
        self._log_actuator_status(actuator)
        mode_note = (
            "только логирую (исправления выключены)"
            if not self.config.fixing_enabled
            else "ИСПРАВЛЯЮ текст"
        )
        log.info("  %s; Alt-тап: %s", mode_note, "включён" if self.config.trigger == "alt_tap" else "выключен")
        log.info("  Ctrl+C или SIGTERM — остановка, SIGUSR1 — пауза/продолжить")

    @staticmethod
    def _log_actuator_status(actuator: Actuator) -> None:
        """Сверить актуатор из конфига с реальными xkb-опциями системы.

        Проверять надо не «совпадает ли актуатор», а «есть ли он среди
        настроенных»: демон подставляет клавишу из конфига, и сломается
        инъекция, только если именно этой опции нет в системе.

        Другие ``grp:*_toggle`` — не проблема, а норма: каждая привязывает
        свою клавишу, поэтому ``sclk_toggle`` (инъекция демона) и
        ``lalt_toggle`` (ручное переключение пользователем) работают вместе.
        """
        configured = all_configured_actuators()
        if not configured:
            log.warning("  в xkb-options нет ни одной grp:*_toggle — переключение раскладки не сработает")
            log.warning("  исправляется командой: layout-fixer actuator set %s", actuator.name)
            return
        names = [item.name for item in configured]
        if actuator.name not in names:
            log.warning(
                "  в xkb-options нет %s — инъекция не переключит раскладку",
                actuator.xkb_option,
            )
            log.warning("  исправляется командой: layout-fixer actuator set %s", actuator.name)
            return
        manual = [name for name in names if name != actuator.name]
        if manual:
            log.info("  прочие grp:*_toggle в системе: %s — переключают раскладку вручную", ", ".join(manual))

    def _install_signals(self, reader: KeyboardReader) -> None:
        assert self._loop is not None
        for sig in (signal.SIGINT, signal.SIGTERM):
            self._loop.add_signal_handler(sig, self._stop, reader)
        self._loop.add_signal_handler(signal.SIGUSR1, self._toggle_pause)

    def _stop(self, reader: KeyboardReader) -> None:
        reader.stop()

    def _toggle_pause(self) -> None:
        self._paused = not self._paused
        state = "на паузе" if self._paused else "работает"
        log.warning("layout-fixer %s", state)
        self._reset_word()
        self._revert = None

    def _shutdown(self) -> None:
        log.info("итоги: %s", ", ".join(f"{key}={value}" for key, value in self._stats.items()))
        if self.decisions.enabled:
            log.info("журнал решений: %s", self.decisions.path)
        self.spell.close()
        self.injector.close()

    # --- синхронизация раскладки -----------------------------------------
    def _on_system_layout(self, layout: str) -> None:
        """Пришло из потока GLib — перебрасываем в цикл asyncio."""
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._apply_system_layout, layout)

    def _apply_system_layout(self, layout: str) -> None:
        """Пришло уведомление об изменении mru-sources.

        Само значение не берём — оно запаздывает на 26–329 мс. Вместо него
        спрашиваем у X-сервера активную группу: так демон замечает и своё
        переключение, и переключение человека (например, клик по индикатору,
        который не даёт клавиши и потому не виден в потоке клавиатуры).
        """
        previous = self.state.current
        self.state.refresh()
        if self.state.current == previous:
            return
        log.info(
            "раскладка изменена вне демона: %s -> %s", previous, self.state.current
        )
        self._reset_word()

    # --- обработка клавиш -------------------------------------------------
    def _handle_event(self, _device, event) -> None:
        code, value = event.code, event.value
        if value not in (0, 1, 2):
            return

        # Backspace — единственное действие, которое может означать «правка не
        # нужна». Всё остальное, кроме самих модификаторов, значит, что
        # пользователь продолжил печатать и исправление прижилось: наблюдение
        # снимается. Модификаторы пропускаем — нажатие Ctrl перед
        # Ctrl+Backspace это часть стирания, а не новый ввод. Отпускания
        # (value=0) тоже не считаем: релиз того же пробела, которым закончилось
        # слово, снял бы наблюдение сразу после того, как оно появилось.
        #
        # Считаем стирание здесь, а не в ветке Backspace ниже: до неё не доходят
        # Ctrl+Backspace и Alt+Backspace (сбрасывают буфер и выходят раньше), а
        # именно так стирают слово чаще всего — иначе отменённые правки молчали.
        if code == e.KEY_BACKSPACE:
            if value != 0:
                self._note_backspace()
        elif code not in MODIFIER_KEYS and value != 0:
            self._revert = None


        self._track_alt_tap(code, value)
        self.mods.update(code, value)
        self._try_pending_fix()
        if value == 0:
            return

        # Alt (или Ctrl/Super) зажат вместе с другой клавишей — это комбинация,
        # а не текстовый ввод: буфер слова продолжать нельзя.
        if code not in MODIFIER_KEYS and self.mods.alt:
            self._alt_used = True

        if self.state.handle_event(code, value):
            log.info("раскладка переключена вручную (%s): %s", self.state.actuator.name, self.state.current)
            self._reset_word()
            return

        name = e.KEY[code]
        if name in CARET_BREAKING or self.mods.command:
            self._reset_word()
            return
        if self._paused:
            return

        if code in MODIFIER_KEYS:
            # Модификатор не несёт текста, но и не должен стирать набранное
            # слово. Раньше Shift уходил в decode(), возвращал None, и демон
            # обнулял буфер. Из-за этого не работало НИЧЕГО, что печатается с
            # Shift: «&», «?», «!», «(» и заглавные буквы посреди слова —
            # слово до них терялось целиком.
            return

        if code == e.KEY_BACKSPACE:
            self.buffer.backspace()
            return
        if code in WORD_SEPARATORS_UNSAFE:
            self._finish_word(WORD_SEPARATORS_UNSAFE[code])
            return
        if code == e.KEY_ESC:
            self._reset_word()
            return

        char = self.tables.decode(
            self.state.current,
            code,
            shift=self.mods.shift,
            altgr=self.mods.altgr,
            caps=self.mods.caps,
        )
        if char is None:
            # Служебная или неизвестная клавиша — не рискуем угадывать текст.
            self._reset_word()
            return
        if char.isalpha():
            # Буква: значит всё отложенное ранее было частью слова.
            self.buffer.commit_pending()
            self.buffer.append(char)
            self._buffer_changed_at = self._last_char_at = time.monotonic()
            return
        if char in self.tables.word_characters:
            # Символ, который в другой раскладке — буква (например «,» = «б»).
            # Пока не знаем, буква это или знак препинания: решим по следующему
            # нажатию — если слово продолжится, это буква.
            self.buffer.append_ambiguous(char)
            self._buffer_changed_at = self._last_char_at = time.monotonic()
            return
        self._finish_word(char)

    def _track_alt_tap(self, code: int, value: int) -> None:
        """Одиночный тап левого Alt = переключить раскладку.

        Клавишу НЕ глотаем: если пользователь нажал Alt+Tab, приложение должно
        получить свой Alt. Различаем по факту нажатия других клавиш, по времени
        и по зажатым модификаторам — иначе Shift+Alt (частая привычка при
        capitals или смене языка) переключал бы раскладку посреди печати.
        """
        if (
            self.config.trigger != "alt_tap"
            or code != e.KEY_LEFTALT
            or self.state.actuator.code == e.KEY_LEFTALT
        ):
            return
        if value == 1:
            self._alt_press = time.monotonic()
            # Shift или Ctrl, зажатые ДО Alt, тоже делают это комбинацией.
            self._alt_used = self.mods.other_modifiers(exclude=e.KEY_LEFTALT)
            return
        if value != 0:
            return
        pressed_at = self._alt_press
        self._alt_press = None
        if pressed_at is None or self._alt_used:
            return
        # Модификатор мог быть зажат уже после Alt (Shift+Alt, Alt+Ctrl).
        if self.mods.other_modifiers(exclude=e.KEY_LEFTALT):
            log.debug("Alt отпущен вместе с другим модификатором — это комбинация")
            return
        if time.monotonic() - pressed_at > self.config.alt_tap_max:
            log.debug("Alt держали долго — считаю это не переключением")
            return
        if self._paused:
            return
        try:
            self.state.toggle()
        except Exception:
            log.exception("не удалось переключить раскладку по тапу Alt")
            return
        log.info("тап Alt -> раскладка %s", self.state.current)
        self._reset_word()

    def _caret_stale(self) -> bool:
        """Клик случился уже после последней буквы слова — курсор мог уехать.

        Раньше здесь проверялось только время («клик был недавно»), хотя
        докстринг обещал другое: старый клик не мешает, если с тех пор
        отпечатано слово. Кликнули, потом напечатали — курсор точно после
        слова, и Backspace починит верно.

        Сверка только по времени и ломала всё: мышь кликает и двигается
        постоянно, и любое слово, набранное в первые секунды после клика,
        отменялось. В логе это выглядело как «демон перестал переводить»,
        хотя он слово распознавал верно — с уверенностью 0.95.
        """
        if not self._caret_maybe_moved:
            return False
        return self._caret_click_at > self._last_char_at

    def _reset_word(self) -> None:
        """Сбросить буфер слова и забыть, что курсор мог уехать."""
        self.buffer.reset()
        self._caret_maybe_moved = False
        self._pending_fix = None
        self._last_char_at = 0.0

    def _on_pointer_click(self, device_name: str, moved: bool = True) -> None:
        """Клик мышью. Опасен только если указатель перед этим двигался.

        Раньше демон был слеп к мыши и правил слово по разделителю, даже если
        курсор уже стоял в другом месте: Backspace съедал символы перед новой
        позицией, и исправленное слово вставлялось в середину предложения.

        Сбрасывать буфер на каждый клик оказалось дорого: мышь кликает 149 раз за
        40 минут, и четыре отмены подряд шли с причиной «курсор мог уехать»,
        хотя курсор стоял на месте. Теперь смотрим, было ли перемещение (EV_REL)
        между кликами — без него клик безобиден.
        """
        if not moved:
            log.debug("клик мышью (%s) без перемещения — курсор на месте", device_name)
            return
        log.info("клик мышью с перемещением (%s) — курсор мог уехать", device_name)
        self._caret_maybe_moved = True
        self._caret_click_at = time.monotonic()
        self._revert = None

    def _reset_word(self) -> None:
        """Сбросить буфер слова и забыть, что курсор мог уехать."""
        self.buffer.reset()
        self._caret_maybe_moved = False
        self._pending_fix = None

    def _arm_revert_watch(self, decision: Decision, injected: str) -> None:
        """Запомнить только что введённое исправление.

        Если пользователь сразу его стирает — правка была лишней. Это
        единственный доступный нам сигнал правды о ложных срабатываниях: сам
        детектор уверен в своей правоте на 0.95 и проверить себя не может.
        """
        self._revert = _InjectedWord(
            decision=decision,
            text=injected,
            word_length=len(decision.converted or ""),
            at=time.monotonic(),
        )

    def _note_backspace(self) -> None:
        """Посчитать Backspace пользователя как возможную отмену исправления."""
        watch = self._revert
        if watch is None:
            return
        if time.monotonic() - watch.at > REVERT_WINDOW_S:
            # Пользователь отошёл, а потом вернулся и стёр по другой причине.
            self._revert = None
            return
        watch.erased += 1
        if watch.erased < watch.word_length:
            # Слово ещё целиком на экране — стирают не его (или стёрли разделитель).
            return
        self._revert = None
        decision = watch.decision
        self._stats["стёрто пользователем"] += 1
        log.info("[СТЁР] %r -> %r: пользователь убрал исправление", decision.word, decision.converted)
        self._record(
            "REVERT",
            decision,
            reason=f"пользователь стёр {watch.text!r} сразу после исправления",
        )

    # --- разбор слова -----------------------------------------------------
    def _finish_word(self, separator: str) -> None:
        # Отложенная пунктуация в конце слова означает, что это был именно
        # разделитель (запятая, точка), а не буква другой раскладки.
        # Но Enter/Tab уже сработали в приложении — их приоритет выше.
        pending = self.buffer.pending
        if pending and separator not in UNSAFE_SEPARATORS:
            if not separator or len(self.buffer) < self.config.min_word_length:
                # При пустом разделителе (правка по паузе) ничего на экране ещё
                # нет — значит отложенные символы часть слова, а не знак.
                # То же при слове короче порога: «dc`» это «всё», где ` — «ё».
                self.buffer.commit_pending()
            else:
                separator = pending[0]

        finished = self.buffer.finish(separator, self.state.current)
        if finished is None:
            return
        self._stats["слов"] += 1
        if not finished.mutable:
            log.debug("слово %s правили вручную — не трогаю", finished)
            return
        decision = self.detector.decide(finished.text, finished.layout)
        if not decision.fix:
            log.debug("оставляю %s", decision)
            self._record("SKIP", decision, reason=decision.reason)
            return

        self._stats["решений исправлять"] += 1
        allowed, why = self.fixer.can_apply(decision, finished.separator)
        if not allowed:
            if self.fixer.is_modifier_block(why):
                # «&», «?», «(» печатаются с Shift, и Shift в этот момент зажат.
                # Отменять жалобу — значит никогда не исправлять слова со знаком
                # вопроса. Ждём отпускания модификатора: это десятки миллисекунд.
                self._pending_fix = (decision, finished.separator)
                log.debug("правка отложена до отпускания модификатора: %s", why)
                return
            self._stats["отменено"] += 1
            log.info("[ОТМЕНА] %s (%s)", decision, why)
            self._record("CANCEL", decision, reason=why)
            return

        if self._caret_stale():
            self._stats["отменено"] += 1
            reason = "после клика мышью курсор мог уехать — не рискуем"
            log.info("[ОТМЕНА] %s (%s)", decision, reason)
            self._record("CANCEL", decision, reason=reason)
            return

        if not self.config.fixing_enabled:
            log.info("[НАБЛЮДЕНИЕ] было бы исправлено: %s", decision)
            self._record("OBSERVE", decision, reason=decision.reason)
            return

        self._apply_fix(decision, finished.separator)

    def _try_pending_fix(self) -> None:
        """Применить отложенную правку, когда модификаторы уже отпущены.

        Слово с «&», «?», «(» или «+» заканчивается символом, который печатается
        с Shift, поэтому в момент решения Shift ещё зажат. Ждём его отпускания —
        это десятки миллисекунд, незаметно человеку.
        """
        pending = self._pending_fix
        if pending is None:
            return
        if self.fixer._modifier_problem() is not None:
            return
        self._pending_fix = None
        decision, separator = pending
        if not self.config.fixing_enabled:
            self._record("OBSERVE", decision, reason=decision.reason)
            return
        self._apply_fix(decision, separator)

    def _apply_fix(self, decision: Decision, separator: str) -> bool:
        """Внести правку и записать её в журнал."""
        log.info("[ИСПРАВЛЯЮ] %s", decision)
        app = self._focused_app()
        if app[0]:
            log.info("  в окне: %s (pid %s)", app[0], app[1])
        if not self.fixer.apply(decision, separator):
            return False
        self._stats["исправлено"] += 1
        if not separator:
            self._stats["исправлено по паузе"] += 1
        self._record("FIX", decision, reason=decision.reason, app=app[0] or "")
        assert decision.converted
        self._arm_revert_watch(decision, decision.converted + separator)
        return True

    def _focused_app(self) -> tuple[str | None, int | None]:
        """Какое приложение в фокусе (None, None — сессия не X11 или неизвестно).

        Пока только читаем и пишем в лог: на этом шаге поведение демона не
        меняется. Дальше из этих же данных можно собрать исключения по
        приложениям — в терминале и в полях паролей автоправка вредна.
        """
        try:
            return self._focus.current()
        except Exception:
            log.debug("не удалось прочитать активное окно", exc_info=True)
            return None, None

    def _record(self, kind: str, decision, reason: str = "", app: str = "") -> None:
        """Записать решение в журнал (файл для отчёта)."""
        self.decisions.record(
            kind,
            word=decision.word,
            layout=decision.layout,
            converted=decision.converted or "",
            target=decision.target_layout or "",
            confidence=decision.confidence,
            reason=reason or decision.reason,
            app=app,
        )

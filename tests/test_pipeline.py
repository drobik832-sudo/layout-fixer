"""Автотесты конвейера: буфер -> детектор -> исправление.

Запуск:  .venv/bin/python -m unittest discover -s tests -v

Особенности:
* инъектор заменён на записывающий, поэтому тесты НЕ печатают в реальные окна;
* чтение клавиатуры не задействовано — события подаются синтетические
  (``evdev.InputEvent``), значит тесты детерминированные и не зависят от железа.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from evdev import InputEvent, ecodes as e  # noqa: E402

from layout_fixer.config import Config  # noqa: E402
from layout_fixer.daemon import Daemon  # noqa: E402
from layout_fixer.layout_state import get_actuator  # noqa: E402
from layout_fixer.reader import KeyboardReader  # noqa: E402


class RecordingInjector:
    """Вместо /dev/uinput — просто записывает, что попросили нажать."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def tap(self, code: int, shift: bool = False, altgr: bool = False) -> None:
        self.calls.append(("tap", code, shift, altgr))

    def key_down(self, code: int) -> None:
        self.calls.append(("key_down", code))

    def key_up(self, code: int) -> None:
        self.calls.append(("key_up", code))

    def backspace(self, count: int = 1) -> None:
        self.calls.append(("backspace", count))

    def type_text(self, text: str, layout: str) -> list[str]:
        self.calls.append(("type", text, layout))
        return []

    def close(self) -> None:
        self.calls.append(("close",))

    # --- удобные выборки для проверок ---------------------------------
    def texts(self) -> list[tuple[str, str]]:
        return [(call[1], call[2]) for call in self.calls if call[0] == "type"]

    def backspaces(self) -> list[int]:
        return [call[1] for call in self.calls if call[0] == "backspace"]

    def actuator_taps(self, actuator_code: int) -> int:
        return sum(1 for call in self.calls if call[0] == "tap" and call[1] == actuator_code)


def press(code: int, value: int = 1) -> InputEvent:
    return InputEvent(0, 0, e.EV_KEY, code, value)


class PipelineTestCase(unittest.TestCase):
    """Общая обвязка: демон с подменённым инъектором."""

    def setUp(self) -> None:
        # Журнал решений — во временный файл: тесты не должны засорять
        # реальный журнал демона ~/.local/state/layout-fixer/decisions.log
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.log_path = Path(self._tmp.name) / "decisions.log"

    def make_daemon(self, **overrides) -> tuple[Daemon, RecordingInjector]:
        overrides.setdefault("decisions_log", str(self.log_path))
        config = Config(**overrides)
        recorder = RecordingInjector()
        daemon = Daemon(config, injector=recorder)
        daemon.state.injector = recorder
        daemon.fixer.injector = recorder
        daemon.state.set_current("us")
        # Источник истины для refresh()/ensure() — подменяем на собственное
        # состояние. Иначе ensure() читает настоящий X-сервер машины, где
        # идут тесты, и результат зависит от того, какая раскладка там сейчас.
        daemon.state._group_reader = lambda layouts: layouts.index(daemon.state.current)
        # Нормализация групп тоже ходит в X-сервер (setxkbmap). В тестах она
        # не нужна: состояние и так наше собственное.
        daemon.state._normalize_groups = lambda: layouts.index(daemon.state.current) \
            if (layouts := daemon.state.layouts) else None
        # В тестах эмулируем, что в системе настроен тот же актуатор, что в конфиге демона
        actuator = get_actuator(config.actuator)
        daemon.state._actuator_codes = {actuator.code}
        return daemon, recorder

    def feed_char(self, daemon: Daemon, char: str, layout: str = "us") -> None:
        """Напечатать один символ так, как это сделала бы физическая клавиатура."""
        key = daemon.tables.to_key(layout, char)
        assert key is not None, f"нет клавиши для {char!r} в {layout!r}"
        code, shift, _altgr = key
        if shift:
            daemon._handle_event(None, press(e.KEY_LEFTSHIFT, 1))
        daemon._handle_event(None, press(code, 1))
        daemon._handle_event(None, press(code, 0))
        if shift:
            daemon._handle_event(None, press(e.KEY_LEFTSHIFT, 0))

    def feed_text(self, daemon: Daemon, text: str, layout: str = "us") -> None:
        for char in text:
            self.feed_char(daemon, char, layout)


class TestFixMode(PipelineTestCase):
    def test_wrong_layout_word_is_fixed_on_space(self) -> None:
        daemon, recorder = self.make_daemon(mode="fix")
        self.feed_text(daemon, "ghbdtn", "us")
        self.feed_char(daemon, " ", "us")  # разделитель завершает слово

        self.assertEqual(recorder.backspaces(), [7], "должно стереться слово + пробел")
        self.assertEqual(
            recorder.actuator_taps(daemon.state.actuator.code),
            1,
            "одно переключение — в целевую раскладку, без возврата",
        )
        self.assertEqual(recorder.texts(), [("привет ", "ru")])
        self.assertEqual(daemon.state.current, "ru", "остаёмся в русской")

    def test_correct_word_is_left_alone(self) -> None:
        daemon, recorder = self.make_daemon(mode="fix")
        self.feed_text(daemon, "hello", "us")
        self.feed_char(daemon, " ", "us")
        self.assertEqual(recorder.calls, [], "правильное слово трогать нельзя")

    def test_russian_word_in_russian_layout_is_left_alone(self) -> None:
        daemon, recorder = self.make_daemon(mode="fix")
        daemon.state.set_current("ru")
        self.feed_text(daemon, "привет", "ru")
        self.feed_char(daemon, " ", "ru")
        self.assertEqual(recorder.calls, [])

    def test_russian_typed_in_english_layout_is_fixed(self) -> None:
        daemon, recorder = self.make_daemon(mode="fix")
        self.feed_text(daemon, "Ghbdtn", "us")
        self.feed_char(daemon, " ", "us")
        self.assertEqual(recorder.texts(), [("Привет ", "ru")])


class TestObserveMode(PipelineTestCase):
    def test_observe_mode_never_injects(self) -> None:
        daemon, recorder = self.make_daemon(mode="observe")
        self.feed_text(daemon, "ghbdtn", "us")
        self.feed_char(daemon, " ", "us")
        self.assertEqual(recorder.calls, [], "в режиме наблюдения инъекций быть не должно")
        self.assertEqual(daemon._stats["решений исправлять"], 1, "решение должно быть зафиксировано")


class TestGuardRails(PipelineTestCase):
    def test_enter_is_not_fixed(self) -> None:
        daemon, recorder = self.make_daemon(mode="fix")
        self.feed_text(daemon, "ghbdtn", "us")
        self._press_and_release(daemon, e.KEY_ENTER)
        self.assertEqual(recorder.calls, [], "после Enter исправлять уже нельзя")
        self.assertEqual(daemon._stats["отменено"], 1)

    def test_hand_edited_word_is_not_fixed(self) -> None:
        daemon, recorder = self.make_daemon(mode="fix")
        self.feed_text(daemon, "ghbdtn", "us")
        daemon._handle_event(None, press(e.KEY_BACKSPACE, 1))  # пользователь сам правил
        self.feed_char(daemon, "n", "us")
        self.feed_char(daemon, " ", "us")
        self.assertEqual(recorder.calls, [])

    def test_ctrl_combo_resets_word(self) -> None:
        daemon, recorder = self.make_daemon(mode="fix")
        self.feed_text(daemon, "ghbdtn", "us")
        daemon._handle_event(None, press(e.KEY_LEFTCTRL, 1))
        self._press_and_release(daemon, e.KEY_C)
        daemon._handle_event(None, press(e.KEY_LEFTCTRL, 0))
        self.feed_char(daemon, " ", "us")
        self.assertEqual(recorder.calls, [], "после Ctrl+C слово не должно исправляться")

    def test_short_word_is_ignored(self) -> None:
        daemon, recorder = self.make_daemon(mode="fix")
        self.feed_text(daemon, "rk", "us")  # «ка» короче min_word_length
        self.feed_char(daemon, " ", "us")
        self.assertEqual(recorder.calls, [])

    @staticmethod
    def _press_and_release(daemon: Daemon, code: int) -> None:
        daemon._handle_event(None, press(code, 1))
        daemon._handle_event(None, press(code, 0))


class TestAltTrigger(PipelineTestCase):
    def test_tap_alt_switches_layout(self) -> None:
        daemon, recorder = self.make_daemon(mode="observe")
        daemon._handle_event(None, press(e.KEY_LEFTALT, 1))
        daemon._handle_event(None, press(e.KEY_LEFTALT, 0))
        self.assertEqual(recorder.actuator_taps(daemon.state.actuator.code), 1)
        self.assertEqual(daemon.state.current, "ru")

    def test_alt_in_combination_does_not_switch(self) -> None:
        daemon, recorder = self.make_daemon(mode="observe")
        daemon._handle_event(None, press(e.KEY_LEFTALT, 1))
        daemon._handle_event(None, press(e.KEY_TAB, 1))
        daemon._handle_event(None, press(e.KEY_TAB, 0))
        daemon._handle_event(None, press(e.KEY_LEFTALT, 0))
        self.assertEqual(recorder.calls, [], "Alt+Tab не должен переключать раскладку")
        self.assertEqual(daemon.state.current, "us")

    def test_leftalt_actuator_does_not_inject_duplicate_toggle(self) -> None:
        daemon, recorder = self.make_daemon(mode="observe", actuator="leftalt", trigger="alt_tap")
        daemon._handle_event(None, press(e.KEY_LEFTALT, 1))
        daemon._handle_event(None, press(e.KEY_LEFTALT, 0))

        self.assertEqual(recorder.calls, [], "левый Alt уже переключает раскладку в системе")
        self.assertEqual(daemon.state.current, "ru")

    def test_long_alt_hold_does_not_switch(self) -> None:
        daemon, recorder = self.make_daemon(mode="observe", alt_tap_max_ms=0)
        daemon._handle_event(None, press(e.KEY_LEFTALT, 1))
        daemon._handle_event(None, press(e.KEY_LEFTALT, 0))
        self.assertEqual(recorder.calls, [])

    def test_trigger_can_be_disabled(self) -> None:
        daemon, recorder = self.make_daemon(mode="observe", trigger="none")
        daemon._handle_event(None, press(e.KEY_LEFTALT, 1))
        daemon._handle_event(None, press(e.KEY_LEFTALT, 0))
        self.assertEqual(recorder.calls, [])


    def test_shift_alt_does_not_switch(self) -> None:
        """Shift+Alt — привычный жест смены языка, но раскладку он не должен трогать.

        Раньше комбинация проходила как чистый тап: модификаторы не считались
        «другими клавишами», и раскладка менялась посреди печати.
        """
        daemon, recorder = self.make_daemon(mode="observe", trigger="alt_tap")
        daemon._handle_event(None, press(e.KEY_LEFTSHIFT, 1))
        daemon._handle_event(None, press(e.KEY_LEFTALT, 1))
        daemon._handle_event(None, press(e.KEY_LEFTALT, 0))
        daemon._handle_event(None, press(e.KEY_LEFTSHIFT, 0))
        self.assertEqual(recorder.calls, [], "Shift+Alt не должен переключать раскладку")
        self.assertEqual(daemon.state.current, "us")

    def test_alt_then_shift_does_not_switch(self) -> None:
        """Тот же жест, но Alt зажат первым."""
        daemon, recorder = self.make_daemon(mode="observe", trigger="alt_tap")
        daemon._handle_event(None, press(e.KEY_LEFTALT, 1))
        daemon._handle_event(None, press(e.KEY_LEFTSHIFT, 1))
        daemon._handle_event(None, press(e.KEY_LEFTALT, 0))
        daemon._handle_event(None, press(e.KEY_LEFTSHIFT, 0))
        self.assertEqual(daemon.state.current, "us")

    def test_ctrl_alt_does_not_switch(self) -> None:
        daemon, recorder = self.make_daemon(mode="observe", trigger="alt_tap")
        daemon._handle_event(None, press(e.KEY_LEFTALT, 1))
        daemon._handle_event(None, press(e.KEY_LEFTCTRL, 1))
        daemon._handle_event(None, press(e.KEY_LEFTALT, 0))
        daemon._handle_event(None, press(e.KEY_LEFTCTRL, 0))
        self.assertEqual(daemon.state.current, "us")

    def test_right_alt_alone_does_not_switch(self) -> None:
        """AltGr не должен переключать раскладку — это клавиша третьего уровня."""
        daemon, recorder = self.make_daemon(mode="observe", trigger="alt_tap")
        daemon._handle_event(None, press(e.KEY_RIGHTALT, 1))
        daemon._handle_event(None, press(e.KEY_RIGHTALT, 0))
        self.assertEqual(daemon.state.current, "us")

    def test_plain_alt_tap_still_switches(self) -> None:
        """Проверка, что не сломали основное поведение."""
        daemon, recorder = self.make_daemon(mode="observe", trigger="alt_tap")
        daemon._handle_event(None, press(e.KEY_LEFTALT, 1))
        daemon._handle_event(None, press(e.KEY_LEFTALT, 0))
        self.assertEqual(recorder.actuator_taps(daemon.state.actuator.code), 1)
        self.assertEqual(daemon.state.current, "ru")


class TestActuatorHandling(PipelineTestCase):
    def test_pressing_actuator_updates_state_without_injection(self) -> None:
        daemon, recorder = self.make_daemon(mode="observe")
        code = daemon.state.actuator.code  # Scroll Lock
        daemon._handle_event(None, press(code, 1))
        daemon._handle_event(None, press(code, 0))
        self.assertEqual(recorder.calls, [], "пользователь нажал сам — инъекция не нужна")
        self.assertEqual(daemon.state.current, "ru")


class TestVirtualLetters(PipelineTestCase):
    """Слова с буквами, которые в латинской раскладке стоят на клавишах пунктуации.

    б, ю, ж, э, х, ъ, ё → , . ; ' [ ] ` — из-за них русские слова, набранные при
    включённой английской раскладке, раньше не распознавались вообще.
    """

    def test_word_with_commas_is_fixed(self) -> None:
        # «j,yjdktybt» — это «обновление», набранное при английской раскладке.
        daemon, recorder = self.make_daemon(mode="fix")
        self.feed_text(daemon, "j,yjdktybt", "us")
        self.feed_char(daemon, " ", "us")
        self.assertEqual(recorder.texts(), [("обновление ", "ru")])

    def test_word_with_grave_is_fixed(self) -> None:
        # «jnx`n» — это «отчёт» (ё стоит на клавише с обратным апострофом).
        daemon, recorder = self.make_daemon(mode="fix")
        self.feed_text(daemon, "jnx`n", "us")
        self.feed_char(daemon, " ", "us")
        self.assertEqual(recorder.texts(), [("отчёт ", "ru")])

    def test_trailing_punctuation_stays_punctuation(self) -> None:
        # «ghbdtn,» — это «привет» + запятая, а не «приветб».
        daemon, recorder = self.make_daemon(mode="fix")
        self.feed_text(daemon, "ghbdtn", "us")
        self.feed_char(daemon, ",", "us")
        self.feed_char(daemon, " ", "us")
        self.assertEqual(recorder.texts(), [("привет,", "ru")])

    def test_correct_russian_with_comma_is_left_alone(self) -> None:
        daemon, recorder = self.make_daemon(mode="fix")
        daemon.state.set_current("ru")
        self.feed_text(daemon, "привет", "ru")
        self.feed_char(daemon, ",", "ru")
        self.feed_char(daemon, " ", "ru")
        self.assertEqual(recorder.calls, [])

    def test_enter_after_punctuation_does_not_fix(self) -> None:
        # Enter уже сработал в приложении — исправлять нельзя, даже если перед ним
        # была запятая.
        daemon, recorder = self.make_daemon(mode="fix")
        self.feed_text(daemon, "ghbdtn", "us")
        self.feed_char(daemon, ",", "us")
        self._press_and_release(daemon, e.KEY_ENTER)
        self.assertEqual(recorder.calls, [])

    @staticmethod
    def _press_and_release(daemon: Daemon, code: int) -> None:
        daemon._handle_event(None, press(code, 1))
        daemon._handle_event(None, press(code, 0))


class TestDetectorThresholds(PipelineTestCase):
    def test_bigram_based_typo_is_rejected(self) -> None:
        # Порог проверяем при явно включённых биграммах — иначе правило не работает.
        daemon, _ = self.make_daemon(mode="fix", use_bigram=True)
        decision = daemon.detector.decide("ghbdtnr", "us")
        self.assertFalse(decision.fix, "опечатка не должна считаться неверной раскладкой")

    def test_unknown_word_is_not_fixed_by_default(self) -> None:
        """По умолчанию (use_bigram=false) правим только подтверждённое словарём."""
        daemon, _ = self.make_daemon(mode="fix")
        decision = daemon.detector.decide("fghbdtn", "us")
        self.assertFalse(decision.fix, "без словарного подтверждения исправлять нельзя")
        self.assertIn("модель биграмм выключена", decision.reason)

    def test_missing_word_found_in_other_layout(self) -> None:
        daemon, _ = self.make_daemon(mode="fix")
        decision = daemon.detector.decide("ghbdtn", "us")
        self.assertTrue(decision.fix)
        self.assertEqual(decision.converted, "привет")
        self.assertGreaterEqual(decision.confidence, 0.9)

    def test_ignored_word_stays(self) -> None:
        daemon, _ = self.make_daemon(mode="fix", ignore_words=["ghbdtn"])
        decision = daemon.detector.decide("ghbdtn", "us")
        self.assertFalse(decision.fix)


class TestBigramVeto(PipelineTestCase):
    """Правило «нет в текущем словаре, но есть в другом» само по себе слепо.

    Оно не спрашивает модель биграмм, поэтому короткий латинский токен
    превращался в русское слово, в которое он разворачивается при смене
    раскладки: «ide» -> «шву». Биграммы были против (us -5.145 против
    ru -7.896), но демон их не смотрел.
    """

    def test_latin_token_is_not_turned_into_its_russian_reading(self) -> None:
        daemon, _ = self.make_daemon(mode="fix")
        decision = daemon.detector.decide("ide", "us")
        self.assertFalse(decision.fix, "«ide» — латинский токен, а не «шву»")
        self.assertIn("биграммы говорят в пользу", decision.reason)

    def test_latin_token_is_not_injected(self) -> None:
        daemon, recorder = self.make_daemon(mode="fix")
        self.feed_text(daemon, "ide", "us")
        self.feed_char(daemon, " ", "us")
        self.assertEqual(recorder.calls, [], "в конвейер не должно ничего уйти")

    def test_common_short_mistyped_words_are_still_fixed(self) -> None:
        """Вето обязано пропускать настоящие опечатки в раскладке."""
        expected = {"rfr": "как", "xnj": "что", "lfq": "дай", "nfr": "так", "ytn": "нет"}
        daemon, _ = self.make_daemon(mode="fix")
        for word, converted in expected.items():
            with self.subTest(word=word):
                decision = daemon.detector.decide(word, "us")
                self.assertTrue(decision.fix, f"«{word}» -> «{converted}» должно правиться")
                self.assertEqual(decision.converted, converted)

    def test_long_wrong_layout_word_is_still_fixed(self) -> None:
        daemon, recorder = self.make_daemon(mode="fix")
        self.feed_text(daemon, "ghbdtn", "us")
        self.feed_char(daemon, " ", "us")
        self.assertEqual(recorder.texts(), [("привет ", "ru")])

    def test_veto_can_be_disabled(self) -> None:
        daemon, _ = self.make_daemon(mode="fix", bigram_veto=False)
        self.assertTrue(daemon.detector.decide("ide", "us").fix)


class TestConfiguredActuator(PipelineTestCase):
    """Несколько grp:*_toggle в системе — это норма, а не конфликт.

    Каждая опция привязывает свою клавишу, поэтому sclk_toggle (инъекция
    демона) и lalt_toggle (ручное переключение пользователем) работают
    вместе. Ломается инъекция только когда в системе НЕТ опции из конфига.
    """

    def test_config_actuator_present_reports_no_warning(self) -> None:
        self._assert_actuator_logs(lambda: [get_actuator("scrolllock")], "scrolllock", warns=False)

    def test_manual_toggle_is_info_not_warning(self) -> None:
        """Ручной переключатель в системе — штатная ситуация, ругаться не на что."""
        message = self._assert_actuator_logs(
            lambda: [get_actuator("scrolllock"), get_actuator("leftalt")],
            "scrolllock",
            warns=False,
        )
        self.assertEqual(message, "")

    def test_no_toggles_at_all_is_reported(self) -> None:
        message = self._assert_actuator_logs(lambda: [], "scrolllock", warns=True)
        self.assertIn("нет ни одной grp:*_toggle", message)

    def test_absent_config_actuator_is_reported(self) -> None:
        """Есть lalt_toggle, но нет sclk_toggle — инъекция демона не сработает."""
        message = self._assert_actuator_logs(lambda: [get_actuator("leftalt")], "scrolllock", warns=True)
        self.assertIn("инъекция не переключит", message)

    def test_manual_toggle_is_logged_as_info(self) -> None:
        from layout_fixer import daemon as daemon_mod

        original = daemon_mod.all_configured_actuators
        daemon_mod.all_configured_actuators = lambda: [
            get_actuator("scrolllock"),
            get_actuator("leftalt"),
        ]
        self.addCleanup(setattr, daemon_mod, "all_configured_actuators", original)

        with self.assertLogs("layout_fixer.daemon", level="INFO") as captured:
            daemon_mod.Daemon._log_actuator_status(get_actuator("scrolllock"))
        message = "\n".join(captured.output)
        self.assertIn("прочие grp:*_toggle", message)
        self.assertIn("leftalt", message)

    def test_configured_actuator_returns_first_in_actors_order(self) -> None:
        from layout_fixer import layout_state

        original = layout_state.read_xkb_options
        layout_state.read_xkb_options = lambda: ["grp_led:scroll", "grp:sclk_toggle", "grp:lalt_toggle"]
        self.addCleanup(setattr, layout_state, "read_xkb_options", original)

        self.assertEqual(layout_state.configured_actuator().name, "leftalt")
        self.assertEqual(
            [a.name for a in layout_state.all_configured_actuators()],
            ["leftalt", "scrolllock"],
        )

    def _assert_actuator_logs(self, fake, actuator_name: str, *, warns: bool) -> str:
        from layout_fixer import daemon as daemon_mod

        original = daemon_mod.all_configured_actuators
        daemon_mod.all_configured_actuators = fake
        self.addCleanup(setattr, daemon_mod, "all_configured_actuators", original)

        if not warns:
            with self.assertNoLogs("layout_fixer.daemon", level="WARNING"):
                daemon_mod.Daemon._log_actuator_status(get_actuator(actuator_name))
            return ""
        with self.assertLogs("layout_fixer.daemon", level="WARNING") as captured:
            daemon_mod.Daemon._log_actuator_status(get_actuator(actuator_name))
        return "\n".join(captured.output)


class TestModifierGuard(PipelineTestCase):
    """Инъекция печатает в «чистую» клавиатуру.

    Решение принимается по ``word.lower()``, поэтому зажатый Shift или
    включённый Caps Lock молча портили регистр: демон решал напечатать
    «привет», а на экране оказывалось «ПРИВЕТ».
    """

    def _type_wrong_layout_word(self, daemon: Daemon) -> None:
        self.feed_text(daemon, "ghbdtn", "us")
        self.feed_char(daemon, " ", "us")

    def test_caps_lock_cancels_fix(self) -> None:
        daemon, recorder = self.make_daemon(mode="fix")
        daemon._handle_event(None, press(e.KEY_CAPSLOCK, 1))
        self._type_wrong_layout_word(daemon)
        self.assertEqual(recorder.calls, [], "при Caps Lock печатать нельзя")
        self.assertEqual(daemon._stats["отменено"], 1)

    def test_held_shift_cancels_fix(self) -> None:
        daemon, recorder = self.make_daemon(mode="fix")
        daemon._handle_event(None, press(e.KEY_LEFTSHIFT, 1))
        self._type_wrong_layout_word(daemon)
        self.assertEqual(recorder.calls, [], "при зажатом Shift печатать нельзя")

    def test_ctrl_cancels_fix(self) -> None:
        """Ctrl превратил бы Backspace в удаление слова, а буквы — в горячие клавиши."""
        daemon, recorder = self.make_daemon(mode="fix")
        daemon._handle_event(None, press(e.KEY_LEFTCTRL, 1))
        self._type_wrong_layout_word(daemon)
        self.assertEqual(recorder.calls, [])

    def test_released_modifiers_still_fix(self) -> None:
        daemon, recorder = self.make_daemon(mode="fix")
        self._type_wrong_layout_word(daemon)
        self.assertEqual(recorder.texts(), [("привет ", "ru")])

    def test_capitalized_word_is_fixed_with_its_capital(self) -> None:
        """Регистр слова берётся из самого слова, а не из состояния клавиатуры."""
        daemon, recorder = self.make_daemon(mode="fix")
        self.feed_text(daemon, "Ghbdtn", "us")
        self.feed_char(daemon, " ", "us")
        self.assertEqual(recorder.texts(), [("Привет ", "ru")])


class FailingUI:
    """Виртуальная клавиатура, у которой syn() падает на заданном по счёту вызове."""

    def __init__(self, fail_at: int | None = None):
        self.events: list[tuple[int, int]] = []
        self._syncs = 0
        self._fail_at = fail_at

    def write_event(self, event) -> None:
        self.events.append((event.code, event.value))

    def syn(self) -> None:
        self._syncs += 1
        if self._syncs == self._fail_at:
            raise OSError("ошибка записи в /dev/uinput")

    def stuck(self) -> list[str]:
        down = {code for code, value in self.events if value == 1}
        up = {code for code, value in self.events if value == 0}
        return [e.KEY[code] for code in down - up]


class TestInjectorReleasesKeys(unittest.TestCase):
    """Залипший на виртуальной клавиатуре Shift переводит весь дальнейший ввод.

    Демон переживает ошибку обработчика (``reader._pump`` ловит Exception),
    поэтому залипший модификатор держался бы до перезапуска.
    """

    def _injector(self, fail_at: int | None = None) -> tuple[object, FailingUI]:
        from layout_fixer.injector import Injector

        injector = Injector.__new__(Injector)
        injector.key_delay = 0.0
        injector._ui = FailingUI() if fail_at is None else FailingUI(fail_at)
        return injector, injector._ui

    def test_happy_path_presses_and_releases(self) -> None:
        injector, ui = self._injector()
        injector.tap(e.KEY_Q, shift=True)
        self.assertEqual(
            [(e.KEY[code], value) for code, value in ui.events],
            [("KEY_LEFTSHIFT", 1), ("KEY_Q", 1), ("KEY_Q", 0), ("KEY_LEFTSHIFT", 0)],
        )
        self.assertEqual(ui.stuck(), [])

    def test_error_on_first_sync_still_releases_keys(self) -> None:
        injector, ui = self._injector(fail_at=1)
        with self.assertRaises(OSError):
            injector.tap(e.KEY_Q, shift=True)
        self.assertEqual(ui.stuck(), [], "клавиши обязаны быть отпущены даже при ошибке")

    def test_error_on_last_sync_does_not_escape(self) -> None:
        """Ошибка в финальном syn() гасится: отпуск клавиш важнее, чем причина.

        Иначе демон упал бы на ровном месте уже после того, как всё напечатал.
        """
        injector, ui = self._injector(fail_at=2)
        injector.tap(e.KEY_Q, altgr=True)  # исключения наружу не выходит
        self.assertEqual(ui.stuck(), [])

    def test_plain_key_error_leaves_nothing_stuck(self) -> None:
        injector, ui = self._injector(fail_at=1)
        with self.assertRaises(OSError):
            injector.tap(e.KEY_A)
        self.assertEqual(ui.stuck(), [])


class TestRevertTracking(PipelineTestCase):
    """Пользователь, стёрший исправление, — сигнал правды о ложных срабатываниях.

    Детектор сам не может себя проверить: он уверен в своей правоте на 0.95.
    Единственный признак, что правка была лишней, — пользователь тут же
    стёр введённый текст.
    """

    def _fix_one(self, daemon: Daemon) -> None:
        self.feed_text(daemon, "ghbdtn", "us")
        self.feed_char(daemon, " ", "us")

    def _erase(self, daemon: Daemon, count: int) -> None:
        for _ in range(count):
            daemon._handle_event(None, press(e.KEY_BACKSPACE, 1))
            daemon._handle_event(None, press(e.KEY_BACKSPACE, 0))

    def test_erasing_injected_word_records_revert(self) -> None:
        daemon, _ = self.make_daemon(mode="fix")
        self._fix_one(daemon)
        self._erase(daemon, 6)  # «привет» — ровно введённое слово
        self.assertEqual(daemon._stats["стёрто пользователем"], 1)
        self.assertIn("REVERT", self.log_path.read_text(encoding="utf-8"))

    def test_erasing_word_and_separator_records_revert(self) -> None:
        daemon, _ = self.make_daemon(mode="fix")
        self._fix_one(daemon)
        self._erase(daemon, 7)  # «привет » вместе с пробелом
        self.assertEqual(daemon._stats["стёрто пользователем"], 1)

    def test_partial_erasure_is_not_a_revert(self) -> None:
        """Стёр пару букв и дописал — это обычная правка, не отмена исправления."""
        daemon, _ = self.make_daemon(mode="fix")
        self._fix_one(daemon)
        self._erase(daemon, 2)
        self.assertEqual(daemon._stats["стёрто пользователем"], 0)
        self.assertNotIn("REVERT", self.log_path.read_text(encoding="utf-8"))

    def test_typing_after_fix_cancels_observation(self) -> None:
        daemon, _ = self.make_daemon(mode="fix")
        self._fix_one(daemon)
        self._erase(daemon, 2)
        self.feed_char(daemon, "ф", "ru")  # продолжил печатать — исправление прижилось
        self._erase(daemon, 6)
        self.assertEqual(daemon._stats["стёрто пользователем"], 0)

    def test_holding_backspace_counts(self) -> None:
        """Автоповтор при зажатом Backspace — тоже стирание."""
        daemon, _ = self.make_daemon(mode="fix")
        self._fix_one(daemon)
        for _ in range(6):
            daemon._handle_event(None, press(e.KEY_BACKSPACE, 2))
        self.assertEqual(daemon._stats["стёрто пользователем"], 1)

    def test_backspace_after_window_is_ignored(self) -> None:
        import layout_fixer.daemon as daemon_mod

        daemon, _ = self.make_daemon(mode="fix")
        self._fix_one(daemon)
        original = daemon_mod.REVERT_WINDOW_S
        daemon_mod.REVERT_WINDOW_S = -1  # окно уже истекло
        self.addCleanup(setattr, daemon_mod, "REVERT_WINDOW_S", original)
        self._erase(daemon, 6)
        self.assertEqual(daemon._stats["стёрто пользователем"], 0)

    def _erase_ctrl(self, daemon: Daemon, count: int) -> None:
        """Стирание словом — Ctrl+Backspace, самый частый способ убрать слово.

        Такое нажатие сбрасывает буфер слова и выходит из обработчика раньше,
        чем путь Backspace. Если считать стирание там, отменённые правки
        молчали бы — ровно это и происходило.
        """
        for _ in range(count):
            daemon._handle_event(None, press(e.KEY_LEFTCTRL, 1))
            daemon._handle_event(None, press(e.KEY_BACKSPACE, 1))
            daemon._handle_event(None, press(e.KEY_BACKSPACE, 0))
            daemon._handle_event(None, press(e.KEY_LEFTCTRL, 0))

    def test_ctrl_backspace_erasure_is_counted(self) -> None:
        daemon, _ = self.make_daemon(mode="fix")
        self._fix_one(daemon)
        self._erase_ctrl(daemon, 6)
        self.assertEqual(daemon._stats["стёрто пользователем"], 1)
        self.assertIn("REVERT", self.log_path.read_text(encoding="utf-8"))

    def test_alt_backspace_erasure_is_counted(self) -> None:
        daemon, _ = self.make_daemon(mode="fix")
        self._fix_one(daemon)
        for _ in range(6):
            daemon._handle_event(None, press(e.KEY_LEFTALT, 1))
            daemon._handle_event(None, press(e.KEY_BACKSPACE, 1))
            daemon._handle_event(None, press(e.KEY_BACKSPACE, 0))
            daemon._handle_event(None, press(e.KEY_LEFTALT, 0))
        self.assertEqual(daemon._stats["стёрто пользователем"], 1)

    def test_shift_backspace_erasure_is_counted(self) -> None:
        daemon, _ = self.make_daemon(mode="fix")
        self._fix_one(daemon)
        for _ in range(6):
            daemon._handle_event(None, press(e.KEY_LEFTSHIFT, 1))
            daemon._handle_event(None, press(e.KEY_BACKSPACE, 1))
            daemon._handle_event(None, press(e.KEY_BACKSPACE, 0))
            daemon._handle_event(None, press(e.KEY_LEFTSHIFT, 0))
        self.assertEqual(daemon._stats["стёрто пользователем"], 1)

    def test_backspace_without_fix_is_harmless(self) -> None:
        daemon, _ = self.make_daemon(mode="fix")
        self.feed_text(daemon, "привет", "ru")
        self._erase(daemon, 3)
        self.assertEqual(daemon._stats["стёрто пользователем"], 0)

    def test_erasing_does_not_disable_next_fix(self) -> None:
        """Стереть введённое демоном слово не должно выключать автоправку дальше.

        Backspace по пустому буферу не означает «пользователь правил слово»,
        но раньше именно так и выходило: следующее слово молча не исправлялось.
        """
        daemon, recorder = self.make_daemon(mode="fix")
        self._fix_one(daemon)
        self._erase(daemon, 6)
        daemon.state.set_current("us")  # пользователь вернулся в английскую раскладку
        self._fix_one(daemon)  # то же самое слово во второй раз
        self.assertEqual(
            [text for text, _ in recorder.texts()].count("привет "),
            2,
            "после стирания первое исправление автоправка должна продолжать работать",
        )

    def test_pause_clears_observation(self) -> None:
        daemon, _ = self.make_daemon(mode="fix")
        self._fix_one(daemon)
        daemon._toggle_pause()
        self._erase(daemon, 6)
        self.assertEqual(daemon._stats["стёрто пользователем"], 0)

    def test_layout_switch_clears_observation(self) -> None:
        """Переключение раскладки — не Backspace, наблюдение снимается."""
        daemon, _ = self.make_daemon(mode="fix")
        self._fix_one(daemon)
        daemon._handle_event(None, press(daemon.state.actuator.code, 1))
        self._erase(daemon, 6)
        self.assertEqual(daemon._stats["стёрто пользователем"], 0)


class TestRevertReport(PipelineTestCase):
    def test_report_shows_false_positive_rate(self) -> None:
        from layout_fixer.decisions_log import Entry, summarize

        moment = datetime(2026, 9, 25, 12, 0, 0)

        def entry(kind: str, word: str, converted: str) -> Entry:
            return Entry(
                timestamp=moment,
                kind=kind,
                word=word,
                layout="us",
                converted=converted,
                target="ru",
                confidence=0.95,
                reason="",
            )

        entries = [entry("FIX", "ghbdtn", "привет") for _ in range(4)]
        entries.append(entry("REVERT", "ide", "шву"))
        report = summarize(entries, tail=0)
        self.assertIn("1 из 4", report)
        self.assertIn("25.0%", report)
        self.assertIn("'ide' -> 'шву'", report)


class TestSeparatorSafety(PipelineTestCase):
    """Запрещено только то, что реально нельзя откатить.

    Раньше требовалось попадание разделителя в рукописный белый список, и любой
    символ вне его отменял правку. За 12 отмен в журнале 4 были напрасными:
    «/» дважды и цифра «3» — их стирать и печатать заново совершенно безопасно.
    """

    def test_slash_separator_is_fixed(self) -> None:
        daemon, recorder = self.make_daemon(mode="fix")
        self.feed_text(daemon, "ghbdtn", "us")
        self.feed_char(daemon, "/", "us")
        self.assertEqual(recorder.texts(), [("привет/", "ru")])

    def test_digit_separator_is_fixed(self) -> None:
        """Русское слово в неверной раскладке перед цифрой: «ыек3» -> «str3»."""
        daemon, recorder = self.make_daemon(mode="fix")
        daemon.state.set_current("ru")
        self.feed_text(daemon, "ыек", "ru")
        self.feed_char(daemon, "3", "ru")
        self.assertEqual(recorder.texts(), [("str3", "us")])

    def test_enter_is_still_forbidden(self) -> None:
        daemon, recorder = self.make_daemon(mode="fix")
        self.feed_text(daemon, "ghbdtn", "us")
        self._press_and_release(daemon, e.KEY_ENTER)
        self.assertEqual(recorder.calls, [], "после Enter исправлять нельзя")

    def test_tab_is_still_forbidden(self) -> None:
        daemon, recorder = self.make_daemon(mode="fix")
        self.feed_text(daemon, "ghbdtn", "us")
        self._press_and_release(daemon, e.KEY_TAB)
        self.assertEqual(recorder.calls, [], "после Tab исправлять нельзя")

    def test_escape_is_still_forbidden(self) -> None:
        daemon, recorder = self.make_daemon(mode="fix")
        self.feed_text(daemon, "ghbdtn", "us")
        self._press_and_release(daemon, e.KEY_ESC)
        self.assertEqual(recorder.calls, [])

    def test_shift_requiring_separator_is_fixed_after_release(self) -> None:
        """Для «(» физический Shift зажат, поэтому правка ждёт его отпускания.

        Раньше это было отменой навсегда: ни «(», ни «?», ни «&» не исправлялись.
        Теперь ждём отпускания Shift — это десятки миллисекунд.
        """
        daemon, recorder = self.make_daemon(mode="fix")
        self.feed_text(daemon, "ghbdtn", "us")
        self.feed_char(daemon, "(", "us")
        self.assertEqual(recorder.texts(), [("привет(", "ru")])

    def test_separator_absent_in_target_layout_is_refused(self) -> None:
        """В русской раскладке «[» — это «х», напечатать «[» там нечем."""
        daemon, recorder = self.make_daemon(mode="fix")
        self.feed_text(daemon, "ghbdtn", "us")
        self.feed_char(daemon, "[", "us")  # неоднозначный символ: пока непонятно, буква или знак
        self.feed_char(daemon, " ", "us")  # пробел решает, что это знак
        self.assertEqual(recorder.calls, [])
        self.assertIn("нельзя напечатать", self.log_path.read_text(encoding="utf-8"))

    def test_separator_untypable_in_target_is_refused(self) -> None:
        daemon, _ = self.make_daemon(mode="fix")
        decision = daemon.detector.decide("ghbdtn", "us")
        allowed, why = daemon.fixer.can_apply(decision, "☃")
        self.assertFalse(allowed)
        self.assertIn("нельзя напечатать", why)

    def test_multi_char_separator_is_refused(self) -> None:
        """apply() стирает ровно len(separator) символов Backspace'ом."""
        daemon, _ = self.make_daemon(mode="fix")
        decision = daemon.detector.decide("ghbdtn", "us")
        allowed, why = daemon.fixer.can_apply(decision, "\n\n")
        self.assertFalse(allowed)
        self.assertIn("не один символ", why)

    @staticmethod
    def _press_and_release(daemon: Daemon, code: int) -> None:
        daemon._handle_event(None, press(code, 1))
        daemon._handle_event(None, press(code, 0))


class TestLayoutRestore(PipelineTestCase):
    """После правки раскладка остаётся ЦЕЛЕВОЙ: вы печатали русский текст в
    английской раскладке, значит продолжать нужно в русской.

    Раньше демон возвращал исходную раскладку, и после каждой опечатки приходилось
    жать Alt, чтобы вернуться к русскому. Это была неверная трактовка: «возвращать»
    означало «остаться в русской», а не «вернуть ту, что была до правки».
    """

    def test_layout_stays_in_target_after_fix(self) -> None:
        daemon, recorder = self.make_daemon(mode="fix")
        self.feed_text(daemon, "ghbdtn", "us")
        self.feed_char(daemon, " ", "us")
        self.assertEqual(daemon.state.current, "ru", "продолжаем в русской")

    def test_text_is_still_typed_in_target_layout(self) -> None:
        daemon, recorder = self.make_daemon(mode="fix")
        self.feed_text(daemon, "ghbdtn", "us")
        self.feed_char(daemon, " ", "us")
        self.assertEqual(recorder.texts(), [("привет ", "ru")], "слово печатается в целевой")
        self.assertEqual(recorder.backspaces(), [7], "стирается слово + пробел")

    def test_actuator_tapped_once(self) -> None:
        """Одно переключение: в целевую раскладку, возврата нет."""
        daemon, recorder = self.make_daemon(mode="fix")
        self.feed_text(daemon, "ghbdtn", "us")
        self.feed_char(daemon, " ", "us")
        self.assertEqual(recorder.actuator_taps(daemon.state.actuator.code), 1)

    def test_restore_can_be_enabled(self) -> None:
        """Явный opt-in: вернуть исходную раскладку (для тех, кто пишет латиницу)."""
        daemon, _ = self.make_daemon(mode="fix", restore_layout=True)
        self.feed_text(daemon, "ghbdtn", "us")
        self.feed_char(daemon, " ", "us")
        self.assertEqual(daemon.state.current, "us", "при restore_layout=true возвращаем")

    def test_no_extra_switch_when_already_in_target(self) -> None:
        """Слово набрано не в той раскладке, значит исходная и целевая разные.

        Проверяем, что без переключения возврата тоже нет: лишний актуатор
        мигал бы индикатором раскладки.
        """
        daemon, recorder = self.make_daemon(mode="fix")
        decision = daemon.detector.decide("ghbdtn", "us")
        daemon.state.set_current("us")
        before = recorder.actuator_taps(daemon.state.actuator.code)
        self.assertEqual(before, 0)
        # Решение требует смены раскладки, значит current != target всегда.
        self.assertNotEqual(decision.layout, decision.target_layout)

    def test_reverse_direction_stays_in_target(self) -> None:
        """Латиница, набранная в русской раскладке: печатаем в us и остаёмся в us."""
        daemon, recorder = self.make_daemon(mode="fix")
        daemon.state.set_current("ru")
        self.feed_text(daemon, "Ышдл", "ru")
        self.feed_char(daemon, " ", "ru")
        self.assertEqual(recorder.texts(), [("Silk ", "us")])
        self.assertEqual(daemon.state.current, "us")

    def test_pause_and_resume_keep_working(self) -> None:
        daemon, recorder = self.make_daemon(mode="fix")
        daemon._toggle_pause()
        self.feed_text(daemon, "ghbdtn", "us")
        self.feed_char(daemon, " ", "us")
        self.assertEqual(recorder.calls, [])


class TestShortWordWithLetterSeparator(PipelineTestCase):
    """Слово не должно рваться последней буквой другой раскладки.

    «dc`» — это «всё», где обратная кавычка в русской раскладке даёт «ё».
    Демон считал её знаком-разделителем, от слова оставалось «dc» — короче
    min_word_length, и исправления не было вовсе.
    """

    def test_word_ending_on_letter_key_is_fixed(self) -> None:
        daemon, recorder = self.make_daemon(mode="fix")
        self.feed_text(daemon, "dc`", "us")
        self.feed_char(daemon, " ", "us")
        self.assertEqual(recorder.texts(), [("всё ", "ru")])

    def test_does_not_break_trailing_punctuation(self) -> None:
        """«ghbdtn,» — это «привет» плюс запятая, а не слово «приветб»."""
        daemon, recorder = self.make_daemon(mode="fix")
        self.feed_text(daemon, "ghbdtn", "us")
        self.feed_char(daemon, ",", "us")
        self.feed_char(daemon, " ", "us")
        self.assertEqual(recorder.texts(), [("привет,", "ru")])

    def test_normal_short_word_still_skipped(self) -> None:
        """Настоящее слово из одной-двух букв разделителем не станет."""
        daemon, recorder = self.make_daemon(mode="fix")
        self.feed_text(daemon, "ok", "us")
        self.feed_char(daemon, " ", "us")
        self.assertEqual(recorder.calls, [])

    def test_reverse_direction_grave(self) -> None:
        """«jnx`n» -> «отчёт»: ` внутри слова по-прежнему становится буквой."""
        daemon, recorder = self.make_daemon(mode="fix")
        self.feed_text(daemon, "jnx`n", "us")
        self.feed_char(daemon, " ", "us")
        self.assertEqual(recorder.texts(), [("отчёт ", "ru")])

    def test_long_word_ending_on_comma_keeps_comma(self) -> None:
        daemon, recorder = self.make_daemon(mode="fix")
        self.feed_text(daemon, "ghbdtn", "us")
        self.feed_char(daemon, ",", "us")
        self.feed_char(daemon, " ", "us")
        self.assertEqual(recorder.texts(), [("привет,", "ru")])


class TestShortWords(PipelineTestCase):
    """Предлоги и союзы — самые частые русские слова и самые частые опечатки.

    «на», «не», «но», «за», «по», «из» в неверной раскладке — это «yf», «yt»,
    «yj», «pf», «gj», «bp». Раньше порог длины отсекал их и они не исправлялись
    никогда: пользователь видел сплошное «слишком короткое».
    """

    def test_prepositions_are_fixed(self) -> None:
        """Те, чья латинская форма не является английским словом."""
        expected = {"yf": "на", "yt": "не", "yj": "но", "gj": "по", "lj": "до"}
        daemon, _ = self.make_daemon(mode="fix")
        for latin, cyrillic in expected.items():
            with self.subTest(word=latin):
                decision = daemon.detector.decide(latin, "us")
                self.assertTrue(decision.fix, f"{latin!r} -> {cyrillic!r} должно исправляться")
                self.assertEqual(decision.converted, cyrillic)

    def test_ambiguous_two_letter_token_is_left_alone(self) -> None:
        """«bp» и «pf» есть в en_US — значит это настоящая двусмысленность.

        Превращать «bp» в «из» было бы ложным срабатыванием: сокращение
        используют и в тексте, и в коде.
        """
        for latin, cyrillic in {"bp": "из", "pf": "за"}.items():
            with self.subTest(word=latin):
                daemon, _ = self.make_daemon(mode="fix")
                decision = daemon.detector.decide(latin, "us")
                self.assertFalse(decision.fix, f"{latin!r} есть в en_US — не трогаем")
                self.assertIn("обеих раскладок", decision.reason)

    def test_preposition_is_injected_in_pipeline(self) -> None:
        daemon, recorder = self.make_daemon(mode="fix")
        self.feed_text(daemon, "yf", "us")
        self.feed_char(daemon, " ", "us")
        self.assertEqual(recorder.texts(), [("на ", "ru")])

    def test_single_letter_still_refused(self) -> None:
        """Одна буква не даёт улик: «d» одинаково похоже на что угодно."""
        for latin, cyrillic in {"d": "в", "c": "с", "r": "к", "e": "у"}.items():
            with self.subTest(word=latin):
                daemon, _ = self.make_daemon(mode="fix")
                decision = daemon.detector.decide(latin, "us")
                self.assertFalse(decision.fix, f"{latin!r} -> {cyrillic!r} трогать нельзя")

    def test_latin_token_still_vetoed(self) -> None:
        """Регресс-тест: биграмм-вето для обычных слов не должен сломаться."""
        daemon, _ = self.make_daemon(mode="fix")
        decision = daemon.detector.decide("ide", "us")
        self.assertFalse(decision.fix, "«ide» -> «шву» по-прежнему не исправляем")

    def test_normal_short_word_not_turned_into_preposition(self) -> None:
        """Двухбуквенный латинский токен не должен превращаться в предлог."""
        for latin in ("ok", "no", "go", "hi"):
            with self.subTest(word=latin):
                daemon, _ = self.make_daemon(mode="fix")
                decision = daemon.detector.decide(latin, "us")
                self.assertFalse(decision.fix, f"{latin!r} есть в словаре us — верное слово")

    def test_can_be_disabled(self) -> None:
        daemon, _ = self.make_daemon(mode="fix", allow_short_words=False)
        decision = daemon.detector.decide("yf", "us")
        self.assertFalse(decision.fix)
        self.assertIn("слишком короткое", decision.reason)

    def test_real_three_letter_words_still_fixed_normally(self) -> None:
        """Обычные слова не должны потерять свой путь (уверенность 0.95)."""
        daemon, _ = self.make_daemon(mode="fix")
        decision = daemon.detector.decide("ghbdtn", "us")
        self.assertTrue(decision.fix)
        self.assertEqual(decision.confidence, 0.95)


class TestExtraWords(PipelineTestCase):
    """Системный словарь hunspell неполон: «перевёт», «столово» в нём нет.

    Такие слова демон определить не может, и включение биграмм ради них
    опасно: на истории use_bigram дал бы на каждый верный ответ столько же
    мусора («ало», «лорлор», «рдай»). Поэтому слова дописываются явно.
    """

    def test_word_absent_from_dictionary_is_not_fixed(self) -> None:
        daemon, _ = self.make_daemon(mode="fix")
        self.assertFalse(daemon.spell.known("ru", "перевёт"), "словарь должен быть неполон")
        self.assertFalse(daemon.detector.decide("gthtd`n", "us").fix)

    def test_extra_word_makes_it_fixable(self) -> None:
        daemon, recorder = self.make_daemon(mode="fix", extra_words=["перевёт"])
        self.assertTrue(daemon.spell.known("ru", "перевёт"))
        self.feed_text(daemon, "gthtd`n", "us")
        self.feed_char(daemon, " ", "us")
        self.assertEqual(recorder.texts(), [("перевёт ", "ru")])

    def test_several_extra_words(self) -> None:
        daemon, _ = self.make_daemon(mode="fix", extra_words=["перевёт", "столово", "солово"])
        for cyrillic, latin in {"перевёт": "gthtd`n", "столово": "cnjkjdj"}.items():
            with self.subTest(word=cyrillic):
                decision = daemon.detector.decide(latin, "us")
                self.assertTrue(decision.fix, f"{latin!r} -> {cyrillic!r}")
                self.assertEqual(decision.converted, cyrillic)

    def test_known_word_is_not_duplicated(self) -> None:
        """Слово, уже известное словарю, дописывать незачем."""
        daemon, _ = self.make_daemon(mode="fix", extra_words=["слово"])
        self.assertTrue(daemon.spell.known("ru", "слово"))

    def test_garbage_word_still_refused(self) -> None:
        """Дописанные слова не дают права чинить произвольную абракадабру."""
        daemon, _ = self.make_daemon(mode="fix", extra_words=["перевёт"])
        for latin in ("fkj", "hlfq", "kjhkjh"):
            with self.subTest(word=latin):
                self.assertFalse(daemon.detector.decide(latin, "us").fix)

    def test_empty_list_is_harmless(self) -> None:
        daemon, recorder = self.make_daemon(mode="fix", extra_words=[])
        self.feed_text(daemon, "ghbdtn", "us")
        self.feed_char(daemon, " ", "us")
        self.assertEqual(recorder.texts(), [("привет ", "ru")])


    def test_word_goes_only_to_its_own_layout(self) -> None:
        """Русское слово дописывается в «ru» и НЕ в «en_US».

        Иначе детектор решит, что слово есть в обеих раскладках, и перестанет
        его исправлять — ровно то, ради чего его дописывали.
        """
        daemon, _ = self.make_daemon(mode="fix", extra_words=["перевёт"])
        self.assertTrue(daemon.spell.known("ru", "перевёт"))
        self.assertFalse(daemon.spell.known("us", "перевёт"), "в en_US русских букв не бывает")

    def test_latin_extra_word_goes_only_to_latin_layout(self) -> None:
        daemon, _ = self.make_daemon(mode="fix", extra_words=["verunya"])
        self.assertTrue(daemon.spell.known("us", "verunya"))
        self.assertFalse(daemon.spell.known("ru", "verunya"))

    def test_extra_word_still_fixable_after_own_layout_fix(self) -> None:
        """Дописанное слово должно и впредь исправляться, а не попадать в «обе раскладки»."""
        daemon, _ = self.make_daemon(mode="fix", extra_words=["перевёт"])
        decision = daemon.detector.decide("gthtd`n", "us")
        self.assertTrue(decision.fix)
        self.assertEqual(decision.converted, "перевёт")


class TestIdleFix(PipelineTestCase):
    """Исправление по паузе в наборе, а не по разделителю.

    9 отмен из 13 за всю историю были из-за Enter: слово распознавалось верно,
    но эффект от Enter уже наступил, отменять нечего. Единственный способ
    закрыть это — править слово по паузе, пока разделитель ещё не нажат.
    """

    def test_word_fixed_without_any_separator(self) -> None:
        daemon, recorder = self.make_daemon(mode="fix")
        self.feed_text(daemon, "hf,jnftn", "us")
        daemon._finish_word("")  # что делает цикл паузы
        self.assertEqual(recorder.texts(), [("работает", "ru")], "без разделителя и без пробела")

    def test_only_word_is_erased_not_separator(self) -> None:
        """На экране нет разделителя — стирать нечего."""
        daemon, recorder = self.make_daemon(mode="fix")
        self.feed_text(daemon, "ghbdtn", "us")
        daemon._finish_word("")
        self.assertEqual(recorder.backspaces(), [6], "только слово, не слово+пробел")

    def test_no_double_fix_after_space(self) -> None:
        """После правки по паузе пробел не должен править слово второй раз."""
        daemon, recorder = self.make_daemon(mode="fix")
        self.feed_text(daemon, "ghbdtn", "us")
        daemon._finish_word("")
        self.feed_char(daemon, " ", "us")
        self.assertEqual(recorder.texts(), [("привет", "ru")], "одна правка, не две")

    def test_enter_after_idle_fix_works(self) -> None:
        """Тот самый случай: слово + Enter. Пауза успела исправить до Enter."""
        daemon, recorder = self.make_daemon(mode="fix")
        self.feed_text(daemon, "ghbdtn", "us")
        daemon._finish_word("")
        self._press_and_release(daemon, e.KEY_ENTER)
        self.assertEqual(recorder.texts(), [("привет", "ru")])
        self.assertEqual(daemon._stats["отменено"], 0, "Enter не должен отменять — слово уже исправлено")

    def test_layout_stays_in_target_after_idle_fix(self) -> None:
        daemon, _ = self.make_daemon(mode="fix")
        self.feed_text(daemon, "ghbdtn", "us")
        daemon._finish_word("")
        self.assertEqual(daemon.state.current, "ru", "после правки по паузе тоже остаёмся в ru")

    def test_correct_word_left_alone(self) -> None:
        daemon, recorder = self.make_daemon(mode="fix")
        self.feed_text(daemon, "hello", "us")
        daemon._finish_word("")
        self.assertEqual(recorder.calls, [], "правильное слово трогать нельзя")

    def test_short_word_with_letter_key_fixed_by_pause(self) -> None:
        """«dc`» без разделителя: отложенная кавычка остаётся частью слова."""
        daemon, recorder = self.make_daemon(mode="fix")
        self.feed_text(daemon, "dc`", "us")
        daemon._finish_word("")
        self.assertEqual(recorder.texts(), [("всё", "ru")])

    def test_empty_buffer_is_noop(self) -> None:
        daemon, recorder = self.make_daemon(mode="fix")
        daemon._finish_word("")
        self.assertEqual(recorder.calls, [])

    def test_idle_counter_tracked(self) -> None:
        daemon, _ = self.make_daemon(mode="fix")
        self.feed_text(daemon, "ghbdtn", "us")
        daemon._finish_word("")
        self.assertEqual(daemon._stats["исправлено по паузе"], 1)

    def test_space_fix_not_counted_as_idle(self) -> None:
        daemon, _ = self.make_daemon(mode="fix")
        self.feed_text(daemon, "ghbdtn", "us")
        self.feed_char(daemon, " ", "us")
        self.assertEqual(daemon._stats["исправлено"], 1)
        self.assertEqual(daemon._stats["исправлено по паузе"], 0)

    def test_idle_can_be_disabled(self) -> None:
        self.assertEqual(Config(idle_fix_ms=0).idle_fix, 0.0)

    @staticmethod
    def _press_and_release(daemon: Daemon, code: int) -> None:
        daemon._handle_event(None, press(code, 1))
        daemon._handle_event(None, press(code, 0))


class TestX11Focus(PipelineTestCase):
    """Чтение активного окна: только наблюдение, поведение не меняется."""

    def test_focus_failure_does_not_break_fixing(self) -> None:
        """Нет X11 / окно не читается — правка всё равно должна состояться."""
        daemon, recorder = self.make_daemon(mode="fix")
        daemon._focus.current = lambda: (_ for _ in ()).throw(RuntimeError("нет DISPLAY"))
        self.feed_text(daemon, "ghbdtn", "us")
        self.feed_char(daemon, " ", "us")
        self.assertEqual(recorder.texts(), [("привет ", "ru")])

    def test_app_name_is_logged(self) -> None:
        daemon, _ = self.make_daemon(mode="fix")
        daemon._focus.current = lambda: ("gnome-terminal", 4242)
        self.feed_text(daemon, "ghbdtn", "us")
        self.feed_char(daemon, " ", "us")
        line = self.log_path.read_text(encoding="utf-8").splitlines()[-1]
        self.assertEqual(line.split("\t")[8], "gnome-terminal")

    def test_unknown_app_leaves_field_empty(self) -> None:
        daemon, _ = self.make_daemon(mode="fix")
        daemon._focus.current = lambda: (None, None)
        self.feed_text(daemon, "ghbdtn", "us")
        self.feed_char(daemon, " ", "us")
        line = self.log_path.read_text(encoding="utf-8").splitlines()[-1]
        self.assertEqual(line.split("\t")[8], "")

    def test_old_log_lines_without_app_still_readable(self) -> None:
        """В журнале 7000 строк без девятой колонки — они должны читаться."""
        from layout_fixer.decisions_log import DecisionLog

        path = Path(self.log_path.parent) / "old.log"
        path.write_text(
            "2026-09-16T11:59:13\tFIX\tghbdtn\tus\tпривет\tru\t0.95\tстарая строка\n",
            encoding="utf-8",
        )
        entries = DecisionLog.read(path)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].converted, "привет")
        self.assertEqual(entries[0].app, "", "нет колонки — пусто, а не исключение")




class FakeDevice:
    """Заглушка устройства ввода: проверяем только путь и имя."""

    def __init__(self, path: str, name: str):
        self.path = path
        self.name = name

    def close(self) -> None:
        pass

    async def async_read_loop(self):
        await asyncio.sleep(3600)
        yield  # pragma: no cover




class TestPointerClick(PipelineTestCase):
    """Клик мышью делает позицию курсора ненадёжной.

    Раньше демон был слеп к мыши и правил слово по разделителю, даже если курсор
    уже стоял в другом месте: Backspace съедал символы перед новой позицией, и
    исправленное слово вставлялось в середину предложения.

    С варианта «сбрасывать буфер на клик» тоже отказались: кликами вы
    выделяете текст и встаёте на строку, за минуту набора 11 кликов съели два
    слова. Теперь клик только помечает «курсор мог уехать», а отказ делается
    в момент, когда правка действительно понадобилась.
    """

    def test_click_cancels_the_fix(self) -> None:
        daemon, recorder = self.make_daemon(mode="fix")
        self.feed_text(daemon, "ghbdtn", "us")
        daemon._on_pointer_click("mouse")
        self.feed_char(daemon, " ", "us")
        self.assertEqual(recorder.calls, [], "после клика текст трогать нельзя")
        self.assertEqual(daemon._stats["отменено"], 1)

    def test_cancellation_is_recorded(self) -> None:
        """Отказ должен попасть в журнал: по нему видно, сколько правок теряется."""
        daemon, _ = self.make_daemon(mode="fix")
        self.feed_text(daemon, "ghbdtn", "us")
        daemon._on_pointer_click("mouse")
        self.feed_char(daemon, " ", "us")
        line = self.log_path.read_text(encoding="utf-8").splitlines()[-1].split("\t")
        self.assertEqual(line[1], "CANCEL")
        self.assertIn("курсор мог уехать", line[7])

    def test_click_does_not_block_normal_words_logging(self) -> None:
        """Важно: обычные слова после клика должны попадать в журнал.

        Иначе мы потеряли бы данные наблюдения — не знали бы, что вообще
        набирается вокруг.
        """
        daemon, _ = self.make_daemon(mode="fix")
        daemon._on_pointer_click("mouse")
        self.feed_text(daemon, "ghbdtn", "us")
        self.feed_char(daemon, " ", "us")
        self.assertIn("ghbdtn", self.log_path.read_text(encoding="utf-8"))

    def test_correct_word_after_click_needs_no_cancellation(self) -> None:
        """Слово, которое и так верное, отменять нечего."""
        daemon, recorder = self.make_daemon(mode="fix")
        daemon._on_pointer_click("mouse")
        self.feed_text(daemon, "hello", "us")
        self.feed_char(daemon, " ", "us")
        self.assertEqual(recorder.calls, [])
        self.assertEqual(daemon._stats["отменено"], 0)

    def test_old_click_stops_blocking(self) -> None:
        """Отпечатали слово после клика — курсор точно после него, клик не мешает."""
        import layout_fixer.daemon as daemon_mod

        daemon, recorder = self.make_daemon(mode="fix")
        self.feed_text(daemon, "ghbdtn", "us")
        daemon._on_pointer_click("mouse")
        daemon._caret_click_at -= 3600  # клик был час назад
        self.feed_char(daemon, " ", "us")
        self.assertEqual(recorder.texts(), [("привет ", "ru")])
        self.assertEqual(daemon._stats["отменено"], 0)

    def test_buffer_survives_click(self) -> None:
        """Клик больше не стирает набранное слово из буфера."""
        daemon, _ = self.make_daemon(mode="fix")
        self.feed_text(daemon, "ghbdtn", "us")
        daemon._on_pointer_click("mouse")
        self.assertEqual(daemon.buffer.text, "ghbdtn")

    def test_word_after_click_is_fixed_once_flag_expired(self) -> None:
        daemon, recorder = self.make_daemon(mode="fix")
        self.feed_text(daemon, "ghbdtn", "us")
        self.feed_char(daemon, " ", "us")
        daemon._on_pointer_click("mouse")
        self.feed_text(daemon, "rfr", "us")
        self.feed_char(daemon, " ", "us")
        self.assertEqual(recorder.texts(), [("привет ", "ru")], "первое слово не тронуто")

    def test_arrow_key_clears_the_flag(self) -> None:
        """Стрелка сама ставит курсор — после неё клик уже ни при чём."""
        daemon, recorder = self.make_daemon(mode="fix")
        daemon._on_pointer_click("mouse")
        daemon._handle_event(None, press(e.KEY_LEFT, 1))
        self.feed_text(daemon, "ghbdtn", "us")
        self.feed_char(daemon, " ", "us")
        self.assertEqual(recorder.texts(), [("привет ", "ru")])


    def test_click_before_typing_does_not_block(self) -> None:
        """Кликнули, ПОТОМ напечатали слово — правка проходит.

        Реальный случай из жалобы: мышь кликнули и пошевелили (467 раз в час), а
        слово набрали следом. Раньше проверка смотрела только на время клика и
        отменяла всё, набранное в следующие секунды: демон слово узнавал, но
        правку отменял — выглядело как «перестал переводить».
        """
        daemon, recorder = self.make_daemon(mode="fix")
        self.feed_text(daemon, "ghb", "us")
        daemon._on_pointer_click("mouse", moved=True)      # клик ПОСЛЕ слова
        self.feed_char(daemon, " ", "us")
        self.assertEqual(recorder.texts(), [], "клик после слова — курсор мог уехать")

        daemon2, rec2 = self.make_daemon(mode="fix")
        daemon2._on_pointer_click("mouse", moved=True)     # клик ДО слова
        self.feed_text(daemon2, "ghb", "us")
        self.feed_char(daemon2, " ", "us")
        self.assertEqual(rec2.texts(), [("при ", "ru")], "клик до слова не мешает")
        self.assertEqual(daemon2._stats["отменено"], 0)

    def test_click_after_last_letter_still_blocks(self) -> None:
        daemon, recorder = self.make_daemon(mode="fix")
        self.feed_text(daemon, "ghb", "us")
        daemon._on_pointer_click("mouse", moved=True)
        self.feed_char(daemon, " ", "us")
        self.assertEqual(recorder.calls, [], "курсор уехал — текст трогать нельзя")

    def test_ensure_recovers_from_wrong_bookkeeping(self) -> None:
        """ensure() сверяется с реальностью, а не со своим индексом.

        Реальная поломка: демон считал, что работает в английской, хотя
        система была в русской. ensure() верил своему индексу, нажимал
        ScrollLock — и русская раскладка уезжала в английскую вместо того,
        чтобы остаться. Правка печаталась в чужой раскладке.
        """
        daemon, _ = self.make_daemon(mode="fix")
        # Реальность — русская, а демон уверен, что английская.
        daemon.state._group_reader = lambda layouts: 1
        daemon.state.set_current("us")

        self.assertEqual(daemon.state.ensure("ru"), False, "переключать нечего: уже ru")
        self.assertEqual(daemon.state.current, "ru")

        self.assertEqual(daemon.state.ensure("ru"), False)
        self.assertEqual(daemon.state.current, "ru")

    def test_fix_does_not_flip_layout_away(self) -> None:
        """Правка в русскую не должна уводить систему в английскую."""
        daemon, recorder = self.make_daemon(mode="fix")
        daemon.state._group_reader = lambda layouts: layouts.index(daemon.state.current)
        self.feed_text(daemon, "ghb", "us")
        self.feed_char(daemon, " ", "us")
        self.assertEqual(recorder.texts(), [("при ", "ru")])
        self.assertEqual(
            daemon.state.current, "ru",
            "после правки должна остаться русская, а не вернуться в us",
        )

    def test_click_without_movement_does_not_block(self) -> None:
        """Клик без перемещения курсор не двигает — правка проходит.

        За 40 минут мышь кликает 149 раз, и большинство кликов не двигают курсор.
        Блокировать после каждого значило бы отменять почти всё: четыре отмены
        подряд с причиной «курсор мог уехать» так и вышли.
        """
        daemon, recorder = self.make_daemon(mode="fix")
        self.feed_text(daemon, "ghbdtn", "us")
        daemon._on_pointer_click("mouse", moved=False)
        self.feed_char(daemon, " ", "us")
        self.assertEqual(recorder.texts(), [("привет ", "ru")])
        self.assertEqual(daemon._stats["отменено"], 0)

    def test_click_with_movement_still_blocks(self) -> None:
        daemon, recorder = self.make_daemon(mode="fix")
        self.feed_text(daemon, "ghbdtn", "us")
        daemon._on_pointer_click("mouse", moved=True)
        self.feed_char(daemon, " ", "us")
        self.assertEqual(recorder.calls, [], "курсор уехал — текст трогать нельзя")

    def test_click_without_movement_keeps_word_in_buffer(self) -> None:
        daemon, _ = self.make_daemon(mode="fix")
        self.feed_text(daemon, "ghbdtn", "us")
        daemon._on_pointer_click("mouse", moved=False)
        self.assertEqual(daemon.buffer.text, "ghbdtn")


    def test_new_device_triggers_group_cleanup(self) -> None:
        """Подключение устройства чинит список раскладок сразу, не по таймеру.

        Новое устройство ввода добавляет группу XKB, и список «us, ru, ru»
        ломает переключение: ScrollLock циклит us -> ru -> ru(ничего) -> us,
        то есть нажатие из русской выглядит как «ничего не произошло».
        Раньше починка шла раз в 30 секунд, и пользователь успевал нажать
        в сломанном окне.
        """
        async def scenario() -> None:
            calls: list[int] = []
            reader = KeyboardReader(
                lambda d, ev: None, on_device_change=lambda: calls.append(1)
            )
            self.assertEqual(calls, [], "до подключения устройства чистить нечего")

            added = FakeDevice(path="/dev/input/event99", name="ydotoold virtual device")
            reader._sync_devices([added])
            self.assertEqual(len(calls), 1, "подключение устройства должно звать починку")

            reader._sync_devices([added])
            self.assertEqual(len(calls), 1, "повторный опрос без новых устройств звать не должен")

            reader._sync_devices([added, FakeDevice(path="/dev/input/event98", name="usb kbd")])
            self.assertEqual(len(calls), 2, "второе новое устройство — снова починка")
            reader.stop()

        asyncio.run(scenario())


class TestTargetLayoutDirection(PipelineTestCase):
    """Обратное направление вредно: демон переписывает русское в латиницу.

    Замер за два дня: us->ru — 24 правки, одно ложное срабатывание (4%);
    ru->us — 6 правок, два ложных (33%). Причём ложными были «рф» -> «ha»,
    «уда» -> «elf», «акуувщь» -> «freedom»: русская раскладка включена
    сознательно, а демон решил, что имелось в виду английское слово.
    """

    def test_reverse_direction_is_refused(self) -> None:
        daemon, _ = self.make_daemon(mode="fix", target_layout="ru")
        for word, latin in {"рф": "ha", "уда": "elf", "акуувщь": "freedom"}.items():
            with self.subTest(word=word):
                decision = daemon.detector.decide(word, "ru")
                self.assertFalse(decision.fix, f"{word!r} -> {latin!r} переписывать нельзя")
                self.assertIn("обратное направление", decision.reason)

    def test_forward_direction_still_works(self) -> None:
        daemon, recorder = self.make_daemon(mode="fix", target_layout="ru")
        self.feed_text(daemon, "ghbdtn", "us")
        self.feed_char(daemon, " ", "us")
        self.assertEqual(recorder.texts(), [("привет ", "ru")])

    def test_reverse_direction_never_injects(self) -> None:
        daemon, recorder = self.make_daemon(mode="fix", target_layout="ru")
        daemon.state.set_current("ru")
        self.feed_text(daemon, "рф", "ru")
        self.feed_char(daemon, " ", "ru")
        self.assertEqual(recorder.calls, [], "текст трогать нельзя")

    def test_zero_margin_restores_old_behaviour(self) -> None:
        """reverse_bigram_margin = 0 — доверять только словарю, как было раньше."""
        daemon, _ = self.make_daemon(mode="fix", reverse_bigram_margin=0.0)
        decision = daemon.detector.decide("рф", "ru")
        self.assertTrue(decision.fix, "при нулевом пороге «рф» -> «ha» снова проходит")
        self.assertEqual(decision.target_layout, "us", "обратное направление работает")

    def test_wrong_target_layout_keeps_reverse(self) -> None:
        """Целевая us — тогда наоборот, в ru->us исправляем."""
        daemon, _ = self.make_daemon(mode="fix", target_layout="us")
        self.assertTrue(daemon.detector.decide("рф", "ru").fix)


class TestReverseDirectionBigrams(PipelineTestCase):
    """Обратное направление нужно, но под словарём оно врёт.

    Латиница, набранная в русской раскладке («Ышдл» -> «Silk»), исправлять
    надо. Но «рф» -> «ha» и «уда» -> «elf» словарём тоже подтверждены, а
    вы их не набирали — русская раскладка была включена осознанно.

    Различает биграмма. Замер за два дня: нужные правки давали перевес
    +3.21…+5.50, ложные +2.35…-0.52. Порог 3.0 стоит между ними.
    """

    NEEDED = {"Ышдл": "Silk", "щзут": "open", "еуые": "test", "сщтмуке": "convert", "дщпщ": "logo"}
    UNWANTED = {"рф": "ha", "уда": "elf", "ет": "tn", "акуувщь": "freedom", "ге": "ef"}

    def test_latin_typed_in_russian_layout_is_fixed(self) -> None:
        for cyrillic, latin in self.NEEDED.items():
            with self.subTest(word=cyrillic):
                daemon, _ = self.make_daemon(mode="fix")
                decision = daemon.detector.decide(cyrillic, "ru")
                self.assertTrue(decision.fix, f"{cyrillic!r} -> {latin!r} должно исправляться")
                self.assertEqual(decision.converted.lower(), latin.lower())

    def test_deliberate_russian_is_left_alone(self) -> None:
        for cyrillic, latin in self.UNWANTED.items():
            with self.subTest(word=cyrillic):
                daemon, _ = self.make_daemon(mode="fix")
                decision = daemon.detector.decide(cyrillic, "ru")
                self.assertFalse(decision.fix, f"{cyrillic!r} -> {latin!r} трогать нельзя")

    def test_pipeline_injects_reverse_fix(self) -> None:
        daemon, recorder = self.make_daemon(mode="fix")
        daemon.state.set_current("ru")
        self.feed_text(daemon, "Ышдл", "ru")
        self.feed_char(daemon, " ", "ru")
        self.assertEqual(recorder.texts(), [("Silk ", "us")])

    def test_forward_direction_unaffected(self) -> None:
        daemon, recorder = self.make_daemon(mode="fix")
        self.feed_text(daemon, "ghbdtn", "us")
        self.feed_char(daemon, " ", "us")
        self.assertEqual(recorder.texts(), [("привет ", "ru")])

    def test_forward_short_words_still_work(self) -> None:
        daemon, _ = self.make_daemon(mode="fix")
        self.assertTrue(daemon.detector.decide("yf", "us").fix, "«yf» -> «на» должно работать")

    def test_stricter_margin_is_more_careful(self) -> None:
        """Порог выше — правок меньше, ложных точно не больше."""
        daemon, _ = self.make_daemon(mode="fix", reverse_bigram_margin=5.9)
        self.assertFalse(daemon.detector.decide("ыщдл", "ru").fix, "5.9 отсекает даже нужные")

    def test_margin_can_be_disabled(self) -> None:
        """Нулевой порог = доверить словарю, как было до этого правила."""
        daemon, _ = self.make_daemon(mode="fix", reverse_bigram_margin=0.0)
        self.assertTrue(daemon.detector.decide("рф", "ru").fix)

    def test_huge_margin_disables_reverse_entirely(self) -> None:
        """Способ отключить обратное направление одной цифрой."""
        daemon, _ = self.make_daemon(mode="fix", reverse_bigram_margin=10.0)
        self.assertFalse(daemon.detector.decide("Ышдл", "ru").fix)
        self.assertTrue(daemon.detector.decide("ghbdtn", "us").fix, "прямое направление живо")


class TestSeparatorNotInTargetLayout(PipelineTestCase):
    """Символа «&» в русской раскладке нет — на этой клавише в русской «?».

    Если вы нажали русский «?», а раскладка была английская, после переключения
    надо печатать «?» — ровно как слово. Раньше правка отменялась, и слова с
    вопросительным знаком не исправлялись никогда.
    """

    def test_ampersand_becomes_question_mark(self) -> None:
        daemon, recorder = self.make_daemon(mode="fix")
        self.feed_text(daemon, "ghbdtn&", "us")
        self.assertEqual(recorder.texts(), [("привет?", "ru")])

    def test_shift_no_longer_erases_the_word(self) -> None:
        """Корень проблемы: decode() для Shift давал None, и буфер обнулялся."""
        daemon, _ = self.make_daemon(mode="fix")
        self.feed_text(daemon, "ghbdtn", "us")
        self.assertEqual(daemon.buffer.text, "ghbdtn")
        daemon._handle_event(None, press(e.KEY_LEFTSHIFT, 1))
        self.assertEqual(daemon.buffer.text, "ghbdtn", "Shift не должен стирать слово")

    def test_word_with_ampersand_is_fixed(self) -> None:
        daemon, recorder = self.make_daemon(mode="fix")
        self.feed_text(daemon, "dsrk.xbnm&", "us")
        self.feed_char(daemon, " ", "us")
        # Знак сам становится разделителем и печатается вместе со словом.
        self.assertEqual(recorder.texts(), [("выключить?", "ru")])

    def test_backspace_count_uses_actual_typed_length(self) -> None:
        """Стираем ровно то, что на экране: слово и один знак."""
        daemon, recorder = self.make_daemon(mode="fix")
        self.feed_text(daemon, "ghbdtn&", "us")
        self.assertEqual(recorder.backspaces(), [7])

    def test_bracket_is_still_refused(self) -> None:
        """«[» в русской раскладке — это «Х», но знак подменять буквой нельзя."""
        daemon, recorder = self.make_daemon(mode="fix")
        self.feed_text(daemon, "ghbdtn", "us")
        self.feed_char(daemon, "[", "us")
        self.feed_char(daemon, " ", "us")
        self.assertEqual(recorder.calls, [], "подставлять букву вместо знака нельзя")

    def test_plain_punctuation_untouched(self) -> None:
        """Обычные знаки печатаются как есть, без подстановок."""
        for sep, expect in ((",", "привет,"), (".", "привет."), ("!", "привет!")):
            with self.subTest(sep=sep):
                daemon, recorder = self.make_daemon(mode="fix")
                self.feed_text(daemon, "ghbdtn", "us")
                self.feed_char(daemon, sep, "us")
                self.feed_char(daemon, " ", "us")
                self.assertEqual(recorder.texts(), [(expect, "ru")])


class TestInjectorCoverage(unittest.TestCase):
    """Виртуальная клавиатура обязана уметь нажать любую клавишу любой раскладки.

    uinput молча игнорирует события для клавиш, не объявленных в ``INJECT_KEYS``:
    «,» и «.» просто не доходили до демона (а это ещё и буквы б/ю в русской
    раскладке), из-за чего исправление половины русских слов не работало.
    """

    def test_every_printable_layout_key_is_declared(self) -> None:
        from layout_fixer.injector import INJECT_KEYS
        from layout_fixer.xkb import LayoutTables

        tables = LayoutTables(["us", "ru"])
        declared = set(INJECT_KEYS)
        missing: dict[str, set[str]] = {}
        for layout in tables.layouts:
            for code, levels in tables.table(layout).items():
                # Служебные клавиши (Enter, Esc, Tab) дают управляющие символы —
                # их объявление проверять не нужно.
                printable = {text for text in levels.values() if len(text) == 1 and text.isprintable()}
                if printable and code not in declared:
                    missing.setdefault(layout, set()).update(printable)
        self.assertEqual(missing, {}, f"клавиши не объявлены в INJECT_KEYS: {missing}")


if __name__ == "__main__":
    unittest.main()

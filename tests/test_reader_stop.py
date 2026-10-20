"""Остановка читающего потока клавиатуры.

История: _read_blocking висел в evdev.read_loop() до следующей клавиши,
поток не-daemon не давал процессу выйти — каждая остановка сервиса длилась
ровно TimeoutStopSec (90 с), после чего systemd звал SIGKILL. Теперь чтение
опрашивается select'ом с таймаутом 0.3 с, и поток обязан выйти сам.
"""

import os
import threading
import time
import unittest

import layout_fixer.reader as reader_mod


class FakeInputDevice:
    """Оболочка с fd, пригодным для select, и тихим read().

    evdev.InputDevice в тест недоступен (нужен настоящий evdev-узел);
    для семантики остановки достаточно fd с отсутствием данных и
    read(), который никогда не вызывается.
    """

    def __init__(self) -> None:
        self._r, self._w = os.pipe()
        self.read_called = False

    @property
    def fd(self) -> int:
        return self._r

    def read(self):
        self.read_called = True
        return []


class ReaderStopTest(unittest.TestCase):
    def _reader(self) -> reader_mod.KeyboardReader:
        reader = object.__new__(reader_mod.KeyboardReader)
        reader._stop = threading.Event()
        reader._tasks = {}
        reader._devices = {}
        reader.on_event = lambda device, event: None
        return reader

    def test_thread_exits_when_never_readable(self):
        reader = self._reader()
        device = FakeInputDevice()
        thread = threading.Thread(
            target=reader._read_blocking, args=(device, None), daemon=False
        )
        started = time.monotonic()
        thread.start()
        # не подаём ни одного события: прежний код не вышел бы никогда
        reader._stop.set()
        thread.join(timeout=2.0)
        finished = time.monotonic() - started

        self.assertFalse(thread.is_alive(), "поток чтения не вышел за 2 с")
        self.assertLess(finished, 2.0, "остановка обязана быть быстрее таймаута")
        self.assertFalse(device.read_called, "read не должен звать для не-каких событий")
        os.close(device._r)
        os.close(device._w)

    def test_thread_stays_alive_until_stop(self):
        # стоп НЕ взведён: поток должен жить, значит select ждёт свои 0.3 с
        reader = self._reader()
        device = FakeInputDevice()
        thread = threading.Thread(target=reader._read_blocking, args=(device, None))
        thread.start()
        time.sleep(0.1)
        self.assertTrue(thread.is_alive())
        reader._stop.set()
        thread.join(timeout=2.0)
        self.assertFalse(thread.is_alive())
        os.close(device._r)
        os.close(device._w)


if __name__ == "__main__":
    unittest.main()

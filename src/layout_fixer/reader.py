"""Чтение событий со всех клавиатур системы.

Особенности:
* события читаются БЕЗ ``grab()`` — клавиатура остаётся полностью рабочей,
  мы лишь подслушиваем поток;
* поддерживается несколько клавиатур (встроенная + внешняя) и горячее
  подключение: список устройств периодически пересканируется;
* собственная виртуальная клавиатура (``Injector``) исключается, иначе
  инъекции попадали бы в наш же обработчик и вызывали рекурсию.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable, Iterable

import evdev
from evdev import InputDevice, ecodes as e

from .injector import VIRTUAL_DEVICE_NAME

log = logging.getLogger(__name__)

EventCallback = Callable[[InputDevice, evdev.InputEvent], None]


def is_keyboard(device: InputDevice) -> bool:
    """Похоже ли устройство на клавиатуру (есть буквенные клавиши)."""
    keys = device.capabilities().get(e.EV_KEY, [])
    return e.KEY_A in keys and e.KEY_Z in keys and e.KEY_SPACE in keys


def is_pointer(device: InputDevice) -> bool:
    """Похоже ли устройство на мышь (есть относительное перемещение)."""
    return bool(device.capabilities().get(e.EV_REL))


def find_pointers(
    exclude_names: Iterable[str] = (VIRTUAL_DEVICE_NAME,),
) -> list[InputDevice]:
    """Устройства с относительным перемещением — мыши, тачпады, трекболы."""
    excluded = set(exclude_names)
    found: list[InputDevice] = []
    for path in sorted(evdev.list_devices()):
        try:
            device = evdev.InputDevice(path)
        except (PermissionError, OSError):
            continue
        if device.name in excluded or not is_pointer(device):
            device.close()
            continue
        found.append(device)
    return found


class PointerWatcher:
    """Следит за мышью, чтобы знать, когда курсор уехал.

    Демон перед правкой считает, что курсор стоит ровно после набранного слова,
    и стирает это число символов назад. Клик мышью ломает это предположение
    молча: Backspace съедает символы перед новой позицией, и исправленное слово
    вставляется в середину предложения. Клавиатурные события о таком не
    сообщают — раньше демон был слеп к мыши целиком.

    Но ловить надо не каждый клик, а только клик **с перемещением**: за 40 минут
    обычной работы мышь кликает 149 раз, и большинство из них не двигают курсор
    (переключение вкладки, выбор текста рядом, закрытие меню). Если блокировать
    правку после каждого клика, демон перестаёт исправлять почти всё: за день
    ушло четыре отмены подряд с причиной «курсор мог уехать».

    Поэтому ``on_click`` получает признак ``moved``: были ли события ``EV_REL``
    (движение указателя) между двумя кликами.
    """

    def __init__(
        self,
        on_click,
        exclude_names: Iterable[str] = (VIRTUAL_DEVICE_NAME,),
        rescan_interval: float = 15.0,
    ):
        self.on_click = on_click
        self.exclude_names = tuple(exclude_names)
        self.rescan_interval = rescan_interval
        self._devices: dict[str, InputDevice] = {}
        self._moved = False

    async def run(self) -> None:
        while True:
            # Поиск открывает КАЖДОЕ устройство ввода и занимает ~190 мс.
            # В event loop это останавливало чтение клавиатуры на 190 мс каждые
            # две секунды, и слова начинали «переключаться дольше». Поэтому
            # поиск идёт в отдельном потоке, а учёт и создание задач — в цикле
            # (создавать задачи из чужого потока нельзя, нет event loop).
            found = await asyncio.to_thread(find_pointers, self.exclude_names)
            self._sync(found)
            await asyncio.sleep(self.rescan_interval)

    def _sync(self, found: list[InputDevice]) -> None:
        """Учёт найденных мышей — обязательно в event loop."""
        known = {device.path: device for device in found}
        for path in list(self._devices):
            if path not in known:
                device = self._devices.pop(path)
                log.info("мышь отключена: %s (%s)", device.name, path)
                with contextlib.suppress(Exception):
                    device.close()
        for path, device in known.items():
            if path in self._devices:
                device.close()
                continue
            self._devices[path] = device
            log.info("слежу за мышью: %s (%s)", device.name, path)
            asyncio.create_task(self._pump(device), name=f"mouse:{path}")

    async def _pump(self, device: InputDevice) -> None:
        try:
            async for event in device.async_read_loop():
                if event.type == e.EV_REL:
                    self._moved = True
                elif event.type == e.EV_KEY and event.value == 1:
                    self.on_click(device.name, self._moved)
                    self._moved = False
        except (OSError, asyncio.CancelledError):
            return

    def close(self) -> None:
        for device in self._devices.values():
            with contextlib.suppress(Exception):
                device.close()
        self._devices.clear()


def find_keyboards(exclude_names: Iterable[str] = (VIRTUAL_DEVICE_NAME,)) -> list[InputDevice]:
    """Все доступные клавиатуры, кроме исключённых по имени."""
    excluded = set(exclude_names)
    found: list[InputDevice] = []
    for path in sorted(evdev.list_devices()):
        try:
            device = evdev.InputDevice(path)
        except (PermissionError, OSError):
            # Нет прав или устройство уже исчезло — пропускаем молча.
            continue
        if device.name in excluded or not is_keyboard(device):
            device.close()
            continue
        found.append(device)
    return found


class KeyboardReader:
    """Асинхронный читатель всех клавиатур с поддержкой горячего подключения."""

    def __init__(
        self,
        on_event: EventCallback,
        exclude_names: Iterable[str] = (VIRTUAL_DEVICE_NAME,),
        rescan_interval: float = 2.0,
        on_device_change: Callable[[], None] | None = None,
    ):
        self.on_event = on_event
        self.exclude_names = tuple(exclude_names)
        self.rescan_interval = rescan_interval
        self.on_device_change = on_device_change
        self._devices: dict[str, InputDevice] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._stop = asyncio.Event()

    # --- управление -----------------------------------------------------
    @property
    def devices(self) -> list[InputDevice]:
        return list(self._devices.values())

    def _sync_devices(self, found: list[InputDevice] | None = None) -> None:
        """Подхватить новые клавиатуры и отпустить пропавшие.

        Список найденных можно передать заранее: поиск открывает каждое
        устройство ввода и занимает ~130 мс. Делать это в event loop нельзя —
        чтение клавиатуры встаёт на это время и слова начинают обрабатываться
        с задержкой. Учёт устройств и создание задач чтения остаётся в цикле.
        """
        added = False
        for path, device in list(self._devices.items()):
            task = self._tasks.get(path)
            if task is not None and task.done() and not task.cancelled():
                with contextlib.suppress(Exception):
                    task.result()
                self._forget(path)
                continue
            if task is None:
                self._forget(path)

        for device in found if found is not None else find_keyboards(self.exclude_names):
            path = device.path
            if path in self._devices:
                device.close()
                continue
            log.info("подключена клавиатура: %s (%s)", device.name, path)
            self._devices[path] = device
            self._tasks[path] = asyncio.create_task(self._pump(device), name=f"kbd:{path}")
            added = True

        # Новое устройство ввода заставляет xf86-input-evdev добавить группу
        # XKB. Со списком «us, ru, ru» переключение циклит
        # us -> ru -> ru(ничего) -> us, и нажатие из русской выглядит как
        # «ничего не произошло». Сообщаем сразу, не дожидаясь таймера:
        # задержка в пять секунд означала, что пользователь успевает нажать
        # в сломанном окне.
        if added and self.on_device_change is not None:
            self.on_device_change()

    def _forget(self, path: str) -> None:
        device = self._devices.pop(path, None)
        self._tasks.pop(path, None)
        if device is not None:
            log.info("клавиатура отключена: %s (%s)", device.name, path)
            with contextlib.suppress(Exception):
                device.close()

    async def _pump(self, device: InputDevice) -> None:
        try:
            async for event in device.async_read_loop():
                if event.type != e.EV_KEY:
                    continue
                try:
                    self.on_event(device, event)
                except Exception:  # ошибка обработчика не должна убивать поток чтения
                    log.exception("ошибка обработки события %s", event)
        except OSError:
            # Устройство отключено — это нормальный сценарий, rescan всё поправит.
            return

    async def run(self) -> None:
        await self._rescan_devices()
        if not self._devices:
            log.warning("клавиатуры не найдены — проверьте права на /dev/input/event*")
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.rescan_interval)
            except asyncio.TimeoutError:
                await self._rescan_devices()
        # stop() синхронный (просто взводит событие) — await здесь был бы ошибкой.
        self.stop()

    async def _rescan_devices(self) -> None:
        """Найти клавиатуры в отдельном потоке и обновить список в цикле."""
        found = await asyncio.to_thread(find_keyboards, self.exclude_names)
        self._sync_devices(found)

    def stop(self) -> None:
        self._stop.set()

    async def stop_async(self) -> None:
        self._stop.set()
        for task in list(self._tasks.values()):
            task.cancel()
        for task in list(self._tasks.values()):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks.clear()
        for path in list(self._devices):
            self._forget(path)

"""Активное окно X11: какое приложение сейчас в фокусе.

Зачем. README обещает, что он «не знает, какое приложение в фокусе»,
и объясняет это отсутствием способа. На Wayland это правда — там до окон не
дотянуться. На X11 (а у вас сессия именно такая) способ есть и он тривиален:
``XGetInputFocus`` отдаёт окно, ``_NET_WM_PID`` — процесс, а ``/proc/<pid>/comm``
— его имя. Это ровно то, чего не хватает для исключений по приложениям:
в терминале и в поле пароля автоправка вредна.

Модуль ничего не меняет в поведении демона — только сообщает. На Wayland или
без ``DISPLAY`` он молча выключается, и демон работает как раньше.

Привязка к X11 сделана через ctypes — тем же приёмом, что уже используется
для libhunspell в ``hunspell.py``, чтобы не тянуть ``python-xlib``.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import logging
import struct
from pathlib import Path

log = logging.getLogger(__name__)

# Окно без _NET_WM_PID: PointerRoot (1) или NoneWindow (0).
_NO_WINDOW = 0


class ActiveWindow:
    """Чтение активного окна. Метод ``current()`` ничего не меняет в системе."""

    def __init__(self) -> None:
        self._lib = None
        self._display = None
        self._pid_atom = 0
        self._available = False
        self._load()

    def _load(self) -> None:
        name = ctypes.util.find_library("X11")
        if not name:
            log.info("libX11 не найдена — активное окно недоступно")
            return
        try:
            lib = ctypes.CDLL(name)
            lib.XOpenDisplay.restype = ctypes.c_void_p
            lib.XOpenDisplay.argtypes = [ctypes.c_char_p]
            lib.XDefaultRootWindow.restype = ctypes.c_ulong
            lib.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
            lib.XGetInputFocus.restype = ctypes.c_int
            lib.XGetInputFocus.argtypes = [
                ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_ulong),
                ctypes.POINTER(ctypes.c_int),
            ]
            lib.XInternAtom.restype = ctypes.c_ulong
            lib.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
            lib.XGetWindowProperty.restype = ctypes.c_int
            lib.XGetWindowProperty.argtypes = [
                ctypes.c_void_p,
                ctypes.c_ulong,
                ctypes.c_ulong,
                ctypes.c_long,
                ctypes.c_long,
                ctypes.c_int,
                ctypes.c_ulong,
                ctypes.POINTER(ctypes.c_ulong),
                ctypes.POINTER(ctypes.c_int),
                ctypes.POINTER(ctypes.c_ulong),
                ctypes.POINTER(ctypes.c_ulong),
                ctypes.POINTER(ctypes.POINTER(ctypes.c_ubyte)),
            ]
            lib.XFree.argtypes = [ctypes.c_void_p]
        except (OSError, AttributeError) as exc:
            log.info("libX11 не пригодна (%s) — активное окно недоступно", exc)
            return

        display = lib.XOpenDisplay(None)  # None = из переменной DISPLAY
        if not display:
            log.info("XOpenDisplay вернул пусто — сессия не X11, активное окно недоступно")
            return
        self._lib = lib
        self._display = display
        self._pid_atom = lib.XInternAtom(display, b"_NET_WM_PID", 0)
        self._available = True
        log.info("активное окно X11 доступно (pid %s)", self.current()[1])

    @property
    def available(self) -> bool:
        return self._available

    def window(self) -> int:
        """Идентификатор сфокусированного окна (0 — нет)."""
        if not self._available:
            return _NO_WINDOW
        focused = ctypes.c_ulong()
        revert = ctypes.c_int()
        # Указатели обязательны: с None здесь segfault.
        if not self._lib.XGetInputFocus(
            self._display, ctypes.byref(focused), ctypes.byref(revert)
        ):
            return _NO_WINDOW
        return focused.value

    def _pid(self, window: int) -> int | None:
        """pid из свойства _NET_WM_PID окна (None — свойства нет)."""
        if not self._available or not window or not self._pid_atom:
            return None
        prop = ctypes.POINTER(ctypes.c_ubyte)()
        items = ctypes.c_ulong()
        actual_format = ctypes.c_int()
        actual_type = ctypes.c_ulong()
        after = ctypes.c_ulong()
        code = self._lib.XGetWindowProperty(
            self._display,
            ctypes.c_ulong(window),
            ctypes.c_ulong(self._pid_atom),
            0,
            1,
            0,
            0,
            ctypes.byref(actual_type),
            ctypes.byref(actual_format),
            ctypes.byref(items),
            ctypes.byref(after),
            ctypes.byref(prop),
        )
        if code != 0 or not prop or not items.value:
            return None
        # Свойство обязано быть 32-битным беззнаковым. Если формат другой
        # (например у временного окна), чтение четырёх байтов даст мусор.
        if actual_format.value != 32:
            return None
        try:
            # items считается в 32-битных единицах, читать надо 4 байта.
            return struct.unpack_from("<I", ctypes.string_at(prop, 4))[0]
        except struct.error:  # pragma: no cover - зависит от ответа сервера
            return None
        finally:
            self._lib.XFree(prop)

    def current(self) -> tuple[str | None, int | None]:
        """Имя процесса и его pid в активном окне: (None, None) — неизвестно.

        В журнале наблюдались ``kthreadd`` — потоки ядра, у которых окна нет.
        Это временные окна (меню, подсказки) с уже освобождённым pid. Отсекаем
        их по пустому ``cmdline``: у настоящего процесса он не пустой.
        """
        if not self._available:
            return None, None
        pid = self._pid(self.window())
        if not pid:
            return None, None
        try:
            name = Path(f"/proc/{pid}/comm").read_text(encoding="utf-8").strip()
            cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().strip(b"\0")
        except OSError:
            return None, pid
        if not cmdline:
            return None, pid
        return name or None, pid

    def close(self) -> None:
        if self._display and self._lib is not None:
            try:
                self._lib.XCloseDisplay.argtypes = [ctypes.c_void_p]
                self._lib.XCloseDisplay(self._display)
            except Exception:  # pragma: no cover - закрытие не критично
                pass
            self._display = None
            self._available = False

    def __del__(self) -> None:
        self.close()
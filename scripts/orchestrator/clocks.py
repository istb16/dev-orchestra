"""How much of a run the machine spent asleep.

``time.monotonic()`` measures a run's duration and drives its deadlines. Whether
it counts sleep is up to the platform: on Windows it is GetTickCount64 before
Python 3.13 and QueryPerformanceCounter from 3.13, and both keep advancing
while the machine is suspended; on Linux and macOS it stops. So on Windows a
run that spanned a sleep has the sleep inside its duration, and elsewhere it
never does.

The runtime budget should be charged only for time the machine was awake. On
Windows that needs a second clock, one that stops during sleep, and the sleep a
run spanned is its duration less the delta on that clock. Elsewhere there is no
second clock and nothing to subtract. See "What the runtime budget counts" in
``references/limits.md``.
"""

from __future__ import annotations

import sys
from typing import Callable, Optional

#: A difference below this is the two clocks' resolutions disagreeing, not a
#: sleep. The interrupt-time clock advances once per timer tick, about 15.6 ms
#: by default, and so does GetTickCount64, which ``time.monotonic()`` is before
#: Python 3.13; from 3.13 it is QueryPerformanceCounter, which is far finer.
#: Either way the two readings can each be off by up to a tick, so the gap
#: between them carries tens of milliseconds of noise, well under a second.
SLEEP_THRESHOLD_SECONDS = 1.0

_selected: Optional[Callable[[], float]] = None
_chosen = False


def _windows_awake_clock() -> Optional[Callable[[], float]]:
    if sys.platform != "win32":
        return None
    import ctypes

    try:
        query = ctypes.WinDLL("kernel32").QueryUnbiasedInterruptTime
    except (AttributeError, OSError):
        return None
    query.argtypes = [ctypes.POINTER(ctypes.c_ulonglong)]
    query.restype = ctypes.c_int

    def read() -> float:
        # 100 ns units, excluding the time the machine spent asleep.
        value = ctypes.c_ulonglong(0)
        if not query(ctypes.byref(value)):
            raise OSError("QueryUnbiasedInterruptTime failed")
        return value.value / 1e7

    try:
        read()
    except OSError:
        return None
    return read


def awake_clock() -> Optional[Callable[[], float]]:
    """A clock in seconds that stops while the machine sleeps, or None where
    ``time.monotonic()`` already is one. Selected once per process."""
    global _selected, _chosen
    if not _chosen:
        _selected = _windows_awake_clock()
        _chosen = True
    return _selected


class Stopwatch:
    """Reads the awake clock beside a run measured on ``time.monotonic()``.

    Constructed before the run's start is read, so choosing the clock never
    lands inside the interval it measures.
    """

    def __init__(self, awake: Optional[Callable[[], float]] = None) -> None:
        self._awake = awake if awake is not None else awake_clock()
        self._started: Optional[float] = None

    def start(self) -> None:
        if self._awake is None:
            return
        try:
            self._started = self._awake()
        except Exception:
            self._started = None

    def read(self, duration: float) -> float:
        """The part of ``duration`` spent asleep: 0 unless it is at least
        ``SLEEP_THRESHOLD_SECONDS``, and never more than ``duration``.

        An awake clock that went backwards is not trusted with any of the run:
        it reads as no sleep, so the run is charged its whole duration rather
        than nothing. The budget is the limit on runaway runs, and a clock it
        cannot read must not switch it off."""
        if self._awake is None or self._started is None:
            return 0.0
        try:
            awake = self._awake() - self._started
        except Exception:
            return 0.0
        if awake < 0:
            return 0.0
        raw = duration - awake
        if raw < SLEEP_THRESHOLD_SECONDS:
            return 0.0
        return min(raw, duration)

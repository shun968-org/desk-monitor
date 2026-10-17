"""Raspberry Pi の状態(温度・CPU 使用率・スロットリング)を集める。ダッシュボードに出す。"""
from __future__ import annotations

import socket
import subprocess
from pathlib import Path

_THERMAL = Path('/sys/class/thermal/thermal_zone0/temp')
_MEMINFO = Path('/proc/meminfo')
_UPTIME = Path('/proc/uptime')


def read_temp() -> float | None:
    """SoC の温度(°C)。読めなければ None。"""
    try:
        return int(_THERMAL.read_text()) / 1000
    except (OSError, ValueError):
        return None


def read_throttled() -> int | None:
    """vcgencmd get_throttled のビット列。0 以外なら低電圧・スロットリングが起きた(起きている)。"""
    try:
        out = subprocess.run(['vcgencmd', 'get_throttled'], capture_output=True,
                             text=True, timeout=2).stdout
        return int(out.strip().split('=')[1], 16)
    except (OSError, subprocess.SubprocessError, IndexError, ValueError):
        return None


def read_memory() -> float | None:
    """メモリ使用率(%)。キャッシュなど解放できる分は使用中に数えない(MemAvailable)。"""
    try:
        info = {k: int(v.split()[0]) for k, v in
                (line.split(':', 1) for line in _MEMINFO.read_text().splitlines() if ':' in line)}
        return round(100.0 * (1 - info['MemAvailable'] / info['MemTotal']), 1)
    except (OSError, KeyError, ValueError, ZeroDivisionError):
        return None


def read_uptime() -> float | None:
    try:
        return float(_UPTIME.read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return None


class CpuMeter:
    """前回の呼び出しからの CPU 使用率(全コア合計を 100% とする)。"""

    def __init__(self, stat: Path = Path('/proc/stat')) -> None:
        self._stat = stat
        self._prev = self._read()

    def _read(self) -> tuple[int, int] | None:
        try:
            fields = [int(v) for v in self._stat.read_text().split('\n', 1)[0].split()[1:]]
        except (OSError, ValueError):
            return None
        idle = fields[3] + (fields[4] if len(fields) > 4 else 0)
        return sum(fields), idle

    def percent(self) -> float | None:
        cur = self._read()
        prev, self._prev = self._prev, cur
        if cur is None or prev is None or cur[0] <= prev[0]:
            return None
        total, idle = cur[0] - prev[0], cur[1] - prev[1]
        return round(100.0 * (total - idle) / total, 1)


def snapshot(meter: CpuMeter) -> dict:
    return {
        'host': socket.gethostname(),
        'temp': read_temp(),
        'cpu': meter.percent(),
        'throttled': read_throttled(),
        'memory': read_memory(),
        'uptime': read_uptime(),
    }

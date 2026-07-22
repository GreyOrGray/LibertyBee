"""pacing — the adaptive idle-throttle (Phase 1.11.2; published with the
corpus runner at KD-045 — a long sweep should be invisible while the operator
works and full-tilt when the box idles).

Ported from the C:\\MOUSE jiggler's poll-loop/threshold structure, upgraded
from cursor-polling to GetLastInputInfo (catches keyboard AND mouse).

Modes:
    pause — stopped (manual: pause.flag or the override file)
    drip  — one worker + an inter-run gap (the default while there is input)
    blast — the slot-pool at cores-2 (only after SUSTAINED idle)

Hysteresis is asymmetric BY CONSTRUCTION: entering blast requires
idle >= blast_after seconds of continuous no-input (GetLastInputInfo resets
to ~0 on any keypress/mouse move, so one touch instantly fails the
condition) — while dropping to drip needs exactly one input event.

CPU gate: blast entry also requires system CPU below cpu_max_pct, so the
farm won't blast over someone else's long-running build/render that involves
no typing. The gate applies ONLY to the drip->blast transition — once
blasting, the farm's own sims own the CPU, so CPU must never be an exit
signal (it would oscillate against itself); only input exits blast.
Measured via kernel32.GetSystemTimes deltas — no psutil dependency.

Manual override: pass override_path to Pacer — a file containing
pause|drip|blast wins over everything; delete the file to resume auto. (The
living farm points it at environments/living_farm/mode.override.)
"""

import ctypes
import time
from ctypes import wintypes
from pathlib import Path

MODES = ("pause", "drip", "blast")

DEFAULT_BLAST_AFTER_S = 600.0   # 10 min sustained idle before blast
DEFAULT_CPU_MAX_PCT = 25.0      # blast-entry gate


# ---------------------------------------------------------------------------
# Idle detector — GetLastInputInfo
# ---------------------------------------------------------------------------

class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


def get_idle_seconds():
    """Seconds since the last user input (keyboard or mouse), system-wide.
    Both GetTickCount and dwTime are DWORD ticks, so the subtraction is
    wrap-consistent under the 49.7-day rollover."""
    lii = _LASTINPUTINFO()
    lii.cbSize = ctypes.sizeof(_LASTINPUTINFO)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii)):
        raise OSError("GetLastInputInfo failed")
    ticks = ctypes.windll.kernel32.GetTickCount()
    return ((ticks - lii.dwTime) & 0xFFFFFFFF) / 1000.0


# ---------------------------------------------------------------------------
# CPU meter — GetSystemTimes deltas (dependency-free psutil stand-in)
# ---------------------------------------------------------------------------

def _ft_to_int(ft):
    return (ft.dwHighDateTime << 32) | ft.dwLowDateTime


class CpuMeter:
    """System-wide CPU %% between successive percent() calls."""

    def __init__(self):
        self._last = None
        self.percent()  # prime so the first real call has a delta

    def _sample(self):
        idle, kernel, user = wintypes.FILETIME(), wintypes.FILETIME(), wintypes.FILETIME()
        if not ctypes.windll.kernel32.GetSystemTimes(
            ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)
        ):
            raise OSError("GetSystemTimes failed")
        # kernel time INCLUDES idle time
        return _ft_to_int(idle), _ft_to_int(kernel) + _ft_to_int(user)

    def percent(self):
        idle, total = self._sample()
        if self._last is None:
            self._last = (idle, total)
            return None
        d_idle, d_total = idle - self._last[0], total - self._last[1]
        self._last = (idle, total)
        if d_total <= 0:
            return None
        return 100.0 * (d_total - d_idle) / d_total


# ---------------------------------------------------------------------------
# The mode state machine
# ---------------------------------------------------------------------------

class Pacer:
    """decide() -> (mode, reason). idle_fn/cpu_fn injectable for tests."""

    def __init__(self, blast_after_s=DEFAULT_BLAST_AFTER_S,
                 cpu_max_pct=DEFAULT_CPU_MAX_PCT, override_path=None,
                 idle_fn=None, cpu_fn=None):
        self.blast_after_s = blast_after_s
        self.cpu_max_pct = cpu_max_pct
        self.override_path = Path(override_path) if override_path else None
        self.idle_fn = idle_fn or get_idle_seconds
        if cpu_fn is None:
            meter = CpuMeter()
            cpu_fn = meter.percent
        self.cpu_fn = cpu_fn
        self.mode = "drip"

    def _override(self):
        if self.override_path and self.override_path.exists():
            val = self.override_path.read_text(encoding="utf-8").strip().lower()
            if val in MODES:
                return val
            raise ValueError(
                f"{self.override_path} contains '{val}' — expected one of {MODES}"
            )
        return None

    def decide(self):
        ov = self._override()
        if ov:
            self.mode = ov
            return self.mode, "manual override"

        idle = self.idle_fn()
        if idle < self.blast_after_s:
            # any input inside the window ends blast instantly
            self.mode = "drip"
            return self.mode, f"idle {idle:.0f}s < {self.blast_after_s:.0f}s"

        if self.mode != "blast":
            cpu = self.cpu_fn()
            if cpu is not None and cpu > self.cpu_max_pct:
                # someone else's load — stay polite, keep dripping
                return self.mode, f"idle {idle:.0f}s but cpu {cpu:.0f}% > {self.cpu_max_pct:.0f}%"
            self.mode = "blast"
            cpu_txt = f"{cpu:.0f}%" if cpu is not None else "n/a"
            return self.mode, f"sustained idle {idle:.0f}s, cpu {cpu_txt}"

        return self.mode, f"idle {idle:.0f}s"


# ---------------------------------------------------------------------------
# Manual observation: python app/src/living_farm/pacing.py [--blast-after N]
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="watch the idle-throttle decide (Ctrl-C to stop)")
    ap.add_argument("--blast-after", type=float, default=DEFAULT_BLAST_AFTER_S)
    ap.add_argument("--cpu-max", type=float, default=DEFAULT_CPU_MAX_PCT)
    ap.add_argument("--interval", type=float, default=2.0)
    args = ap.parse_args()

    pacer = Pacer(blast_after_s=args.blast_after, cpu_max_pct=args.cpu_max)
    meter = CpuMeter()
    try:
        while True:
            mode, reason = pacer.decide()
            cpu = meter.percent()
            cpu_txt = " ? " if cpu is None else f"{round(cpu):>3}"
            print(f"idle={get_idle_seconds():7.1f}s  cpu={cpu_txt}%  "
                  f"mode={mode:<5}  ({reason})", flush=True)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass

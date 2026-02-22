# SPDX-License-Identifier: GPL-2.0
"""Read NVMe QoS kernel counters from debugfs."""

import sys
from pathlib import Path
from typing import Dict, Optional


DEBUGFS_BASE = Path("/sys/kernel/debug/nvme_qos")

COUNTER_NAMES = [
    "high_enqueued", "normal_enqueued",
    "high_dispatched", "normal_dispatched",
    "wc_high_fallback", "wc_normal_fallback",
    "credit_refills", "kicks", "kick_empty",
    "sq_throttled", "doorbells",
]


class QoSKernelStats:
    def __init__(self, controller: str):
        """controller: e.g. 'nvme0'"""
        self.controller = controller
        self._dir = DEBUGFS_BASE / controller
        self._warned = False

    @property
    def available(self) -> bool:
        stats_file = self._dir / "stats"
        try:
            return stats_file.exists() and stats_file.is_file()
        except PermissionError:
            return False

    def _warn_once(self, reason: str) -> None:
        if not self._warned:
            print(f"Warning: kernel QoS stats unavailable: {reason}", file=sys.stderr)
            self._warned = True

    def read_raw(self) -> Optional[str]:
        try:
            return (self._dir / "stats").read_text()
        except PermissionError:
            self._warn_once("permission denied")
            return None
        except FileNotFoundError:
            self._warn_once("debugfs not mounted or CONFIG_NVME_QOS not enabled")
            return None
        except OSError as e:
            self._warn_once(f"OS error: {e}")
            return None

    def read_per_queue(self) -> Optional[list]:
        text = self.read_raw()
        if text is None:
            return None

        lines = text.strip().splitlines()
        if len(lines) < 2:
            return []

        # First line is header
        rows = []
        for line in lines[1:]:
            parts = line.split()
            if len(parts) < 12:
                continue
            row = {"queue": int(parts[0])}
            for i, name in enumerate(COUNTER_NAMES):
                row[name] = int(parts[i + 1])
            rows.append(row)
        return rows

    def read_aggregate(self) -> Optional[Dict[str, int]]:
        rows = self.read_per_queue()
        if rows is None:
            return None

        totals = {name: 0 for name in COUNTER_NAMES}
        for row in rows:
            for name in COUNTER_NAMES:
                totals[name] += row.get(name, 0)
        return totals

    def reset(self) -> bool:
        try:
            reset_file = self._dir / "stats_reset"
            reset_file.write_text("1")
            return True
        except (PermissionError, FileNotFoundError, OSError) as e:
            self._warn_once(f"cannot reset counters: {e}")
            return False

    def snapshot(self) -> Optional[Dict[str, int]]:
        """Take a snapshot for delta calculation. Alias for read_aggregate."""
        return self.read_aggregate()

    @staticmethod
    def delta(before: Dict[str, int], after: Dict[str, int]) -> Dict[str, int]:
        return {name: after.get(name, 0) - before.get(name, 0)
                for name in COUNTER_NAMES}

    @staticmethod
    def validate_fairness(
        counters: Dict[str, int],
        weight: int,
    ) -> Dict[str, object]:
        """Check if dispatch ratio matches expected WRR weight.

        Detects two regimes:
        - demand-limited: high-priority requests < configured weight share,
          so the scheduler can't fill the full weight. Expected actual_hi_pct
          ≈ demand_hi_pct. Tolerance: 20%.
        - weight-limited: high-priority requests >= configured weight share,
          WRR caps high at its share. Expected actual_hi_pct ≈ weight_hi_pct.
          Tolerance: 15%.

        Returns dict with:
          - expected_hi_pct: alias for weight_hi_pct (backward compat)
          - weight_hi_pct: expected high share based on weight alone
          - demand_hi_pct: high share based on enqueue demand
          - effective_expected_hi_pct: the target used for fairness check
          - demand_limited: True if demand < weight share
          - actual_hi_pct: observed high share from counters
          - deviation_pct: absolute deviation from effective expected
          - normal_starved: True if normal got zero dispatches
          - wc_pct: work-conserving fallback percentage
          - fair: "OK" or "WARN"
        """
        hi = counters.get("high_dispatched", 0) + counters.get("wc_high_fallback", 0)
        norm = counters.get("normal_dispatched", 0) + counters.get("wc_normal_fallback", 0)
        total = hi + norm

        weight_hi_pct = (weight / (weight + 1)) * 100 if weight > 0 else 50.0

        # Determine demand from enqueue counters
        hi_enq = counters.get("high_enqueued", 0)
        norm_enq = counters.get("normal_enqueued", 0)
        total_enq = hi_enq + norm_enq

        if total_enq > 0:
            demand_hi_pct = (hi_enq / total_enq) * 100
        else:
            demand_hi_pct = 0.0

        demand_limited = demand_hi_pct < weight_hi_pct

        if demand_limited:
            effective_expected = demand_hi_pct
            tolerance = 20.0
        else:
            effective_expected = weight_hi_pct
            tolerance = 15.0

        if total == 0:
            return {
                "expected_hi_pct": round(weight_hi_pct, 1),
                "weight_hi_pct": round(weight_hi_pct, 1),
                "demand_hi_pct": round(demand_hi_pct, 1),
                "effective_expected_hi_pct": round(effective_expected, 1),
                "demand_limited": demand_limited,
                "actual_hi_pct": 0.0,
                "deviation_pct": round(effective_expected, 1),
                "normal_starved": True,
                "wc_pct": 0.0,
                "fair": "WARN",
            }

        actual_hi_pct = (hi / total) * 100
        deviation_pct = abs(actual_hi_pct - effective_expected)

        wc_total = counters.get("wc_high_fallback", 0) + counters.get("wc_normal_fallback", 0)
        wc_pct = (wc_total / total) * 100 if total else 0.0

        normal_starved = norm == 0

        fair = "OK" if (deviation_pct <= tolerance and not normal_starved) else "WARN"

        return {
            "expected_hi_pct": round(weight_hi_pct, 1),
            "weight_hi_pct": round(weight_hi_pct, 1),
            "demand_hi_pct": round(demand_hi_pct, 1),
            "effective_expected_hi_pct": round(effective_expected, 1),
            "demand_limited": demand_limited,
            "actual_hi_pct": round(actual_hi_pct, 1),
            "deviation_pct": round(deviation_pct, 1),
            "normal_starved": normal_starved,
            "wc_pct": round(wc_pct, 1),
            "fair": fair,
        }

    @staticmethod
    def format_summary(
        counters: Dict[str, int],
        fairness: Dict[str, object],
    ) -> str:
        """Format a one-line kernel stats summary for terminal output.

        e.g.: disp=31:69 enq=30:70 kicks=0/14K refill=8K throttle=0 fair=OK(demand-lim)
        """
        hi = counters.get("high_dispatched", 0) + counters.get("wc_high_fallback", 0)
        norm = counters.get("normal_dispatched", 0) + counters.get("wc_normal_fallback", 0)
        total = hi + norm

        if total > 0:
            hi_pct = round(hi / total * 100)
            norm_pct = 100 - hi_pct
            disp_str = f"disp={hi_pct}:{norm_pct}"
        else:
            disp_str = "disp=0:0"

        hi_enq = counters.get("high_enqueued", 0)
        norm_enq = counters.get("normal_enqueued", 0)
        total_enq = hi_enq + norm_enq
        if total_enq > 0:
            enq_hi_pct = round(hi_enq / total_enq * 100)
            enq_norm_pct = 100 - enq_hi_pct
            enq_str = f"enq={enq_hi_pct}:{enq_norm_pct}"
        else:
            enq_str = "enq=0:0"

        kicks = counters.get("kicks", 0)
        kick_empty = counters.get("kick_empty", 0)
        refills = counters.get("credit_refills", 0)
        throttle = counters.get("sq_throttled", 0)

        fair_str = fairness.get("fair", "?")
        if fairness.get("demand_limited"):
            fair_str += "(demand-lim)"
        elif fair_str == "OK":
            fair_str += "(weight-lim)"

        return (f"{disp_str} {enq_str} kicks={_si(kicks)}/{_si(kick_empty)} "
                f"refill={_si(refills)} throttle={_si(throttle)} fair={fair_str}")


def _si(n: int) -> str:
    if n < 1000:
        return str(n)
    elif n < 1_000_000:
        return f"{n/1000:.0f}K"
    else:
        return f"{n/1_000_000:.1f}M"

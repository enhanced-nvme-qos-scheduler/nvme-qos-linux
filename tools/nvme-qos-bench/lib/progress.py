# SPDX-License-Identifier: GPL-2.0
"""tinygrad-style progress output: single-line updates, colored output, SI units."""

import sys
import time
from contextlib import contextmanager
from typing import Optional, Iterator, Iterable, TypeVar

T = TypeVar('T')

# ANSI color codes
COLORS = ['black', 'red', 'green', 'yellow', 'blue', 'magenta', 'cyan', 'white']

def colored(st: str, color: str, bold: bool = False) -> str:
    """Apply ANSI color to string. Returns unchanged if color invalid or not a tty."""
    if not sys.stderr.isatty() or color not in COLORS:
        return st
    code = 30 + COLORS.index(color)
    if bold:
        return f"\033[1;{code}m{st}\033[0m"
    return f"\033[{code}m{st}\033[0m"

def si_format(num: float, suffix: str = "", precision: int = 1) -> str:
    """Format number with SI prefix (K/M/G/T)."""
    for unit in ['', 'K', 'M', 'G', 'T']:
        if abs(num) < 1000:
            return f"{num:.{precision}f}{unit}{suffix}"
        num /= 1000
    return f"{num:.{precision}f}P{suffix}"

def format_us(us: float) -> str:
    """Format microseconds with appropriate unit."""
    if us < 1000:
        return f"{us:.0f}us"
    elif us < 1000000:
        return f"{us/1000:.1f}ms"
    else:
        return f"{us/1000000:.2f}s"

def format_pct_change(old: float, new: float) -> str:
    """Format percentage change with color coding."""
    if old == 0:
        return "N/A"
    pct = ((new - old) / old) * 100
    sign = "+" if pct >= 0 else ""
    # For latency: negative is good (green), positive is bad (red)
    if pct <= -30:
        color = 'green'
    elif pct <= -10:
        color = 'yellow'
    elif pct < 0:
        color = 'white'
    elif pct < 5:
        color = 'yellow'
    else:
        color = 'red'
    return colored(f"{sign}{pct:.1f}%", color)

class Progress:
    """Single-line progress indicator with \r updates."""

    def __init__(self, desc: str = "", total: Optional[int] = None):
        self.desc = desc
        self.total = total
        self.current = 0
        self.start_time = time.time()

    def update(self, n: int = 1, msg: str = "") -> None:
        """Update progress and redraw line."""
        self.current += n
        self._draw(msg)

    def set(self, n: int, msg: str = "") -> None:
        """Set progress to specific value."""
        self.current = n
        self._draw(msg)

    def _draw(self, msg: str = "") -> None:
        """Draw progress bar on single line."""
        if not sys.stderr.isatty():
            return

        elapsed = time.time() - self.start_time

        if self.total:
            pct = min(100, 100 * self.current // self.total)
            bar_width = 20
            filled = bar_width * self.current // self.total
            bar = '█' * filled + ' ' * (bar_width - filled)
            line = f"\r{self.desc}: {pct:3d}%|{bar}| {self.current}/{self.total}"
        else:
            line = f"\r{self.desc}: {self.current}"

        if msg:
            line += f" {msg}"

        # Pad to clear previous content
        print(f"{line:<80}", end="", file=sys.stderr, flush=True)

    def finish(self, final_msg: str = "") -> float:
        """Complete progress and print final message. Returns elapsed time."""
        elapsed = time.time() - self.start_time
        if final_msg:
            print(f"\r{final_msg:<80}", file=sys.stderr)
        else:
            print(file=sys.stderr)
        return elapsed

    def elapsed(self) -> float:
        """Return elapsed time since start."""
        return time.time() - self.start_time

def tqdm(iterable: Iterable[T], desc: str = "", total: Optional[int] = None) -> Iterator[T]:
    """Iterate with tinygrad-style progress bar."""
    if total is None:
        try:
            total = len(iterable)  # type: ignore
        except TypeError:
            total = None

    p = Progress(desc, total)
    for i, x in enumerate(iterable):
        p.set(i)
        yield x
    p.set(total or i + 1)
    p.finish()

@contextmanager
def timing(desc: str = ""):
    """Context manager for timing blocks."""
    start = time.time()
    yield
    elapsed = time.time() - start
    if desc:
        print(f"{desc}: {elapsed:.2f}s", file=sys.stderr)

def print_header(device: str, kernel: str, qos_available: bool) -> None:
    """Print benchmark header line."""
    from . import __version__
    qos_status = colored("available", "green") if qos_available else colored("unavailable", "yellow")
    print(f"nvme-qos-bench v{__version__} | {device} | kernel {kernel} | QoS: {qos_status}", file=sys.stderr)

def print_warning(msg: str) -> None:
    """Print warning message."""
    print(colored(f"WARNING: {msg}", "yellow"), file=sys.stderr)

def print_result(label: str, p50_us: float, p90_us: float, p99_us: float, p999_us: float,
                 iops: float, cpu_pct: float, runtime: float,
                 p99_change: Optional[float] = None) -> None:
    """Print single result line in dense format."""
    line = (f"{label:<18} "
            f"p50={format_us(p50_us):>8} "
            f"p90={format_us(p90_us):>8} "
            f"p99={format_us(p99_us):>8} "
            f"p999={format_us(p999_us):>8} "
            f"iops={si_format(iops):>8} "
            f"cpu={cpu_pct:5.1f}% [{runtime:.1f}s]")
    if p99_change is not None:
        line += f"  {format_pct_change(1.0, 1.0 + p99_change/100)}"
    print(line, file=sys.stderr)

def print_separator() -> None:
    """Print separator line."""
    print("---", file=sys.stderr)

def print_summary(p99_range: tuple, iops_range: tuple, cpu_range: tuple,
                   norm_p99_range: Optional[tuple] = None) -> None:
    """Print summary line with ranges."""
    p99_str = f"p99 {p99_range[0]:+.1f}% to {p99_range[1]:+.1f}%"
    iops_str = f"iops {iops_range[0]:+.1f}% to {iops_range[1]:+.1f}%"
    cpu_str = f"cpu {cpu_range[0]:+.1f}% to {cpu_range[1]:+.1f}%"
    parts = [p99_str, iops_str, cpu_str]
    if norm_p99_range:
        parts.append(f"norm_p99 {norm_p99_range[0]:+.1f}% to {norm_p99_range[1]:+.1f}%")
    print(f"summary: {' | '.join(parts)}", file=sys.stderr)

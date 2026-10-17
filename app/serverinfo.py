"""A quick health report: RAM, CPU load, uptime, disk, and the data dir.

Everything is read from the container's own view of the system. ``/proc/meminfo``
and ``/proc/loadavg`` are *not* namespaced by Docker, so those numbers describe
the host and are labelled as such; the container's own memory comes from the
cgroup v2 files. Paths are injectable so the tests can point at fixtures.
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

from app import stt

_KIB = 1024
_UNITS = ("B", "KB", "MB", "GB", "TB", "PB")

_SERVER_WORDS = r"server|vps|mesin"
_METRIC_WORDS = r"ram|memori|memory|cpu|disk|storage|penyimpanan|load|uptime|resource|sumber daya"
_TRIGGERS = (
    "cek|check|lihat|liat|status|kondisi|gimana|gmn|bagaimana|"
    "sehat|berapa|sisa|pakai|pemakaian|usage"
)

_SERVER_RE = re.compile(rf"\b({_SERVER_WORDS})\b")
_METRIC_RE = re.compile(rf"\b({_METRIC_WORDS})\b")
_TRIGGER_RE = re.compile(rf"\b({_TRIGGERS})\b")


def server_status_request(text: str) -> bool:
    """True when a plain message is asking about the server or its resources.

    Needs both a trigger ("cek", "kondisi", "gimana", ...) and a target (server
    or a resource word), so "berapa lama route ini" does not trip it.
    """
    t = " ".join(text.lower().split())
    if "check_server" in t:
        return True
    if not _TRIGGER_RE.search(t):
        return False
    return bool(_SERVER_RE.search(t) or _METRIC_RE.search(t))


def _fmt_bytes(value: float) -> str:
    value = float(value)
    for unit in _UNITS:
        if abs(value) < _KIB:
            return f"{value:.0f} {unit}" if unit in ("B", "KB") else f"{value:.1f} {unit}"
        value /= _KIB
    return f"{value:.1f} PB"


def _fmt_duration(seconds: float) -> str:
    seconds = int(seconds)
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"{days}d {hours}j"
    if hours:
        return f"{hours}j {minutes}m"
    return f"{minutes}m"


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text()
    except OSError:
        return None


def _meminfo(path: Path) -> dict[str, int]:
    """Parse /proc/meminfo into bytes, keyed without the trailing colon."""
    text = _read_text(path)
    out: dict[str, int] = {}
    if not text:
        return out
    for line in text.splitlines():
        key, sep, rest = line.partition(":")
        if not sep:
            continue
        parts = rest.split()
        if parts and parts[0].isdigit():
            out[key] = int(parts[0]) * _KIB
    return out


def _cgroup_memory(cgroup: Path) -> tuple[int | None, int | None, int | None]:
    """Return (limit, current, inactive_file) in bytes, any of which may be None."""
    limit_raw = _read_text(cgroup / "memory.max")
    current_raw = _read_text(cgroup / "memory.current")
    limit = int(limit_raw) if limit_raw and limit_raw.strip().isdigit() else None
    current = int(current_raw) if current_raw and current_raw.strip().isdigit() else None

    inactive = None
    stat = _read_text(cgroup / "memory.stat") or ""
    for line in stat.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] == "inactive_file" and parts[1].isdigit():
            inactive = int(parts[1])
    return limit, current, inactive


def _loadavg(proc: Path) -> tuple[float, float, float] | None:
    text = _read_text(proc / "loadavg")
    if not text:
        return None
    parts = text.split()
    try:
        return float(parts[0]), float(parts[1]), float(parts[2])
    except (IndexError, ValueError):
        return None


def _uptime(proc: Path) -> float | None:
    text = _read_text(proc / "uptime")
    if not text:
        return None
    try:
        return float(text.split()[0])
    except (IndexError, ValueError):
        return None


def _rss(proc: Path, pid: int = 1) -> int | None:
    """Resident set size of a process, in bytes."""
    text = _read_text(proc / str(pid) / "status")
    if not text:
        return None
    for line in text.splitlines():
        if line.startswith("VmRSS:"):
            parts = line.split()
            if len(parts) >= 2 and parts[1].isdigit():
                return int(parts[1]) * _KIB
    return None


def _dir_size(path: Path) -> int:
    total = 0
    try:
        for root, _dirs, files in os.walk(path):
            for name in files:
                file = Path(root) / name
                try:
                    # The HuggingFace cache stores snapshots as symlinks to blobs;
                    # counting the link and the blob would double the model size.
                    if file.is_symlink():
                        continue
                    total += file.stat().st_size
                except OSError:
                    continue
    except OSError:
        return total
    return total


def collect(
    *,
    proc: Path = Path("/proc"),
    cgroup: Path = Path("/sys/fs/cgroup"),
    root: Path = Path("/"),
    db_path: Path | None = None,
    data_dir: Path | None = None,
    stt_model: str = "",
) -> str:
    """Build the /check_server report. Missing sources are simply skipped."""
    lines = ["🖥️ Kondisi server"]

    rss = _rss(proc)
    if rss is not None:
        lines.append(f"• RAM bot: {_fmt_bytes(rss)}")

    limit, current, inactive = _cgroup_memory(cgroup)
    if current is not None:
        used = current - (inactive or 0)
        if limit:
            pct = used / limit * 100
            lines.append(f"• RAM container: {_fmt_bytes(used)} / {_fmt_bytes(limit)} ({pct:.0f}%)")
        else:
            lines.append(f"• RAM container: {_fmt_bytes(used)}")

    mem = _meminfo(proc / "meminfo")
    if "MemTotal" in mem:
        total = mem["MemTotal"]
        available = mem.get("MemAvailable", total)
        pct = (total - available) / total * 100 if total else 0.0
        total_s, avail_s = _fmt_bytes(total), _fmt_bytes(available)
        lines.append(f"• RAM host: {avail_s} bebas dari {total_s} ({pct:.0f}% dipakai)")

    load = _loadavg(proc)
    if load is not None:
        cores = os.cpu_count() or 1
        lines.append(f"• CPU: {cores} core, load {load[0]:.2f} / {load[1]:.2f} / {load[2]:.2f}")

    up = _uptime(proc)
    if up is not None:
        lines.append(f"• Uptime host: {_fmt_duration(up)}")

    # Prefer the data dir: it is a bind mount onto the host filesystem, so the
    # numbers match `df`. Inside the container, "/" is an overlay with its own
    # (misleading) size.
    disk_path = data_dir if data_dir is not None else root
    try:
        usage = shutil.disk_usage(disk_path)
    except OSError:
        usage = None
    if usage is not None and usage.total:
        pct = usage.used / usage.total * 100
        total_s, free_s = _fmt_bytes(usage.total), _fmt_bytes(usage.free)
        lines.append(f"• Disk: {free_s} sisa dari {total_s} ({pct:.0f}% dipakai)")

    if data_dir is not None:
        lines.append(f"• Data: {_fmt_bytes(_dir_size(data_dir))}")

    if db_path is not None:
        try:
            size = db_path.stat().st_size
            wal = db_path.with_name(db_path.name + "-wal")
            wal_size = wal.stat().st_size if wal.exists() else 0
            extra = f" (+WAL {_fmt_bytes(wal_size)})" if wal_size else ""
            lines.append(f"• Database: {_fmt_bytes(size)}{extra}")
        except OSError:
            pass

    if stt_model:
        state = "aktif di RAM" if stt._model is not None else "belum di-load"
        lines.append(f"• Whisper: {stt_model} ({state})")

    return "\n".join(lines)

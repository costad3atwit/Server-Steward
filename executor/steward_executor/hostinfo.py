"""Read-only host metrics. /proc/{uptime,loadavg,meminfo} show host values inside a
container; /sys/class/hwmon is readable when sysfs is mounted (the Docker default)."""

from __future__ import annotations

import shutil
from pathlib import Path


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def host_info(disks: list, root: str = "/") -> dict:
    r = Path(root)
    info: dict = {}

    up = _read(r / "proc/uptime")
    if up:
        info["uptime_hours"] = round(float(up.split()[0]) / 3600, 1)

    load = _read(r / "proc/loadavg")
    if load:
        info["load"] = [float(x) for x in load.split()[:3]]

    mem = _read(r / "proc/meminfo")
    if mem:
        fields = {}
        for line in mem.splitlines():
            key, _, rest = line.partition(":")
            if key in ("MemTotal", "MemAvailable"):
                fields[key] = int(rest.split()[0])  # kB
        if len(fields) == 2:
            info["memory_gb"] = {
                "total": round(fields["MemTotal"] / 1048576, 1),
                "available": round(fields["MemAvailable"] / 1048576, 1),
            }

    disk_out = []
    for d in disks:
        try:
            u = shutil.disk_usage(d.path)
            disk_out.append({
                "name": d.name,
                "used_pct": round(u.used / u.total * 100, 1),
                "free_gb": round(u.free / 1e9, 1),
                "total_gb": round(u.total / 1e9, 1),
            })
        except OSError as exc:
            disk_out.append({"name": d.name, "error": exc.strerror or str(exc)})
    info["disks"] = disk_out

    temps: dict[str, float] = {}
    hwmon = r / "sys/class/hwmon"
    if hwmon.is_dir():
        for dev in sorted(hwmon.iterdir()):
            name = (_read(dev / "name") or dev.name).strip()
            for sensor in dev.glob("temp*_input"):
                raw = _read(sensor)
                if raw is None:
                    continue
                try:
                    c = int(raw.strip()) / 1000
                except ValueError:
                    continue
                temps[name] = max(temps.get(name, c), c)
    if temps:
        info["temps_c"] = {k: round(v, 1) for k, v in sorted(temps.items())}

    return info

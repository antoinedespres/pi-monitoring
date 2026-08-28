"""Host metric collection over SSH.

Each collector runs one small read-only command on the Pi (via a piclient.Pi
connection) and parses the output. Standard tools only (cat /proc, df, ps,
docker, apt) so there is nothing to install on the Pi. A PiUnreachable raised
by the transport propagates up; command-level failures (e.g. docker not
permitted) are returned as an {"error": ...} marker instead.
"""
import json
import re


# --------------------------------------------------------------------------- #
# CPU / load / uptime
# --------------------------------------------------------------------------- #
def _parse_cpu_line(line):
    nums = [int(x) for x in line.split()[1:]]
    idle = nums[3] + (nums[4] if len(nums) > 4 else 0)  # idle + iowait
    return idle, sum(nums)


def cpu_percent(pi, sample_seconds=0.3):
    """Overall CPU utilisation, sampled over a short window in one round-trip."""
    code, out, _ = pi.run(f"cat /proc/stat; sleep {sample_seconds}; cat /proc/stat")
    cpu_lines = [ln for ln in out.splitlines() if ln.startswith("cpu ")]
    if len(cpu_lines) < 2:
        return 0.0
    idle1, total1 = _parse_cpu_line(cpu_lines[0])
    idle2, total2 = _parse_cpu_line(cpu_lines[1])
    dtotal = total2 - total1
    if dtotal <= 0:
        return 0.0
    return round(100.0 * (1.0 - (idle2 - idle1) / dtotal), 1)


def cpu_count(pi):
    code, out, _ = pi.run("nproc")
    try:
        return int(out.strip())
    except ValueError:
        return 1


def loadavg(pi):
    _, out, _ = pi.run("cat /proc/loadavg")
    one, five, fifteen = out.split()[:3]
    return float(one), float(five), float(fifteen)


def uptime_seconds(pi):
    _, out, _ = pi.run("cat /proc/uptime")
    return float(out.split()[0])


def format_duration(seconds):
    seconds = int(seconds)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    return " ".join(parts)


# --------------------------------------------------------------------------- #
# Memory
# --------------------------------------------------------------------------- #
def meminfo(pi):
    _, out, _ = pi.run("cat /proc/meminfo")
    info = {}
    for line in out.splitlines():
        key, _, rest = line.partition(":")
        if rest.strip():
            info[key] = int(rest.split()[0]) * 1024  # kB -> bytes
    total = info.get("MemTotal", 0)
    available = info.get("MemAvailable", info.get("MemFree", 0))
    used = total - available
    swap_total = info.get("SwapTotal", 0)
    swap_free = info.get("SwapFree", 0)
    return {
        "total": total,
        "available": available,
        "used": used,
        "percent": round(100.0 * used / total, 1) if total else 0.0,
        "swap_total": swap_total,
        "swap_used": swap_total - swap_free,
        "swap_percent": round(100.0 * (swap_total - swap_free) / swap_total, 1) if swap_total else 0.0,
    }


# --------------------------------------------------------------------------- #
# Temperature
# --------------------------------------------------------------------------- #
def temperature_c(pi):
    """CPU temperature in °C, or None if unreadable."""
    _, out, _ = pi.run("cat /sys/class/thermal/thermal_zone0/temp 2>/dev/null || true")
    out = out.strip()
    if out.isdigit():
        return round(int(out) / 1000.0, 1)
    # Fallback for Raspberry Pi OS: vcgencmd measure_temp -> "temp=48.3'C"
    _, out, _ = pi.run("vcgencmd measure_temp 2>/dev/null || true")
    m = re.search(r"([\d.]+)", out)
    return round(float(m.group(1)), 1) if m else None


# --------------------------------------------------------------------------- #
# Disk
# --------------------------------------------------------------------------- #
def disk_usage(pi, path="/"):
    _, out, _ = pi.run(f"df -P -B1 {path} | tail -1")
    parts = out.split()
    if len(parts) < 6:
        return {"path": path, "error": "df returned no data"}
    total = int(parts[1])
    used = int(parts[2])
    free = int(parts[3])
    return {
        "path": parts[5],
        "total": total,
        "used": used,
        "free": free,
        "percent": round(100.0 * used / total, 1) if total else 0.0,
    }


def disk_usages(pi):
    """Every real, local filesystem (df with pseudo-fs excluded)."""
    code, out, _ = pi.run(
        "df -P -B1 -x tmpfs -x devtmpfs -x overlay -x squashfs -x aufs 2>/dev/null | tail -n +2"
    )
    results = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 6:
            continue
        total, used, free = int(parts[1]), int(parts[2]), int(parts[3])
        mount = parts[5]
        if total == 0 or mount.startswith(("/boot/firmware/.", "/snap")):
            continue
        results.append({
            "path": mount,
            "total": total,
            "used": used,
            "free": free,
            "percent": round(100.0 * used / total, 1) if total else 0.0,
        })
    results.sort(key=lambda u: u["path"])
    return results or [disk_usage(pi, "/")]


# --------------------------------------------------------------------------- #
# Processes (ps on the Pi - no PID-namespace tricks needed)
# --------------------------------------------------------------------------- #
def top_by_cpu(pi, n=5):
    _, out, _ = pi.run(f"ps -eo pcpu,pid,comm --sort=-pcpu --no-headers | head -n {n}")
    rows = []
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        try:
            rows.append((float(parts[0]), parts[2].strip(), parts[1]))
        except ValueError:
            continue
    return rows


def top_by_mem(pi, n=5):
    _, out, _ = pi.run(f"ps -eo rss,pid,comm --sort=-rss --no-headers | head -n {n}")
    rows = []
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        try:
            rows.append((int(parts[0]) * 1024, parts[2].strip(), parts[1]))  # rss is in kB
        except ValueError:
            continue
    return rows


# --------------------------------------------------------------------------- #
# Docker (docker CLI on the Pi)
# --------------------------------------------------------------------------- #
def docker_containers(pi):
    """All containers on the Pi. Returns a list of dicts, or a dict with an
    `error` key if the docker command isn't available/permitted."""
    code, out, err = pi.run("docker ps -a --no-trunc --format '{{json .}}' 2>&1")
    if code != 0:
        msg = (out or err).strip().splitlines()[0] if (out or err).strip() else "docker command failed"
        if "permission denied" in msg.lower():
            msg = "permission denied on the Docker socket (add the SSH user to the docker group)"
        elif "not found" in msg.lower() or "command not found" in msg.lower():
            msg = "docker not installed on the Pi"
        return {"error": msg}

    containers = []
    for line in out.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            c = json.loads(line)
        except json.JSONDecodeError:
            continue
        status = c.get("Status", "")
        state = c.get("State", "")  # docker >= 20.10 sets this: running/exited/...
        health = None
        m = re.search(r"\((healthy|unhealthy|starting)\)", status)
        if m:
            health = m.group(1)
        containers.append({
            "name": c.get("Names", "?"),
            "state": state or ("running" if status.startswith("Up") else "exited"),
            "status": status,
            "health": health,
            "image": c.get("Image", "?"),
        })
    containers.sort(key=lambda c: c["name"])
    return containers


# --------------------------------------------------------------------------- #
# APT updates (native apt on the Pi - accurate, honours the Pi's own state)
# --------------------------------------------------------------------------- #
def apt_status(pi):
    reboot_code, _, _ = pi.run("test -e /var/run/reboot-required -o -e /run/reboot-required")
    reboot = reboot_code == 0
    reboot_pkgs = []
    if reboot:
        _, rp, _ = pi.run("cat /run/reboot-required.pkgs /var/run/reboot-required.pkgs 2>/dev/null | sort -u")
        reboot_pkgs = [p.strip() for p in rp.splitlines() if p.strip()]

    code, out, err = pi.run("apt-get -s dist-upgrade 2>/dev/null | grep '^Inst' || true")
    if code not in (0, 1):  # grep exits 1 on no match, which is fine
        return {"error": "apt-get simulation failed on the Pi", "reboot_required": reboot, "reboot_pkgs": reboot_pkgs}

    packages = []
    for line in out.splitlines():
        if line.startswith("Inst "):
            packages.append({"name": line.split()[1], "security": "security" in line.lower()})

    _, ts, _ = pi.run("stat -c %Y /var/lib/apt/lists 2>/dev/null || true")
    try:
        last_update = int(ts.strip()) if ts.strip() else None
    except ValueError:
        last_update = None

    return {
        "count": len(packages),
        "security_count": sum(1 for p in packages if p["security"]),
        "packages": packages,
        "reboot_required": reboot,
        "reboot_pkgs": reboot_pkgs,
        "last_update": last_update,
    }


# --------------------------------------------------------------------------- #
# APT history (what was actually installed, not just what is pending)
# --------------------------------------------------------------------------- #
HISTORY_LOG = "/var/log/apt/history.log"
_HISTORY_MARKER = "--- history ---"

# "wget:arm64 (1.25.0-2ubuntu4.3, 1.25.0-2ubuntu4.4)" -> name, versions.
# Matched per package rather than split on commas, because the version pairs
# contain commas themselves.
_HISTORY_PKG_RE = re.compile(r"([^\s,()]+?)(?::[a-z0-9]+)?\s+\(([^)]*)\)")


def _norm_ts(value):
    """history.log separates date and time with two spaces; collapse it so the
    fixed-width timestamps compare correctly as plain strings."""
    return " ".join(value.split()) if value else ""


def _parse_history_packages(value):
    packages = []
    for name, versions in _HISTORY_PKG_RE.findall(value or ""):
        parts = [v.strip() for v in versions.split(",")]
        # Upgrades read "(old, new)"; installs read "(version)" or
        # "(version, automatic)" when the package came in as a dependency.
        if len(parts) == 2 and parts[1] != "automatic":
            packages.append({"name": name, "old": parts[0], "new": parts[1]})
        else:
            packages.append({"name": name, "old": None, "new": parts[0]})
    return packages


def apt_history(pi, tail_bytes=200000):
    """Recent apt transactions, parsed from the Pi's /var/log/apt/history.log.

    Returns {"now": ..., "runs": [...]}, or an {"error": ...} marker. Every
    timestamp stays in the log's own "YYYY-MM-DD HH:MM:SS" form *in the Pi's
    local time*, and the Pi's current clock is read in the same round-trip: the
    bot runs on the VPS in UTC while the Pi is on Europe/Paris, so comparing the
    two clocks directly would be off by an hour or two. Fixed-width timestamps
    also sort lexicographically, so the watermark needs no date parsing.

    `unattended` marks a run started by unattended-upgrades. Its --dry-run
    simulations land in this log too, and are deliberately excluded - reporting
    them would announce upgrades that were never installed.
    """
    _, out, _ = pi.run(
        f"date '+%Y-%m-%d %H:%M:%S'; echo '{_HISTORY_MARKER}'; "
        f"tail -c {tail_bytes} {HISTORY_LOG} 2>/dev/null || true"
    )
    header, marker, body = out.partition(_HISTORY_MARKER)
    now = header.strip()
    if not marker or not now:
        return {"error": "could not read the apt history on the Pi", "now": None, "runs": []}

    runs = []
    for block in re.split(r"\n\s*\n", body):
        entry = {}
        for line in block.splitlines():
            key, sep, value = line.partition(":")
            if sep and key and not key[0].isspace():
                entry[key.strip()] = value.strip()
        start = _norm_ts(entry.get("Start-Date", ""))
        if not start:
            continue  # tail -c can slice the oldest block in half
        commandline = entry.get("Commandline", "")
        runs.append({
            "start": start,
            "end": _norm_ts(entry.get("End-Date", "")) or start,
            "commandline": commandline,
            "requested_by": entry.get("Requested-By"),
            "unattended": (commandline.startswith("/usr/bin/unattended-upgrade")
                           and "--dry-run" not in commandline),
            "upgrade": _parse_history_packages(entry.get("Upgrade")),
            "install": _parse_history_packages(entry.get("Install")),
            "remove": _parse_history_packages(
                " ".join(v for v in (entry.get("Remove"), entry.get("Purge")) if v)
            ),
            "error": entry.get("Error"),
        })
    runs.sort(key=lambda r: r["start"])
    return {"now": now, "runs": runs}


# --------------------------------------------------------------------------- #
# Formatting helpers
# --------------------------------------------------------------------------- #
def human_bytes(n):
    step = 1024.0
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < step:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= step
    return f"{n:.1f} PiB"

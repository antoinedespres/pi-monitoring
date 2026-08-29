#!/usr/bin/env python3
"""Entry point for pi-monitoring.

Runs on the VPS (next to the sibling bots) and watches a Raspberry Pi over SSH.
Two jobs:
  1. Serve read-only slash commands (/status, /cpu, /mem, /disk, /docker,
     /updates, /uptime) over the Discord Gateway.
  2. Run a background poll loop that raises alerts for:
       - the Pi becoming unreachable over SSH (and recovering),
       - CPU / RAM / disk / temperature crossing thresholds,
       - Docker containers going down / unhealthy / recovering,
       - a reboot becoming required,
       - unattended-upgrades having installed something (what, and which
         versions), read back from the Pi's apt history.

Because the bot is off-box, "Pi is down" is itself an alert rather than
something that silently takes the monitor with it.
"""
import datetime
import logging
import os
import re
import time

import system
from alerting import CRITICAL, INFO, OK, WARNING, DiscordNotifier, Monitor
from commands import InteractionHandler, fetch_application_id, register_guild_commands
from piclient import Pi, PiUnreachable
from presence import GatewayPresence
from state import State

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("pi-monitoring")

# unattended-upgrades with MinimalSteps splits one nightly run into several
# small dpkg transactions, each landing as its own history.log entry. Entries
# this close together are folded into one report instead of one embed per batch.
UPGRADE_SESSION_GAP_SECONDS = 900
HISTORY_TIME_FMT = "%Y-%m-%d %H:%M:%S"


def env(name, default=None, cast=str):
    value = os.environ.get(name, default)
    return cast(value) if value is not None else None


def build_pi():
    host = env("PI_SSH_HOST")
    user = env("PI_SSH_USER", "pi")
    if not host:
        raise SystemExit("PI_SSH_HOST must be set to the Pi's hostname/IP (see .env.example)")
    return Pi(
        host=host,
        user=user,
        port=env("PI_SSH_PORT", "22", int),
        key_path=env("PI_SSH_KEY_PATH"),
        key_str=env("PI_SSH_KEY"),
    )


def poll_loop(pi, notifier, host_label, interval, state):
    cpu_warn = env("CPU_WARN_PERCENT", "80", float)
    cpu_crit = env("CPU_CRIT_PERCENT", "95", float)
    mem_warn = env("MEM_WARN_PERCENT", "80", float)
    mem_crit = env("MEM_CRIT_PERCENT", "92", float)
    disk_warn = env("DISK_WARN_PERCENT", "80", float)
    disk_crit = env("DISK_CRIT_PERCENT", "90", float)
    temp_warn = env("TEMP_WARN_C", "70", float)
    temp_crit = env("TEMP_CRIT_C", "80", float)
    fail_threshold = env("UNREACHABLE_THRESHOLD", "3", int)
    report_upgrades = env("REPORT_INSTALLED_UPDATES", "1") not in ("0", "false", "no")
    upgrade_settle = env("INSTALLED_UPDATES_SETTLE_SECONDS", "180", int)

    reach_mon = Monitor("Reachability", notifier, 1, 1, "Reachability")
    cpu_mon = Monitor("CPU", notifier, cpu_warn, cpu_crit, "CPU usage", unit="%")
    mem_mon = Monitor("Memory", notifier, mem_warn, mem_crit, "RAM usage", unit="%")
    disk_mon = Monitor("Disk /", notifier, disk_warn, disk_crit, "Disk usage", unit="%")
    temp_mon = Monitor("Temperature", notifier, temp_warn, temp_crit, "CPU temperature", unit="°C")
    container_mons = {}
    reboot_mon = Monitor("Reboot", notifier, 1, 1, "Reboot required")

    consecutive_failures = 0
    prev_cpu = 0.0

    while True:
        try:
            pi.check()
        except PiUnreachable as exc:
            consecutive_failures += 1
            # Debounce: a single missed poll (Wi-Fi failover, brief blip) isn't
            # an outage. Alert only once we've missed `fail_threshold` in a row.
            if consecutive_failures >= fail_threshold:
                reach_mon.force(
                    CRITICAL, f"🔌 {host_label} is unreachable",
                    f"No SSH response for {consecutive_failures} consecutive checks.",
                    {"Host": host_label, "Error": str(exc)[:200]},
                )
            time.sleep(interval)
            continue

        if consecutive_failures:
            reach_mon.force(OK, f"✅ {host_label} is back",
                            "SSH is responding again.", {"Host": host_label})
            consecutive_failures = 0

        try:
            cpu = system.cpu_percent(pi)
            # Sustained smoothing: feed the lower of the last two samples so a
            # momentary spike doesn't trip an alert.
            cpu_mon.update(min(cpu, prev_cpu), context={"Host": host_label, "Instant": f"{cpu:.0f}%"})
            prev_cpu = cpu

            mem = system.meminfo(pi)
            mem_mon.update(mem["percent"], context={"Host": host_label, "Used": system.human_bytes(mem["used"])})

            disk = system.disk_usage(pi, "/")
            if "error" not in disk:
                disk_mon.update(disk["percent"], context={"Host": host_label, "Free": system.human_bytes(disk["free"])})

            temp = system.temperature_c(pi)
            if temp is not None:
                temp_mon.update(temp, context={"Host": host_label})

            _check_containers(pi, notifier, host_label, container_mons)

            apt = system.apt_status(pi)
            if report_upgrades:
                _check_apt_history(pi, notifier, host_label, state, apt, upgrade_settle)
            if "error" not in apt:
                if apt["reboot_required"]:
                    reboot_mon.force(WARNING, f"{host_label}: reboot required",
                                     "A package update needs a reboot to take effect.",
                                     {"Packages": ", ".join(apt["reboot_pkgs"][:10]) or "n/a"})
                else:
                    reboot_mon.force(OK, f"{host_label}: reboot cleared", "No reboot pending.")
        except PiUnreachable:
            # Dropped mid-collection: let the next iteration's check() handle it.
            logger.warning("Pi dropped during metric collection")
        except Exception:
            logger.exception("poll loop iteration failed")

        time.sleep(interval)


def _check_containers(pi, notifier, host_label, mons):
    containers = system.docker_containers(pi)
    if isinstance(containers, dict):  # error dict - skip this round quietly
        return
    for c in containers:
        mon = mons.get(c["name"])
        state = _container_state(c)
        if mon is None:
            # Seed at the current state so we don't alert on a container that
            # was already stopped when the bot started.
            mon = Monitor(c["name"], notifier, 1, 1, "Container")
            mon.state = state
            mons[c["name"]] = mon
            continue
        if state == OK:
            mon.force(OK, f"🐳 {c['name']}: recovered", f"Container `{c['name']}` is healthy again.",
                      {"Host": host_label, "Status": c["status"]})
        elif state == WARNING:
            mon.force(WARNING, f"🐳 {c['name']}: unhealthy", f"Container `{c['name']}` reports an unhealthy state.",
                      {"Host": host_label, "Status": c["status"]})
        else:
            mon.force(CRITICAL, f"🐳 {c['name']}: down", f"Container `{c['name']}` is no longer running.",
                      {"Host": host_label, "State": c["state"], "Status": c["status"]})


def _container_state(c):
    if c["state"] != "running":
        return CRITICAL
    if c["health"] == "unhealthy":
        return WARNING
    return OK


def _parse_ts(value):
    try:
        return datetime.datetime.strptime(value, HISTORY_TIME_FMT)
    except (TypeError, ValueError):
        return None


def _check_apt_history(pi, notifier, host_label, state, apt, settle_seconds):
    """Report what unattended-upgrades installed on the Pi, once per run.

    A run is only reported after `settle_seconds` of quiet, so a multi-step
    upgrade is announced once and complete rather than batch by batch. The
    watermark is the last reported End-Date, persisted so a redeploy neither
    replays the last upgrade nor loses the one that happened while it was down.
    """
    hist = system.apt_history(pi)
    now = _parse_ts(hist.get("now"))
    if "error" in hist or now is None:
        return

    runs = [r for r in hist["runs"] if r["unattended"] and _parse_ts(r["end"])]
    seen = state.get("apt_history_seen")
    if seen is None:
        # First start: adopt the present, so a fresh deploy doesn't announce
        # every upgrade already sitting in the log.
        state.set("apt_history_seen", runs[-1]["end"] if runs else hist["now"])
        return

    for session in _group_upgrade_sessions([r for r in runs if r["end"] > seen]):
        if (now - _parse_ts(session[-1]["end"])).total_seconds() < settle_seconds:
            break  # still upgrading - report it whole on a later poll
        _notify_upgrade_session(notifier, host_label, session, apt)
        state.set("apt_history_seen", session[-1]["end"])


def _group_upgrade_sessions(runs):
    sessions = []
    for run in runs:
        previous_end = _parse_ts(sessions[-1][-1]["end"]) if sessions else None
        start = _parse_ts(run["start"])
        if previous_end and start and (start - previous_end).total_seconds() <= UPGRADE_SESSION_GAP_SECONDS:
            sessions[-1].append(run)
        else:
            sessions.append([run])
    return sessions


def _dedupe(packages):
    """A package can span several MinimalSteps batches; keep its first sighting."""
    seen, unique = set(), []
    for p in packages:
        if p["name"] not in seen:
            seen.add(p["name"])
            unique.append(p)
    return unique


# Packaging suffixes that repeat on nearly every line of a Raspberry Pi OS
# upgrade: Debian's "+deb13u1" / "~deb13u2" and Raspberry Pi's own "+rpt1".
# Requiring a digit after the tag keeps these off version strings that merely
# contain the letters (+debian1, +debug2).
_DEB_SUFFIX_RE = re.compile(r"[+~]deb\d[0-9a-z.]*", re.IGNORECASE)
_RPT_SUFFIX_RE = re.compile(r"[+~]rpt\d[0-9a-z.]*", re.IGNORECASE)

# Tried most-aggressive first. A suffix only earns room on the line when
# dropping it would leave an upgrade reading as one from a version to itself -
# which is what a Debian security respin (deb13u1 -> deb13u2) and a Raspberry
# Pi rebuild (rpt1 -> rpt2) both look like. When it is identical on both sides,
# as on a chromium version bump, it says nothing and goes.
_VERSION_TRIMS = (
    lambda v: _RPT_SUFFIX_RE.sub("", _DEB_SUFFIX_RE.sub("", v)),
    lambda v: _DEB_SUFFIX_RE.sub("", v),
    lambda v: v,
)


def _short_version(value):
    """For a lone version (an install or a removal) there is nothing to tell
    apart, so the shortest form always applies."""
    return _VERSION_TRIMS[0](value) if value else value


def _short_version_pair(old, new):
    if not old or not new:
        return _short_version(old), _short_version(new)
    for trim in _VERSION_TRIMS:
        short_old, short_new = trim(old), trim(new)
        if short_old != short_new:
            return short_old, short_new
    return old, new


def _clip_lines(lines, limit=1000):
    """Discord caps an embed field value at 1024 characters."""
    kept, total = [], 0
    for line in lines:
        if total + len(line) + 1 > limit:
            kept.append(f"... and {len(lines) - len(kept)} more")
            break
        kept.append(line)
        total += len(line) + 1
    return "\n".join(kept) or "-"


def _notify_upgrade_session(notifier, host_label, session, apt):
    upgraded, installed, removed, errors = [], [], [], []
    for run in session:
        upgraded += run["upgrade"]
        installed += run["install"]
        removed += run["remove"]
        if run["error"]:
            errors.append(run["error"])
    upgraded, installed, removed = _dedupe(upgraded), _dedupe(installed), _dedupe(removed)

    if not (upgraded or installed or removed or errors):
        return  # a no-op run (e.g. only autoremove candidates) isn't news

    fields = {}
    if upgraded:
        fields[f"Upgraded ({len(upgraded)})"] = _clip_lines(
            [f"`{p['name']}` {old} -> {new}"
             for p in upgraded for old, new in [_short_version_pair(p["old"], p["new"])]])
    if installed:
        fields[f"Installed ({len(installed)})"] = _clip_lines(
            [f"`{p['name']}` {_short_version(p['new'])}" for p in installed])
    if removed:
        fields[f"Removed ({len(removed)})"] = _clip_lines(
            [f"`{p['name']}` {_short_version(p['old'] or p['new'])}" for p in removed])
    if errors:
        fields["Errors"] = _clip_lines(errors)
    if apt.get("reboot_required"):
        fields["Reboot required"] = ", ".join(apt.get("reboot_pkgs") or [])[:1000] or "yes"

    window = session[0]["start"]
    if session[-1]["end"] != window:
        window += f" -> {session[-1]['end'].split()[-1]}"
    summary = ", ".join(part for part in (
        f"{len(upgraded)} upgraded" if upgraded else "",
        f"{len(installed)} installed" if installed else "",
        f"{len(removed)} removed" if removed else "",
    ) if part)

    notifier.send_embed(
        f"\U0001F4E6 {host_label}: automatic updates installed",
        f"unattended-upgrades ran and applied **{summary}**.\n`{window}` (Pi local time)",
        fields,
        WARNING if errors else INFO,
        inline=False,
    )


def main():
    token = env("DISCORD_BOT_TOKEN")
    channel_id = env("DISCORD_CHANNEL_ID")
    guild_id = env("DISCORD_GUILD_ID")
    host_label = env("HOST_LABEL", "raspberrypi")
    interval = env("POLL_INTERVAL_SECONDS", "60", int)

    if not token or not channel_id:
        raise SystemExit("DISCORD_BOT_TOKEN and DISCORD_CHANNEL_ID must be set (see .env.example)")

    pi = build_pi()
    state = State(env("STATE_PATH", "/var/lib/pi-monitoring/state.json"))
    notifier = DiscordNotifier(token, channel_id)
    version = env("APP_VERSION", "dev")
    logger.info("pi-monitoring starting (version %s), watching %s@%s", version, pi.user, pi.host)
    notifier.send_embed(
        "pi-monitoring started",
        "Deployment succeeded and the bot is now watching the Pi over SSH.",
        {"Version": version, "Host": host_label, "Target": f"{pi.user}@{pi.host}", "Poll": f"{interval}s"},
        INFO,
    )

    on_interaction = None
    if guild_id:
        application_id = fetch_application_id(token)
        register_guild_commands(token, application_id, guild_id)
        on_interaction = InteractionHandler(application_id, token, pi, host_label).handle
    else:
        logger.info("DISCORD_GUILD_ID not set - slash commands disabled")

    GatewayPresence(token, status_text=f"{host_label} (CPU/RAM/disk/docker)", on_interaction=on_interaction).start()

    poll_loop(pi, notifier, host_label, interval, state)


if __name__ == "__main__":
    main()

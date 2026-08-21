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
       - a reboot becoming required.

Because the bot is off-box, "Pi is down" is itself an alert rather than
something that silently takes the monitor with it.
"""
import logging
import os
import time

import system
from alerting import CRITICAL, INFO, OK, WARNING, DiscordNotifier, Monitor
from commands import InteractionHandler, fetch_application_id, register_guild_commands
from piclient import Pi, PiUnreachable
from presence import GatewayPresence

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("pi-monitoring")


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


def poll_loop(pi, notifier, host_label, interval):
    cpu_warn = env("CPU_WARN_PERCENT", "80", float)
    cpu_crit = env("CPU_CRIT_PERCENT", "95", float)
    mem_warn = env("MEM_WARN_PERCENT", "80", float)
    mem_crit = env("MEM_CRIT_PERCENT", "92", float)
    disk_warn = env("DISK_WARN_PERCENT", "80", float)
    disk_crit = env("DISK_CRIT_PERCENT", "90", float)
    temp_warn = env("TEMP_WARN_C", "70", float)
    temp_crit = env("TEMP_CRIT_C", "80", float)
    fail_threshold = env("UNREACHABLE_THRESHOLD", "3", int)

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


def main():
    token = env("DISCORD_BOT_TOKEN")
    channel_id = env("DISCORD_CHANNEL_ID")
    guild_id = env("DISCORD_GUILD_ID")
    host_label = env("HOST_LABEL", "raspberrypi")
    interval = env("POLL_INTERVAL_SECONDS", "60", int)

    if not token or not channel_id:
        raise SystemExit("DISCORD_BOT_TOKEN and DISCORD_CHANNEL_ID must be set (see .env.example)")

    pi = build_pi()
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

    poll_loop(pi, notifier, host_label, interval)


if __name__ == "__main__":
    main()

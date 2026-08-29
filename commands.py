"""Discord slash commands: registration + interaction handling.

Read-only views over the same collectors the alerting loop uses (system.py),
run against the Pi over SSH. Handled over the Gateway connection that also
drives the bot's presence.
"""
import datetime
import logging

import requests

import system
from alerting import COLOR, CRITICAL, INFO, OK, WARNING
from piclient import PiUnreachable

logger = logging.getLogger("pi-monitoring")

API = "https://discord.com/api/v10"

# Discord interaction/response types
TYPE_APPLICATION_COMMAND = 2
CALLBACK_DEFERRED_CHANNEL_MESSAGE = 5

# Aliases onto the shared palette in alerting.py - a command's embed uses the
# same colour as the alert for the same condition.
COLOR_OK = COLOR[OK]
COLOR_INFO = COLOR[INFO]
COLOR_WARN = COLOR[WARNING]
COLOR_ERROR = COLOR[CRITICAL]

COMMANDS = [
    {"name": "status", "description": "Overview: CPU, RAM, disk, temperature, uptime, containers, updates"},
    {"name": "cpu", "description": "CPU load, utilisation and the top processes by CPU"},
    {"name": "mem", "description": "Memory and swap usage, and the top processes by memory"},
    {"name": "disk", "description": "Disk usage per mounted filesystem"},
    {"name": "docker", "description": "State of every Docker container on the Pi"},
    {"name": "updates", "description": "Pending apt package updates and reboot-required status"},
    {"name": "uptime", "description": "Uptime and load averages"},
]


def fetch_application_id(bot_token):
    resp = requests.get(f"{API}/oauth2/applications/@me", headers={"Authorization": f"Bot {bot_token}"}, timeout=15)
    resp.raise_for_status()
    return resp.json()["id"]


def register_guild_commands(bot_token, application_id, guild_id):
    """Guild-scoped registration: updates instantly (global commands can take
    up to an hour to propagate), which matters while iterating/testing."""
    url = f"{API}/applications/{application_id}/guilds/{guild_id}/commands"
    resp = requests.put(url, headers={"Authorization": f"Bot {bot_token}"}, json=COMMANDS, timeout=15)
    if resp.status_code >= 300:
        logger.error("Failed to register slash commands: %s %s", resp.status_code, resp.text)
    else:
        logger.info("Registered %d slash command(s) for guild %s", len(COMMANDS), guild_id)


def _bar(percent, width=10):
    filled = int(round(percent / 100.0 * width))
    filled = max(0, min(width, filled))
    return "█" * filled + "░" * (width - filled)


def _usage_line(percent):
    icon = "🟢" if percent < 80 else ("🟠" if percent < 90 else "🔴")
    return f"{icon} `{_bar(percent)}` {percent:.0f}%"


class InteractionHandler:
    def __init__(self, application_id, bot_token, pi, host_label):
        self.application_id = application_id
        self.bot_token = bot_token
        self.pi = pi
        self.host_label = host_label

    def handle(self, interaction):
        """Entry point for a Gateway INTERACTION_CREATE dispatch. Runs in its
        own thread - deferring/responding does blocking HTTP."""
        if interaction.get("type") != TYPE_APPLICATION_COMMAND:
            return
        interaction_id = interaction["id"]
        token = interaction["token"]
        name = interaction["data"]["name"]
        logger.info("Slash command received: /%s", name)

        self._defer(interaction_id, token)
        try:
            embed = self._dispatch(name)
        except PiUnreachable as exc:
            embed = {
                "title": f"🔌 {self.host_label} unreachable",
                "description": f"Could not reach the Pi over SSH.\n`{exc}`",
                "color": COLOR_ERROR,
            }
        except Exception:
            logger.exception("Error handling /%s", name)
            embed = {"title": "⚠️ Error", "description": "Something went wrong handling that command.", "color": COLOR_ERROR}
        embed.setdefault("footer", {"text": self.host_label})
        self._respond(token, embed)

    def _defer(self, interaction_id, token):
        url = f"{API}/interactions/{interaction_id}/{token}/callback"
        resp = requests.post(url, json={"type": CALLBACK_DEFERRED_CHANNEL_MESSAGE}, timeout=10)
        if resp.status_code >= 300:
            logger.error("Failed to defer interaction: %s %s", resp.status_code, resp.text)

    def _respond(self, token, embed):
        url = f"{API}/webhooks/{self.application_id}/{token}/messages/@original"
        resp = requests.patch(url, json={"embeds": [embed]}, timeout=10)
        if resp.status_code >= 300:
            logger.error("Failed to send interaction response: %s %s", resp.status_code, resp.text)

    def _dispatch(self, name):
        return {
            "status": self._cmd_status,
            "cpu": self._cmd_cpu,
            "mem": self._cmd_mem,
            "disk": self._cmd_disk,
            "docker": self._cmd_docker,
            "updates": self._cmd_updates,
            "uptime": self._cmd_uptime,
        }.get(name, lambda: {"title": "Unknown command", "color": COLOR_WARN})()

    # --- commands ---------------------------------------------------------- #
    def _cmd_status(self):
        pi = self.pi
        cpu = system.cpu_percent(pi)
        mem = system.meminfo(pi)
        disk = system.disk_usage(pi, "/")
        temp = system.temperature_c(pi)
        one, five, fifteen = system.loadavg(pi)
        up = system.format_duration(system.uptime_seconds(pi))

        containers = system.docker_containers(pi)
        if isinstance(containers, dict):
            docker_line = f"⚠️ {containers['error']}"
        else:
            running = sum(1 for c in containers if c["state"] == "running")
            unhealthy = sum(1 for c in containers if c["health"] == "unhealthy")
            docker_line = f"{running}/{len(containers)} running"
            if unhealthy:
                docker_line += f", 🔴 {unhealthy} unhealthy"

        apt = system.apt_status(pi)
        if "error" in apt:
            updates_line = f"⚠️ {apt['error']}"
        else:
            updates_line = f"{apt['count']} pending"
            if apt["security_count"]:
                updates_line += f" (🔴 {apt['security_count']} security)"
            if apt["reboot_required"]:
                updates_line += " · ♻️ reboot required"

        disk_line = _usage_line(disk["percent"]) if "error" not in disk else f"⚠️ {disk['error']}"
        temp_line = f"{temp:.1f} °C" if temp is not None else "n/a"

        fields = [
            {"name": "🧠 CPU", "value": f"{_usage_line(cpu)}\nload {one:.2f} / {five:.2f} / {fifteen:.2f}", "inline": True},
            {"name": "💾 RAM", "value": f"{_usage_line(mem['percent'])}\n{system.human_bytes(mem['used'])} / {system.human_bytes(mem['total'])}", "inline": True},
            {"name": "🌡️ Temp", "value": temp_line, "inline": True},
            {"name": "🗄️ Disk /", "value": f"{disk_line}\n{system.human_bytes(disk['used'])} / {system.human_bytes(disk['total'])}" if "error" not in disk else disk_line, "inline": True},
            {"name": "🐳 Docker", "value": docker_line, "inline": True},
            {"name": "📦 Updates", "value": updates_line, "inline": True},
        ]
        return {"title": f"🖥️ {self.host_label} — status", "color": COLOR_INFO, "fields": fields,
                "footer": {"text": f"uptime {up}"}}

    def _cmd_cpu(self):
        pi = self.pi
        cpu = system.cpu_percent(pi)
        one, five, fifteen = system.loadavg(pi)
        ncpu = system.cpu_count(pi)
        top = system.top_by_cpu(pi, 6)
        lines = [f"`{pct:5.1f}%` {name} (pid {pid})" for pct, name, pid in top] or ["_no active processes seen_"]
        return {
            "title": f"🧠 CPU — {self.host_label}",
            "color": COLOR_INFO,
            "description": f"{_usage_line(cpu)}  across {ncpu} core(s)\nLoad average: **{one:.2f}** / {five:.2f} / {fifteen:.2f}",
            "fields": [{"name": "Top processes by CPU", "value": "\n".join(lines), "inline": False}],
        }

    def _cmd_mem(self):
        pi = self.pi
        mem = system.meminfo(pi)
        top = system.top_by_mem(pi, 6)
        lines = [f"`{system.human_bytes(rss):>9}` {name} (pid {pid})" for rss, name, pid in top] or ["_no processes seen_"]
        desc = (f"**RAM** {_usage_line(mem['percent'])}\n"
                f"{system.human_bytes(mem['used'])} used / {system.human_bytes(mem['total'])} total\n")
        if mem["swap_total"]:
            desc += f"**Swap** {_usage_line(mem['swap_percent'])}\n{system.human_bytes(mem['swap_used'])} / {system.human_bytes(mem['swap_total'])}"
        else:
            desc += "**Swap** none configured"
        return {
            "title": f"💾 Memory — {self.host_label}",
            "color": COLOR_INFO,
            "description": desc,
            "fields": [{"name": "Top processes by memory", "value": "\n".join(lines), "inline": False}],
        }

    def _cmd_disk(self):
        usages = system.disk_usages(self.pi)
        fields = []
        worst = 0
        for u in usages:
            if "error" in u:
                fields.append({"name": u["path"], "value": f"⚠️ {u['error']}", "inline": False})
                continue
            worst = max(worst, u["percent"])
            fields.append({
                "name": u["path"],
                "value": f"{_usage_line(u['percent'])}\n{system.human_bytes(u['used'])} / {system.human_bytes(u['total'])}  ·  {system.human_bytes(u['free'])} free",
                "inline": False,
            })
        color = COLOR_OK if worst < 80 else (COLOR_WARN if worst < 90 else COLOR_ERROR)
        return {"title": f"🗄️ Disk — {self.host_label}", "color": color, "fields": fields}

    def _cmd_docker(self):
        containers = system.docker_containers(self.pi)
        if isinstance(containers, dict):
            return {"title": "🐳 Docker", "description": f"⚠️ {containers['error']}", "color": COLOR_ERROR}
        if not containers:
            return {"title": "🐳 Docker", "description": "No containers found.", "color": COLOR_WARN}

        def icon(c):
            if c["state"] != "running":
                return "🔴"
            if c["health"] == "unhealthy":
                return "🟠"
            if c["health"] == "starting":
                return "🟡"
            return "🟢"

        lines = [f"{icon(c)} **{c['name']}** — {c['status']}" for c in containers]
        running = sum(1 for c in containers if c["state"] == "running")
        down = len(containers) - running
        color = COLOR_OK if down == 0 else COLOR_WARN
        return {
            "title": f"🐳 Docker — {self.host_label}",
            "description": "\n".join(lines)[:4000],
            "color": color,
            "footer": {"text": f"{running} running · {down} stopped · {len(containers)} total"},
        }

    def _cmd_updates(self):
        apt = system.apt_status(self.pi)
        if "error" in apt:
            desc = f"⚠️ {apt['error']}"
            if apt.get("reboot_required"):
                desc += "\n♻️ **Reboot required.**"
            return {"title": "📦 Updates", "description": desc, "color": COLOR_WARN}

        count = apt["count"]
        sec = apt["security_count"]
        if count == 0:
            desc = "✅ System is up to date."
            color = COLOR_OK
        else:
            desc = f"**{count}** package(s) upgradable"
            desc += f", including 🔴 **{sec}** security update(s)." if sec else "."
            color = COLOR_ERROR if sec else COLOR_WARN

        fields = []
        if count:
            names = [("🔴 " if p["security"] else "") + p["name"] for p in apt["packages"][:25]]
            more = f"\n… and {count - 25} more" if count > 25 else ""
            fields.append({"name": "Packages", "value": ", ".join(names) + more, "inline": False})
        if apt["reboot_required"]:
            pkgs = ", ".join(apt["reboot_pkgs"][:10]) if apt["reboot_pkgs"] else ""
            fields.append({"name": "♻️ Reboot required", "value": pkgs or "yes", "inline": False})

        footer = "pi-monitoring"
        if apt.get("last_update"):
            ago = system.format_duration(datetime.datetime.now().timestamp() - apt["last_update"])
            footer = f"apt lists refreshed {ago} ago"
        return {"title": f"📦 Updates — {self.host_label}", "description": desc, "color": color,
                "fields": fields, "footer": {"text": footer}}

    def _cmd_uptime(self):
        secs = system.uptime_seconds(self.pi)
        up = system.format_duration(secs)
        one, five, fifteen = system.loadavg(self.pi)
        boot = datetime.datetime.now() - datetime.timedelta(seconds=secs)
        return {
            "title": f"⏱️ Uptime — {self.host_label}",
            "color": COLOR_INFO,
            "description": f"Up **{up}**\nBooted {boot.strftime('%Y-%m-%d %H:%M')}\nLoad average: {one:.2f} / {five:.2f} / {fifteen:.2f}",
        }

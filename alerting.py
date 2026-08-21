"""Alert state machine + Discord embed delivery:
OK -> WARNING -> CRITICAL -> (recovered to) OK monitor lifecycle.

Same shape as the sibling bots' alerting.py, with the `force()` helper from
vps-monitoring for state transitions that aren't driven by a numeric threshold
(e.g. a Docker container going down)."""
import datetime
import logging

import requests

logger = logging.getLogger("pi-monitoring")

OK, WARNING, CRITICAL, INFO = "OK", "WARNING", "CRITICAL", "INFO"

COLOR = {
    OK: 0x2ECC71,        # green
    WARNING: 0xE67E22,   # orange
    CRITICAL: 0xE74C3C,  # red
    INFO: 0x3498DB,      # blue - standalone notice, not part of a monitor's state
}
EMOJI = {OK: "✅", WARNING: "⚠️", CRITICAL: "\U0001F6A8", INFO: "\U0001F535"}


class DiscordNotifier:
    def __init__(self, bot_token, channel_id):
        self.channel_id = channel_id
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bot {bot_token}",
            "Content-Type": "application/json",
        })

    def send_embed(self, title, description, fields, state, timestamp=None):
        payload = {
            "embeds": [{
                "title": f"{EMOJI[state]} {title}",
                "description": description,
                "color": COLOR[state],
                "fields": [{"name": k, "value": str(v), "inline": True} for k, v in fields.items()],
                "timestamp": timestamp or datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "footer": {"text": "pi-monitoring"},
            }]
        }
        url = f"https://discord.com/api/v10/channels/{self.channel_id}/messages"
        resp = self.session.post(url, json=payload, timeout=10)
        if resp.status_code >= 300:
            logger.error("Discord API error %s: %s", resp.status_code, resp.text)
        else:
            logger.info("Alert sent: %s [%s]", title, state)


class Monitor:
    """Tracks OK/WARNING/CRITICAL state for one metric, only notifying on
    transitions so a sustained condition doesn't spam the channel."""

    def __init__(self, name, notifier, warn_threshold, crit_threshold, metric_label, unit=""):
        self.name = name
        self.notifier = notifier
        self.warn_threshold = warn_threshold
        self.crit_threshold = crit_threshold
        self.metric_label = metric_label
        self.unit = unit
        self.state = OK

    def update(self, value, context=None, timestamp=None):
        context = context or {}
        new_state = OK
        if value >= self.crit_threshold:
            new_state = CRITICAL
        elif value >= self.warn_threshold:
            new_state = WARNING

        if new_state == self.state:
            return
        previous = self.state
        self.state = new_state

        if new_state == OK:
            title = f"{self.name}: back to normal"
            description = f"Back to normal (was {previous})."
        elif new_state == WARNING:
            title = f"{self.name}: elevated"
            description = f"{self.metric_label} crossed the warning threshold."
        else:
            title = f"{self.name}: critical"
            description = f"{self.metric_label} crossed the critical threshold."

        fields = {
            self.metric_label: f"{value}{self.unit}",
            "Warn at": f"{self.warn_threshold}{self.unit}",
            "Critical at": f"{self.crit_threshold}{self.unit}",
        }
        fields.update(context)
        self.notifier.send_embed(title, description, fields, new_state, timestamp=timestamp)

    def force(self, state, title, description, fields=None, timestamp=None):
        """Emit an alert for a non-numeric transition (e.g. container down),
        de-duplicated the same way update() is: nothing is sent while the
        state is unchanged."""
        if state == self.state:
            return
        self.state = state
        self.notifier.send_embed(title, description, fields or {}, state, timestamp=timestamp)

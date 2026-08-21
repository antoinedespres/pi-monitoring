"""Maintains a Discord Gateway connection so the bot shows as online with a
status, and dispatches INTERACTION_CREATE events (slash commands) to a
callback. Alerts are sent over plain REST (see alerting.py) - this
connection is not used to deliver them.

Copied from the sibling home-assistant-bot / vps-monitoring bots so the whole
fleet shares one battle-tested Gateway implementation."""
import json
import logging
import threading
import time

import requests
import websocket

logger = logging.getLogger("pi-monitoring")

GATEWAY_VERSION = 10
OP_DISPATCH = 0
OP_HEARTBEAT = 1
OP_RECONNECT = 7
OP_INVALID_SESSION = 9


class GatewayPresence:
    def __init__(self, token, status_text="the Raspberry Pi", on_interaction=None):
        self.token = token
        self.status_text = status_text
        # Called (in its own thread, so it can't block the heartbeat loop)
        # with the interaction payload whenever Discord dispatches
        # INTERACTION_CREATE - e.g. a slash command invocation.
        self.on_interaction = on_interaction
        self._stop = threading.Event()

    def start(self):
        threading.Thread(target=self._run_forever, daemon=True).start()

    def _run_forever(self):
        while not self._stop.is_set():
            try:
                self._connect_once()
            except Exception:
                logger.exception("Gateway presence connection error, reconnecting in 5s")
            time.sleep(5)

    def _gateway_url(self):
        resp = requests.get(
            "https://discord.com/api/v10/gateway/bot",
            headers={"Authorization": f"Bot {self.token}"},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()["url"]

    def _connect_once(self):
        url = f"{self._gateway_url()}/?v={GATEWAY_VERSION}&encoding=json"
        ws = websocket.create_connection(url, timeout=30)
        try:
            hello = json.loads(ws.recv())
            interval = hello["d"]["heartbeat_interval"] / 1000

            ws.send(json.dumps({
                "op": 2,
                "d": {
                    "token": self.token,
                    "intents": 0,
                    "properties": {"os": "linux", "browser": "pi-monitoring", "device": "pi-monitoring"},
                    "presence": {
                        "since": None,
                        "activities": [{"name": self.status_text, "type": 3}],  # type 3 = Watching
                        "status": "online",
                        "afk": False,
                    },
                },
            }))
            logger.info("Gateway presence connected")

            last_heartbeat = time.time()
            ws.settimeout(1)
            while not self._stop.is_set():
                try:
                    msg = ws.recv()
                except websocket.WebSocketTimeoutException:
                    msg = None
                if msg:
                    data = json.loads(msg)
                    op = data.get("op")
                    if op == OP_DISPATCH and data.get("t") == "INTERACTION_CREATE" and self.on_interaction:
                        threading.Thread(target=self.on_interaction, args=(data["d"],), daemon=True).start()
                    elif op == OP_HEARTBEAT:  # server requested an immediate heartbeat
                        ws.send(json.dumps({"op": 1, "d": None}))
                    elif op in (OP_RECONNECT, OP_INVALID_SESSION):
                        break
                if time.time() - last_heartbeat >= interval:
                    ws.send(json.dumps({"op": 1, "d": None}))
                    last_heartbeat = time.time()
        finally:
            ws.close()

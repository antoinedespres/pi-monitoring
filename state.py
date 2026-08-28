"""Tiny JSON state file for facts that must survive a restart.

Only one thing needs it today: how far through the Pi's apt history the bot has
already reported. Without it every deploy would either replay the last upgrade
or silently skip one.

Best-effort by design. If the path can't be read or written the bot logs once
and carries on with in-memory state, which costs at most one missed
notification around a restart rather than taking the monitor down.
"""
import json
import logging
import os
import tempfile

logger = logging.getLogger("pi-monitoring")


class State:
    def __init__(self, path):
        self.path = path
        self._data = {}
        self._writable = bool(path)
        if path:
            self._load()

    def _load(self):
        try:
            with open(self.path, encoding="utf-8") as fh:
                self._data = json.load(fh)
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as exc:
            logger.warning("Ignoring unreadable state file %s: %s", self.path, exc)

    def get(self, key, default=None):
        return self._data.get(key, default)

    def set(self, key, value):
        self._data[key] = value
        self._save()

    def _save(self):
        if not self._writable:
            return
        directory = os.path.dirname(self.path) or "."
        try:
            os.makedirs(directory, exist_ok=True)
            # Write-then-rename so a crash mid-write can't leave a truncated file.
            fd, tmp = tempfile.mkstemp(dir=directory)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self._data, fh)
            os.replace(tmp, self.path)
        except OSError as exc:
            self._writable = False
            logger.warning("State file %s is not writable (%s) - continuing in memory only",
                           self.path, exc)

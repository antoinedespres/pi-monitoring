"""SSH transport to the monitored Raspberry Pi.

The bot runs on the VPS (alongside the sibling bots) and reaches the Pi over
the network - by design, so that losing contact with the Pi is itself an
alertable signal rather than taking the monitor down with it. Metrics are
collected by running small read-only commands on the Pi over SSH (typically
across the tailnet).

`PiUnreachable` is raised when the SSH connection itself can't be established or
a command dies mid-flight - the monitor treats that as "Pi down". A command
that runs but exits non-zero is *not* unreachable; it returns its exit code so
individual collectors can degrade gracefully (e.g. docker not permitted)."""
import logging
import threading

import paramiko

logger = logging.getLogger("pi-monitoring")


class PiUnreachable(Exception):
    """The Pi could not be reached (connect failed, auth failed, or the
    transport dropped)."""


class Pi:
    def __init__(self, host, user, key_path=None, key_str=None, port=22, connect_timeout=8, command_timeout=30):
        self.host = host
        self.user = user
        self.port = port
        self.key_path = key_path
        self.key_str = key_str
        self.connect_timeout = connect_timeout
        self.command_timeout = command_timeout
        self._client = None
        self._lock = threading.Lock()  # commands come from the poll loop and slash-command threads

    def _load_key(self):
        if self.key_str:
            import io
            for cls in (paramiko.Ed25519Key, paramiko.RSAKey, paramiko.ECDSAKey):
                try:
                    return cls.from_private_key(io.StringIO(self.key_str))
                except paramiko.SSHException:
                    continue
            raise PiUnreachable("PI_SSH_KEY is not a usable OpenSSH private key")
        if self.key_path:
            for cls in (paramiko.Ed25519Key, paramiko.RSAKey, paramiko.ECDSAKey):
                try:
                    return cls.from_private_key_file(self.key_path)
                except paramiko.SSHException:
                    continue
            raise PiUnreachable(f"key at {self.key_path} is not a usable OpenSSH private key")
        return None  # fall back to the agent / default keys

    def _connect(self):
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            client.connect(
                hostname=self.host,
                port=self.port,
                username=self.user,
                pkey=self._load_key(),
                timeout=self.connect_timeout,
                banner_timeout=self.connect_timeout,
                auth_timeout=self.connect_timeout,
                allow_agent=False,
                look_for_keys=self.key_path is None and self.key_str is None,
            )
        except (paramiko.SSHException, OSError) as exc:
            raise PiUnreachable(f"cannot connect to {self.user}@{self.host}:{self.port} - {exc}") from exc
        self._client = client

    def _ensure(self):
        if self._client is None or self._client.get_transport() is None or not self._client.get_transport().is_active():
            self._connect()

    def run(self, command):
        """Run a command on the Pi. Returns (exit_code, stdout, stderr).
        Raises PiUnreachable if the SSH connection can't be established or
        drops. One automatic reconnect is attempted on a dropped transport."""
        with self._lock:
            for attempt in (1, 2):
                try:
                    self._ensure()
                    stdin, stdout, stderr = self._client.exec_command(command, timeout=self.command_timeout)
                    out = stdout.read().decode("utf-8", "replace")
                    err = stderr.read().decode("utf-8", "replace")
                    code = stdout.channel.recv_exit_status()
                    return code, out, err
                except PiUnreachable:
                    raise
                except (paramiko.SSHException, OSError) as exc:
                    # Stale connection: drop it and retry once with a fresh one.
                    self._client = None
                    if attempt == 2:
                        raise PiUnreachable(f"command failed on {self.host}: {exc}") from exc

    def check(self):
        """Cheap liveness probe. Raises PiUnreachable if the Pi is down."""
        code, _, _ = self.run("true")
        return code == 0

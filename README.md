# Pi Monitoring

Discord bot that monitors a Raspberry Pi — CPU, RAM, disk, temperature, Docker
containers and pending apt updates — with **read-only slash commands** and
**proactive alert embeds** (🟢 recovered / 🟠 warning / 🔴 critical).

**It runs on the VPS**, next to the sibling bots (`home-assistant-bot`,
`vps-monitoring`), and watches the Pi **remotely over SSH** (across the tailnet).
That placement is deliberate: because the monitor lives off-box, the Pi going
offline is itself an alert — a bot running *on* the Pi would simply go dark with
it. Same Gateway implementation, alerting model and CI/CD flow as its siblings.

## Slash commands

Set `DISCORD_GUILD_ID` to enable them (registered in your server on startup —
instant, unlike global commands which can take up to an hour):

- `/status` — one-glance overview: CPU, RAM, disk `/`, temperature, uptime, containers running, updates pending.
- `/cpu` — utilisation across all cores, load averages, top processes by CPU.
- `/mem` — RAM + swap usage, top processes by memory.
- `/disk` — usage per mounted filesystem, with a bar and free space.
- `/docker` — every container with a 🟢/🟠/🔴 state and its status line.
- `/updates` — upgradable apt packages (security ones flagged), and whether a reboot is required.
- `/uptime` — uptime, boot time and load averages.

If the Pi can't be reached when a command runs, the bot replies with a clear
🔌 *unreachable* embed instead of failing silently.

## Proactive alerts

The background poll loop (`POLL_INTERVAL_SECONDS`, default 60s) sends an embed
**only on a state transition**, so a sustained condition never spams the channel:

- **Pi unreachable** — no SSH response for `UNREACHABLE_THRESHOLD` consecutive checks (default 3, to debounce a brief blip or the Ethernet→Wi-Fi failover switchover), and a 🟢 when it comes back. *This is the headline feature: you find out when the Pi drops.*
- **CPU / RAM / disk / temperature** cross configurable warn/critical thresholds, with a 🟢 on recovery. (CPU must stay high across two polls to trip, so a momentary spike is ignored.)
- **Docker containers** going down (🔴), unhealthy (🟠) or recovering (🟢). Containers already stopped when the bot starts are not alerted — only real transitions.
- **Reboot required** appearing after a package update.

## How it collects metrics

Every collector runs one small read-only command on the Pi over SSH and parses
the output — standard tools only, so there is **nothing to install on the Pi**:

| Data | Command on the Pi |
|---|---|
| CPU % / load / uptime | `cat /proc/stat`, `/proc/loadavg`, `/proc/uptime` |
| Memory | `cat /proc/meminfo` |
| Temperature | `cat /sys/class/thermal/thermal_zone0/temp` (fallback `vcgencmd`) |
| Disk | `df -PB1` |
| Top processes | `ps -eo pcpu,rss,pid,comm` |
| Containers | `docker ps -a --format '{{json .}}'` |
| Updates | `apt-get -s dist-upgrade`, `test -e /run/reboot-required` |

`PiUnreachable` (SSH connect/transport failure) is treated as "Pi down";
a command that merely exits non-zero (e.g. docker not permitted) degrades to a
`⚠️` note in that one command rather than taking anything else down.

## Setup

1. **Discord bot**: create an Application + Bot at [discord.com/developers/applications](https://discord.com/developers/applications), copy its token, invite it with `Send Messages` + `Embed Links` (OAuth2 → URL Generator → scope `bot`).
2. **Deploy key**: generate a dedicated keypair, authorise it on the Pi, and drop the private key next to `docker-compose.yml` as `pi_id_ed25519` (git-ignored):
   ```bash
   ssh-keygen -t ed25519 -f pi_id_ed25519 -N "" -C "pi-monitoring"
   ssh-copy-id -i pi_id_ed25519.pub antoine@<pi-tailscale-host>
   ```
   For `/docker` to work, that user must be in the `docker` group on the Pi.
3. Copy `.env.example` to `.env` and fill in the Discord values and `PI_SSH_HOST` (the Pi's Tailscale hostname/IP). Adjust thresholds if you like.
4. Run it on the VPS (which must be on the same tailnet as the Pi):
   ```
   docker compose up -d
   ```

## CI/CD

`.github/workflows/release-and-deploy.yml`: on every push to `main` it bumps the
patch version in `VERSION`, tags the commit, builds a Docker image, pushes it to
`ghcr.io/<owner>/pi-monitoring`, then SSHes into the VPS and runs
`docker compose pull && docker compose up -d` — identical to the sibling bots.

Required repo secrets: `VPS_HOST`, `VPS_SSH_PORT`, `VPS_SSH_USER`, `VPS_SSH_KEY`
(a dedicated deploy keypair). The VPS must have run `docker login ghcr.io` once
with a token that has `read:packages`, and hold this repo's `docker-compose.yml`,
`.env` and `pi_id_ed25519` in `~/apps/pi-monitoring/`.

## Notes

- No `discord.py`: the Gateway/interactions layer is implemented directly on `requests` + `websocket-client` (`presence.py`), shared with the sibling bots. SSH uses `paramiko`. The Gateway connection is used only for presence + slash-command dispatch; alerts go over plain REST.
- One SSH connection is reused across the poll loop and slash-command threads (guarded by a lock) and transparently reconnected if it drops.

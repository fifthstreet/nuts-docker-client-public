# How it works

One Python file (`nuts_client.py`, standard library only), one image, two containers. Linux hosts with systemd only.

```
 NUT server <--TCP 3493, every poll-- nuts-client (unprivileged) --writes--> /box/shutdown-request  (volume nuts-box)
 (owns the UPS)                       dashboard :8099, notifications, MQTT                |
                                                                      nuts-helper (privileged, no network)
                                                                      deletes the request, then:
                                                                      nsenter -> host systemd: systemctl poweroff
```

## The switch

`active` is one boolean, saved in `/data/settings.json`, default **OFF**. The dashboard (or the Home Assistant switch) is the only thing that changes it.

| Switch | A UPS event reaches a shutdown rule | Notifications |
|---|---|---|
| ON | a request file is written to the drop-box; the helper powers the host off | always |
| OFF | nothing is written; one "NOT shutting down" notice per outage | only if "notifications while OFF" is enabled |

Turning it ON while a rule is already met acts on the next poll (the dashboard asks you to confirm if the UPS is on battery right now). Turning it OFF deletes any unconsumed request.

## Rules

| Condition | Result |
|---|---|
| on battery (`OB`) for at least `on_battery_seconds` (600) | shutdown rule met |
| `battery.runtime` below `min_runtime` (300) while on battery | shutdown rule met |
| low battery (`LB`) | shutdown rule met |
| forced shutdown (`FSD`) from the NUT master | shutdown rule met |
| NUT unreachable for 3 polls **while on battery** | shutdown rule met (the NUT machine is probably going down itself); the on-battery timer also keeps running without data |
| NUT unreachable for 3 polls, mains OK | notification only |
| back on mains (`OL`) | "power restored", counters reset |

One request per outage. If the request cannot be written you get one "CANNOT shut down" alert, and the write is retried every poll. The start of an outage is saved (`/data/outage.json`), so restarting the client during an outage resumes the timer instead of restarting it.

## The two containers

Same image (`python:3.12-alpine`), different command.

**`nuts-client`**: read-only filesystem, all capabilities dropped, `no-new-privileges`. Volumes: `nuts-data` (`/data`: events, settings, outage start) and `nuts-box` (`/box`). It never touches the host. It also mounts the host's `/etc/hostname` read-only, only to learn the host's name.

**`nuts-helper`**: `privileged`, `pid: host`, `network_mode: none`, read-only, root. Only volume: `nuts-box`. No settings, no Docker socket, no network. Once a second it:

1. Writes `/box/helper-heartbeat` every 2 s, and every 60 s proves it can reach the host (`nsenter -t 1 -m -u -i -n -p -- systemctl --version`).
2. If `/box/shutdown-request` exists: **deletes it first**, ignores it if older than 10 minutes, otherwise records `/box/last-shutdown.json`, logs to the host journal (`logger -t nuts-docker-client`), runs `sync`, and powers off:
   1. `systemctl poweroff` in the host namespace, wait 45 s;
   2. `systemctl --force poweroff`, wait 20 s;
   3. `sync` + sysrq `o` (immediate), wait 30 s;
   4. still running: heartbeat `state: failed`, which the client turns into a "SHUTDOWN FAILED" alert.
3. Logs anything unexpected and carries on. Docker restarts the container if the process ever dies.

The helper does **not** `docker stop` containers itself: a container stopped by hand under `restart: unless-stopped` would not come back after the reboot. `systemctl poweroff` lets Docker stop everything the normal way, and everything returns at boot.

## Checks the client makes

- Request still present after 20 s: "shutdown request NOT picked up" alert (sent even with notifications off).
- Helper reports `failed`: "SHUTDOWN FAILED" alert.
- After a boot, if `/box/last-shutdown.json` exists: an event and a notification that the host is back after a UPS shutdown (once).
- A dashboard warning whenever the switch is ON and the drop-box is unwritable, or the helper is missing, stale, cannot reach the host, is in dry-run, or has failed.
- Each step of the main loop runs inside an exception guard: an unexpected error is logged as an event and the loop carries on.

## Dashboard and Home Assistant

The dashboard is plain HTTP and open: anyone who can reach the port can use the switch and settings, so keep it on a trusted network and set `NUTS_DASH_ALLOW`. POSTs must be JSON and same-origin. Secrets are write-only (never shown or logged).

For Home Assistant, a background thread keeps one MQTT connection (a minimal built-in MQTT 3.1.1 client). It publishes retained discovery configs under `homeassistant/<component>/nuts_<name>/<id>/config`, a retained JSON state document on `nuts/nuts_<name>/state`, events on `.../event`, and availability (with a last-will) on `.../availability`. It accepts commands on `.../<name>/set`, never retained ones.

Each entity belongs to one of two groups, **ups** (readings of the UPS itself) or **host** (about this server). Two settings choose which groups a server publishes; a disabled entity is removed by publishing an empty retained config. Entity identity does not change when you toggle a group, so history and dashboards survive.

## Files

`nuts_client.py` (everything) · `Dockerfile` · `docker-compose.yml` and `docker-compose.portainer.yml` (the same without `build:`; a test keeps them equal) · `.env.example` · `scripts/docker-shutdown-timeout.sh` (optional host tuning) · `tests/` (fake NUT server, fake MQTT broker, fake host operations).

## Limits

- Linux with systemd only. Needs Docker with `privileged` containers and `pid: host` (not rootless Docker).
- It polls NUT instead of holding an `upsmon` session, so it does not take part in NUT's "all secondaries are done, then the master powers off" handshake. Powering the UPS off stays the NUT server's job.
- After a UPS shutdown the host will not power back on by itself unless the outlets lose power (BIOS "restore on AC power loss") or you use Wake-on-LAN.
- Containers are stopped by the normal systemd/Docker shutdown, so each gets its own grace period (see [configuration](configuration.md#slow-stopping-containers)).
- Pushover is the only notifier besides Home Assistant. One UPS per server. No retry queue if Pushover is down at the moment of an event (the shutdown still proceeds).
- Tested with automated tests, fake host operations and a dry-run mode. Do your own real test before relying on it.

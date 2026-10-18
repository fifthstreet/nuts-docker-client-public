# Configuration

There are two places to configure the app:

- **`.env`** (or the environment variables of your Portainer stack): defaults used the **first time** the app starts.
- **The dashboard's Settings panel**: applied immediately and saved in the `nuts-data` Docker volume. **A saved dashboard value wins over `.env`.**

So use `.env` to set things up, then change things on the dashboard. To go back to `.env` values, change them on the dashboard, or delete the settings with `docker compose down -v` (this also deletes the event log). Never commit your `.env`.

An empty value in `.env` means "use the default".

## Variables

| Variable | Default | Meaning |
|---|---|---|
| `TZ` | `UTC` | Your time zone (e.g. `Europe/Berlin`); used for log and event times. |
| `NUT_HOST` | `127.0.0.1` | The machine running the NUT server. |
| `NUT_PORT` | `3493` | NUT port. |
| `NUT_UPS` | `ups` | UPS name on the NUT server. |
| `NUTS_ACTIVE` | `off` | The shutdown switch. `on` = shut this host down on a UPS event. Normally leave `off` and use the dashboard. |
| `NUTS_REPORT` | `yes` | Send notifications while the switch is OFF. |
| `NUTS_ON_BATTERY_SECONDS` | `600` | Shut down after this long on battery. |
| `NUTS_MIN_RUNTIME_SECONDS` | `300` | ...or when the UPS estimates less runtime than this (`0` turns the rule off). |
| `NUTS_POLL_SECONDS` | `5` | How often the UPS is read. |
| `NUTS_COMM_FAIL_LIMIT` | `3` | Failed polls before a "UPS link lost" notice. |
| `NUTS_HEARTBEAT_SECONDS` | `60` | How often a status line is written to the container log. |
| `NUTS_HOST_LABEL` | the host's hostname | This server's name in the dashboard, notifications and Home Assistant. Must be unique if you run several servers. |
| `NUTS_STARTUP_NOTICE` | `yes` | Send a notification when the client starts. |
| `PUSHOVER_TOKEN`, `PUSHOVER_USER` | empty | Pushover application token and user key. Without them nothing is notified (the dashboard warns you). |
| `NUTS_DASH_ALLOW` | empty (anyone) | Comma-separated client IPs / CIDRs allowed to open the dashboard, e.g. `192.168.0.0/24`. Careful not to lock yourself out. |
| `NUTS_DASH_PUBLISH_PORT` | `8099` | The port the dashboard is published on, on the host. |
| `NUTS_DASH_URL` | empty | The dashboard's address (e.g. `http://192.168.0.20:8099`); linked from the Home Assistant device page. |
| `NUTS_MQTT_HOST`, `NUTS_MQTT_PORT`, `NUTS_MQTT_USER`, `NUTS_MQTT_PASSWORD` | empty / `1883` | MQTT broker for Home Assistant. Empty host = Home Assistant link off. |
| `NUTS_HA_UPS`, `NUTS_HA_HOST` | `yes`, `yes` | Which Home Assistant entities this server publishes. See [Several servers](multiple-servers.md). |
| `NUTS_HELPER_DRYRUN` | `no` | `yes` = the helper logs what it would do but never powers the host off. |

## The dashboard

The Settings panel has the same options (secrets such as the Pushover keys and the MQTT password are write-only: leave blank to keep the current value, a single `-` clears it) plus buttons to **Test NUT connection**, **Test MQTT connection** and **Send test notification** (labelled `[TEST - IGNORE]`).

Also on the dashboard: the live cards, a list of warnings, and the event log (also kept in `/data/events.jsonl`). The same data is available as JSON at `/api/status` and `/api/events`.

## Slow-stopping containers

When the host powers off, Docker stops your containers the normal way: each gets SIGTERM, then SIGKILL after its own grace period (**10 seconds** unless the service sets `stop_grace_period`). Databases and game servers may need longer. Either set `stop_grace_period` on those services, or raise the default for the whole host:

```bash
sudo scripts/docker-shutdown-timeout.sh 120 30
```

This backs up `/etc/docker/daemon.json`, validates the change and reloads Docker without restarting any container. The first number is how long the whole shutdown may take, the second (Docker 28+) the default grace per container.

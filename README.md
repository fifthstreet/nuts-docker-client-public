# nuts-docker-client

**Shut down a Docker host cleanly when your UPS battery is running out.** A small container that watches a [NUT](https://networkupstools.org/) server (for example a NAS with a USB UPS) and powers the host off before the battery dies.

- Runs entirely in Docker. Nothing is installed on the host.
- One **ON / OFF switch** on a web dashboard. OFF (the default) only watches and reports; it never shuts anything down.
- Live UPS readings, an event log and every setting in the browser.
- Optional notifications ([Pushover](https://pushover.net/)) and [Home Assistant](docs/home-assistant.md) (MQTT).
- Runs on several servers that share one UPS, each with its own switch.
- Plain Python (standard library only), one file, with tests.

NUT already ships its own client (`upsmon`) and it is a fine choice. This project is for people who would rather run the client in Docker, with a dashboard, a switch that defaults to "do nothing", and Home Assistant support.

```
 NUT server --(TCP 3493)--> nuts-client ----writes a request file----> nuts-helper
 (owns the UPS)            dashboard :8099                            (privileged, no network)
                           notifications                              powers the host off
```

## Read this first

- It **powers off the machine it runs on.** The switch starts OFF. Test it before you turn it ON (see [Test it safely](#test-it-safely)).
- The dashboard has **no login**. Anyone who can open the page can use the switch. Keep it on a trusted network and restrict it with `NUTS_DASH_ALLOW` (see [docs/configuration.md](docs/configuration.md)).
- The `nuts-helper` container is **privileged** and shares the host's process namespace; that is how it can power the host off. It has no network and no ports, and only reads one file.

## Requirements

- A Linux host with systemd and Docker (with the Compose plugin). Rootless Docker is not supported.
- A NUT server that can see your UPS (many NAS units have one built in; otherwise run the [NUT](https://networkupstools.org/) `upsd` on any machine the UPS is plugged into), reachable from the Docker host on port 3493. The server must allow this host to connect (on Synology: **DSM > Hardware & Power > UPS > Permitted DiskStation Devices**).

## Quick start

```bash
git clone https://github.com/fifthstreet/nuts-docker-client-public.git
cd nuts-docker-client-public
cp .env.example .env
```

Edit `.env`: set `NUT_HOST` to your NUT server and, if you want notifications, the two Pushover keys. Everything else has a sensible default. Then:

```bash
docker compose up -d --build
```

Open `http://<this-host>:8099/` and check:

1. **UPS link: ok** and **Shutdown helper: ready** are green.
2. In *Settings*, press **Test NUT connection** and **Send test notification**.
3. When you are happy, flip the switch to **ON**.

That is the whole setup. The switch position and your settings are remembered across restarts and updates.

## What makes it shut down

While the switch is ON, the host is powered off when any of these is true:

| Condition | Default |
|---|---|
| UPS has been on battery for longer than | 10 minutes |
| UPS-estimated runtime is below | 5 minutes |
| UPS reports low battery | always |
| NUT server orders a shutdown | always |
| NUT server stops answering **while on battery** | after 3 failed polls |

If the NUT server is simply unreachable while mains power is fine, you only get a notification. The two time limits are settings. A shutdown request is sent once per outage.

## Update

```bash
cd nuts-docker-client-public
git pull
docker compose up -d --build
```

Your settings, the switch position and the event log live in a Docker volume and are kept. Read [CHANGELOG.md](CHANGELOG.md) first if you are jumping several versions. To remove old images afterwards: `docker image prune`.

## Uninstall

```bash
docker compose down        # stop and remove the containers; settings are kept
docker compose down -v     # ...and delete the settings and event log too
```

## Test it safely

- `docker compose exec nuts-helper python /app/nuts_client.py helper-selftest` proves the helper can reach the host's systemd. It only runs `systemctl --version`.
- Set `NUTS_HELPER_DRYRUN=yes` in `.env` and run `docker compose up -d`. The helper then does everything except power off, and the dashboard shows it as not ready. Set it back to `no` for real use.
- `docker compose run --rm nuts-client check` reads the UPS once and prints the result.
- The real test is a deliberate one: with the switch ON and a short on-battery limit, unplug the UPS from the wall while you are watching. Do this at a quiet moment.

After a shutdown the host stays off until power returns and the machine starts again (BIOS "restore on AC power loss", Wake-on-LAN, or by hand). Docker then brings your containers back, and the dashboard tells you the host came back after a UPS shutdown.

## More

- [Configuration](docs/configuration.md): every setting and environment variable
- [Several servers on one UPS](docs/multiple-servers.md)
- [Home Assistant](docs/home-assistant.md)
- [Portainer](docs/portainer.md)
- [How it works](docs/how-it-works.md): the rules, the two containers, safety design, limits
- [Troubleshooting](docs/troubleshooting.md)
- [Changelog](CHANGELOG.md) · [Contributing](CONTRIBUTING.md) · [MIT License](LICENSE)

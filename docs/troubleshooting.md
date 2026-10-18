# Troubleshooting

Start with the dashboard's warning lines and event log, then the container logs:

```bash
docker compose logs --tail 50 nuts-client
docker compose logs --tail 50 nuts-helper
```

| Symptom | Likely cause and fix |
|---|---|
| **UPS link: LOST** | The Docker host cannot read the NUT server. Check `NUT_HOST`, port and UPS name in Settings and press **Test NUT connection**. Check the NUT server allows this host (on Synology, add the Docker host's IP under *Permitted DiskStation Devices*; use the host's IP, not the container's). From the host: `docker compose run --rm nuts-client check`. |
| **Shutdown helper: NOT READY** | The helper container is not running, cannot reach the host (it needs `privileged: true` and `pid: host`; rootless Docker will not work) or is in dry-run mode. Run `docker compose exec nuts-helper python /app/nuts_client.py helper-selftest`. |
| "Pushover is not configured" | Enter the Pushover keys in Settings (or `.env`), then press **Send test notification**. |
| Home Assistant card says **not connected** | Press **Test MQTT connection**. Check broker host, port, user and password, and that the broker is reachable from the Docker host. |
| Entities missing in Home Assistant | The two publishing toggles may have removed them: see [Several servers](multiple-servers.md). Also check that the MQTT integration is set up in Home Assistant. |
| A duplicate device in Home Assistant | Two servers share the same name (names must be unique), or an old entry exists from before a rename. Delete the stale device in Home Assistant; retained topics for a renamed server are removed automatically. |
| I changed `.env` but nothing happened | A value saved on the dashboard wins over `.env`. Change it on the dashboard. See [Configuration](configuration.md). |
| Times in the log are wrong | Set `TZ` (e.g. `Europe/Berlin`) in `.env` and run `docker compose up -d`. |
| I locked myself out with the allowed-IPs list | Reset the saved settings: `docker compose down -v` (this also deletes the event log), then start again. |
| Containers were killed during a shutdown | They exceeded their grace period. See [slow-stopping containers](configuration.md#slow-stopping-containers). |
| The host did not come back after a shutdown | Expected: see "After a shutdown" in the [README](../README.md#test-it-safely). Set the BIOS to restore power on AC loss, or use Wake-on-LAN. |
| The dashboard port is taken | Set `NUTS_DASH_PUBLISH_PORT` in `.env` and run `docker compose up -d`. |

Still stuck? Open an issue and include the output of `docker compose ps`, the two log commands above, and the dashboard's warning lines. Remove any keys or passwords first.

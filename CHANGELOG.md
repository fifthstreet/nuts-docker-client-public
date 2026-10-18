# Changelog

## 0.7.0 - first public release

- One container image for every server: the server's name defaults to the host's hostname and is shown in the dashboard heading, browser tab, notifications and Home Assistant.
- Two Home Assistant toggles (`NUTS_HA_UPS`, `NUTS_HA_HOST`) so several servers on one UPS do not create duplicate entities. Turning a group off removes its entities from Home Assistant; turning it on restores them, without reconnecting. Renaming a server removes the old device.
- Dashboard address setting (`NUTS_DASH_URL`) linking the Home Assistant device page to the dashboard; `NUTS_DASH_PUBLISH_PORT` to change the published port.
- The ON / OFF shutdown switch (default OFF), live UPS cards, event log and settings in the browser.
- Everything in Docker: an unprivileged client plus a separate privileged helper that powers the host off. No host installation.
- Shutdown rules: time on battery, UPS runtime, low battery, forced shutdown from the NUT server, and a vanished NUT server while on battery.
- Pushover notifications and Home Assistant over MQTT (with HomeKit battery attributes on the shutdown switch).
- Portainer-ready compose file; dry-run mode and a helper self-test for safe testing.

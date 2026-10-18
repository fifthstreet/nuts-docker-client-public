# Home Assistant

The app can publish itself to [Home Assistant](https://www.home-assistant.io/) over MQTT, with automatic discovery. Nothing to install in Home Assistant besides the MQTT integration and a broker.

## Set up

1. In the dashboard open **Settings**, enter your MQTT broker's host (and user / password if it needs them), press **Test MQTT connection**, then **Save**. (Or set `NUTS_MQTT_HOST` and friends in `.env`.)
2. A device called **`<server name> UPS`** appears in Home Assistant. The dashboard's *Home Assistant* card shows the device name and whether it is connected.

Leave the broker host empty to turn the link off.

## What you get

The dashboard and Home Assistant show the same data: both are generated from one table in the code, and a test fails if they drift apart.

| Data | Dashboard | Home Assistant |
|---|---|---|
| UPS status, battery charge, battery remaining, battery load, input voltage | cards | sensors |
| On battery for, shutdown in, shutdown requests | cards | sensors |
| UPS link, shutdown helper | cards | binary sensors |
| UPS model, client uptime | header | diagnostic sensors |
| Warnings | warning lines | *Warnings* sensor and *Needs attention* binary sensor |
| On battery (yes/no) | UPS status card | binary sensor |
| Events | event log | *UPS event* entity (on battery, power restored, link lost / restored, shutdown requested / blocked / failed, switch on / off, host restarted, test) and *Last event* sensor |
| Shutdown switch, notify while OFF | switch and checkbox | two switches |
| Test notification | button | button |
| Shutdown timers | Settings | two number entities |
| Addresses, keys, name, polling, allowed IPs | Settings | dashboard only |

Values are pushed whenever they change and events instantly. The device shows *unavailable* if the container stops.

With several servers on one UPS, choose which server publishes which group of entities: see [Several servers](multiple-servers.md).

## HomeKit

The **Shutdown switch** also carries the live UPS readings as attributes (`battery_level`, `battery_charging`, `battery_remaining_s`, `battery_load_pct`, `input_voltage`, `ups_status`). Home Assistant's HomeKit bridge turns `battery_level` and `battery_charging` into a **Battery service on the switch's tile** in Apple Home. HomeKit has no place for runtime, load or voltage on a switch, so those stay in Home Assistant. If the Battery service does not appear, reload the HomeKit bridge entry and check the switch is among the bridge's included entities.

## Security

Anyone who can publish to your MQTT broker can use these controls, **including the shutdown switch**. If the broker is shared or reachable from untrusted devices, use broker accounts and access rules. Connection settings and secrets are never published to MQTT.

## Changing the server name

If you change a server's name, the old device is removed from the broker automatically so Home Assistant does not keep a duplicate.

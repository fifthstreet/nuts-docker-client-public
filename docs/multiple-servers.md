# Several servers on one UPS

Run the same container on every Docker host that is plugged into the UPS. Each server has its **own switch** and shuts itself down; they do not talk to each other.

## Setting up each server

On every server: clone the repo, create `.env`, run `docker compose up -d --build` as in the [quick start](../README.md#quick-start), and allow the host on the NUT server. A few things differ per server:

| Setting | What to do |
|---|---|
| Name (`NUTS_HOST_LABEL`) | Usually nothing: it defaults to the host's own hostname. It must be unique. |
| `NUTS_DASH_PUBLISH_PORT` | Only if port 8099 is taken on that host. |
| `NUTS_DASH_URL` | Optional; adds a link from Home Assistant to that server's dashboard. |
| `NUTS_HA_UPS` / `NUTS_HA_HOST` | Which Home Assistant entities it publishes (below). |

Each server's dashboard heading and notifications carry its name, so you always know which one you are looking at. Every new install starts with the switch **OFF**.

## Avoiding duplicate entities in Home Assistant

Every server can see the same UPS, so the UPS readings would show up once per server. Two settings stop that:

| Setting | Publishes | Set it to yes on |
|---|---|---|
| `NUTS_HA_UPS` (*Home Assistant: publish the UPS readings*) | UPS status, battery charge / remaining / load, input voltage, UPS model, on-battery | **one** server only |
| `NUTS_HA_HOST` (*Home Assistant: publish this server's entities*) | the shutdown switch, notify switch, test button, events, UPS link, shutdown helper, timers, warnings, uptime | every server |

Example: server 1 keeps both at `yes`; on server 2 set `NUTS_HA_UPS=no`. Home Assistant then shows one set of UPS readings and one shutdown switch per server.

You can change both on the dashboard at any time. Turning a group off **removes its entities from Home Assistant**; turning it on brings them back, without reconnecting. While a server's own group is off, Home Assistant cannot control it. The UPS readings stay on that server's dashboard either way.

## Tips

- Give the NUT server a stable address; every server points `NUT_HOST` at it.
- Each server has its own time limits, so you can make some servers shut down earlier than others.
- Test each server separately with its own [dry run](../README.md#test-it-safely).

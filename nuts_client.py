#!/usr/bin/env python3
"""nuts-docker-client -- watch a NUT (Network UPS Tools) server and shut this host down when the battery is running out.

One switch on the dashboard (default :8099):
  ON  = when the UPS says so (on battery too long, runtime too low, low battery, NAS forced shutdown) this host is shut
        down (by the separate nuts-helper container), and notifications are sent.
  OFF = this host is never shut down. "Send notifications while OFF" decides whether it still reports.
The dashboard also shows the live UPS data, the event log and every setting. Standard library only.
"""
import collections
import html
import http.server
import ipaddress
import json
import os
import queue
import re
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
import urllib.request

__version__ = "0.7.0"
# The drop-box is a Docker volume shared by the two containers: the client writes the request, the helper acts on it.
REQUEST_FILE = "shutdown-request"
HEARTBEAT_FILE = "helper-heartbeat"     # written by the helper every 2 s; the dashboard's proof that the helper is alive
LAST_SHUTDOWN_FILE = "last-shutdown.json"
HELPER_LOG = "helper.log"
REQUEST_MAX_AGE = 600               # seconds: the helper ignores (and deletes) older requests, e.g. left over from before a reboot
HEARTBEAT_STALE = 20                # seconds without a heartbeat = helper not ready
EVENT_LOG_MAX_BYTES = 5 * 1024 * 1024
HOSTNAME_FILE = "/host-hostname"    # the host's /etc/hostname, bind-mounted read-only by the compose file: the default host label


# ----------------------------------------------------------------------------- config
def _int(env, key, default):
    try:
        return int(env.get(key, default))
    except (TypeError, ValueError):
        return int(default)


def _yes(v):
    return str(v).strip().lower() in ("1", "yes", "true", "on")


def default_label(env):
    """The name this server goes by: the host's own hostname (mounted into the container), else the container's hostname."""
    try:
        with open(env.get("NUTS_HOSTNAME_FILE", HOSTNAME_FILE)) as f:
            name = f.read().strip().splitlines()[0].strip()
        if name:
            return name[:40]
    except (OSError, IndexError):
        pass
    return socket.gethostname()


class Config:
    """Deployment defaults (env) + live settings (changed on the dashboard, persisted in /data/settings.json)."""

    def __init__(self, env=None):
        # An empty value means "not set" (compose passes ${VAR:-} as an empty string), so the built-in default applies.
        e = {k: v for k, v in dict(os.environ if env is None else env).items() if v != ""}
        self.nut_host = e.get("NUT_HOST", "127.0.0.1")
        self.nut_port = _int(e, "NUT_PORT", 3493)
        self.ups = e.get("NUT_UPS", "ups")
        self.poll_seconds = _int(e, "NUTS_POLL_SECONDS", 5)
        self.on_battery_seconds = _int(e, "NUTS_ON_BATTERY_SECONDS", 600)   # shut down after this long on battery
        self.min_runtime = _int(e, "NUTS_MIN_RUNTIME_SECONDS", 300)         # ...or when UPS runtime falls below this
        self.comm_fail_limit = _int(e, "NUTS_COMM_FAIL_LIMIT", 3)
        self.active = _yes(e.get("NUTS_ACTIVE", "off"))                      # THE switch; the dashboard overrides and persists it
        self.report = _yes(e.get("NUTS_REPORT", "yes"))                      # notifications while OFF
        self.request_dir = e.get("NUTS_REQUEST_DIR", "/box")
        self.pushover_token = e.get("PUSHOVER_TOKEN", "")
        self.pushover_user = e.get("PUSHOVER_USER", "")
        self.startup_notice = _yes(e.get("NUTS_STARTUP_NOTICE", "yes"))
        self.label = e.get("NUTS_HOST_LABEL") or default_label(e)
        self.dash_port = _int(e, "NUTS_DASH_PORT", 8099)                     # 0 disables the dashboard
        self.dash_allow = [x.strip() for x in e.get("NUTS_DASH_ALLOW", "").split(",") if x.strip()]
        self.data_dir = e.get("NUTS_DATA_DIR", "/data")                      # settings + event log
        self.heartbeat_seconds = _int(e, "NUTS_HEARTBEAT_SECONDS", 60)
        self.mqtt_host = e.get("NUTS_MQTT_HOST", "")                         # empty = Home Assistant link off
        self.mqtt_port = _int(e, "NUTS_MQTT_PORT", 1883)
        self.mqtt_user = e.get("NUTS_MQTT_USER", "")
        self.mqtt_password = e.get("NUTS_MQTT_PASSWORD", "")
        self.ha_ups = _yes(e.get("NUTS_HA_UPS", "yes"))                      # HA: publish the UPS readings (turn on for ONE server only)
        self.ha_host = _yes(e.get("NUTS_HA_HOST", "yes"))                    # HA: publish this server's own entities
        self.dash_url = e.get("NUTS_DASH_URL", "")                           # this dashboard's address, linked from the HA device page


# ----------------------------------------------------------------------------- editable settings (schema drives the UI)
_NAME = re.compile(r"^[A-Za-z0-9_.\-]{1,253}$")
_TOKEN = re.compile(r"^[A-Za-z0-9]{0,64}$")


def _cidrs(v):
    out = []
    for item in (v if isinstance(v, list) else str(v).replace(" ", "").split(",")):
        if item:
            ipaddress.ip_network(item, strict=False)
            out.append(item)
    return out


SCHEMA = [
    # key, label, kind, min, max, help, secret
    ("report", "Send notifications while OFF", "bool", 0, 1, "OFF never shuts the host down; this decides whether it still reports events.", False),
    ("nut_host", "NUT server host / IP", "str", 1, 253, "The machine that runs the NUT server and owns the UPS (e.g. 192.168.0.10). This Docker host must be allowed to connect to it.", False),
    ("nut_port", "NUT port", "int", 1, 65535, "Normally 3493.", False),
    ("ups", "UPS name", "str", 1, 64, "Name of the UPS on the NUT server (Synology: ups).", False),
    ("poll_seconds", "Poll every (s)", "int", 2, 300, "How often the UPS is read.", False),
    ("on_battery_seconds", "Shut down after on battery for (s)", "int", 30, 86400, "Keep this shorter than any timer after which the NUT server shuts itself down (Synology DSM Safe Mode: 15 min).", False),
    ("min_runtime", "...or when UPS runtime is below (s)", "int", 0, 7200, "0 disables the runtime rule.", False),
    ("comm_fail_limit", "Polls failed before 'link lost' notice", "int", 1, 100, "Notification only - losing the link never shuts the host down.", False),
    ("label", "Host label in notices", "str", 1, 40, "", False),
    ("heartbeat_seconds", "Console heartbeat every (s)", "int", 10, 3600, "", False),
    ("startup_notice", "Notify when the client starts", "bool", 0, 1, "", False),
    ("pushover_token", "Pushover app token", "token", 0, 64, "Write-only; leave blank to keep the current one. A single '-' clears it.", True),
    ("pushover_user", "Pushover user key", "token", 0, 64, "Write-only; leave blank to keep the current one. A single '-' clears it.", True),
    ("mqtt_host", "Home Assistant: MQTT broker host / IP", "str", 0, 253, "Leave empty to turn the Home Assistant link off. The device appears in HA automatically (MQTT discovery).", False),
    ("mqtt_port", "MQTT port", "int", 1, 65535, "Normally 1883.", False),
    ("mqtt_user", "MQTT user", "str", 0, 64, "", False),
    ("mqtt_password", "MQTT password", "secret", 0, 128, "Write-only; leave blank to keep the current one. A single '-' clears it.", True),
    ("ha_ups", "Home Assistant: publish the UPS readings", "bool", 0, 1, "Status, battery charge / remaining / load, input voltage, model, on-battery. The same for every server on this UPS: switch it on for ONE server only, otherwise Home Assistant shows duplicates. Turning it off removes those entities from Home Assistant.", False),
    ("ha_host", "Home Assistant: publish this server's entities", "bool", 0, 1, "Shutdown switch, notify switch, test button, events, UPS link, helper, timers, warnings, uptime: everything that belongs to THIS server (no duplicates between servers). Turning it off removes them from Home Assistant and ignores its commands.", False),
    ("dash_url", "Dashboard address (optional)", "str", 0, 200, "e.g. http://192.168.0.20:8099 - linked from this server's Home Assistant device page.", False),
    ("dash_allow", "Dashboard allowed client IPs / CIDRs", "cidrs", 0, 0, "Comma separated, e.g. 192.168.0.0/24. Empty = any client. Careful not to lock yourself out.", False),
]
SCHEMA_BY_KEY = {s[0]: s for s in SCHEMA}


def validate_setting(key, raw):
    _, _, kind, lo, hi, _, _ = SCHEMA_BY_KEY[key]
    if kind == "int":
        v = int(raw)
        if not lo <= v <= hi:
            raise ValueError("%s must be between %d and %d" % (key, lo, hi))
        return v
    if kind == "bool":
        return raw if isinstance(raw, bool) else _yes(raw)
    if kind == "str":
        v = str(raw).strip()
        if not lo <= len(v) <= hi or any(ord(c) < 32 for c in v):
            raise ValueError("%s must be %d-%d printable characters" % (key, lo, hi))
        if key in ("nut_host", "ups", "mqtt_host") and v and not _NAME.match(v):
            raise ValueError("%s contains characters that are not allowed" % key)
        if key == "dash_url" and v and not re.match(r"^https?://[A-Za-z0-9_.\-:\[\]]+(/\S*)?$", v):
            raise ValueError("dash_url must look like http://192.168.0.20:8099")
        return v
    if kind == "secret":
        v = str(raw)
        if len(v) > hi or any(ord(c) < 32 for c in v):
            raise ValueError("%s must be at most %d printable characters" % (key, hi))
        return v
    if kind == "token":
        v = str(raw).strip()
        if v != "-" and not _TOKEN.match(v):
            raise ValueError("%s must be letters and digits only" % key)
        return v
    if kind == "cidrs":
        try:
            return _cidrs(raw)
        except ValueError as ex:
            raise ValueError("dashboard allow-list: %s" % ex)
    raise ValueError("unknown setting " + key)


class SettingsStore:
    def __init__(self, cfg):
        self.cfg = cfg
        self.path = os.path.join(cfg.data_dir, "settings.json") if cfg.data_dir and os.path.isdir(cfg.data_dir) and os.access(cfg.data_dir, os.W_OK) else None
        self.problems = []

    def load(self):
        if not self.path or not os.path.exists(self.path):
            return
        try:
            with open(self.path) as f:
                data = json.load(f)
        except (OSError, ValueError) as ex:
            self.problems.append("settings.json unreadable (%s) - using defaults, switch is OFF" % ex)
            return
        for k, v in data.items():
            if k in SCHEMA_BY_KEY:
                try:
                    setattr(self.cfg, k, validate_setting(k, v) if not SCHEMA_BY_KEY[k][6] else v)
                except (ValueError, TypeError) as ex:
                    self.problems.append("ignored saved setting %s: %s" % (k, ex))
        if isinstance(data.get("active"), bool):
            self.cfg.active = data["active"]

    def save_overrides(self, changes):
        """Persist changed keys (atomic, 0600)."""
        if not self.path:
            return False
        try:
            data = {}
            if os.path.exists(self.path):
                with open(self.path) as f:
                    data = json.load(f)
            data.update(changes)
            tmp = self.path + ".tmp"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, indent=1)
            os.replace(tmp, self.path)
            return True
        except (OSError, ValueError) as ex:
            log("settings save failed: %s" % ex)
            return False

    def public_view(self):
        out = []
        for key, label, kind, lo, hi, helptext, secret in SCHEMA:
            val = getattr(self.cfg, key)
            if secret:
                val = "(set)" if val else "(not set)"
            elif kind == "cidrs":
                val = ", ".join(val)
            out.append({"key": key, "label": label, "kind": kind, "help": helptext, "secret": secret, "value": val})
        return out

    def apply(self, changes):
        """Validate everything first; apply + persist only if ALL are valid. Returns (changed_keys, errors)."""
        clean, errors = {}, []
        for k, raw in changes.items():
            if k not in SCHEMA_BY_KEY:
                errors.append("unknown setting: %s" % k)
                continue
            if SCHEMA_BY_KEY[k][6] and str(raw).strip() == "":
                continue  # blank secret = keep
            try:
                clean[k] = validate_setting(k, raw)
            except (ValueError, TypeError) as ex:
                errors.append(str(ex))
        if errors:
            return [], errors
        persist, changed = {}, []
        for k, v in clean.items():
            if SCHEMA_BY_KEY[k][6]:
                v = "" if v == "-" else v
            if getattr(self.cfg, k) != v:
                setattr(self.cfg, k, v)
                persist[k] = v
                changed.append(k)
        if persist and not self.save_overrides(persist):
            return changed, ["applied in memory only: the settings file is not writable (changes will be lost on restart)"]
        return changed, []


# ----------------------------------------------------------------------------- drop-box (the only way this container acts on the host)
def helper_info(cfg):
    """What the shutdown helper last said about itself (its heartbeat file), plus ready/problem for the dashboard."""
    info = {"present": False, "ready": False, "state": "", "detail": "", "age": None, "dry_run": False, "host_ok": False}
    try:
        with open(os.path.join(cfg.request_dir, HEARTBEAT_FILE)) as f:
            d = json.load(f)
        info.update(d)
        info["present"] = True
        info["age"] = round(time.time() - float(d.get("ts", 0)), 1)
    except (OSError, ValueError, TypeError):
        pass
    if not info["present"]:
        info["problem"] = "the shutdown helper is not running (start it with: docker compose up -d nuts-helper)"
    elif info["age"] > HEARTBEAT_STALE:
        info["problem"] = "the shutdown helper stopped responding (last heartbeat %ds ago)" % info["age"]
    elif not info.get("host_ok"):
        info["problem"] = "the shutdown helper cannot reach the host (it needs privileged: true and pid: host)"
    elif info.get("dry_run"):
        info["problem"] = "the shutdown helper is in DRY RUN mode (NUTS_HELPER_DRYRUN): it will not power the host off"
    elif info.get("state") == "failed":
        info["problem"] = "the shutdown helper reports a failed shutdown: %s" % info.get("detail", "")
    else:
        info["problem"] = ""
        info["ready"] = True
    return info


def drop_box_status(cfg):
    """(writable, helper_info)."""
    d = cfg.request_dir
    return (os.path.isdir(d) and os.access(d, os.W_OK)), helper_info(cfg)


def clear_request(cfg):
    """Remove an unconsumed shutdown request (e.g. left over from while the helper was not running). True if one was there."""
    path = os.path.join(cfg.request_dir, REQUEST_FILE)
    try:
        os.remove(path)
        return True
    except FileNotFoundError:
        return False
    except OSError as ex:
        log("could not remove stale request %s: %s" % (path, ex))
        return False


class Actuator:
    """Asks the HOST to shut down by writing a request file into the shared drop-box volume.
    The privileged helper container (a separate, network-less container) does the real work."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.history = []

    def shutdown(self, reason):
        tmp = os.path.join(self.cfg.request_dir, REQUEST_FILE + ".tmp")
        with open(tmp, "w") as f:
            json.dump({"ts": time.time(), "reason": reason, "label": self.cfg.label}, f)
        os.replace(tmp, os.path.join(self.cfg.request_dir, REQUEST_FILE))
        self.history.append(reason)


class OutageStore:
    """Remembers when the current on-battery outage began (wall clock) so that a client restart mid-outage does not restart the timer."""

    def __init__(self, cfg):
        d = cfg.data_dir
        self.path = os.path.join(d, "outage.json") if d and os.path.isdir(d) and os.access(d, os.W_OK) else None

    def load(self):
        try:
            with open(self.path) as f:
                return float(json.load(f)["since"])
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def save(self, wall):
        if self.path:
            try:
                with open(self.path + ".tmp", "w") as f:
                    json.dump({"since": wall}, f)
                os.replace(self.path + ".tmp", self.path)
            except OSError as ex:
                log("could not save outage start: %s" % ex)

    def clear(self):
        if self.path:
            try:
                os.remove(self.path)
            except OSError:
                pass


# ----------------------------------------------------------------------------- what is exposed (one table: dashboard AND Home Assistant)
# Every live value below is shown on the dashboard (cards, or the header for card=False) AND published to Home Assistant as
# an entity, from the same numbers. kind: text | num | dur (seconds) | link (ON/OFF). The tests enforce that the two stay equal.
FIELDS = [
    # key, name, kind, unit, ha device_class, ha state_class, ha icon, ha entity_category, dashboard card
    ("status", "UPS status", "text", "", None, None, "mdi:battery-charging", None, True),
    ("battery", "Battery Charge", "num", "%", "battery", "measurement", None, None, True),
    ("runtime", "Battery Remaining", "dur", "s", "duration", "measurement", None, None, True),
    ("load", "Battery Load", "num", "%", None, "measurement", "mdi:gauge", None, True),
    ("input_voltage", "Input voltage", "num", "V", "voltage", "measurement", None, None, True),
    ("on_battery_for", "On battery for", "dur", "s", "duration", None, None, None, True),
    ("shutdown_in", "Shutdown in", "dur", "s", "duration", None, None, None, True),
    ("shutdown_requests", "Shutdown requests", "num", "", None, "total_increasing", None, None, True),
    ("link", "UPS link", "link", "", "connectivity", None, None, "diagnostic", True),
    ("helper", "Shutdown helper", "ready", "", "connectivity", None, None, "diagnostic", True),
    ("model", "UPS model", "text", "", None, None, "mdi:information-outline", "diagnostic", False),
    ("uptime", "Client uptime", "dur", "s", "duration", None, None, "diagnostic", False),
    ("warnings", "Warnings", "text", "", None, None, "mdi:alert-circle-outline", "diagnostic", False),
    ("last_event", "Last event", "text", "", None, None, "mdi:history", "diagnostic", False),
]
# Derived yes/no values for automations (HA binary sensors; the dashboard shows the same facts in the UPS status card / warning lines).
BINARY = [("on_battery", "On battery", "problem", None), ("attention", "Needs attention", "problem", "diagnostic")]
# Settings that Home Assistant may also change (number entities); everything else is edited on the dashboard only (secrets, addresses).
HA_NUMBERS = [("on_battery_seconds", "Shutdown after on battery for", 30, 86400, 30), ("min_runtime", "Shutdown when runtime below", 0, 7200, 30)]


# ----------------------------------------------------------------------------- shared state / audit trail
def log(msg):
    print("%s %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg), flush=True)


class State:
    """Thread-safe live state for the dashboard + an event ring buffer mirrored to a JSONL file."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.started = time.time()
        self.lock = threading.RLock()
        self.events = collections.deque(maxlen=500)
        self.sample = None
        self.last_poll = None
        self.last_ok = None
        self.last_error = ""
        self.polls = self.poll_errors = 0
        self.engine = self.actuator = self.notifier = None
        self.loop_tick = time.time()
        self.event_file = None
        self.settings = SettingsStore(cfg)
        self.last_push_test = 0.0
        self.ha = None              # HomeAssistantLink, set in run_loop
        self.last_event = ""
        if cfg.data_dir and os.path.isdir(cfg.data_dir) and os.access(cfg.data_dir, os.W_OK):
            self.event_file = os.path.join(cfg.data_dir, "events.jsonl")

    def event(self, kind, message):
        rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "kind": kind, "message": message}
        with self.lock:
            self.events.append(rec)
            if kind != "status":
                self.last_event = "%s: %s" % (kind, message)
        if self.event_file:
            try:
                if os.path.exists(self.event_file) and os.path.getsize(self.event_file) > EVENT_LOG_MAX_BYTES:
                    os.replace(self.event_file, self.event_file + ".1")
                with open(self.event_file, "a") as f:
                    f.write(json.dumps(rec) + "\n")
            except OSError as ex:
                log("event log write failed: %s" % ex)

    def record_poll(self, sample, error=""):
        with self.lock:
            self.polls += 1
            self.last_poll = time.time()
            if sample is not None:
                self.sample, self.last_ok, self.last_error = sample, self.last_poll, ""
            else:
                self.poll_errors += 1
                self.last_error = error

    def warnings(self):
        cfg = self.cfg
        out = list(self.settings.problems)
        writable, helper = drop_box_status(cfg)
        if cfg.active and not writable:
            out.append("Switch is ON but the shutdown drop-box %s is not writable: this host will NOT be shut down." % cfg.request_dir)
        elif cfg.active and not helper["ready"]:
            out.append("Switch is ON but %s: this host will NOT be shut down." % helper["problem"])
        if (cfg.active or cfg.report) and not (cfg.pushover_token and cfg.pushover_user):
            out.append("Pushover is not configured: no notifications will be sent.")
        if cfg.mqtt_host and self.ha is not None and not self.ha.connected:
            out.append("Home Assistant (MQTT) not connected: %s" % (self.ha.error or "connecting..."))
        return out

    def watch_shutdown(self):
        """While a shutdown request is outstanding, make sure the helper picks it up, and shout if it does not or fails."""
        eng, cfg = self.engine, self.cfg
        if eng is None or not eng.latched:
            self._alerted = set()
            return
        done = getattr(self, "_alerted", set())
        self._alerted = done
        try:
            age = time.time() - os.path.getmtime(os.path.join(cfg.request_dir, REQUEST_FILE))
        except OSError:
            age = None
        info = helper_info(cfg)
        if age is None and "ack" not in done and info.get("state") in ("shutting_down", "failed"):
            done.add("ack")
            self.event("shutdown", "the helper took the shutdown request (%s)" % info.get("detail", ""))
        if age is not None and age > 20 and "pickup" not in done:
            done.add("pickup")
            self.notifier.send("%s: shutdown request NOT picked up" % cfg.label,
                               "A shutdown was requested %ds ago but the helper has not acted on it (%s)." % (age, info["problem"] or "no problem reported"),
                               priority=1, force=True, code="shutdown_failed")
        if info.get("state") == "failed" and "failed" not in done:
            done.add("failed")
            self.notifier.send("%s: SHUTDOWN FAILED" % cfg.label, "The helper could not power the host off: %s" % info.get("detail", ""),
                               priority=1, force=True, code="shutdown_failed")

    def report_last_shutdown(self):
        """After a boot: tell the user (once) that the previous run ended in a UPS-requested power-off."""
        path = os.path.join(self.cfg.request_dir, LAST_SHUTDOWN_FILE)
        try:
            with open(path) as f:
                rec = json.load(f)
        except (OSError, ValueError):
            return
        try:
            os.replace(path, path[:-5] + ".seen.json")
        except OSError:
            pass
        when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(rec.get("ts", 0)))
        text = "This host was powered off by the UPS shutdown at %s (%s) and is back up." % (when, rec.get("reason", "reason unknown"))
        self.event("shutdown", text)
        self.notifier.send("%s: back up after a UPS shutdown" % self.cfg.label, text, code="host_restarted")

    def emit(self, code, message):
        """Real-time event for Home Assistant (no-op when the link is off)."""
        if self.ha is not None:
            self.ha.emit(code, message)

    def _values(self, s):
        """The flat key/value document behind both the dashboard cards and Home Assistant (see FIELDS)."""
        u = s["ups"] or {}
        warns = s["warnings"]
        return {
            "switch": "ON" if s["active"] else "OFF", "report": "ON" if s["report"] else "OFF",
            "status": u.get("status") or "unknown", "on_battery": "ON" if "OB" in (u.get("status") or "") else "OFF",
            "link": "ON" if s["link"] == "ok" else "OFF", "helper": "ON" if s["helper"]["ready"] else "OFF", "battery": u.get("charge"), "runtime": u.get("runtime"),
            "load": u.get("load"), "input_voltage": u.get("input_voltage"), "model": u.get("model"),
            "on_battery_for": s["on_battery_for"], "shutdown_in": s["shutdown_in"], "shutdown_requests": s["shutdown_requests"],
            "uptime": s["uptime_seconds"] // 60 * 60,          # minute resolution so it does not republish every second
            "warnings": " | ".join(warns)[:250] if warns else "OK", "attention": "ON" if warns else "OFF",
            "on_battery_seconds": s["rules"]["on_battery_seconds"], "min_runtime": s["rules"]["min_runtime"],
            "last_event": self.last_event[:250], "version": __version__,
        }

    def ha_payload(self):
        """Everything Home Assistant gets, as one flat JSON document (published retained whenever it changes)."""
        return self.snapshot()["values"]

    def snapshot(self):
        with self.lock:
            s, eng, act, cfg = self.sample, self.engine, self.actuator, self.cfg
            now = time.time()
            snap = {
                "version": __version__, "label": cfg.label, "active": cfg.active, "report": cfg.report,
                "warnings": self.warnings(),
                "uptime_seconds": int(now - self.started),
                "nut": "%s@%s:%s" % (cfg.ups, cfg.nut_host, cfg.nut_port),
                "link": "ok" if (self.last_ok and self.last_poll == self.last_ok) else "LOST",
                "last_poll_age": None if self.last_poll is None else round(now - self.last_poll, 1),
                "last_error": self.last_error, "polls": self.polls, "poll_errors": self.poll_errors,
                "ups": None if s is None else {
                    "status": " ".join(sorted(s["status"])), "charge": s.get("charge"), "runtime": s.get("runtime"),
                    "load": s.get("load"), "input_voltage": s.get("input_voltage"), "model": s.get("model")},
                "rules": {"on_battery_seconds": cfg.on_battery_seconds, "min_runtime": cfg.min_runtime, "poll_seconds": cfg.poll_seconds},
                "on_battery_for": None, "shutdown_in": None, "latched": False,
                "shutdown_requests": 0 if act is None else len(act.history),
                "helper": {k: v for k, v in helper_info(cfg).items() if k in ("present", "ready", "state", "detail", "age", "dry_run", "host_ok", "problem")},
                "mqtt": {"enabled": bool(cfg.mqtt_host), "connected": bool(self.ha and self.ha.connected), "error": self.ha.error if self.ha else "",
                         "device": "%s UPS" % cfg.label, "node": ha_node(cfg), "topic": "nuts/" + ha_node(cfg),
                         "ups": cfg.ha_ups, "host": cfg.ha_host},
            }
            if eng is not None:
                snap["latched"] = eng.latched
                if eng.on_batt_since is not None:
                    el = int(eng.clock() - eng.on_batt_since)
                    snap["on_battery_for"] = el
                    snap["shutdown_in"] = max(0, cfg.on_battery_seconds - el)
            snap["values"] = self._values(snap)
            snap["fields"] = [{"key": f[0], "name": f[1], "kind": f[2], "unit": f[3], "card": f[8]} for f in FIELDS]
            return snap

    def recent(self, limit=200, kind=None):
        with self.lock:
            evs = list(self.events)
        if kind:
            evs = [e for e in evs if e["kind"] == kind]
        return evs[-limit:][::-1]

    def set_active(self, on, source="the dashboard"):
        cfg = self.cfg
        if cfg.active == on:
            return on
        cfg.active = on
        self.settings.save_overrides({"active": on})
        if not on:
            clear_request(cfg)
        self.event("switch", "shutdown switch turned %s from %s" % ("ON" if on else "OFF", source))
        self.emit("switch_on" if on else "switch_off", "from %s" % source)
        self.notifier.send("%s: shutdown switch %s" % (cfg.label, "ON" if on else "OFF"),
                           "This host WILL be shut down on a UPS event." if on else "This host will not be shut down.", force=True)
        return on


# ----------------------------------------------------------------------------- notifier
class Notifier:
    """Pushover. Sent when the switch is ON, or OFF with 'notifications while OFF' enabled (or force). Failures never raise."""

    def __init__(self, cfg, state=None, kind="notice"):
        self.cfg, self.state, self.kind = cfg, state, kind

    def send(self, title, message, priority=0, force=False, code=None):
        log("NOTICE %s | %s" % (title, message))
        if code and self.state:
            self.state.emit(code, message)
        wanted = force or self.cfg.active or self.cfg.report
        sent = False
        if wanted and self.cfg.pushover_token and self.cfg.pushover_user:
            data = urllib.parse.urlencode({
                "token": self.cfg.pushover_token, "user": self.cfg.pushover_user,
                "title": title[:250], "message": message[:1000], "priority": priority}).encode()
            try:
                urllib.request.urlopen("https://api.pushover.net/1/messages.json", data, timeout=10).read()
                sent = True
            except Exception as ex:  # noqa: BLE001 - alerting must never crash the watcher
                log("pushover failed: %s" % ex)
        if self.state:
            self.state.event(self.kind, "%s | %s%s" % (title, message, "" if sent else ("  (pushover NOT sent)" if wanted else "  (notifications off)")))
        return sent


# ----------------------------------------------------------------------------- NUT protocol
def nut_list_vars(cfg, timeout=5, host=None, port=None, ups=None):
    """LIST VAR <ups> over NUT's plain-text protocol. Read-only; needs no login. Returns {name: value}."""
    out = {}
    with socket.create_connection((host or cfg.nut_host, port or cfg.nut_port), timeout=timeout) as s:
        s.sendall(("LIST VAR %s\n" % (ups or cfg.ups)).encode())
        f = s.makefile("r")
        first = f.readline().strip()
        if not first.startswith("BEGIN LIST VAR"):
            raise RuntimeError("NUT said: %s" % first)
        for line in f:
            line = line.strip()
            if line.startswith("END LIST VAR"):
                break
            if line.startswith("VAR ") and '"' in line:
                name = line.split(" ", 3)[2]
                out[name] = line.split('"', 1)[1].rsplit('"', 1)[0]
        try:
            s.sendall(b"LOGOUT\n")
        except OSError:
            pass
    if "ups.status" not in out:
        raise RuntimeError("NUT reply had no ups.status")
    return out


def _num(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def sample_from_vars(v):
    return {"status": set(v["ups.status"].split()), "runtime": _num(v.get("battery.runtime")),
            "charge": _num(v.get("battery.charge")), "load": _num(v.get("ups.load")),
            "input_voltage": _num(v.get("input.voltage")), "model": v.get("ups.model") or v.get("device.model")}


def poll(cfg):
    return sample_from_vars(nut_list_vars(cfg))


# ----------------------------------------------------------------------------- decision engine
class Engine:
    def __init__(self, cfg, actuator, notifier, clock=time.monotonic, store=None, wall=time.time):
        self.cfg, self.act, self.note, self.clock, self.store, self.wall = cfg, actuator, notifier, clock, store, wall
        self.on_batt_since = None
        self.latched = False        # a shutdown request was written this outage
        self.reported = False       # the "condition reached but switch is OFF" notice was sent this outage
        self.failed = False         # the "cannot shut down" alert was sent this outage
        self.comm_fails = 0
        self.comm_alerted = False
        self._store_checked = False

    def _trigger(self, reason):
        label = self.cfg.label
        if self.cfg.active:
            if self.latched:
                return
            try:
                self.act.shutdown(reason)
            except OSError as ex:
                if not self.failed:
                    self.failed = True
                    self.note.send("%s: CANNOT shut down" % label, "%s - but the shutdown request could not be written: %s" % (reason, ex),
                                   priority=1, force=True, code="shutdown_failed")
                return
            self.latched = True
            self.note.send("%s: SHUTTING DOWN" % label, "Reason: %s" % reason, priority=1, code="shutdown_requested")
        elif not self.reported:
            self.reported = True
            self.note.send("%s: shutdown condition reached (switch is OFF - NOT shutting down)" % label, "Reason: %s" % reason, code="shutdown_blocked")

    def step(self, sample):
        """sample = {'status': set, 'runtime': int|None, ...} or None when the NUT server was unreachable."""
        label = self.cfg.label
        now = self.clock()
        if sample is None:
            self.comm_fails += 1
            if self.comm_fails >= self.cfg.comm_fail_limit and not self.comm_alerted:
                self.comm_alerted = True
                self.note.send("%s: UPS link lost" % label,
                               "Cannot read NUT %s:%s. With mains OK this is notification only; if the UPS is on battery the shutdown rules still apply."
                               % (self.cfg.nut_host, self.cfg.nut_port), code="link_lost")
            if self.on_batt_since is not None:
                # Mid-outage the machine that serves NUT may be shutting itself down (e.g. Synology Safe Mode): keep the timer running and
                # treat a vanished NUT server as the end of the line instead of waiting for data that will never come.
                elapsed = now - self.on_batt_since
                if elapsed >= self.cfg.on_battery_seconds:
                    self._trigger("on battery for %ds (limit %ds); NUT server not answering" % (elapsed, self.cfg.on_battery_seconds))
                if self.comm_fails >= self.cfg.comm_fail_limit:
                    self._trigger("NUT server stopped answering while the UPS was on battery")
            return
        if self.comm_alerted:
            self.note.send("%s: UPS link restored" % label, "NUT reachable again.", code="link_restored")
        self.comm_fails, self.comm_alerted = 0, False
        st = sample["status"]
        if "OB" in st and self.on_batt_since is None:
            began = self.store.load() if self.store else None
            if began is not None and 0 <= self.wall() - began < 86400:
                self.on_batt_since = now - (self.wall() - began)        # client restarted mid-outage: keep counting from the real start
                note = "Client restarted while on battery; %ds of the outage already elapsed." % (self.wall() - began)
            else:
                self.on_batt_since = now
                if self.store:
                    self.store.save(self.wall())
                note = "Mains lost."
            self.note.send("%s: on battery" % label,
                           ("%s Shutdown after %ss on battery or runtime < %ss." % (note, self.cfg.on_battery_seconds, self.cfg.min_runtime))
                           if self.cfg.active else "%s The shutdown switch is OFF, so this host will not be shut down." % note, code="on_battery")
        elif "OB" not in st and not self._store_checked and self.on_batt_since is None:
            if self.store:
                self.store.clear()                                       # power came back while the client was down
        self._store_checked = True
        if "FSD" in st:
            self._trigger("NUT master signalled forced shutdown (FSD)")
        if "LB" in st:
            self._trigger("UPS reports LOW BATTERY")
        if "OB" in st:
            elapsed = now - self.on_batt_since
            if elapsed >= self.cfg.on_battery_seconds:
                self._trigger("on battery for %ds (limit %ds)" % (elapsed, self.cfg.on_battery_seconds))
            rt = sample.get("runtime")
            if self.cfg.min_runtime and rt is not None and rt < self.cfg.min_runtime:
                self._trigger("UPS runtime %ds below limit %ds" % (rt, self.cfg.min_runtime))
        elif "OL" in st and self.on_batt_since is not None:
            self.note.send("%s: power restored" % label, "Back on mains after %ds." % (now - self.on_batt_since), code="power_restored")
            self.on_batt_since = None
            self.latched = self.reported = self.failed = False
            if self.store:
                self.store.clear()


# ----------------------------------------------------------------------------- the shutdown helper (separate privileged container)
HOST_NS = ["nsenter", "-t", "1", "-m", "-u", "-i", "-n", "-p", "--"]     # run a command in the host's namespaces (needs privileged + pid: host)


def _run(cmd, timeout):
    try:
        return subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=timeout).returncode
    except (OSError, subprocess.SubprocessError):
        return 127


def _write_sysrq():
    with open("/proc/sysrq-trigger", "w") as f:
        f.write("o")


class Helper:
    """Watches the drop-box for a shutdown request and powers the host off. Deliberately tiny and dependency-free:
    no network, no Docker socket, no settings. Its only inputs are the request file and the environment."""

    def __init__(self, box="/box", dry_run=False, run=_run, sleep=time.sleep, now=time.time, sync=None, sysrq=_write_sysrq, waits=(45, 20, 30)):
        self.box, self.dry_run, self.run, self.sleep, self.now = box, dry_run, run, sleep, now
        self.sync = sync or getattr(os, "sync", lambda: None)
        self.sysrq, self.waits = sysrq, waits
        self.state, self.detail, self.host_ok = "idle", "", False
        self.started = now()
        self._last_host_check = self._last_beat = 0.0
        self._host_checked = False

    # ---- logging: console (docker logs) + a persistent file in the volume + the host's journal
    def log(self, msg):
        line = "%s helper: %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
        print(line, flush=True)
        path = os.path.join(self.box, HELPER_LOG)
        try:
            if os.path.exists(path) and os.path.getsize(path) > 1024 * 1024:
                os.replace(path, path + ".1")
            with open(path, "a") as f:
                f.write(line + "\n")
        except OSError:
            pass

    def host_log(self, msg):
        self.run(HOST_NS + ["logger", "-t", "nuts-docker-client", msg], 10)

    def heartbeat(self):
        data = {"ts": self.now(), "state": self.state, "detail": self.detail, "version": __version__, "host_ok": self.host_ok,
                "dry_run": self.dry_run, "pid": os.getpid(), "started": self.started}
        path = os.path.join(self.box, HEARTBEAT_FILE)
        try:
            with open(path + ".tmp", "w") as f:
                json.dump(data, f)
            os.chmod(path + ".tmp", 0o644)
            os.replace(path + ".tmp", path)
        except OSError as ex:
            print("helper: cannot write heartbeat: %s" % ex, flush=True)

    def check_host(self):
        ok = self.run(HOST_NS + ["systemctl", "--version"], 15) == 0
        if ok != self.host_ok or not self._host_checked:
            self._host_checked = True
            self.log("host access %s" % ("OK (can run systemctl in the host namespace)" if ok else "FAILED (needs privileged: true and pid: host)"))
        self.host_ok = ok
        return ok

    def handle(self, path):
        """A request appeared. Delete it FIRST (so nothing can ever replay it), then power the host off."""
        try:
            age = self.now() - os.path.getmtime(path)
            with open(path) as f:
                text = f.read(2000).strip()
        except OSError as ex:
            self.log("request unreadable: %s" % ex)
            age, text = 0, ""
        try:
            os.remove(path)
        except OSError as ex:
            self.log("could not delete the request (%s); continuing" % ex)
        try:
            reason = json.loads(text).get("reason", text)
        except (ValueError, AttributeError):
            reason = text or "no reason given"
        if age > REQUEST_MAX_AGE:
            self.log("ignored a stale request (%ds old): %s" % (age, reason))
            return
        self.log("SHUTDOWN REQUEST received: %s" % reason)
        if self.dry_run:
            self.state, self.detail = "idle", "DRY RUN: would have powered the host off (%s)" % reason
            self.log("DRY RUN: not powering off")
            self.heartbeat()
            return
        self.state, self.detail = "shutting_down", reason
        self.heartbeat()
        try:
            with open(os.path.join(self.box, LAST_SHUTDOWN_FILE), "w") as f:
                json.dump({"ts": self.now(), "reason": reason}, f)
        except OSError as ex:
            self.log("could not write the shutdown record: %s" % ex)
        self.host_log("UPS shutdown requested: %s - powering off" % reason)
        self.sync()
        attempts = [("systemctl poweroff", lambda: self.run(HOST_NS + ["systemctl", "poweroff"], 30)),
                    ("systemctl --force poweroff", lambda: self.run(HOST_NS + ["systemctl", "--force", "poweroff"], 30)),
                    ("sysrq o (immediate)", lambda: (self.sync(), self.sysrq(), 0)[2])]
        for (name, fn), wait in zip(attempts, self.waits):
            self.log("attempt: %s" % name)
            try:
                rc = fn()
            except Exception as ex:  # noqa: BLE001
                rc = "error %s" % ex
            self.log("  %s returned %s; waiting %ds for the host to go down" % (name, rc, wait))
            self.sleep(wait)
            self.log("  still running after %s" % name)
        self.state, self.detail = "failed", "the host did not power off after 3 attempts (%s)" % reason
        self.log("FAILED: %s" % self.detail)
        self.heartbeat()

    def tick(self):
        now = self.now()
        req = os.path.join(self.box, REQUEST_FILE)
        if os.path.exists(req):
            self.handle(req)
        if now - self._last_host_check >= 60:
            self._last_host_check = now
            self.check_host()
        if now - self._last_beat >= 2:
            self._last_beat = now
            self.heartbeat()

    def loop(self):
        self.log("started v%s box=%s dry_run=%s" % (__version__, self.box, self.dry_run))
        self.check_host()
        self._last_host_check = self.now()
        if self.state == "idle" and self.dry_run:
            self.detail = "DRY RUN: will not power the host off"
        self.heartbeat()
        while True:
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - the helper must never die
                self.log("tick error: %s" % traceback.format_exc().replace("\n", " | "))
            time.sleep(1)


def helper_health(box):
    try:
        with open(os.path.join(box, HEARTBEAT_FILE)) as f:
            return time.time() - float(json.load(f)["ts"]) < 15
    except (OSError, ValueError, KeyError, TypeError):
        return False


# ----------------------------------------------------------------------------- Home Assistant (MQTT, with HA discovery)
def _mq_str(s):
    b = s.encode()
    return len(b).to_bytes(2, "big") + b


def _mq_len(n):
    out = bytearray()
    while True:
        d, n = n % 128, n // 128
        out.append(d | 128 if n else d)
        if not n:
            return bytes(out)


class MqttClient:
    """Just enough MQTT 3.1.1 (QoS 0, retained publish, subscribe, keepalive, last-will) so the container needs no libraries."""

    def __init__(self, host, port, client_id, user="", password="", will=None, keepalive=60):
        self.host, self.port, self.client_id, self.user, self.password = host, port, client_id, user, password
        self.will, self.keepalive = will, keepalive      # will = (topic, payload) published retained if we vanish
        self.sock = None
        self.buf = b""

    def connect(self, timeout=5):
        self.sock = socket.create_connection((self.host, self.port), timeout=timeout)
        flags = 0x02                                    # clean session
        body = _mq_str("MQTT") + bytes([4]) + b"\x00" + self.keepalive.to_bytes(2, "big") + _mq_str(self.client_id)
        if self.will:
            flags |= 0x04 | 0x20                        # will + will retain
            body += _mq_str(self.will[0]) + _mq_str(self.will[1])
        if self.user:
            flags |= 0x80
            body += _mq_str(self.user)
            if self.password:
                flags |= 0x40
                body += _mq_str(self.password)
        body = body[:7] + bytes([flags]) + body[8:]
        self.sock.sendall(bytes([0x10]) + _mq_len(len(body)) + body)
        data = b""
        while len(data) < 4:
            chunk = self.sock.recv(4 - len(data))
            if not chunk:
                raise ConnectionError("broker closed the connection")
            data += chunk
        if data[0] != 0x20 or data[3] != 0:
            codes = {1: "bad protocol", 2: "client id rejected", 3: "broker unavailable", 4: "bad user or password", 5: "not authorised"}
            raise ConnectionError("broker refused the connection: %s" % codes.get(data[3], "code %d" % data[3]))

    def publish(self, topic, payload, retain=False):
        body = _mq_str(topic) + (payload if isinstance(payload, bytes) else payload.encode())
        self.sock.sendall(bytes([0x30 | (1 if retain else 0)]) + _mq_len(len(body)) + body)

    def subscribe(self, topic):
        body = (1).to_bytes(2, "big") + _mq_str(topic) + b"\x00"
        self.sock.sendall(bytes([0x82]) + _mq_len(len(body)) + body)

    def ping(self):
        self.sock.sendall(b"\xc0\x00")

    def poll(self, timeout=1.0):
        """Wait up to `timeout` for data; return the incoming PUBLISHes as [(topic, payload, retained)]."""
        self.sock.settimeout(timeout)
        try:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("broker closed the connection")
            self.buf += chunk
        except socket.timeout:
            pass
        out = []
        while len(self.buf) >= 2:
            mult, n, i = 1, 0, 1
            while True:
                if i >= len(self.buf):
                    return out                          # length bytes not all here yet
                d = self.buf[i]
                n += (d & 127) * mult
                mult *= 128
                i += 1
                if not d & 128:
                    break
            if len(self.buf) < i + n:
                return out                              # packet not complete yet
            first, body, self.buf = self.buf[0], self.buf[i:i + n], self.buf[i + n:]
            if first >> 4 == 3 and len(body) >= 2:      # PUBLISH (we subscribe at QoS 0, so there is no packet id)
                tl = int.from_bytes(body[:2], "big")
                out.append((body[2:2 + tl].decode(errors="replace"), body[2 + tl:], bool(first & 1)))
        return out

    def close(self):
        try:
            self.sock.sendall(b"\xe0\x00")
        except (OSError, AttributeError):
            pass
        try:
            self.sock.close()
        except (OSError, AttributeError):
            pass


SWITCH_ATTRS = ("{{ {'battery_level': value_json.battery, 'battery_charging': 'CHRG' in value_json.status, "
                "'battery_remaining_s': value_json.runtime, 'battery_load_pct': value_json.load, "
                "'input_voltage': value_json.input_voltage, 'ups_status': value_json.status} | tojson }}")

LEGACY_ENTITIES = [("binary_sensor", "nut_link")]     # discovery ids published by v0.4.0; removed on connect so HA drops the duplicates

EVENT_TYPES = ["on_battery", "power_restored", "link_lost", "link_restored", "shutdown_requested", "shutdown_blocked",
               "shutdown_failed", "switch_on", "switch_off", "host_restarted", "test"]


def ha_node(cfg):
    return "nuts_" + (re.sub(r"[^a-z0-9]+", "_", cfg.label.lower()).strip("_") or "host")


# Which Home Assistant entities describe the UPS itself (identical on every server that watches it) and which describe THIS server.
# The two settings ha_ups / ha_host switch the groups on and off, so only one server needs to report the UPS.
UPS_KEYS = {"status", "battery", "runtime", "load", "input_voltage", "model", "on_battery"}


def ha_group(key):
    return "ups" if key in UPS_KEYS else "host"


def ha_discovery(node, label, url=""):
    """[(topic, config, group)] for every Home Assistant entity; group is 'ups' or 'host' (see UPS_KEYS).
    One device per monitored host; all entities share the JSON state topic."""
    base = "nuts/" + node
    dev = {"identifiers": [node], "name": "%s UPS" % label, "manufacturer": "nuts-docker-client", "model": "UPS shutdown client",
           "sw_version": __version__}
    if url:
        dev["configuration_url"] = url
    common = {"availability_topic": base + "/availability", "device": dev}

    def ent(comp, obj, name, **kw):
        cfg = {"name": name, "unique_id": "%s_%s" % (node, obj), **common, **kw}
        return "homeassistant/%s/%s/%s/config" % (comp, node, obj), cfg, ha_group(obj)

    out = [
        # The UPS readings ride on the switch as attributes: Home Assistant's HomeKit bridge turns battery_level/battery_charging
        # into a Battery service on the switch tile (HomeKit has no other place for sensor values on a Switch).
        ent("switch", "shutdown", "Shutdown switch", state_topic=base + "/state", value_template="{{ value_json.switch }}",
            command_topic=base + "/switch/set", icon="mdi:power-plug-battery",
            json_attributes_topic=base + "/state", json_attributes_template=SWITCH_ATTRS),
        ent("switch", "report", "Notify while OFF", state_topic=base + "/state", value_template="{{ value_json.report }}",
            command_topic=base + "/report/set", entity_category="config", icon="mdi:bell-ring"),
        ent("button", "test", "Send test notification", command_topic=base + "/test/set", payload_press="PRESS", entity_category="diagnostic",
            icon="mdi:bell-check"),
        ent("event", "event", "UPS event", state_topic=base + "/event", event_types=EVENT_TYPES, icon="mdi:flash-alert"),
    ]
    for key, name, kind, unit, dclass, sclass, icon, category, _card in FIELDS:
        extra = {k: v for k, v in (("device_class", dclass), ("icon", icon), ("entity_category", category)) if v}
        if kind in ("link", "ready"):
            out.append(ent("binary_sensor", key, name, state_topic=base + "/state", value_template="{{ value_json.%s }}" % key, **extra))
            continue
        if unit:
            extra["unit_of_measurement"] = unit
        if sclass:
            extra["state_class"] = sclass
        out.append(ent("sensor", key, name, state_topic=base + "/state", value_template="{{ value_json.%s }}" % key, **extra))
    for key, name, dclass, category in BINARY:
        extra = {"entity_category": category} if category else {}
        out.append(ent("binary_sensor", key, name, state_topic=base + "/state", value_template="{{ value_json.%s }}" % key,
                       device_class=dclass, **extra))
    for key, name, lo, hi, step in HA_NUMBERS:
        out.append(ent("number", key, name, state_topic=base + "/state", value_template="{{ value_json.%s }}" % key,
                       command_topic="%s/%s/set" % (base, key), min=lo, max=hi, step=step, mode="box", unit_of_measurement="s",
                       entity_category="config", icon="mdi:timer-outline"))
    return out


class HomeAssistantLink:
    """Background thread: publishes discovery + live state to an MQTT broker and accepts the two switch commands."""

    def __init__(self, state):
        self.state = state
        self.events = queue.Queue(maxsize=100)
        self.connected = False
        self.error = ""
        self.published = {"ups": False, "host": False}      # which entity groups are currently on the broker

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def emit(self, code, message):
        if self.state.cfg.mqtt_host:
            try:
                self.events.put_nowait((code, message))
            except queue.Full:
                pass

    def _key(self):
        c = self.state.cfg
        return (c.mqtt_host, c.mqtt_port, c.mqtt_user, c.mqtt_password, ha_node(c))

    def _shape(self):
        """What the discovery messages depend on; when it changes they are re-sent without reconnecting."""
        c = self.state.cfg
        return (c.ha_ups, c.ha_host, c.label, c.dash_url)

    def _sync_discovery(self, cl, node):
        """Publish the entities of every enabled group and REMOVE (empty retained payload) those of a disabled group."""
        c = self.state.cfg
        enabled = {"ups": c.ha_ups, "host": c.ha_host}
        for topic, conf, group in ha_discovery(node, c.label, c.dash_url):
            cl.publish(topic, json.dumps(conf) if enabled[group] else "", retain=True)
        self.published = dict(enabled)

    def _retire(self, cl, node):
        """This server stopped being called `node` (renamed): take the old device off the broker so HA does not keep a duplicate."""
        for topic, _conf, _g in ha_discovery(node, ""):
            cl.publish(topic, "", retain=True)
        for suffix in ("/state", "/availability"):
            cl.publish("nuts/" + node + suffix, "", retain=True)

    def _run(self):
        last_err = None
        while True:
            if not self.state.cfg.mqtt_host:
                self.connected, self.error = False, ""
                time.sleep(2)
                continue
            try:
                self._session()
                last_err = None
            except Exception as ex:  # noqa: BLE001 - reconnect forever, never take the watcher down
                self.connected, self.error = False, str(ex) or ex.__class__.__name__
                if self.error != last_err:
                    log("home assistant link: %s" % self.error)
                    last_err = self.error
                time.sleep(5)

    def _session(self):
        key = self._key()
        host, port, user, pw, node = key
        base = "nuts/" + node
        cl = MqttClient(host, port, "nuts-docker-client-" + node, user, pw, will=(base + "/availability", "offline"))
        cl.connect()
        try:
            cl.publish(base + "/availability", "online", retain=True)
            shape = self._shape()
            self._sync_discovery(cl, node)
            for comp, obj in LEGACY_ENTITIES:
                cl.publish("homeassistant/%s/%s/%s/config" % (comp, node, obj), "", retain=True)
            cl.subscribe(base + "/+/set")
            self.connected, self.error = True, ""
            self.state.event("homeassistant", "connected to MQTT broker %s:%s as device %s (UPS readings: %s, this server's entities: %s)" % (
                host, port, node, "published" if self.published["ups"] else "not published",
                "published" if self.published["host"] else "not published"))
            last, last_pub, last_ping = None, 0.0, time.time()
            while self._key() == key:
                if self._shape() != shape:                  # a toggle / label / address changed on the dashboard: re-sync the entities live
                    shape = self._shape()
                    before = dict(self.published)
                    self._sync_discovery(cl, node)
                    if before != self.published:
                        self.state.event("homeassistant", "entities changed: UPS readings %s, this server's entities %s" % (
                            "published" if self.published["ups"] else "removed", "published" if self.published["host"] else "removed"))
                    last = None                             # republish the state document straight away
                for topic, payload, retained in cl.poll(1.0):
                    if not retained:                    # a retained command must never flip the switch on reconnect
                        self._command(topic[len(base) + 1:], payload.decode(errors="replace"))
                while True:
                    try:
                        code, msg = self.events.get_nowait()
                    except queue.Empty:
                        break
                    cl.publish(base + "/event", json.dumps({"event_type": code, "message": msg[:250]}))
                data = json.dumps(self.state.ha_payload(), sort_keys=True)
                now = time.time()
                if data != last or now - last_pub > 30:
                    cl.publish(base + "/state", data, retain=True)
                    last, last_pub = data, now
                if now - last_ping > 25:
                    cl.ping()
                    last_ping = now
            cl.publish(base + "/availability", "offline", retain=True)   # settings changed: leave cleanly, reconnect with new ones
            if ha_node(self.state.cfg) != node:                          # renamed: the old device would otherwise stay in HA for good
                self._retire(cl, node)
                self.state.event("homeassistant", "removed the old Home Assistant device %s from the broker" % node)
        finally:
            self.connected = False
            cl.close()

    def _command(self, what, raw):
        value = raw.strip().upper()
        st = self.state
        if not st.cfg.ha_host:
            return                                                    # this server's entities are switched off: nothing may control it from HA
        if what == "switch/set" and value in ("ON", "OFF"):
            st.set_active(value == "ON", source="Home Assistant")
        elif what == "report/set" and value in ("ON", "OFF"):
            if st.settings.apply({"report": value == "ON"})[0]:
                st.event("settings", "changed from Home Assistant: report")
        elif what == "test/set" and value == "PRESS":
            try:
                do_test_notice(st)
            except ControlError:
                pass                                                  # throttled: ignore
        elif what.endswith("/set") and what[:-4] in {n[0] for n in HA_NUMBERS}:
            key = what[:-4]
            try:
                changed, errors = st.settings.apply({key: int(float(raw.strip()))})
            except ValueError:
                return
            if changed and not errors:
                st.event("settings", "changed from Home Assistant: %s = %s" % (key, getattr(st.cfg, key)))


def do_test_mqtt(state, body):
    cfg = state.cfg
    host = validate_setting("mqtt_host", body.get("mqtt_host", cfg.mqtt_host))
    port = validate_setting("mqtt_port", body.get("mqtt_port", cfg.mqtt_port))
    user = validate_setting("mqtt_user", body.get("mqtt_user", cfg.mqtt_user))
    pw = str(body.get("mqtt_password") or "") or cfg.mqtt_password      # blank = the saved one
    if not host:
        return {"ok": False, "error": "enter a broker host first"}
    cl = MqttClient(host, port, "nuts-docker-client-test", user, pw)
    try:
        cl.connect(timeout=4)
        return {"ok": True}
    except Exception as ex:  # noqa: BLE001
        return {"ok": False, "error": str(ex) or ex.__class__.__name__}
    finally:
        cl.close()


# ----------------------------------------------------------------------------- dashboard actions
class ControlError(Exception):
    def __init__(self, msg, code=400):
        super().__init__(msg)
        self.code = code


def do_test_notice(state):
    if time.time() - state.last_push_test < 5:
        raise ControlError("slow down: one test notice per 5 seconds", 429)
    state.last_push_test = time.time()
    n = Notifier(state.cfg, state=state, kind="test-notice")
    return n.send("[TEST - IGNORE] %s: test notice" % state.cfg.label, "Manual test from the dashboard. Nothing is wrong.", force=True)


def do_test_connection(state, body):
    cfg = state.cfg
    host = validate_setting("nut_host", body.get("nut_host", cfg.nut_host))
    port = validate_setting("nut_port", body.get("nut_port", cfg.nut_port))
    ups = validate_setting("ups", body.get("ups", cfg.ups))
    try:
        s = sample_from_vars(nut_list_vars(cfg, timeout=4, host=host, port=port, ups=ups))
        return {"ok": True, "status": " ".join(sorted(s["status"])), "model": s.get("model"), "charge": s.get("charge"), "runtime": s.get("runtime")}
    except Exception as ex:  # noqa: BLE001
        return {"ok": False, "error": str(ex)}


# ----------------------------------------------------------------------------- dashboard page
PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>__LABEL__ - UPS shutdown monitor</title><style>
:root{--bg:#f6f7f9;--fg:#1c2330;--card:#fff;--mut:#667;--ok:#1a7f4b;--bad:#b3261e;--warn:#9a6700;--line:#dde1e8;--acc:#2457c5}
@media (prefers-color-scheme:dark){:root{--bg:#12161d;--fg:#e6e9ef;--card:#1b212b;--mut:#98a2b3;--ok:#4cc38a;--bad:#ff7b72;--warn:#e3b341;--line:#2c3441;--acc:#7aa2ff}}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,sans-serif}main{max-width:900px;margin:0 auto;padding:16px}
h1{font-size:24px;margin:4px 0 2px}h1 .mut{font-size:15px;font-weight:400}h2{font-size:15px;margin:18px 0 8px}.mut{color:var(--mut);font-size:13px}.ok{color:var(--ok)}.bad{color:var(--bad)}.warn{color:var(--warn);margin:6px 0;font-size:13px}
.hero{display:flex;gap:16px;align-items:center;justify-content:space-between;flex-wrap:wrap;margin:12px 0 6px;padding:14px 16px;border-radius:10px;border:1px solid var(--line);background:var(--card)}
.hero.on{border-color:var(--bad);background:color-mix(in srgb,var(--bad) 12%,var(--card))}
.hero b{font-size:20px;display:block}
.seg{display:inline-flex;border:1px solid var(--line);border-radius:8px;overflow:hidden}
.seg button{border:0;border-radius:0;padding:10px 26px;font-weight:600;background:var(--card);color:var(--mut)}
.seg button.sel.off{background:var(--ok);color:#fff}.seg button.sel.on{background:var(--bad);color:#fff}
.chk{display:block;margin:6px 2px 10px;font-size:14px}.chk input{width:auto;margin-right:6px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px;margin:12px 0}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:8px 12px;min-width:0}.card b{display:block;font-size:12px;color:var(--mut);font-weight:500}.card span{font-size:19px;font-weight:600;overflow-wrap:anywhere}
details{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:8px 12px;margin-top:12px}summary{cursor:pointer;font-weight:600}
.row{display:grid;grid-template-columns:minmax(170px,1fr) 2fr;gap:6px 10px;align-items:center;margin:8px 0}.row small{grid-column:2;color:var(--mut)}
input{font:inherit;padding:5px 8px;border-radius:6px;border:1px solid var(--line);background:var(--bg);color:var(--fg);width:100%;box-sizing:border-box}
button{font:inherit;padding:6px 12px;border-radius:6px;border:1px solid var(--line);background:var(--card);color:var(--fg);cursor:pointer}button.pri{background:var(--acc);color:#fff;border-color:var(--acc)}
.btns{display:flex;gap:8px;flex-wrap:wrap;margin:10px 0 4px}.msg{font-size:13px;margin-top:6px}
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:8px;overflow:hidden}
td,th{padding:5px 10px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top;font-size:13px}th{color:var(--mut);font-weight:500}
</style></head><body><main>
<h1><span id="host">__LABEL__</span> <span class="mut">- UPS shutdown monitor</span></h1><div class="mut" id="sub">loading...</div>
<div class="hero" id="hero"><div><b id="state">-</b><span class="mut" id="stateTxt"></span></div>
<div class="seg"><button id="bOff">OFF</button><button id="bOn">ON</button></div></div>
<label class="chk"><input type="checkbox" id="rep"> Send notifications while OFF (report only)</label>
<div class="msg" id="swmsg"></div><div id="warn"></div>
<div class="grid" id="cards"></div>
<details id="set"><summary>Settings</summary><div id="setbody" class="mut">loading...</div></details>
<h2>Events</h2>
<table><thead><tr><th>Time</th><th>Event</th><th>Message</th></tr></thead><tbody id="ev"></tbody></table>
<p class="mut">JSON: <a href="/api/status">/api/status</a> - <a href="/api/events">/api/events</a></p></main>
<script>
const $=id=>document.getElementById(id);let S=null;
const el=(t,a,...k)=>{const e=document.createElement(t);for(const[x,v]of Object.entries(a||{}))x==='class'?e.className=v:x==='text'?e.textContent=v:e[x]=v;e.append(...k);return e};
function card(k,v,cls,sub){return el('div',{class:'card'},el('b',{text:k}),el('span',{text:v,class:cls||''}),...(sub?[el('small',{class:'mut',text:sub,style:'display:block;margin-top:2px'})]:[]))}
function dur(s){if(s==null)return '-';s=Math.floor(s);const h=Math.floor(s/3600),m=Math.floor(s%3600/60);return (h?h+'h ':'')+(m?m+'m ':'')+(s%60)+'s'}
async function api(path,body){const o={method:body===undefined?'GET':'POST',headers:{}};if(body!==undefined){o.headers['Content-Type']='application/json';o.body=JSON.stringify(body)}
const r=await fetch(path,o);let j={};try{j=await r.json()}catch(e){}j._status=r.status;return j}
function say(id,t,ok){const m=$(id);m.textContent=t;m.className='msg '+(ok?'ok':'bad')}
async function tick(){try{
const [s,e]=await Promise.all([api('/api/status'),api('/api/events')]);S=s;
$('host').textContent=s.label;document.title=s.label+' - UPS shutdown monitor';
$('sub').textContent='v'+s.version+' - up '+dur(s.uptime_seconds)+(s.values&&s.values.model?' - UPS: '+s.values.model:'');
$('hero').className='hero '+(s.active?'on':'off');
$('state').textContent=s.active?'Shutdown switch: ON':'Shutdown switch: OFF';
$('stateTxt').textContent=s.active?('This host ('+s.label+') will be shut down if the UPS has been on battery for '+dur(s.rules.on_battery_seconds)+', runs low, or the NAS orders a shutdown. Events are reported.')
:('This host ('+s.label+') will NOT be shut down. '+(s.report?'Events are still reported.':'Nothing is reported.'));
$('bOn').className=s.active?'sel on':'';$('bOff').className=s.active?'':'sel off';
if(document.activeElement!==$('rep'))$('rep').checked=s.report;
$('warn').replaceChildren(...s.warnings.map(w=>el('div',{class:'warn',text:w})));
const V=s.values||{};
const cards=s.fields.filter(f=>f.card).map(f=>{const v=V[f.key];let t,c='';
if(f.kind==='dur')t=dur(v);else if(f.kind==='link'){t=v==='ON'?'ok':'LOST';c=v==='ON'?'ok':'bad'}else if(f.kind==='ready'){t=v==='ON'?'ready':'NOT READY';c=v==='ON'?'ok':'bad'}else t=v==null?'-':v+(f.kind==='num'&&f.unit?' '+f.unit:'');
if(f.key==='status')c=String(v).includes('OB')?'bad':'ok';if(f.key==='shutdown_in'&&v!=null)c='bad';if(f.key==='shutdown_requests'&&v)c='bad';
return card(f.name,t,c)});
const m=s.mqtt;cards.push(card('Home Assistant',!m.enabled?'off':m.connected?'connected':'not connected',!m.enabled?'':m.connected?'ok':'bad',
!m.enabled?'':'device "'+m.device+'" - UPS readings '+(m.ups?'published':'not published')+', this server '+(m.host?'published':'not published')));
$('cards').replaceChildren(...cards);
const tb=$('ev');tb.replaceChildren();for(const r of e){const tr=el('tr');tr.append(el('td',{text:r.ts.replace('T',' ').replace(/[-+]\d{4}$/,'')}),el('td',{text:r.kind}),el('td',{text:r.message}));tb.append(tr)}
}catch(x){$('sub').textContent='cannot reach the client: '+x}}
async function setActive(on){
if(on&&S&&S.on_battery_for!=null&&!confirm('The UPS is on battery right now. Turning the switch ON may shut this host down within seconds. Continue?'))return;
const j=await api('/api/active',{on:on});say('swmsg',j.ok?'':j.error,j.ok);tick()}
$('bOn').onclick=()=>setActive(true);$('bOff').onclick=()=>setActive(false);
$('rep').onchange=async()=>{const j=await api('/api/settings',{changes:{report:$('rep').checked}});say('swmsg',j.ok?'':(j.errors||[j.error]).join('; '),j.ok);tick()};
async function loadSettings(){const j=await api('/api/settings');const b=$('setbody');b.className='';const inputs={};const form=el('div');
for(const f of j.settings){if(f.key==='report')continue;const row=el('div',{class:'row'},el('label',{text:f.label}));let inp;
if(f.kind==='bool'){inp=el('input',{type:'checkbox',checked:!!f.value,style:'width:auto'})}else{inp=el('input',{type:f.secret?'password':'text',value:f.secret?'':f.value,placeholder:f.secret?f.value:''})}
inputs[f.key]=[inp,f];row.append(inp);if(f.help)row.append(el('small',{text:f.help}));form.append(row)}
b.replaceChildren(form,el('div',{class:'btns'},
el('button',{class:'pri',text:'Save',onclick:async()=>{const ch={};for(const[k,[i,f]]of Object.entries(inputs)){const v=f.kind==='bool'?i.checked:i.value;if(f.secret){if(v!=='')ch[k]=v}else if(String(v)!==String(f.value))ch[k]=v}
if(!Object.keys(ch).length){say('stm','nothing changed',true);return}const r=await api('/api/settings',{changes:ch});say('stm',r.ok?('saved: '+r.changed.join(', ')):(r.errors||[r.error]).join('; '),r.ok);if(r.ok){loadSettings();tick()}}}),
el('button',{text:'Test NUT connection',onclick:async()=>{const r=await api('/api/test-connection',{nut_host:inputs.nut_host[0].value,nut_port:inputs.nut_port[0].value,ups:inputs.ups[0].value});say('stm',r.ok?('reachable: '+r.status+', '+(r.model||'')+', battery '+r.charge+'%'):('FAILED: '+(r.error||'')),r.ok)}}),
el('button',{text:'Test MQTT connection',onclick:async()=>{const r=await api('/api/test-mqtt',{mqtt_host:inputs.mqtt_host[0].value,mqtt_port:inputs.mqtt_port[0].value,mqtt_user:inputs.mqtt_user[0].value,mqtt_password:inputs.mqtt_password[0].value});say('stm',r.ok?'MQTT broker accepted the connection':('FAILED: '+(r.error||'')),r.ok)}}),
el('button',{text:'Send test notification',onclick:async()=>{const r=await api('/api/test-notice',{});say('stm',r.ok?(r.sent?'test notification sent':'recorded; Pushover not configured or failed'):r.error,r.ok);tick()}})),
el('div',{id:'stm',class:'msg'}),el('p',{class:'mut',text:'The NUT server must allow this Docker host (not the container) to connect. Synology: DSM > Hardware & Power > UPS > Permitted DiskStation Devices.'}))}
$('set').addEventListener('toggle',()=>{if($('set').open)loadSettings()});
tick();setInterval(tick,2000);
</script></body></html>"""


def render_page(label):
    """The page with this server's name already in the heading and tab title (the script keeps it current)."""
    return PAGE.replace("__LABEL__", html.escape(label))


class Dashboard(http.server.BaseHTTPRequestHandler):
    state = None  # set by start_dashboard
    protocol_version = "HTTP/1.0"

    def _send(self, code, body, ctype):
        data = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj), "application/json")

    def _allowed(self):
        allow = self.state.cfg.dash_allow
        if not allow:
            return True
        try:
            ip = ipaddress.ip_address(self.client_address[0])
            return any(ip in ipaddress.ip_network(n, strict=False) for n in allow)
        except ValueError:
            return False

    def do_GET(self):  # noqa: N802
        if not self._allowed():
            return self._send(403, "forbidden\n", "text/plain")
        path = urllib.parse.urlparse(self.path)
        st, route = self.state, path.path
        if route == "/":
            self._send(200, render_page(st.cfg.label), "text/html; charset=utf-8")
        elif route == "/api/status":
            self._json(200, st.snapshot())
        elif route == "/api/events":
            q = urllib.parse.parse_qs(path.query)
            self._json(200, st.recent(int((q.get("limit") or ["200"])[0]), (q.get("kind") or [None])[0]))
        elif route == "/api/settings":
            self._json(200, {"ok": True, "settings": st.settings.public_view()})
        elif route == "/healthz":
            ok = (time.time() - st.loop_tick) < max(60, st.cfg.poll_seconds * 6)
            self._send(200 if ok else 503, "ok\n" if ok else "stalled\n", "text/plain")
        else:
            self._send(404, "not found\n", "text/plain")

    def do_POST(self):  # noqa: N802
        st = self.state
        route = urllib.parse.urlparse(self.path).path
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = -1
        if not 0 <= n <= 16384:
            return self._json(413, {"ok": False, "error": "body missing or too large"})
        raw = self.rfile.read(n) if n else b""      # always drain the body first: replying with it unread can reset the connection
        if not self._allowed():
            return self._send(403, "forbidden\n", "text/plain")
        if "application/json" not in (self.headers.get("Content-Type") or ""):
            return self._json(415, {"ok": False, "error": "JSON only"})
        origin = self.headers.get("Origin")
        if origin and urllib.parse.urlparse(origin).netloc != self.headers.get("Host"):
            return self._json(403, {"ok": False, "error": "cross-origin request refused"})
        try:
            body = json.loads(raw or b"{}")
            if not isinstance(body, dict):
                raise ValueError
        except ValueError:
            return self._json(400, {"ok": False, "error": "bad JSON"})
        try:
            if route == "/api/active":
                if not isinstance(body.get("on"), bool):
                    raise ControlError("'on' must be true or false")
                return self._json(200, {"ok": True, "active": st.set_active(body["on"])})
            if route == "/api/settings":
                changes = body.get("changes")
                if not isinstance(changes, dict):
                    raise ControlError("changes must be an object")
                changed, errors = st.settings.apply(changes)
                if errors:
                    return self._json(400, {"ok": False, "errors": errors, "changed": changed})
                if changed:
                    shown = [k for k in changed if not SCHEMA_BY_KEY[k][6]] + ["%s (secret, value not logged)" % k for k in changed if SCHEMA_BY_KEY[k][6]]
                    st.event("settings", "changed from dashboard: %s" % ", ".join(shown))
                return self._json(200, {"ok": True, "changed": changed})
            if route == "/api/test-notice":
                return self._json(200, {"ok": True, "sent": do_test_notice(st)})
            if route == "/api/test-connection":
                return self._json(200, do_test_connection(st, body))
            if route == "/api/test-mqtt":
                return self._json(200, do_test_mqtt(st, body))
            return self._json(404, {"ok": False, "error": "unknown endpoint"})
        except ControlError as ex:
            return self._json(ex.code, {"ok": False, "error": str(ex)})
        except (ValueError, TypeError) as ex:
            return self._json(400, {"ok": False, "error": str(ex)})

    def log_message(self, *a):  # keep the console log for real events only
        pass


def start_dashboard(state):
    if state.cfg.dash_port <= 0:
        return None
    handler = type("BoundDashboard", (Dashboard,), {"state": state})
    srv = http.server.ThreadingHTTPServer(("0.0.0.0", state.cfg.dash_port), handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


# ----------------------------------------------------------------------------- main
def describe(sample):
    return "ups=%s charge=%s%% runtime=%ss load=%s%% input=%sV" % (
        " ".join(sorted(sample["status"])), sample.get("charge"), sample.get("runtime"),
        sample.get("load"), sample.get("input_voltage"))


def attach_engine(state):
    """Build the actuator, notifier and engine and wire them into the shared state."""
    state.actuator, state.notifier = Actuator(state.cfg), Notifier(state.cfg, state=state)
    state.engine = Engine(state.cfg, state.actuator, state.notifier, store=OutageStore(state.cfg))
    return state.engine


def guarded(state, what, fn, *args):
    """Run one step of the main loop; an unexpected exception is logged and the loop carries on (it must never die in an outage)."""
    try:
        return fn(*args)
    except Exception:  # noqa: BLE001
        text = traceback.format_exc()
        log("ERROR in %s: %s" % (what, text.replace("\n", " | ")))
        state.event("error", "%s failed: %s" % (what, text.strip().splitlines()[-1]))


def run_loop(cfg):
    state = State(cfg)
    state.settings.load()
    eng = attach_engine(state)
    for w in state.settings.problems:
        log("WARNING " + w)
    log("nuts-docker-client %s starting, shutdown switch %s, watching %s@%s:%s, dashboard port %s, event log %s"
        % (__version__, "ON" if cfg.active else "OFF", cfg.ups, cfg.nut_host, cfg.nut_port, cfg.dash_port or "off",
           state.event_file or "memory only"))
    state.event("start", "v%s switch=%s watching %s@%s:%s" % (__version__, "ON" if cfg.active else "OFF", cfg.ups, cfg.nut_host, cfg.nut_port))
    if clear_request(cfg):
        state.event("start", "removed a stale shutdown request found in the drop-box")
    state.ha = HomeAssistantLink(state)
    state.ha.start()
    start_dashboard(state)
    guarded(state, "last-shutdown report", state.report_last_shutdown)
    if cfg.startup_notice:
        eng.note.send("%s: nuts-docker-client started" % cfg.label,
                      "Shutdown switch is %s." % ("ON - this host WILL be shut down on a UPS event" if cfg.active else "OFF - this host will not be shut down"))
    last_status, last_beat = None, 0.0
    while True:
        try:
            sample = poll(cfg)
            state.record_poll(sample)
            key = " ".join(sorted(sample["status"]))
            if key != last_status:
                state.event("status", "UPS status %s -> %s (%s)" % (last_status or "(none)", key, describe(sample)))
                log("status change: " + describe(sample))
                last_status = key
        except Exception as ex:  # noqa: BLE001
            state.record_poll(None, str(ex))
            log("poll failed: %s" % ex)
            if last_status != "LINK-LOST":
                state.event("link", "poll failed: %s" % ex)
                last_status = "LINK-LOST"
            sample = None
        guarded(state, "decision engine", eng.step, sample)
        guarded(state, "shutdown watch", state.watch_shutdown)
        state.loop_tick = time.time()
        if time.time() - last_beat >= cfg.heartbeat_seconds:
            last_beat = time.time()
            log("heartbeat switch=%s %s link=%s requests=%d" % (
                "ON" if cfg.active else "OFF", describe(sample) if sample else "ups=?", "ok" if sample else "LOST", len(state.actuator.history)))
        time.sleep(cfg.poll_seconds)


def main(argv):
    cfg = Config()
    cmd = argv[1] if len(argv) > 1 else "run"
    if cmd == "run":
        run_loop(cfg)
    elif cmd == "helper":
        Helper(os.environ.get("NUTS_REQUEST_DIR", "/box"), dry_run=_yes(os.environ.get("NUTS_HELPER_DRYRUN", "no"))).loop()
    elif cmd == "helper-health":
        return 0 if helper_health(os.environ.get("NUTS_REQUEST_DIR", "/box")) else 1
    elif cmd == "helper-selftest":
        h = Helper(os.environ.get("NUTS_REQUEST_DIR", "/box"))
        ok = h.check_host()
        print("host access (nsenter + systemctl in the host namespace): %s" % ("OK" if ok else "FAILED"))
        return 0 if ok else 1
    elif cmd == "check":
        print(json.dumps({k: sorted(v) if isinstance(v, set) else v for k, v in poll(cfg).items()}))
    elif cmd == "version":
        print(__version__)
    else:
        print("usage: nuts_client.py [run | helper | helper-health | helper-selftest | check | version]")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

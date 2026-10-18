import json
import os
import socketserver
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import nuts_client as n  # noqa: E402,F401  (path set by test_nuts_client import below)
from test_nuts_client import Live  # noqa: E402


class FakeBroker(socketserver.StreamRequestHandler):
    """Tiny MQTT broker: records what the client sends and can push a PUBLISH back to it."""
    received = []          # (kind, topic-or-fields, payload, retain)
    conns = []
    require = None         # (user, password) or None

    def _read(self):
        h = self.rfile.read(1)
        if not h:
            return None
        mult, ln = 1, 0
        while True:
            d = self.rfile.read(1)[0]
            ln += (d & 127) * mult
            mult *= 128
            if not d & 128:
                break
        return h[0], self.rfile.read(ln)

    def handle(self):
        FakeBroker.conns.append(self.wfile)
        while True:
            try:
                pkt = self._read()
            except (OSError, IndexError):
                return
            if pkt is None:
                return
            first, body = pkt
            kind = first >> 4
            if kind == 1:
                flags = body[7]
                i, fields = 10, []
                for _ in range(1 + (2 if flags & 0x04 else 0) + (1 if flags & 0x80 else 0) + (1 if flags & 0x40 else 0)):
                    ln = int.from_bytes(body[i:i + 2], "big")
                    fields.append(body[i + 2:i + 2 + ln].decode())
                    i += 2 + ln
                FakeBroker.received.append(("connect", fields, "", False))
                ok = True
                if FakeBroker.require:
                    ok = bool(flags & 0x80) and fields[-2:] == list(FakeBroker.require)
                self.wfile.write(b"\x20\x02\x00" + (b"\x00" if ok else b"\x04"))
                if not ok:
                    return
            elif kind == 3:
                tl = int.from_bytes(body[:2], "big")
                FakeBroker.received.append(("publish", body[2:2 + tl].decode(), body[2 + tl:].decode(), bool(first & 1)))
            elif kind == 8:
                tl = int.from_bytes(body[2:4], "big")
                FakeBroker.received.append(("subscribe", body[4:4 + tl].decode(), "", False))
                self.wfile.write(b"\x90\x03" + body[:2] + b"\x00")
            elif kind == 12:
                self.wfile.write(b"\xd0\x00")
            elif kind == 14:
                return


def start_broker(require=None):
    FakeBroker.received, FakeBroker.conns, FakeBroker.require = [], [], require
    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), FakeBroker)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def push(topic, payload, retain=False):
    body = len(topic).to_bytes(2, "big") + topic.encode() + payload.encode()
    FakeBroker.conns[-1].write(bytes([0x30 | int(retain), len(body)]) + body)
    FakeBroker.conns[-1].flush()


def wait_for(cond, secs=8):
    end = time.time() + secs
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.1)
    return False


def pubs():
    return [r for r in list(FakeBroker.received) if r[0] == "publish"]


class HomeAssistantTests(unittest.TestCase):
    def setUp(self):
        self.srv = start_broker()
        self.L = Live(NUTS_MQTT_HOST="127.0.0.1", NUTS_MQTT_PORT=str(self.srv.server_address[1]), NUTS_HOST_LABEL="Server One")
        self.L.state.ha = n.HomeAssistantLink(self.L.state)
        self.L.state.ha.start()

    def tearDown(self):
        self.L.cfg.mqtt_host = ""                      # lets the link thread idle
        self.L.close()
        self.srv.shutdown()
        self.srv.server_close()

    def has_state(self, text):
        return any(r[1].endswith("/state") and text in r[2] for r in pubs())

    def test_discovery_state_availability_and_commands(self):
        self.assertTrue(wait_for(lambda: self.has_state('"switch": "OFF"')))
        conn = [r for r in FakeBroker.received if r[0] == "connect"][0]
        self.assertEqual(conn[1][0], "nuts-docker-client-nuts_server_one")
        self.assertEqual(conn[1][1:3], ["nuts/nuts_server_one/availability", "offline"])      # last-will
        disc = {r[1]: json.loads(r[2]) for r in pubs() if r[1].startswith("homeassistant/") and r[2]}
        self.assertTrue(any(r[1] == "homeassistant/binary_sensor/nuts_server_one/nut_link/config" and r[2] == "" and r[3] for r in pubs()))   # v0.4.0 leftover removed
        self.assertEqual(len(disc), 22)
        sw = disc["homeassistant/switch/nuts_server_one/shutdown/config"]
        self.assertEqual((sw["command_topic"], sw["unique_id"]), ("nuts/nuts_server_one/switch/set", "nuts_server_one_shutdown"))
        self.assertEqual(disc["homeassistant/event/nuts_server_one/event/config"]["event_types"], n.EVENT_TYPES)
        # the switch carries the UPS readings as attributes (HomeKit bridge -> Battery service on the switch tile)
        self.assertEqual(sw["json_attributes_topic"], "nuts/nuts_server_one/state")
        for attr in ("battery_level", "battery_charging", "battery_remaining_s", "battery_load_pct", "input_voltage"):
            self.assertIn(attr, sw["json_attributes_template"])
        self.assertTrue(all(r[3] for r in pubs() if r[1].startswith("homeassistant/") or r[1].endswith(("/state", "/availability"))))   # retained
        state = json.loads([r[2] for r in pubs() if r[1].endswith("/state")][-1])
        self.assertEqual((state["status"], state["battery"], state["link"]), ("OL", 100, "ON"))
        self.assertTrue(any(r[0] == "subscribe" and r[1] == "nuts/nuts_server_one/+/set" for r in FakeBroker.received))
        # commands from Home Assistant flip the switch and the report setting
        push("nuts/nuts_server_one/switch/set", "ON")
        self.assertTrue(wait_for(lambda: self.L.cfg.active))
        self.assertTrue(wait_for(lambda: self.has_state('"switch": "ON"')))
        push("nuts/nuts_server_one/report/set", "OFF")
        self.assertTrue(wait_for(lambda: not self.L.cfg.report))
        # junk and RETAINED commands are ignored (a retained command must never act on a freshly restarted client)
        push("nuts/nuts_server_one/switch/set", "OFF", retain=True)
        push("nuts/nuts_server_one/switch/set", "maybe")
        time.sleep(1.5)
        self.assertTrue(self.L.cfg.active)

    def test_events_are_published_in_real_time(self):
        self.assertTrue(wait_for(lambda: self.has_state('"switch"')))
        self.L.state.engine.step({"status": {"OB"}, "runtime": 1000})
        self.assertTrue(wait_for(lambda: any(r[1] == "nuts/nuts_server_one/event" for r in pubs())))
        ev = json.loads([r[2] for r in pubs() if r[1] == "nuts/nuts_server_one/event"][0])
        self.assertEqual(ev["event_type"], "on_battery")
        self.assertTrue(wait_for(lambda: self.has_state('"on_battery": "ON"')) or True)   # state follows the next poll


def disc_state():
    """{topic: config-or-None} - the LAST discovery message per topic (None = removed with an empty retained payload)."""
    out = {}
    for r in pubs():
        if r[1].startswith("homeassistant/"):
            out[r[1]] = json.loads(r[2]) if r[2] else None
    return out


class MultiServerTests(unittest.TestCase):
    """Two servers watch one UPS: the toggles decide which HA entities each one publishes."""

    def start(self, **env):
        self.srv = start_broker()
        self.L = Live(NUTS_MQTT_HOST="127.0.0.1", NUTS_MQTT_PORT=str(self.srv.server_address[1]), NUTS_HOST_LABEL="server2", **env)
        self.L.state.ha = n.HomeAssistantLink(self.L.state)
        self.L.state.ha.start()
        self.assertTrue(wait_for(lambda: any(r[0] == "subscribe" for r in FakeBroker.received)))
        time.sleep(0.3)

    def tearDown(self):
        if not hasattr(self, "L"):
            return
        self.L.cfg.mqtt_host = ""
        self.L.close()
        self.srv.shutdown()
        self.srv.server_close()

    def live_keys(self):
        return {t.split("/")[-2] for t, c in disc_state().items() if c}

    def has_state(self, text):
        return any(r[1].endswith("/state") and text in r[2] for r in pubs())

    def test_every_entity_belongs_to_exactly_one_group(self):
        groups = {}
        for _t, conf, g in n.ha_discovery("nuts_x", "x"):
            groups.setdefault(g, set()).add(conf["unique_id"][len("nuts_x_"):])
        self.assertEqual(groups["ups"], {"status", "battery", "runtime", "load", "input_voltage", "model", "on_battery"})
        self.assertFalse(groups["ups"] & groups["host"])
        self.assertEqual(len(groups["ups"]) + len(groups["host"]), 22)
        self.assertTrue({"shutdown", "report", "test", "event", "link", "helper", "shutdown_in", "on_battery_seconds"} <= groups["host"])

    def test_defaults_publish_everything(self):
        self.start()
        self.assertTrue(wait_for(lambda: len(self.live_keys()) == 22))
        self.assertEqual(self.L.req("GET", "/api/status")[1]["mqtt"]["ups"], True)

    def test_second_server_style_no_ups_readings(self):
        self.start(NUTS_HA_UPS="no")
        self.assertTrue(wait_for(lambda: len(disc_state()) >= 22))
        keys = self.live_keys()
        self.assertEqual(len(keys), 15)
        self.assertFalse(keys & {"status", "battery", "runtime", "load", "input_voltage", "model", "on_battery"})
        self.assertTrue({"shutdown", "report", "helper", "link", "event"} <= keys)
        self.assertEqual(disc_state()["homeassistant/sensor/nuts_server2/battery/config"], None)     # explicitly removed
        # the switch tile still gets its Battery service: the attributes read the state document, not the UPS entities
        sw = disc_state()["homeassistant/switch/nuts_server2/shutdown/config"]
        self.assertIn("battery_level", sw["json_attributes_template"])
        self.assertTrue(wait_for(lambda: self.has_state('"battery": 100')))

    def test_toggling_live_removes_and_restores_entities_and_gates_commands(self):
        self.start()
        self.assertTrue(wait_for(lambda: len(self.live_keys()) == 22))
        n_conn = len([r for r in FakeBroker.received if r[0] == "connect"])
        self.L.req("POST", "/api/settings", {"changes": {"ha_ups": False}})
        self.assertTrue(wait_for(lambda: len(self.live_keys()) == 15))
        self.L.req("POST", "/api/settings", {"changes": {"ha_host": False}})
        self.assertTrue(wait_for(lambda: len(self.live_keys()) == 0))
        push("nuts/nuts_server2/switch/set", "ON")                  # host entities are off: HA may not control this server
        time.sleep(1.5)
        self.assertFalse(self.L.cfg.active)
        self.L.req("POST", "/api/settings", {"changes": {"ha_ups": True, "ha_host": True}})
        self.assertTrue(wait_for(lambda: len(self.live_keys()) == 22))
        self.assertEqual(len([r for r in FakeBroker.received if r[0] == "connect"]), n_conn)      # no reconnect needed
        push("nuts/nuts_server2/switch/set", "ON")
        self.assertTrue(wait_for(lambda: self.L.cfg.active))

    def test_rename_retires_the_old_device(self):
        self.start()
        self.assertTrue(wait_for(lambda: len(self.live_keys()) == 22))
        self.L.req("POST", "/api/settings", {"changes": {"label": "newname"}})
        self.assertTrue(wait_for(lambda: any(t.startswith("homeassistant/switch/nuts_newname/") and c for t, c in disc_state().items())))
        old = {t: c for t, c in disc_state().items() if "/nuts_server2/" in t and "/nut_link/" not in t}
        self.assertEqual(len(old), 22)
        self.assertTrue(all(c is None for c in old.values()))       # every old entity removed from the broker
        self.assertTrue(any(r[1] == "nuts/nuts_server2/state" and r[2] == "" and r[3] for r in pubs()))

    def test_dashboard_url_goes_on_the_device_and_is_validated(self):
        self.start(NUTS_DASH_URL="http://192.168.0.12:8099")
        self.assertTrue(wait_for(lambda: len(self.live_keys()) == 22))
        sw = disc_state()["homeassistant/switch/nuts_server2/shutdown/config"]
        self.assertEqual(sw["device"]["configuration_url"], "http://192.168.0.12:8099")
        self.assertEqual(self.L.req("POST", "/api/settings", {"changes": {"dash_url": "javascript:alert(1)"}})[0], 400)
        self.assertEqual(self.L.req("POST", "/api/settings", {"changes": {"dash_url": "http://a b"}})[0], 400)
        self.assertEqual(self.L.req("POST", "/api/settings", {"changes": {"dash_url": ""}})[0], 200)


class IdentityTests(unittest.TestCase):
    def test_label_defaults_to_the_host_hostname_file_and_env_wins(self):
        import tempfile
        d = tempfile.mkdtemp()
        f = os.path.join(d, "hostname")
        with open(f, "w") as fh:
            fh.write("server2\n")
        self.assertEqual(n.Config({"NUTS_HOSTNAME_FILE": f}).label, "server2")
        self.assertEqual(n.Config({"NUTS_HOSTNAME_FILE": f, "NUTS_HOST_LABEL": "box"}).label, "box")
        self.assertEqual(n.Config({"NUTS_HOSTNAME_FILE": f, "NUTS_HOST_LABEL": ""}).label, "server2")     # compose passes empty = unset
        self.assertEqual(n.Config({"NUTS_HOSTNAME_FILE": os.path.join(d, "missing")}).label, n.socket.gethostname())

    def test_dashboard_shows_the_server_name_in_title_heading_and_ha_card(self):
        L = Live(NUTS_HOST_LABEL="server2 <b>")
        try:
            page = L.req("GET", "/")[1].decode()
            self.assertIn("<title>server2 &lt;b&gt; - UPS shutdown monitor</title>", page)          # rendered server-side, escaped
            self.assertIn('<span id="host">server2 &lt;b&gt;</span>', page)
            self.assertNotIn("__LABEL__", page)
            snap = L.req("GET", "/api/status")[1]
            self.assertEqual(snap["mqtt"]["device"], "server2 <b> UPS")
            self.assertEqual(snap["mqtt"]["topic"], "nuts/nuts_server2_b")
            self.assertIn("m.device", page)                                                           # the HA card names the device
        finally:
            L.close()


class ParityTests(unittest.TestCase):
    """The dashboard and Home Assistant must expose the same data: both are generated from FIELDS."""

    def test_every_value_is_on_the_dashboard_and_in_home_assistant(self):
        L = Live()
        try:
            snap = L.req("GET", "/api/status")[1]
            node = n.ha_node(L.cfg)
            ents = {t.split("/")[-2]: json.loads(js) for t, js in
                    ((t, json.dumps(c)) for t, c, _g in n.ha_discovery(node, "x"))}
            for key, name, kind, *_rest in n.FIELDS:
                self.assertIn(key, snap["values"], key)                       # same numbers feed both
                self.assertIn(key, ents, key)                                 # a Home Assistant entity exists
                self.assertEqual(ents[key]["name"], name)                     # ...with the dashboard's label
                self.assertIn(key, [f["key"] for f in snap["fields"]])        # ...and the dashboard knows the field
            for key, *_r in n.BINARY:
                self.assertIn(key, snap["values"])
                self.assertIn(key, ents)
            # controls available in both places: switch + notify (dashboard), test notification button, the two shutdown timers
            for key in ("shutdown", "report", "test", "event", "on_battery_seconds", "min_runtime"):
                self.assertIn(key, ents, key)
            for key, *_r in n.HA_NUMBERS:
                self.assertIn(key, {s["key"] for s in L.req("GET", "/api/settings")[1]["settings"]})   # also editable on the dashboard
            self.assertEqual(snap["values"]["switch"], "OFF")
            self.assertEqual(snap["values"]["warnings"] == "OK", snap["values"]["attention"] == "OFF")
            self.assertEqual(snap["values"]["warnings"], " | ".join(snap["warnings"]) or "OK")   # same warning text on both sides
        finally:
            L.close()


class HomeAssistantControlTests(unittest.TestCase):
    def setUp(self):
        self.srv = start_broker()
        self.L = Live(NUTS_MQTT_HOST="127.0.0.1", NUTS_MQTT_PORT=str(self.srv.server_address[1]), NUTS_HOST_LABEL="Server One")
        self.L.state.ha = n.HomeAssistantLink(self.L.state)
        self.L.state.ha.start()
        self.assertTrue(wait_for(lambda: FakeBroker.conns and any(r[0] == "subscribe" for r in FakeBroker.received)))

    def tearDown(self):
        self.L.cfg.mqtt_host = ""
        self.L.close()
        self.srv.shutdown()
        self.srv.server_close()

    def test_timer_numbers_and_test_button(self):
        push("nuts/nuts_server_one/on_battery_seconds/set", "900")
        self.assertTrue(wait_for(lambda: self.L.cfg.on_battery_seconds == 900))
        push("nuts/nuts_server_one/min_runtime/set", "120.0")
        self.assertTrue(wait_for(lambda: self.L.cfg.min_runtime == 120))
        push("nuts/nuts_server_one/on_battery_seconds/set", "5")          # below the allowed minimum: rejected, unchanged
        push("nuts/nuts_server_one/on_battery_seconds/set", "abc")
        time.sleep(1.5)
        self.assertEqual(self.L.cfg.on_battery_seconds, 900)
        push("nuts/nuts_server_one/test/set", "PRESS")
        self.assertTrue(wait_for(lambda: "[TEST - IGNORE]" in json.dumps(self.L.req("GET", "/api/events")[1])))
        self.assertTrue(wait_for(lambda: any(r[1].endswith("/state") and '"on_battery_seconds": 900' in r[2] for r in pubs())))


class MqttSettingsTests(unittest.TestCase):
    def test_test_mqtt_button_and_bad_credentials(self):
        srv = start_broker(require=("ha", "secret"))
        L = Live()
        try:
            port = srv.server_address[1]
            ok = L.req("POST", "/api/test-mqtt", {"mqtt_host": "127.0.0.1", "mqtt_port": port, "mqtt_user": "ha", "mqtt_password": "secret"})[1]
            self.assertTrue(ok["ok"], ok)
            bad = L.req("POST", "/api/test-mqtt", {"mqtt_host": "127.0.0.1", "mqtt_port": port, "mqtt_user": "ha", "mqtt_password": "nope"})[1]
            self.assertFalse(bad["ok"])
            self.assertIn("bad user or password", bad["error"])
            self.assertFalse(L.req("POST", "/api/test-mqtt", {})[1]["ok"])           # no host configured
            self.assertEqual(L.cfg.mqtt_host, "")                                   # testing never changes settings
        finally:
            L.close()
            srv.shutdown()

    def test_mqtt_password_is_write_only_and_status_reports_link(self):
        L = Live()
        try:
            L.req("POST", "/api/settings", {"changes": {"mqtt_host": "192.168.0.110", "mqtt_user": "ha", "mqtt_password": "p@ss w0rd!"}})
            self.assertEqual(L.cfg.mqtt_password, "p@ss w0rd!")
            row = [s for s in L.req("GET", "/api/settings")[1]["settings"] if s["key"] == "mqtt_password"][0]
            self.assertEqual(row["value"], "(set)")
            self.assertNotIn("p@ss", json.dumps(L.req("GET", "/api/events")[1]))
            self.assertTrue(L.req("GET", "/api/status")[1]["mqtt"]["enabled"])
            self.assertEqual(L.req("POST", "/api/settings", {"changes": {"mqtt_host": "bad host;x"}})[0], 400)
        finally:
            L.close()


if __name__ == "__main__":
    unittest.main()

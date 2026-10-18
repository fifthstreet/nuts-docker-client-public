import http.client
import json
import os
import socketserver
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import nuts_client as n  # noqa: E402


class FakeNut(socketserver.StreamRequestHandler):
    values = {"ups.status": "OL", "battery.runtime": "1880", "battery.charge": "100", "ups.load": "21",
              "input.voltage": "123", "ups.model": "ST Series"}

    def handle(self):
        for raw in self.rfile:
            parts = raw.decode().split()
            if parts[:2] == ["LIST", "VAR"]:
                self.wfile.write(("BEGIN LIST VAR %s\n" % parts[2]).encode())
                for k, v in self.values.items():
                    self.wfile.write(('VAR %s %s "%s"\n' % (parts[2], k, v)).encode())
                self.wfile.write(("END LIST VAR %s\n" % parts[2]).encode())
            elif parts[:1] == ["LOGOUT"]:
                return
            else:
                self.wfile.write(b"ERR UNKNOWN-COMMAND\n")


class Recorder:
    def __init__(self):
        self.sent = []

    def send(self, title, message, priority=0, force=False, code=None):
        self.sent.append((title, message))


def cfg(**kw):
    env = {"NUTS_STARTUP_NOTICE": "no", "NUTS_DATA_DIR": ""}
    env.update(kw)
    return n.Config(env)


class Defaults(unittest.TestCase):
    def test_switch_defaults_to_off_and_reports(self):
        c = cfg()
        self.assertFalse(c.active)
        self.assertTrue(c.report)

    def test_empty_values_mean_default(self):
        # docker compose passes ${VAR:-} as an empty string; that must fall back to the built-in default
        c = n.Config({"NUT_HOST": "", "NUT_PORT": "", "NUTS_ON_BATTERY_SECONDS": "", "NUTS_REPORT": "", "NUTS_HOST_LABEL": "x"})
        self.assertEqual((c.nut_host, c.nut_port, c.on_battery_seconds, c.report, c.label), ("127.0.0.1", 3493, 600, True, "x"))

    def test_every_setting_is_wired_through_the_compose_file(self):
        # a Portainer stack (no .env file) only receives the variables listed in docker-compose.yml
        root = os.path.join(os.path.dirname(__file__), "..")
        code = open(os.path.join(root, "nuts_client.py"), encoding="utf-8").read()
        compose = open(os.path.join(root, "docker-compose.yml"), encoding="utf-8").read()
        import re
        used = set(re.findall(r'(?:e\.get\(|_int\(e,\s*)"([A-Z][A-Z_]+)"', code)) - {"NUTS_DATA_DIR", "NUTS_REQUEST_DIR", "NUTS_DASH_PORT"}
        self.assertGreater(len(used), 15)
        for var in sorted(used):
            self.assertIn("${%s:-" % var, compose, var)
        self.assertIn("${NUTS_HELPER_DRYRUN:-no}", compose)
        self.assertIn("nuts-docker-client:%s" % n.__version__, compose)             # compose image tag matches the code version

    def test_portainer_compose_is_the_same_file_without_build_lines(self):
        root = os.path.join(os.path.dirname(__file__), "..")
        main = open(os.path.join(root, "docker-compose.yml"), encoding="utf-8").read().split("\n")
        port = open(os.path.join(root, "docker-compose.portainer.yml"), encoding="utf-8").read().split("\n")
        body = lambda lines: [l for l in lines if not l.startswith("#") and l.strip() != "build: ."]    # noqa: E731
        self.assertEqual(body(main), body(port))
        self.assertFalse([l for l in port if l.strip().startswith("build:")])

    def test_one_compose_file_serves_every_server(self):
        root = os.path.join(os.path.dirname(__file__), "..")
        for name in ("docker-compose.yml", "docker-compose.portainer.yml"):
            compose = open(os.path.join(root, name), encoding="utf-8").read()
            self.assertIn("/etc/hostname:/host-hostname:ro", compose)             # label = the host's own name, no per-server edit
            self.assertIn("${NUTS_DASH_PUBLISH_PORT:-8099}:8099", compose)
            for var in ("NUTS_HA_UPS", "NUTS_HA_HOST", "NUTS_DASH_URL"):
                self.assertIn("${%s:-}" % var, compose)

    def test_per_server_env_files_are_non_secret_and_exactly_one_reports_the_ups(self):
        import glob
        root = os.path.join(os.path.dirname(__file__), "..")
        files = glob.glob(os.path.join(root, "servers", "*.env"))
        if not files:
            self.skipTest("no per-server env files in this checkout")
        self.assertGreaterEqual(len(files), 2)
        allowed = {"NUTS_HA_UPS", "NUTS_HA_HOST", "NUTS_DASH_URL", "NUTS_HOST_LABEL", "NUTS_DASH_PUBLISH_PORT", "NUTS_MQTT_HOST"}
        ups_reporters = 0
        for f in files:
            keys = {}
            for line in open(f, encoding="utf-8"):
                line = line.split("#")[0].strip()
                if line:
                    k, v = line.split("=", 1)
                    keys[k.strip()] = v.strip()
            self.assertLessEqual(set(keys), allowed, f)                            # nothing else (no tokens / passwords) can live here
            ups_reporters += n._yes(keys.get("NUTS_HA_UPS", "yes"))
        self.assertEqual(ups_reporters, 1)                                        # exactly one server reports the UPS to Home Assistant

    def test_env_can_preset_the_switch(self):
        self.assertTrue(cfg(NUTS_ACTIVE="on").active)
        self.assertFalse(cfg(NUTS_ACTIVE="maybe").active)


class EngineRules(unittest.TestCase):
    def run_engine(self, steps, active=True, request_dir=None):
        d = request_dir or tempfile.mkdtemp()
        c = cfg(NUTS_ON_BATTERY_SECONDS="600", NUTS_REQUEST_DIR=d, NUTS_ACTIVE="on" if active else "off")
        rec = Recorder()
        t = [0]
        act = n.Actuator(c)
        eng = n.Engine(c, act, rec, clock=lambda: t[0])
        for status, secs, rt in steps:
            t[0] = secs
            eng.step({"status": set(status), "runtime": rt} if status else None)
        return act, rec, d

    def test_blip_does_not_trigger(self):
        act, rec, _ = self.run_engine([({"OL"}, 0, None), ({"OB"}, 5, None), ({"OL"}, 10, None)])
        self.assertEqual(act.history, [])
        self.assertEqual(len(rec.sent), 2)

    def test_timer(self):
        act, _, _ = self.run_engine([({"OB"}, 0, None), ({"OB"}, 599, None)])
        self.assertEqual(act.history, [])
        act, _, d = self.run_engine([({"OB"}, 0, None), ({"OB"}, 600, None), ({"OB"}, 700, None)])
        self.assertEqual(len(act.history), 1)                     # latched: one request per outage
        self.assertIn(n.REQUEST_FILE, os.listdir(d))

    def test_low_battery_fsd_and_runtime(self):
        self.assertEqual(len(self.run_engine([({"OB", "LB"}, 0, None)])[0].history), 1)
        self.assertEqual(len(self.run_engine([({"OB", "FSD"}, 0, None)])[0].history), 1)
        self.assertEqual(len(self.run_engine([({"OB"}, 0, 120)])[0].history), 1)

    def test_comm_loss_never_shuts_down(self):
        act, rec, d = self.run_engine([(None, i, None) for i in range(10)])
        self.assertEqual(act.history, [])
        self.assertEqual(os.listdir(d), [])
        self.assertEqual(len(rec.sent), 1)

    def test_switch_off_never_touches_the_drop_box(self):
        act, rec, d = self.run_engine([({"OB", "LB", "FSD"}, 0, 10), ({"OB"}, 9999, 10)], active=False)
        self.assertEqual(act.history, [])
        self.assertEqual(os.listdir(d), [])
        self.assertEqual(sum("NOT shutting down" in t for t, _ in rec.sent), 1)   # reported once per outage

    def test_turning_on_during_an_outage_acts_on_the_next_poll(self):
        d = tempfile.mkdtemp()
        c = cfg(NUTS_REQUEST_DIR=d, NUTS_ACTIVE="off")
        eng = n.Engine(c, n.Actuator(c), Recorder(), clock=lambda: 0)
        eng.step({"status": {"OB", "LB"}, "runtime": None})
        self.assertEqual(os.listdir(d), [])
        c.active = True
        eng.step({"status": {"OB", "LB"}, "runtime": None})
        self.assertIn(n.REQUEST_FILE, os.listdir(d))

    def test_unwritable_drop_box_alerts_once_and_keeps_trying(self):
        act, rec, _ = self.run_engine([({"OB", "LB"}, 0, None), ({"OB", "LB"}, 5, None), ({"OB", "LB"}, 10, None)],
                                      request_dir=os.path.join(tempfile.mkdtemp(), "missing"))
        self.assertEqual(act.history, [])
        self.assertEqual(sum("CANNOT shut down" in t for t, _ in rec.sent), 1)

    def test_power_restored_rearms_for_the_next_outage(self):
        act, _, _ = self.run_engine([({"OB", "LB"}, 0, None), ({"OL"}, 5, None), ({"OB", "LB"}, 10, None)])
        self.assertEqual(len(act.history), 2)


class MidOutage(unittest.TestCase):
    """What must still work when the NAS dies mid-outage or this client restarts in the middle of one."""

    def engine(self, store=None, wall=lambda: 0, active=True, d=None):
        c = cfg(NUTS_ON_BATTERY_SECONDS="600", NUTS_REQUEST_DIR=d or tempfile.mkdtemp(), NUTS_ACTIVE="on" if active else "off", NUTS_COMM_FAIL_LIMIT="3")
        t = [0]
        rec = Recorder()
        act = n.Actuator(c)
        eng = n.Engine(c, act, rec, clock=lambda: t[0], store=store, wall=wall)
        return eng, act, rec, t

    def test_nut_server_vanishing_while_on_battery_triggers_shutdown(self):
        eng, act, rec, t = self.engine()
        eng.step({"status": {"OB"}, "runtime": 1800})
        for i in (1, 2):
            t[0] = i * 15
            eng.step(None)
        self.assertEqual(act.history, [])                      # 2 failed polls: could be a blip
        t[0] = 45
        eng.step(None)                                         # 3rd: the NAS is gone (e.g. Synology Safe Mode) -> go
        self.assertEqual(len(act.history), 1)
        self.assertIn("stopped answering", act.history[0])

    def test_timer_keeps_running_while_the_link_is_down(self):
        eng, act, rec, t = self.engine()
        eng.step({"status": {"OB"}, "runtime": 1800})
        eng.comm_fails = -100                                  # pretend the 3-poll rule has not been reached
        t[0] = 601
        eng.step(None)
        self.assertEqual(len(act.history), 1)
        self.assertIn("limit 600", act.history[0])

    def test_link_loss_on_mains_is_still_only_a_notice(self):
        eng, act, rec, t = self.engine()
        eng.step({"status": {"OL"}, "runtime": 1800})
        for i in range(1, 20):
            t[0] = i * 15
            eng.step(None)
        self.assertEqual(act.history, [])
        self.assertEqual(sum("link lost" in x for x, _ in rec.sent), 1)

    def test_restart_mid_outage_resumes_the_timer(self):
        d = tempfile.mkdtemp()
        c = cfg(NUTS_DATA_DIR=d)
        store = n.OutageStore(c)
        wall = [1000.0]
        eng, act, rec, t = self.engine(store=store, wall=lambda: wall[0])
        eng.step({"status": {"OB"}, "runtime": 1800})           # outage starts at wall 1000, saved
        self.assertEqual(store.load(), 1000.0)
        # the client crashes and restarts 500 s later; the new engine starts counting from the real start of the outage
        wall[0] = 1500.0
        eng2, act2, rec2, t2 = self.engine(store=store, wall=lambda: wall[0])
        eng2.step({"status": {"OB"}, "runtime": 1700})
        self.assertEqual(act2.history, [])
        self.assertIn("500s of the outage already elapsed", rec2.sent[0][1])
        t2[0] = 100                                             # 500 + 100 >= 600
        eng2.step({"status": {"OB"}, "runtime": 1600})
        self.assertEqual(len(act2.history), 1)

    def test_power_restored_clears_the_saved_outage(self):
        d = tempfile.mkdtemp()
        store = n.OutageStore(cfg(NUTS_DATA_DIR=d))
        eng, act, rec, t = self.engine(store=store, wall=lambda: 50.0)
        eng.step({"status": {"OB"}, "runtime": 1800})
        self.assertIsNotNone(store.load())
        t[0] = 30
        eng.step({"status": {"OL"}, "runtime": 1800})
        self.assertIsNone(store.load())
        # and a record left from before a client restart is dropped when power is already back
        store.save(10.0)
        eng3, *_ = self.engine(store=store, wall=lambda: 60.0)
        eng3.step({"status": {"OL"}, "runtime": 1800})
        self.assertIsNone(store.load())


class GuardedLoop(unittest.TestCase):
    def test_an_exception_in_one_step_does_not_kill_the_loop(self):
        st = n.State(cfg())
        out = n.guarded(st, "boom", lambda: 1 / 0)
        self.assertIsNone(out)
        self.assertIn("boom failed", st.recent()[0]["message"])
        self.assertEqual(n.guarded(st, "fine", lambda x: x + 1, 1), 2)


class Notifications(unittest.TestCase):
    def sent_with(self, active, report, force=False):
        c = cfg(NUTS_ACTIVE="on" if active else "off", NUTS_REPORT="yes" if report else "no", PUSHOVER_TOKEN="t", PUSHOVER_USER="u")
        calls = []
        orig = n.urllib.request.urlopen
        n.urllib.request.urlopen = lambda *a, **k: calls.append(a) or type("R", (), {"read": lambda s: b""})()
        try:
            n.Notifier(c).send("x", "y", force=force)
        finally:
            n.urllib.request.urlopen = orig
        return len(calls)

    def test_matrix(self):
        self.assertEqual(self.sent_with(True, False), 1)       # ON always reports
        self.assertEqual(self.sent_with(True, True), 1)
        self.assertEqual(self.sent_with(False, True), 1)       # OFF + report only
        self.assertEqual(self.sent_with(False, False), 0)      # OFF, silent
        self.assertEqual(self.sent_with(False, False, force=True), 1)


class Protocol(unittest.TestCase):
    def test_poll_against_fake_nut(self):
        srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), FakeNut)
        srv.daemon_threads = True
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        c = cfg(NUT_HOST="127.0.0.1", NUT_PORT=str(srv.server_address[1]))
        s = n.poll(c)
        srv.shutdown()
        self.assertEqual(s["status"], {"OL"})
        self.assertEqual((s["runtime"], s["charge"], s["load"], s["input_voltage"]), (1880, 100, 21, 123))


class Live:
    """A real dashboard server on 127.0.0.1 backed by temp data and drop-box dirs."""

    def __init__(self, **env):
        self.data, self.box = tempfile.mkdtemp(), tempfile.mkdtemp()
        e = dict(NUTS_DATA_DIR=self.data, NUTS_REQUEST_DIR=self.box, NUTS_STARTUP_NOTICE="no")
        e.update(env)
        self.cfg = n.Config(e)
        self.state = n.State(self.cfg)
        self.state.settings.load()
        n.attach_engine(self.state)
        self.state.record_poll({"status": {"OL"}, "runtime": 1880, "charge": 100, "load": 21, "input_voltage": 123, "model": "x"})
        handler = type("H", (n.Dashboard,), {"state": self.state})
        self.srv = n.http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.srv.daemon_threads = True
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.port = self.srv.server_address[1]

    def req(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        h = dict(headers or {})
        data = None
        if body is not None:
            data = json.dumps(body)
            h["Content-Type"] = "application/json"
        conn.request(method, path, data, h)
        r = conn.getresponse()
        raw = r.read()
        conn.close()
        try:
            return r.status, json.loads(raw)
        except ValueError:
            return r.status, raw

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


class SwitchTests(unittest.TestCase):
    def test_toggle_persists_and_survives_restart(self):
        L = Live()
        try:
            self.assertFalse(L.req("GET", "/api/status")[1]["active"])
            self.assertEqual(L.req("POST", "/api/active", {"on": True})[0], 200)
            self.assertTrue(L.req("GET", "/api/status")[1]["active"])
            c2 = n.Config({"NUTS_DATA_DIR": L.data})            # a restart: fresh Config + load
            n.SettingsStore(c2).load()
            self.assertTrue(c2.active)
            self.assertEqual(L.req("POST", "/api/active", {"on": False})[0], 200)
            c3 = n.Config({"NUTS_DATA_DIR": L.data})
            n.SettingsStore(c3).load()
            self.assertFalse(c3.active)
            text = open(os.path.join(L.data, "events.jsonl")).read()
            self.assertIn("switch turned ON", text)
            self.assertIn("switch turned OFF", text)
        finally:
            L.close()

    def test_only_real_booleans_are_accepted(self):
        L = Live()
        try:
            for bad in ("true", 1, None, "on", [True]):
                self.assertEqual(L.req("POST", "/api/active", {"on": bad})[0], 400, bad)
            self.assertFalse(L.cfg.active)
        finally:
            L.close()

    def test_turning_off_removes_a_pending_request(self):
        L = Live()
        try:
            L.req("POST", "/api/active", {"on": True})
            with open(os.path.join(L.box, n.REQUEST_FILE), "w") as f:
                f.write("x")
            L.req("POST", "/api/active", {"on": False})
            self.assertNotIn(n.REQUEST_FILE, os.listdir(L.box))
        finally:
            L.close()

    def beat(self, L, **kw):
        d = {"ts": time.time(), "state": "idle", "detail": "", "host_ok": True, "dry_run": False}
        d.update(kw)
        with open(os.path.join(L.box, n.HEARTBEAT_FILE), "w") as f:
            json.dump(d, f)

    def warnings(self, L):
        return L.req("GET", "/api/status")[1]["warnings"]

    def test_warnings_when_on_and_the_helper_is_not_ready(self):
        L = Live()
        try:
            L.req("POST", "/api/active", {"on": True})
            self.assertTrue(any("helper is not running" in w and "NOT be shut down" in w for w in self.warnings(L)))   # no heartbeat
            self.beat(L, ts=time.time() - 60)
            self.assertTrue(any("stopped responding" in w for w in self.warnings(L)))
            self.beat(L, host_ok=False)
            self.assertTrue(any("cannot reach the host" in w for w in self.warnings(L)))
            self.beat(L, dry_run=True)
            self.assertTrue(any("DRY RUN" in w for w in self.warnings(L)))
            self.beat(L, state="failed", detail="boom")
            self.assertTrue(any("failed shutdown: boom" in w for w in self.warnings(L)))
            self.beat(L)
            self.assertFalse(any("helper" in w for w in self.warnings(L)))
            self.assertEqual(L.req("GET", "/api/status")[1]["values"]["helper"], "ON")
            L.cfg.request_dir = L.box + "-missing"
            self.assertTrue(any("drop-box" in w and "NOT be shut down" in w for w in self.warnings(L)))
        finally:
            L.close()

    def test_no_helper_warning_while_off(self):
        L = Live()
        try:
            self.assertFalse(any("helper" in w for w in self.warnings(L)))
            self.assertEqual(L.req("GET", "/api/status")[1]["values"]["helper"], "OFF")     # but the card says NOT READY
        finally:
            L.close()

    def test_watch_shutdown_alerts_when_the_helper_does_not_pick_the_request_up(self):
        L = Live()
        try:
            sent = []
            L.state.notifier.send = lambda title, message, priority=0, force=False, code=None: sent.append((title, code))
            L.cfg.active = True
            self.beat(L)
            L.state.engine.latched = True
            req = os.path.join(L.box, n.REQUEST_FILE)
            with open(req, "w") as f:
                f.write("{}")
            L.state.watch_shutdown()
            self.assertEqual(sent, [])                                    # fresh: give the helper a moment
            os.utime(req, (time.time() - 30, time.time() - 30))
            L.state.watch_shutdown()
            L.state.watch_shutdown()
            self.assertEqual([c for _, c in sent], ["shutdown_failed"])   # alerted once
            os.remove(req)
            self.beat(L, state="failed", detail="no luck")
            L.state.watch_shutdown()
            self.assertTrue(any("SHUTDOWN FAILED" in t for t, _ in sent))
        finally:
            L.close()

    def test_report_last_shutdown_once_after_boot(self):
        L = Live()
        try:
            sent = []
            L.state.notifier.send = lambda title, message, priority=0, force=False, code=None: sent.append((title, message, code))
            with open(os.path.join(L.box, n.LAST_SHUTDOWN_FILE), "w") as f:
                json.dump({"ts": time.time() - 3600, "reason": "UPS reports LOW BATTERY"}, f)
            L.state.report_last_shutdown()
            L.state.report_last_shutdown()
            self.assertEqual(len(sent), 1)
            self.assertEqual(sent[0][2], "host_restarted")
            self.assertIn("LOW BATTERY", sent[0][1])
            self.assertIn("back up", json.dumps(L.req("GET", "/api/events")[1]))
        finally:
            L.close()

    def test_stale_request_is_cleared(self):
        d = tempfile.mkdtemp()
        c = cfg(NUTS_REQUEST_DIR=d)
        with open(os.path.join(d, n.REQUEST_FILE), "w") as f:
            f.write("old")
        self.assertTrue(n.clear_request(c))
        self.assertFalse(n.clear_request(c))
        self.assertEqual(os.listdir(d), [])


class DashboardTests(unittest.TestCase):
    def test_views_and_routes(self):
        L = Live()
        try:
            code, snap = L.req("GET", "/api/status")
            self.assertEqual((code, snap["active"], snap["report"]), (200, False, True))
            self.assertEqual(L.req("GET", "/")[0], 200)
            self.assertEqual(L.req("GET", "/healthz")[0], 200)
            self.assertEqual(L.req("GET", "/api/settings")[0], 200)
            for verb in ("PUT", "DELETE"):
                self.assertEqual(L.req(verb, "/api/status")[0], 501)
            for gone in ("/api/login", "/api/arm", "/api/disarm", "/api/maintenance", "/api/simulate", "/api/password"):
                self.assertEqual(L.req("POST", gone, {})[0], 404, gone)
        finally:
            L.close()

    def test_post_hardening(self):
        L = Live()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", L.port, timeout=5)
            conn.request("POST", "/api/active", '{"on": true}', {"Content-Type": "text/plain"})   # not JSON
            self.assertEqual(conn.getresponse().status, 415)
            conn.close()
            self.assertEqual(L.req("POST", "/api/active", {"on": True}, headers={"Origin": "http://evil.example"})[0], 403)
            self.assertFalse(L.cfg.active)
        finally:
            L.close()

    def test_ip_allowlist_blocks_everything(self):
        L = Live(NUTS_DASH_ALLOW="10.9.9.0/24")
        try:
            self.assertEqual(L.req("GET", "/api/status")[0], 403)
            self.assertEqual(L.req("POST", "/api/active", {"on": True})[0], 403)
            self.assertFalse(L.cfg.active)
        finally:
            L.close()

    def test_page_script_brackets_balance(self):
        # the page is one inline script: a stray quote breaks the whole dashboard (it did, once)
        script = n.PAGE.split("<script>")[1].split("</script>")[0]
        self.assertEqual(script.count("{"), script.count("}"))
        self.assertEqual(script.count("("), script.count(")"))


class SettingsTests(unittest.TestCase):
    def test_validate_apply_persist_and_mask_secrets(self):
        L = Live()
        try:
            code, j = L.req("GET", "/api/settings")
            keys = {s["key"] for s in j["settings"]}
            self.assertTrue({"report", "nut_host", "on_battery_seconds", "pushover_token", "dash_allow"} <= keys)
            code, j = L.req("POST", "/api/settings", {"changes": {"poll_seconds": 1, "label": "ok"}})
            self.assertEqual(code, 400)
            self.assertNotEqual(L.cfg.label, "ok")
            self.assertEqual(L.req("POST", "/api/settings", {"changes": {"nut_host": "bad host;rm"}})[0], 400)
            self.assertEqual(L.req("POST", "/api/settings", {"changes": {"dash_allow": "not-an-ip"}})[0], 400)
            self.assertEqual(L.req("POST", "/api/settings", {"changes": {"active": True}})[0], 400)   # the switch has its own endpoint
            code, j = L.req("POST", "/api/settings", {"changes": {
                "nut_host": "192.168.0.10", "on_battery_seconds": 900, "label": "server1", "startup_notice": True, "report": False,
                "pushover_token": "abc123TOKEN", "pushover_user": "usr456"}})
            self.assertEqual(code, 200)
            self.assertEqual((L.cfg.on_battery_seconds, L.cfg.label, L.cfg.pushover_token, L.cfg.report), (900, "server1", "abc123TOKEN", False))
            _, j = L.req("GET", "/api/settings")
            tok = [s for s in j["settings"] if s["key"] == "pushover_token"][0]
            self.assertEqual(tok["value"], "(set)")                      # secret never returned
            self.assertNotIn("abc123TOKEN", json.dumps(L.req("GET", "/api/events")[1]))  # nor logged
            self.assertTrue(os.path.exists(os.path.join(L.data, "settings.json")))
            if os.name != "nt":
                self.assertEqual(oct(os.stat(os.path.join(L.data, "settings.json")).st_mode & 0o777), "0o600")
            c2 = n.Config({"NUTS_DATA_DIR": L.data})
            n.SettingsStore(c2).load()
            self.assertEqual((c2.on_battery_seconds, c2.label, c2.startup_notice, c2.report), (900, "server1", True, False))
            L.req("POST", "/api/settings", {"changes": {"pushover_token": ""}})   # blank secret keeps
            self.assertEqual(L.cfg.pushover_token, "abc123TOKEN")
            L.req("POST", "/api/settings", {"changes": {"pushover_token": "-"}})  # '-' clears
            self.assertEqual(L.cfg.pushover_token, "")
        finally:
            L.close()

    def test_test_connection(self):
        srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), FakeNut)
        srv.daemon_threads = True
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        L = Live()
        try:
            code, j = L.req("POST", "/api/test-connection", {"nut_host": "127.0.0.1", "nut_port": srv.server_address[1], "ups": "ups"})
            self.assertTrue(j["ok"] and j["status"] == "OL")
            code, j = L.req("POST", "/api/test-connection", {"nut_host": "127.0.0.1", "nut_port": 1, "ups": "ups"})
            self.assertFalse(j["ok"])
            self.assertNotEqual(L.cfg.nut_port, 1)                      # testing never changes the live settings
        finally:
            L.close()
            srv.shutdown()

    def test_test_notice_is_labelled_and_throttled(self):
        L = Live()
        try:
            self.assertEqual(L.req("POST", "/api/test-notice", {})[0], 200)
            self.assertIn("[TEST - IGNORE]", json.dumps(L.req("GET", "/api/events")[1]))
            self.assertEqual(L.req("POST", "/api/test-notice", {})[0], 429)
        finally:
            L.close()


if __name__ == "__main__":
    unittest.main()

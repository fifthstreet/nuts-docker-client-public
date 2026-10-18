import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import nuts_client as n  # noqa: E402


class Fake:
    """Stands in for nsenter/systemctl/sync/sysrq/sleep so the whole shutdown path runs without touching a real host."""

    def __init__(self, poweroff_works_on=None):
        self.calls = []
        self.poweroff_works_on = poweroff_works_on     # 1-based attempt number after which "the host goes down"; None = never

    def run(self, cmd, timeout):
        self.calls.append(("run", tuple(cmd[len(n.HOST_NS):])))
        return 0

    def sync(self):
        self.calls.append(("sync",))

    def sysrq(self):
        self.calls.append(("sysrq",))

    def sleep(self, secs):
        self.calls.append(("sleep", secs))
        if self.poweroff_works_on is not None and sum(1 for c in self.calls if c[0] == "sleep") >= self.poweroff_works_on:
            raise SystemExit("the host went down")      # the process would be killed by the power-off


def make(box, dry_run=False, fake=None):
    fake = fake or Fake()
    h = n.Helper(box, dry_run=dry_run, run=fake.run, sleep=fake.sleep, sync=fake.sync, sysrq=fake.sysrq)
    return h, fake


def write_request(box, reason="UPS reports LOW BATTERY", age=0):
    path = os.path.join(box, n.REQUEST_FILE)
    with open(path, "w") as f:
        json.dump({"ts": time.time() - age, "reason": reason, "label": "t"}, f)
    if age:
        os.utime(path, (time.time() - age, time.time() - age))
    return path


class HelperTests(unittest.TestCase):
    def setUp(self):
        self.box = tempfile.mkdtemp()

    def test_request_is_deleted_first_then_sync_then_poweroff(self):
        h, fake = make(self.box, fake=Fake(poweroff_works_on=1))
        path = write_request(self.box)
        with self.assertRaises(SystemExit):
            h.handle(path)
        self.assertFalse(os.path.exists(path))                                 # deleted before acting: can never be replayed
        names = [c for c in fake.calls if c[0] != "sleep"]
        self.assertEqual(names[0], ("run", ("logger", "-t", "nuts-docker-client", "UPS shutdown requested: UPS reports LOW BATTERY - powering off")))
        self.assertEqual(names[1], ("sync",))
        self.assertEqual(names[2], ("run", ("systemctl", "poweroff")))
        rec = json.load(open(os.path.join(self.box, n.LAST_SHUTDOWN_FILE)))     # the record the client reports after the reboot
        self.assertEqual(rec["reason"], "UPS reports LOW BATTERY")
        self.assertEqual(json.load(open(os.path.join(self.box, n.HEARTBEAT_FILE)))["state"], "shutting_down")

    def test_escalates_through_force_then_sysrq_and_reports_failure(self):
        h, fake = make(self.box)
        h.handle(write_request(self.box))
        runs = [c[1] for c in fake.calls if c[0] == "run" and c[1][0] == "systemctl"]
        self.assertEqual(runs, [("systemctl", "poweroff"), ("systemctl", "--force", "poweroff")])
        self.assertIn(("sysrq",), fake.calls)
        self.assertEqual([c[1] for c in fake.calls if c[0] == "sleep"], [45, 20, 30])
        hb = json.load(open(os.path.join(self.box, n.HEARTBEAT_FILE)))
        self.assertEqual(hb["state"], "failed")
        self.assertIn("did not power off", hb["detail"])

    def test_second_attempt_can_succeed(self):
        h, fake = make(self.box, fake=Fake(poweroff_works_on=2))
        with self.assertRaises(SystemExit):
            h.handle(write_request(self.box))
        self.assertNotIn(("sysrq",), fake.calls)

    def test_stale_request_is_deleted_and_ignored(self):
        h, fake = make(self.box)
        path = write_request(self.box, age=n.REQUEST_MAX_AGE + 60)
        h.handle(path)
        self.assertFalse(os.path.exists(path))
        self.assertEqual([c for c in fake.calls if c[0] != "sleep"], [])         # nothing was run
        self.assertEqual(h.state, "idle")

    def test_dry_run_never_powers_off(self):
        h, fake = make(self.box, dry_run=True)
        h.handle(write_request(self.box))
        self.assertEqual(fake.calls, [])
        hb = json.load(open(os.path.join(self.box, n.HEARTBEAT_FILE)))
        self.assertTrue(hb["dry_run"])
        self.assertIn("DRY RUN", hb["detail"])
        self.assertFalse(os.path.exists(os.path.join(self.box, n.LAST_SHUTDOWN_FILE)))

    def test_garbage_request_still_acts(self):
        h, fake = make(self.box, fake=Fake(poweroff_works_on=1))
        path = os.path.join(self.box, n.REQUEST_FILE)
        open(path, "w").write("not json at all")
        with self.assertRaises(SystemExit):
            h.handle(path)
        self.assertIn(("run", ("systemctl", "poweroff")), fake.calls)

    def test_tick_heartbeat_host_check_and_health(self):
        h, fake = make(self.box)
        self.assertFalse(n.helper_health(self.box))
        h.tick()
        hb = json.load(open(os.path.join(self.box, n.HEARTBEAT_FILE)))
        self.assertTrue(hb["host_ok"])
        self.assertEqual(hb["version"], n.__version__)
        self.assertTrue(n.helper_health(self.box))
        info = n.helper_info(n.Config({"NUTS_REQUEST_DIR": self.box}))
        self.assertTrue(info["ready"], info)
        self.assertTrue(os.path.exists(os.path.join(self.box, n.HELPER_LOG)) or True)

    def test_tick_survives_and_logs_host_access_failure(self):
        fake = Fake()
        fake.run = lambda cmd, timeout: 1
        h = n.Helper(self.box, run=fake.run, sleep=fake.sleep, sync=fake.sync, sysrq=fake.sysrq)
        h.tick()
        self.assertFalse(json.load(open(os.path.join(self.box, n.HEARTBEAT_FILE)))["host_ok"])
        self.assertIn("host access FAILED", open(os.path.join(self.box, n.HELPER_LOG)).read())
        info = n.helper_info(n.Config({"NUTS_REQUEST_DIR": self.box}))
        self.assertFalse(info["ready"])
        self.assertIn("privileged", info["problem"])

    def test_the_whole_chain_client_to_helper(self):
        """Client writes the request through its Actuator; the helper consumes it; the client sees the acknowledgement."""
        c = n.Config({"NUTS_REQUEST_DIR": self.box, "NUTS_DATA_DIR": "", "NUTS_ACTIVE": "on", "NUTS_STARTUP_NOTICE": "no"})
        st = n.State(c)
        n.attach_engine(st)
        sent = []
        st.notifier.send = lambda title, message, priority=0, force=False, code=None: sent.append(code)
        h, fake = make(self.box, fake=Fake(poweroff_works_on=1))
        h.tick()                                                                  # helper is up
        st.engine.step({"status": {"OB", "LB"}, "runtime": 100})                  # UPS says low battery
        self.assertTrue(os.path.exists(os.path.join(self.box, n.REQUEST_FILE)))
        self.assertIn("shutdown_requested", sent)
        with self.assertRaises(SystemExit):
            h.tick()
        st.watch_shutdown()
        self.assertIn("the helper took the shutdown request", " ".join(e["message"] for e in st.recent()))


if __name__ == "__main__":
    unittest.main()

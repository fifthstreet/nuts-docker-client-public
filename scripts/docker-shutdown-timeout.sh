#!/bin/bash
# Optional host tuning: raise Docker's daemon-wide shutdown timeout so a power-off (which Docker handles by stopping every container)
# is allowed to take longer than the 15 s default. Safe to re-run; backs up daemon.json first; reloads the daemon WITHOUT restarting containers.
#   sudo ./docker-shutdown-timeout.sh [shutdown_seconds] [stop_seconds]
#       shutdown_seconds  daemon-wide ceiling for the whole shutdown (default 120, allowed 15-600)
#       stop_seconds      OPTIONAL: default grace period each container gets before it is killed (daemon.json `default-stop-timeout`,
#                         Docker 28+; stock default 10). 10-300. Omit to leave it alone.
# Note: each container gets its OWN grace period (10 s unless it sets `stop_grace_period`/--stop-timeout or you pass stop_seconds) before it
# is killed; shutdown-timeout alone is only the ceiling for the whole shutdown.
set -eu
T="${1:-120}"
S="${2:-}"
case "$T" in ''|*[!0-9]*) echo "seconds must be a whole number"; exit 1;; esac
if [ "$T" -lt 15 ] || [ "$T" -gt 600 ]; then echo "choose a value between 15 and 600"; exit 1; fi
if [ -n "$S" ]; then
  case "$S" in *[!0-9]*) echo "stop_seconds must be a whole number"; exit 1;; esac
  if [ "$S" -lt 10 ] || [ "$S" -gt 300 ]; then echo "stop_seconds: choose 10-300"; exit 1; fi
fi
[ "$(id -u)" = 0 ] || { echo "run as root: sudo $0 [seconds]"; exit 1; }
command -v python3 >/dev/null || { echo "python3 is required"; exit 1; }
command -v dockerd >/dev/null || { echo "dockerd not found"; exit 1; }
CONF=/etc/docker/daemon.json
NEW=/etc/docker/daemon.json.new
mkdir -p /etc/docker
[ -f "$CONF" ] || echo '{}' > "$CONF"
BAK="$CONF.bak-$(date +%Y%m%d-%H%M%S)"
cp -p "$CONF" "$BAK"
echo "backup: $BAK"; sha256sum "$CONF" "$BAK"
python3 - "$CONF" "$NEW" "$T" "$S" <<'PY'
import json, sys
src, dst, t, stop = sys.argv[1:5]
d = json.load(open(src))
d["shutdown-timeout"] = int(t)
if stop:
    d["default-stop-timeout"] = int(stop)
with open(dst, "w") as f:
    json.dump(d, f, indent=2)
    f.write("\n")
PY
dockerd --validate --config-file="$NEW"          # refuses to continue (set -e) if the new file is invalid
echo "--- new $CONF:"; cat "$NEW"
mv "$NEW" "$CONF"
systemctl reload docker                           # SIGHUP: shutdown-timeout is live-reloadable; containers keep running
sleep 2
echo "docker: $(systemctl is-active docker)"
journalctl -u docker --since "-2min" --no-pager 2>/dev/null | grep -i -E "reload|shutdown-timeout" | tail -4 || true
echo "containers still running: $(docker ps -q | wc -l)"

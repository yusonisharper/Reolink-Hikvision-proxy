#!/bin/sh
# ExecStopPost: one line per stop, with what systemd saw (watchdog, oom-kill, signal, exit code).
# The journal on this device is small and shared, so it would not keep this for long.
f=/data/bcproxy/logs/restarts.log
mkdir -p "$(dirname "$f")"
echo "$(date '+%F %T') result=${SERVICE_RESULT:-?} code=${EXIT_CODE:-?} status=${EXIT_STATUS:-?}" >> "$f"
if [ "$(wc -l < "$f")" -gt 2000 ]; then
    tail -n 1000 "$f" > "$f.tmp" && mv "$f.tmp" "$f"
fi
exit 0

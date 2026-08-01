"""
bg3_supervisor.py
=================
Thin process supervisor for bg3_watcher.py.

Relaunches the watcher if it crashes. Logs all restarts with timestamps to
bg3_watcher.log so crash-loops are visible, not silent.

USAGE
-----
    python bg3_supervisor.py

    The supervisor stops itself after MAX_RESTARTS consecutive crashes to
    prevent runaway crash-loop behaviour.
"""

import subprocess, sys, time, datetime, pathlib

WATCHER         = "bg3_watcher.py"
OP_LOG          = pathlib.Path("bg3_watcher.log")
MAX_RESTARTS    = 20
RESTART_DELAY_S = 3
# A watcher that exits in less than this many seconds is always treated as a
# crash — even if the OS reports exit code 0 (e.g. Task Manager kill on Windows).
MIN_UPTIME_S    = 5


def log(msg: str) -> None:
    ts   = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] [SUPERVISOR] {msg}"
    print(line)
    with open(OP_LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def main() -> None:
    log("Supervisor started.")
    restarts = 0

    while restarts < MAX_RESTARTS:
        log(f"Launching watcher (attempt {restarts + 1}/{MAX_RESTARTS})")
        t_start = time.monotonic()
        try:
            # Use Popen + wait() so a Task Manager kill on Windows is reliably
            # captured as a non-zero return code instead of being swallowed.
            proc    = subprocess.Popen([sys.executable, WATCHER])
            retcode = proc.wait()
        except Exception as exc:
            log(f"ERROR: Failed to launch watcher: {exc}")
            retcode = -1

        uptime = time.monotonic() - t_start

        # A clean voluntary exit (Ctrl-C inside watcher raises SystemExit(0)).
        # Guard: if it exited in under MIN_UPTIME_S it was killed/crashed,
        # regardless of what the OS says the return code was.
        if retcode == 0 and uptime >= MIN_UPTIME_S:
            log("Watcher exited cleanly (Ctrl-C or normal exit). Supervisor stopping.")
            break

        restarts += 1
        reason = (
            f"killed within {uptime:.1f}s (exit code {retcode})"
            if uptime < MIN_UPTIME_S
            else f"crashed (code {retcode})"
        )
        log(f"Watcher {reason}. "
            f"Restarting in {RESTART_DELAY_S}s... ({restarts}/{MAX_RESTARTS})")
        time.sleep(RESTART_DELAY_S)

    if restarts >= MAX_RESTARTS:
        log(f"ERROR: Reached max restarts ({MAX_RESTARTS}). "
            f"Stopping to prevent crash-loop. Check bg3_watcher.log for details.")
        sys.exit(1)


if __name__ == "__main__":
    main()

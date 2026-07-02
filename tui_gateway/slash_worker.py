"""Persistent slash-command worker — one HermesCLI per TUI session.

Protocol: reads JSON lines from stdin {id, command}, writes {id, ok, output|error} to stdout.

Self-protection (defence-in-depth against orphaned workers):
  1. **Parent watchdog** — always-on daemon thread using ``psutil`` to check
     the parent's PID + create_time fingerprint. Configurable poll interval
     (``HERMES_SLASH_WATCHDOG_POLL_S``, default 2 s) and in-flight grace
     period (``HERMES_SLASH_WATCHDOG_GRACE_S``, default 5 s) so a running
     slash command can finish/flush before the worker exits.
  2. **Parent-PID poll** (fallback) — the main loop checks ``os.getppid()``
     on each stdin timeout; works even if psutil is somehow not reachable.
"""

# Stop a ``utils/`` (or ``proxy/``, ``ui/``) package in the launch directory
# from shadowing Hermes's own top-level modules.  This worker is spawned as
# ``-m tui_gateway.slash_worker`` and inherits the user's CWD, so the ``import
# cli`` below would otherwise resolve ``utils`` to a colliding local package
# and crash the child in a retry loop (issue #51286).  ``hermes_bootstrap``
# lives at the repo root, so importing it is safe before the guard runs (its
# name won't collide with a user package), and it owns the canonical
# path-hardening logic shared with the other entry points — #51693 added the
# guard to ``entry.py``/``acp_adapter/entry.py`` but missed this child.
import hermes_bootstrap

hermes_bootstrap.harden_import_path()

import argparse
import contextlib
import io
import json
import os
import select
import sys
import threading
import time
import psutil

import cli as cli_mod
from cli import HermesCLI
from rich.console import Console

# Max seconds of inactivity before the worker self-exits.
_IDLE_TIMEOUT_S = 1800  # 30 minutes

# How often the stdin poll returns to re-check conditions when idle.
_POLL_INTERVAL_S = 60

# Env-overridable so the integration test can drive sub-second timing.
def _env_float(name: str, default: float) -> float:
    """Parse a float env knob, falling back to ``default`` on absent/malformed
    values. A bare ``float(os.environ.get(...))`` would raise ValueError at
    import time on a typo (e.g. ``HERMES_SLASH_WATCHDOG_POLL_S=2s``) and kill
    the worker before it can serve a single command."""
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


_WATCHDOG_POLL_S = max(0.05, _env_float("HERMES_SLASH_WATCHDOG_POLL_S", 2.0))
_ORPHAN_GRACE_S = max(0.0, _env_float("HERMES_SLASH_WATCHDOG_GRACE_S", 5.0))
_in_flight = threading.Event()  # set while a command is executing


def _is_orphaned(original_ppid, parent_create_time, getppid=os.getppid) -> bool:
    """True once our spawning gateway is gone."""
    if getppid() != original_ppid:
        return True
    try:
        if not psutil.pid_exists(original_ppid):
            return True
        return psutil.Process(original_ppid).create_time() != parent_create_time
    except psutil.Error:
        return True


def _start_parent_death_watchdog(original_ppid, parent_create_time) -> None:
    def _loop():
        while not _is_orphaned(original_ppid, parent_create_time):
            time.sleep(_WATCHDOG_POLL_S)
        deadline = time.monotonic() + _ORPHAN_GRACE_S
        while _in_flight.is_set() and time.monotonic() < deadline:
            time.sleep(0.05)
        os._exit(0)

    threading.Thread(target=_loop, daemon=True).start()


def _run(cli: HermesCLI, command: str) -> str:
    cmd = (command or "").strip()
    if not cmd:
        return ""
    if not cmd.startswith("/"):
        cmd = f"/{cmd}"

    buf = io.StringIO()

    # Rich Console captures its file handle at construction time, so
    # contextlib.redirect_stdout won't affect it. Swap the console's
    # underlying file to our buffer so self.console.print() is captured.
    cli.console = Console(file=buf, force_terminal=True, width=120)

    old = getattr(cli_mod, "_cprint", None)
    if old is not None:
        cli_mod._cprint = lambda text: print(text)

    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            cli.process_command(cmd)
    finally:
        if old is not None:
            cli_mod._cprint = old

    # Desktop chat bubbles render plain text, not ANSI. A worker-routed command
    # that emits Rich color (e.g. /journey building its own Console, which picks
    # up truecolor from the gateway's inherited COLORTERM) would otherwise leak
    # raw escapes; strip them at the single choke point. (The TUI opens /journey
    # as an overlay, so it never travels this path.)
    from tools.ansi_strip import strip_ansi

    return strip_ansi(buf.getvalue().rstrip())


def main():
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("--session-key", required=True)
    p.add_argument("--model", default="")
    args = p.parse_args()

    os.environ["HERMES_SESSION_KEY"] = args.session_key
    os.environ["HERMES_INTERACTIVE"] = "1"

    # Start before the (hundreds-of-ms) HermesCLI build — that window is itself
    # an orphan risk if the gateway dies mid-spawn.
    orig_ppid = os.getppid()
    try:
        parent_create_time = psutil.Process(orig_ppid).create_time()
    except psutil.Error:
        parent_create_time = 0.0
    _start_parent_death_watchdog(orig_ppid, parent_create_time)

    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        cli = HermesCLI(model=args.model or None, compact=True, resume=args.session_key, verbose=False)

    parent_pid = os.getppid()
    last_command_time = time.monotonic()

    while True:
        # Guard 1: parent-PID check (works without psutil, covers basic
        # orphan scenarios but not PID reuse).
        if os.getppid() != parent_pid:
            break

        # Guard 2: idle timeout — bounds resource use when the upstream
        # cleanup path (close_on_disconnect + server-side teardown) misses
        # a code path.
        idle = time.monotonic() - last_command_time
        if idle >= _IDLE_TIMEOUT_S:
            break

        # Poll stdin with a timeout so we can periodically re-check the
        # conditions above. Without this, a blocking ``for raw in sys.stdin``
        # would hang forever if the pipe is left open.
        poll_timeout = min(_POLL_INTERVAL_S, _IDLE_TIMEOUT_S - idle)
        try:
            r, _, _ = select.select([sys.stdin], [], [], poll_timeout)
        except (ValueError, OSError):
            break  # stdin closed or invalid

        if not r:
            continue  # Timeout — loop back to check parent/idle

        raw = sys.stdin.readline()
        if not raw:
            break  # EOF — stdin closed

        line = raw.strip()
        if not line:
            continue

        _in_flight.set()
        rid = None
        try:
            req = json.loads(line)
            rid = req.get("id")
            out = _run(cli, req.get("command", ""))
            sys.stdout.write(json.dumps({"id": rid, "ok": True, "output": out}) + "\n")
            sys.stdout.flush()
            last_command_time = time.monotonic()
        except Exception as e:
            sys.stdout.write(json.dumps({"id": rid, "ok": False, "error": str(e)}) + "\n")
            sys.stdout.flush()
        finally:
            _in_flight.clear()


if __name__ == "__main__":
    main()

"""Persistent slash-command worker — one HermesCLI per TUI session.

Protocol: reads JSON lines from stdin {id, command}, writes {id, ok, output|error} to stdout.

Three-layer defence-in-depth against orphaned subprocesses:

1. **Parent watchdog (P1, psutil)** — a daemon thread monitors the parent's
   PID + create_time fingerprint every 10 s and exits if the parent disappears.
   Handles crashes, SIGKILL, and PID reuse.
2. **Idle timeout + getppid() poll (P1, no deps)** — the main stdin loop
   uses select.select() with a 60 s timeout so it can periodically check
   os.getppid() and a 30-minute idle deadline. Works without psutil.
3. **Server-side cleanup (P0)** — when a WebSocket disconnects, sessions
   marked close_on_disconnect=true are finalised and their worker is killed.
   Normal TUI sessions still fall back to the stdio transport for historical
   reconnect compatibility.
"""

import argparse
import contextlib
import io
import json
import os
import select
import sys
import threading
import time

import cli as cli_mod
from cli import HermesCLI
from rich.console import Console


# ── Parent watchdog (P1, psutil) ──────────────────────────────────────
# A daemon thread monitors the parent's PID + create_time fingerprint
# every 10 s and exits if the parent disappears. Handles crashes, SIGKILL,
# and PID reuse (critical on Windows).

_HAS_PSUTIL: bool = False
_PARENT_PID: int = os.getppid()
_PARENT_CREATE_TIME: float | None = None

try:
    import psutil as _psutil

    _HAS_PSUTIL = True
    try:
        _parent_proc = _psutil.Process(_PARENT_PID)
        _PARENT_CREATE_TIME = _parent_proc.create_time()
    except Exception:
        pass
except ImportError:
    pass


def _parent_watchdog() -> None:
    """Daemon thread: exit if the parent PID + create_time don't match."""
    if not _HAS_PSUTIL or _PARENT_CREATE_TIME is None:
        return  # fall through to getppid() poll in main loop
    while True:
        time.sleep(10)
        try:
            pp = _psutil.Process(_PARENT_PID)
            if pp.create_time() != _PARENT_CREATE_TIME:
                os._exit(0)
        except _psutil.NoSuchProcess:
            os._exit(0)


# ── Idle deadline ─────────────────────────────────────────────────────
_IDLE_TIMEOUT_S: int = 30 * 60  # 30 minutes
_STDIN_POLL_S: int = 60  # check getppid() every 60 s even without input
_LAST_ACTIVITY: float = time.time()


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

    return buf.getvalue().rstrip()


def main():
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("--session-key", required=True)
    p.add_argument("--model", default="")
    args = p.parse_args()

    os.environ["HERMES_SESSION_KEY"] = args.session_key
    os.environ["HERMES_INTERACTIVE"] = "1"

    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        cli = HermesCLI(model=args.model or None, compact=True, resume=args.session_key, verbose=False)

    # Layer 1: parent watchdog thread (psutil)
    threading.Thread(target=_parent_watchdog, daemon=True).start()

    # Layer 2: idle timeout + getppid() poll — use select() so we can
    # periodically check os.getppid() even when no input arrives.
    global _LAST_ACTIVITY
    while True:
        try:
            rlist, _, _ = select.select([sys.stdin], [], [], _STDIN_POLL_S)
        except (ValueError, InterruptedError):
            break

        if not rlist:
            # Timeout — poll parent and idle deadline
            if _PARENT_PID != os.getppid():
                os._exit(0)
            if time.time() - _LAST_ACTIVITY > _IDLE_TIMEOUT_S:
                os._exit(0)
            continue

        raw = rlist[0].readline()
        if not raw:
            break

        line = raw.strip()
        if not line:
            continue

        _LAST_ACTIVITY = time.time()
        rid = None
        try:
            req = json.loads(line)
            rid = req.get("id")
            out = _run(cli, req.get("command", ""))
            sys.stdout.write(json.dumps({"id": rid, "ok": True, "output": out}) + "\n")
            sys.stdout.flush()
        except Exception as e:
            sys.stdout.write(json.dumps({"id": rid, "ok": False, "error": str(e)}) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()

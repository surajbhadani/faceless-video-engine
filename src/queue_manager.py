"""Background queue-buffer maintenance + manual build-queue triggers.

Runs as a daemon thread started by portal.py. Spawns
`python run_daily.py --build-queue N` as a detached subprocess so a long
render never blocks the HTTP server, and never runs two builds at once.
"""
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE_FILE = ROOT / "data" / "portal_state.json"
BUILD_LOG = ROOT / "logs" / "portal_build.log"
CHECK_INTERVAL_SECONDS = 600  # 10 minutes

_lock = threading.Lock()
_process: subprocess.Popen | None = None
_process_target: int | None = None
_last_error: str | None = None


def _default_state() -> dict:
    return {"target_buffer": 7, "maintenance_enabled": True}


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return {**_default_state(), **json.loads(STATE_FILE.read_text(encoding="utf-8"))}
        except Exception:
            pass
    return _default_state()


def save_state(**updates) -> dict:
    state = {**load_state(), **updates}
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    return state


def build_status() -> dict:
    with _lock:
        running = _process is not None and _process.poll() is None
        return {
            "running": running,
            "target": _process_target if running else None,
            "last_error": _last_error,
        }


def trigger_build(count: int) -> bool:
    """Start a build-queue run in the background. Returns False if one is
    already running (never stacks two renders on top of each other)."""
    global _process, _process_target, _last_error
    if count <= 0:
        return False
    with _lock:
        if _process is not None and _process.poll() is None:
            return False
        try:
            BUILD_LOG.parent.mkdir(parents=True, exist_ok=True)
            log_fh = open(BUILD_LOG, "a", encoding="utf-8")
            _process = subprocess.Popen(
                [sys.executable, str(ROOT / "run_daily.py"), "--build-queue", str(count)],
                cwd=str(ROOT), stdout=log_fh, stderr=subprocess.STDOUT,
            )
            _process_target = count
            _last_error = None
        except Exception as exc:
            _last_error = str(exc)
            return False
    return True


def _maintain_loop(queue_depth_fn) -> None:
    while True:
        try:
            state = load_state()
            status = build_status()
            if state.get("maintenance_enabled", True) and not status["running"]:
                depth = queue_depth_fn()
                target = int(state.get("target_buffer", 7))
                if depth < target:
                    trigger_build(target - depth)
        except Exception:
            pass
        time.sleep(CHECK_INTERVAL_SECONDS)


def start_background_maintenance(queue_depth_fn) -> None:
    t = threading.Thread(target=_maintain_loop, args=(queue_depth_fn,), daemon=True)
    t.start()

"""Read/update the macOS launchd schedules (daily post + queue refill).

Edits the live plists in ~/Library/LaunchAgents (the ones launchd actually
reads), mirrors the change back into launchd/ for reference, then reloads the
job so the new time takes effect immediately. Uses only plistlib + launchctl
— no new dependency.
"""
import plistlib
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LAUNCH_AGENTS = Path.home() / "Library" / "LaunchAgents"
PROJECT_LAUNCHD = ROOT / "launchd"

DAILY_LABEL = "com.facelessvideoengine.dailypost"
REFILL_LABEL = "com.facelessvideoengine.queuerefill"
DAILY_NAME = f"{DAILY_LABEL}.plist"
REFILL_NAME = f"{REFILL_LABEL}.plist"


def _load_plist(path: Path) -> dict | None:
    if not path.exists():
        return None
    with open(path, "rb") as f:
        return plistlib.load(f)


def _launchctl_list() -> str:
    try:
        return subprocess.run(
            ["launchctl", "list"], capture_output=True, text=True, timeout=10
        ).stdout
    except Exception:
        return ""


def _interval(data: dict | None) -> dict:
    if not data:
        return {"hour": None, "minute": None}
    sci = data.get("StartCalendarInterval", {}) or {}
    return {"hour": sci.get("Hour"), "minute": sci.get("Minute")}


def get_schedule() -> dict:
    daily = _load_plist(LAUNCH_AGENTS / DAILY_NAME) or _load_plist(PROJECT_LAUNCHD / DAILY_NAME)
    refill = _load_plist(LAUNCH_AGENTS / REFILL_NAME) or _load_plist(PROJECT_LAUNCHD / REFILL_NAME)
    loaded = _launchctl_list()

    refill_count = 1
    if refill:
        args = refill.get("ProgramArguments", [])
        if "--build-queue" in args:
            idx = args.index("--build-queue")
            if idx + 1 < len(args):
                try:
                    refill_count = int(args[idx + 1])
                except ValueError:
                    pass

    return {
        "daily": {**_interval(daily), "loaded": DAILY_LABEL in loaded},
        "refill": {**_interval(refill), "count": refill_count, "loaded": REFILL_LABEL in loaded},
    }


def _write_and_reload(live_path: Path, data: dict, label: str) -> None:
    live_path.parent.mkdir(parents=True, exist_ok=True)
    with open(live_path, "wb") as f:
        plistlib.dump(data, f)
    try:
        (PROJECT_LAUNCHD / live_path.name).write_bytes(live_path.read_bytes())
    except Exception:
        pass
    subprocess.run(["launchctl", "unload", str(live_path)], capture_output=True, timeout=10)
    subprocess.run(["launchctl", "load", str(live_path)], capture_output=True, timeout=10)


def set_daily_schedule(hour: int, minute: int) -> None:
    path = LAUNCH_AGENTS / DAILY_NAME
    data = _load_plist(path) or _load_plist(PROJECT_LAUNCHD / DAILY_NAME)
    if data is None:
        raise RuntimeError(f"{DAILY_NAME} not found in LaunchAgents or launchd/")
    data["StartCalendarInterval"] = {"Hour": int(hour), "Minute": int(minute)}
    _write_and_reload(path, data, DAILY_LABEL)


def set_refill_schedule(hour: int, minute: int, count: int) -> None:
    path = LAUNCH_AGENTS / REFILL_NAME
    data = _load_plist(path) or _load_plist(PROJECT_LAUNCHD / REFILL_NAME)
    if data is None:
        raise RuntimeError(f"{REFILL_NAME} not found in LaunchAgents or launchd/")
    data["StartCalendarInterval"] = {"Hour": int(hour), "Minute": int(minute)}
    args = data.get("ProgramArguments", [])
    if "--build-queue" in args:
        idx = args.index("--build-queue")
        if idx + 1 < len(args):
            args[idx + 1] = str(int(count))
        else:
            args.append(str(int(count)))
    else:
        args += ["--build-queue", str(int(count))]
    data["ProgramArguments"] = args
    _write_and_reload(path, data, REFILL_LABEL)

"""macOS launchd scheduling support for periodic pkportal sync."""

from __future__ import annotations

import os
import plistlib
import subprocess
from pathlib import Path

from pkportal.config import PROJECT_ROOT, Config, ensure_runtime_dirs

LAUNCHD_LABEL = "com.staracademy.pkportal.sync"


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def default_pkportal_bin() -> Path:
    return PROJECT_ROOT / ".venv" / "bin" / "python"


def build_plist_dict(
    cfg: Config,
    pkportal_bin: Path,
    hour: int = 18,
    minute: int = 30,
) -> dict:
    """Build LaunchAgent plist configuration."""
    if pkportal_bin.name.startswith("python"):
        prog_args = [str(pkportal_bin), "-m", "pkportal.cli", "sync"]
    else:
        prog_args = [str(pkportal_bin), "sync"]

    return {
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": prog_args,
        "WorkingDirectory": str(PROJECT_ROOT),
        "StartCalendarInterval": {"Hour": int(hour), "Minute": int(minute)},
        "LimitLoadToSessionType": "Aqua",
        "EnvironmentVariables": {
            "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        },
        "StandardOutPath": str(cfg.logs_dir / "launchd.out.log"),
        "StandardErrorPath": str(cfg.logs_dir / "launchd.err.log"),
    }


def _gui_domain() -> str:
    return f"gui/{os.getuid()}"


def install_schedule(
    cfg: Config,
    pkportal_bin: Path | None = None,
    hour: int = 18,
    minute: int = 30,
) -> Path:
    """Install and bootstrap the daily macOS LaunchAgent."""
    ensure_runtime_dirs(cfg)
    target_bin = pkportal_bin or default_pkportal_bin()
    target_plist = plist_path()
    target_plist.parent.mkdir(parents=True, exist_ok=True)

    payload = build_plist_dict(cfg, target_bin, hour=hour, minute=minute)
    with target_plist.open("wb") as f:
        plistlib.dump(payload, f)

    domain = _gui_domain()
    subprocess.run(
        ["launchctl", "bootout", domain, str(target_plist)],
        capture_output=True,
        text=True,
        check=False,
    )
    res = subprocess.run(
        ["launchctl", "bootstrap", domain, str(target_plist)],
        capture_output=True,
        text=True,
        check=False,
    )
    if res.returncode != 0:
        raise RuntimeError(
            f"launchctl bootstrap failed ({res.returncode}): {res.stderr.strip() or res.stdout.strip()}"
        )
    return target_plist


def uninstall_schedule() -> bool:
    """Unload and remove the LaunchAgent plist if present."""
    target_plist = plist_path()
    existed = target_plist.exists()
    domain = _gui_domain()
    subprocess.run(
        ["launchctl", "bootout", domain, str(target_plist)],
        capture_output=True,
        text=True,
        check=False,
    )
    if existed:
        target_plist.unlink(missing_ok=True)
    return existed


def schedule_status() -> dict:
    """Return installed plist and launchd load status."""
    target_plist = plist_path()
    installed = target_plist.exists()
    interval = None
    if installed:
        try:
            with target_plist.open("rb") as f:
                data = plistlib.load(f)
            interval = data.get("StartCalendarInterval")
        except Exception:
            interval = None

    domain = _gui_domain()
    res = subprocess.run(
        ["launchctl", "print", f"{domain}/{LAUNCHD_LABEL}"],
        capture_output=True,
        text=True,
        check=False,
    )
    return {
        "label": LAUNCHD_LABEL,
        "plist_path": str(target_plist),
        "installed": installed,
        "loaded": res.returncode == 0,
        "interval": interval,
    }

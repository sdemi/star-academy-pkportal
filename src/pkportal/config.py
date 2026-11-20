"""Configuration paths and persistence for pkportal."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

DEFAULT_PORTAL_URL = "https://staracademyboston.com/pkportal/"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
APP_SUPPORT_DIR = PROJECT_ROOT / "data"
LOGS_DIR = APP_SUPPORT_DIR / "logs"
DEFAULT_OUTPUT_DIR = Path.home() / "Pictures" / "Star Academy iCloud"
DEFAULT_TIMEZONE = "America/New_York"


@dataclass
class Config:
    portal_url: str = DEFAULT_PORTAL_URL
    output_dir: Path = DEFAULT_OUTPUT_DIR
    timezone: str = DEFAULT_TIMEZONE
    app_support_dir: Path = APP_SUPPORT_DIR
    logs_dir: Path = LOGS_DIR
    schedule_hour: int = 18
    schedule_minute: int = 30
    group_by_day_subfolder: bool = True

    def __post_init__(self) -> None:
        self.output_dir = Path(self.output_dir).expanduser()
        self.app_support_dir = Path(self.app_support_dir).expanduser()
        self.logs_dir = Path(self.logs_dir).expanduser()

    @property
    def chrome_profile_dir(self) -> Path:
        return self.app_support_dir / "chrome-profile"

    @property
    def storage_state_path(self) -> Path:
        return self.app_support_dir / "storage_state.json"

    @property
    def session_meta_path(self) -> Path:
        return self.app_support_dir / "session_meta.json"

    @property
    def state_db_path(self) -> Path:
        return self.app_support_dir / "state.db"

    @property
    def inspect_dir(self) -> Path:
        return self.app_support_dir / "inspect"

    @property
    def config_path(self) -> Path:
        return self.app_support_dir / "config.json"

    @property
    def gphotos_client_secret_path(self) -> Path:
        return self.app_support_dir / "gphotos_client_secret.json"

    @property
    def gphotos_token_path(self) -> Path:
        return self.app_support_dir / "gphotos_token.json"


def _mkdir_mode(path: Path, mode: int) -> None:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, mode)


def ensure_runtime_dirs(cfg: Config) -> None:
    """Create project-local runtime directories for cookies, Chrome profile, state DB, and logs."""
    _mkdir_mode(cfg.app_support_dir, 0o700)
    _mkdir_mode(cfg.chrome_profile_dir, 0o700)
    _mkdir_mode(cfg.logs_dir, 0o755)


def load_config(path: Path | None = None) -> Config:
    """Load config.json if present, otherwise return defaults."""
    cfg = Config()
    cfg_path = path or cfg.config_path
    if not cfg_path.exists():
        return cfg

    raw = json.loads(cfg_path.read_text(encoding="utf-8"))
    return Config(
        portal_url=raw.get("portal_url", DEFAULT_PORTAL_URL),
        output_dir=Path(raw.get("output_dir", str(DEFAULT_OUTPUT_DIR))),
        timezone=raw.get("timezone", DEFAULT_TIMEZONE),
        app_support_dir=Path(raw.get("app_support_dir", str(APP_SUPPORT_DIR))),
        logs_dir=Path(raw.get("logs_dir", str(LOGS_DIR))),
        schedule_hour=int(raw.get("schedule_hour", 18)),
        schedule_minute=int(raw.get("schedule_minute", 30)),
        group_by_day_subfolder=bool(raw.get("group_by_day_subfolder", True)),
    )


def save_config(cfg: Config, path: Path | None = None) -> Path:
    """Write configuration to config.json atomically."""
    ensure_runtime_dirs(cfg)
    cfg_path = path or cfg.config_path
    payload = {
        "portal_url": cfg.portal_url,
        "output_dir": str(cfg.output_dir),
        "timezone": cfg.timezone,
        "app_support_dir": str(cfg.app_support_dir),
        "logs_dir": str(cfg.logs_dir),
        "schedule_hour": cfg.schedule_hour,
        "schedule_minute": cfg.schedule_minute,
        "group_by_day_subfolder": cfg.group_by_day_subfolder,
    }
    tmp_path = cfg_path.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp_path, cfg_path)
    return cfg_path


def config_to_dict(cfg: Config) -> dict:
    """Return a JSON-serializable dictionary of the config."""
    data = asdict(cfg)
    for key in ("output_dir", "app_support_dir", "logs_dir"):
        data[key] = str(data[key])
    data["storage_state_path"] = str(cfg.storage_state_path)
    data["state_db_path"] = str(cfg.state_db_path)
    data["chrome_profile_dir"] = str(cfg.chrome_profile_dir)
    data["gphotos_client_secret_path"] = str(cfg.gphotos_client_secret_path)
    data["gphotos_token_path"] = str(cfg.gphotos_token_path)
    return data

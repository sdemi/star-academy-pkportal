"""Command-line interface for pkportal."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from pkportal import auth, gphotos, inspector, metadata, portal, schedule
from pkportal.config import Config, config_to_dict, ensure_runtime_dirs, load_config, save_config
from pkportal.state import StateDB


def _cmd_inspect(args: argparse.Namespace) -> int:
    del args
    cfg = load_config()
    inspector.run_inspect(cfg)
    return 0


def _cmd_login(args: argparse.Namespace) -> int:
    cfg = load_config()
    cookies = auth.interactive_login(cfg, auto_close_on_auth=not args.no_auto_close)
    print(f"Saved {len(cookies)} cookies to {cfg.storage_state_path}")
    return 0


def _cmd_sync(args: argparse.Namespace) -> int:
    cfg = load_config()
    if args.output:
        cfg.output_dir = Path(args.output).expanduser()
    result = portal.sync_portal(
        cfg,
        dry_run=args.dry_run,
        relogin=args.relogin,
        redownload_missing=args.redownload_missing,
        limit_days=args.limit_days,
        limit_files=args.limit_files,
        workers=args.workers,
    )
    gp_failed = 0
    if args.gphotos:
        gp_res = gphotos.upload_to_gphotos(
            cfg,
            dry_run=args.dry_run,
            album_title=args.gphotos_album_title,
            no_album=args.gphotos_no_album,
            workers=args.workers,
        )
        result["gphotos"] = gp_res
        gp_failed = int(gp_res.get("files_failed", 0))
    print(json.dumps(result, indent=2))
    return 1 if (result["files_failed"] > 0 or gp_failed > 0) else 0


def _cmd_status(args: argparse.Namespace) -> int:
    del args
    cfg = load_config()
    ensure_runtime_dirs(cfg)
    cookies = auth.summarize_cookies(cfg.storage_state_path)
    with StateDB(cfg.state_db_path) as db:
        db_stats = db.get_stats()
        gp_stats = db.get_gphotos_stats()

    try:
        exiftool_path = metadata.find_exiftool()
    except RuntimeError as exc:
        exiftool_path = f"MISSING ({exc})"

    sched = schedule.schedule_status()
    payload = {
        "portal_url": cfg.portal_url,
        "output_dir": str(cfg.output_dir),
        "timezone": cfg.timezone,
        "exiftool": exiftool_path,
        "storage_state_exists": cfg.storage_state_path.exists(),
        "cookies_count": len(cookies),
        "cookies": cookies,
        "db": db_stats,
        "gphotos": {**gphotos.gphotos_auth_status(cfg), **gp_stats},
        "schedule": sched,
    }
    print(json.dumps(payload, indent=2))
    return 0


def _cmd_gphotos(args: argparse.Namespace) -> int:
    cfg = load_config()
    if args.gphotos_action == "auth":
        secret_path = Path(args.client_secret).expanduser() if args.client_secret else None
        res = gphotos.authorize_gphotos(
            cfg,
            client_secret_path=secret_path,
            timeout_seconds=args.timeout,
        )
        print(json.dumps(res, indent=2))
        return 0
    if args.gphotos_action == "upload":
        res = gphotos.upload_to_gphotos(
            cfg,
            dry_run=args.dry_run,
            album_title=args.album_title,
            no_album=args.no_album,
            class_name=args.class_name,
            season_name=args.season,
            day=args.day,
            limit=args.limit,
            workers=args.workers,
        )
        print(json.dumps(res, indent=2))
        return 1 if res["files_failed"] > 0 else 0
    if args.gphotos_action == "status":
        ensure_runtime_dirs(cfg)
        with StateDB(cfg.state_db_path) as db:
            gp_stats = db.get_gphotos_stats()
        payload = {**gphotos.gphotos_auth_status(cfg), **gp_stats}
        print(json.dumps(payload, indent=2))
        return 0
    return 1


def _cmd_schedule(args: argparse.Namespace) -> int:
    cfg = load_config()
    if args.schedule_action == "install":
        cfg.schedule_hour = args.hour
        cfg.schedule_minute = args.minute
        save_config(cfg)
        plist = schedule.install_schedule(cfg, hour=args.hour, minute=args.minute)
        print(f"Installed and loaded LaunchAgent: {plist}")
    elif args.schedule_action == "uninstall":
        removed = schedule.uninstall_schedule()
        print("Uninstalled LaunchAgent." if removed else "LaunchAgent was not installed.")
    elif args.schedule_action == "status":
        print(json.dumps(schedule.schedule_status(), indent=2))
    return 0


def _cmd_config(args: argparse.Namespace) -> int:
    cfg = load_config()
    if args.config_action == "show":
        print(json.dumps(config_to_dict(cfg), indent=2))
    elif args.config_action == "set":
        if args.output:
            cfg.output_dir = Path(args.output).expanduser()
        if args.timezone:
            cfg.timezone = args.timezone
        if args.portal_url:
            cfg.portal_url = args.portal_url
        if args.day_subfolders:
            cfg.group_by_day_subfolder = True
        elif args.flat_season:
            cfg.group_by_day_subfolder = False
        saved = save_config(cfg)
        print(f"Saved config to {saved}")
        print(json.dumps(config_to_dict(cfg), indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pkportal",
        description="Sync Star Academy PK Portal photos and videos with album-date metadata.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_inspect = sub.add_parser("inspect", help="Open Chrome to inspect the portal DOM/network and save cookies.")
    p_inspect.set_defaults(func=_cmd_inspect)

    p_login = sub.add_parser("login", help="Open Chrome to log in and refresh saved cookies.")
    p_login.add_argument(
        "--no-auto-close",
        action="store_true",
        help="Keep Chrome open until you manually close it instead of closing on authentication.",
    )
    p_login.set_defaults(func=_cmd_login)

    p_sync = sub.add_parser("sync", help="Download any new photos and videos from the portal.")
    p_sync.add_argument("--output", help="Override output folder.")
    p_sync.add_argument("--dry-run", action="store_true", help="List changes without downloading.")
    p_sync.add_argument("--relogin", action="store_true", help="Force interactive Chrome login first.")
    p_sync.add_argument(
        "--redownload-missing",
        action="store_true",
        help="Re-download items whose local file no longer exists on disk.",
    )
    p_sync.add_argument("--workers", type=int, default=4, help="Concurrent downloads (default 4).")
    p_sync.add_argument("--limit-days", type=int, help="Only check the most recent N days.")
    p_sync.add_argument("--limit-files", type=int, help="Download at most N new files in this run.")
    p_sync.add_argument(
        "--gphotos",
        action="store_true",
        help="After syncing from the portal, upload unuploaded items to Google Photos.",
    )
    sync_gp_grp = p_sync.add_mutually_exclusive_group()
    sync_gp_grp.add_argument(
        "--gphotos-album-title",
        help="Override target Google Photos album title when --gphotos is set.",
    )
    sync_gp_grp.add_argument(
        "--gphotos-no-album",
        action="store_true",
        help="Upload to Google Photos library without adding items to an album (when --gphotos is set).",
    )
    p_sync.set_defaults(func=_cmd_sync)

    p_gp = sub.add_parser("gphotos", help="Authenticate and upload synced media to Google Photos.")
    gp_sub = p_gp.add_subparsers(dest="gphotos_action", required=True)

    p_gp_auth = gp_sub.add_parser("auth", help="Authorize Google Photos access via Desktop OAuth.")
    p_gp_auth.add_argument(
        "--client-secret",
        help="Path to downloaded Google Cloud Desktop OAuth client_secret JSON file.",
    )
    p_gp_auth.add_argument(
        "--timeout",
        type=int,
        default=300,
        help="Seconds to wait for OAuth browser callback (default 300).",
    )
    p_gp_auth.set_defaults(func=_cmd_gphotos)

    p_gp_up = gp_sub.add_parser("upload", help="Upload locally synced media not yet in Google Photos.")
    p_gp_up.add_argument("--dry-run", action="store_true", help="Preview pending uploads without calling Google Photos.")
    gp_album_grp = p_gp_up.add_mutually_exclusive_group()
    gp_album_grp.add_argument("--album-title", help="Override target Google Photos album title.")
    gp_album_grp.add_argument(
        "--no-album",
        action="store_true",
        help="Upload to Google Photos library without adding items to an album.",
    )
    p_gp_up.add_argument("--class-name", help="Filter pending items by class name (e.g. 'Group D').")
    p_gp_up.add_argument("--season", help="Filter pending items by season (e.g. 'Fall 2026').")
    p_gp_up.add_argument("--day", help="Filter pending items by album date (YYYY-MM-DD).")
    p_gp_up.add_argument("--limit", type=int, help="Upload at most N pending files.")
    p_gp_up.add_argument("--workers", type=int, default=4, help="Concurrent byte upload workers (default 4).")
    p_gp_up.set_defaults(func=_cmd_gphotos)

    p_gp_stat = gp_sub.add_parser("status", help="Show Google Photos auth and upload status.")
    p_gp_stat.set_defaults(func=_cmd_gphotos)

    p_status = sub.add_parser("status", help="Show cookie metadata, sync stats, exiftool, and schedule status.")
    p_status.set_defaults(func=_cmd_status)

    p_sched = sub.add_parser("schedule", help="Manage optional daily macOS launchd schedule.")
    sched_sub = p_sched.add_subparsers(dest="schedule_action", required=True)
    p_sched_inst = sched_sub.add_parser("install", help="Install and enable daily LaunchAgent.")
    p_sched_inst.add_argument("--hour", type=int, default=18, help="Hour (0-23, default 18).")
    p_sched_inst.add_argument("--minute", type=int, default=30, help="Minute (0-59, default 30).")
    p_sched_inst.set_defaults(func=_cmd_schedule)
    p_sched_uninst = sched_sub.add_parser("uninstall", help="Unload and remove LaunchAgent.")
    p_sched_uninst.set_defaults(func=_cmd_schedule)
    p_sched_stat = sched_sub.add_parser("status", help="Show LaunchAgent status.")
    p_sched_stat.set_defaults(func=_cmd_schedule)

    p_cfg = sub.add_parser("config", help="View or update saved configuration.")
    cfg_sub = p_cfg.add_subparsers(dest="config_action", required=True)
    p_cfg_show = cfg_sub.add_parser("show", help="Print current configuration.")
    p_cfg_show.set_defaults(func=_cmd_config)
    p_cfg_set = cfg_sub.add_parser("set", help="Update saved configuration.")
    p_cfg_set.add_argument("--output", help="Default output directory.")
    p_cfg_set.add_argument("--timezone", help="Local timezone for album dates (default America/New_York).")
    p_cfg_set.add_argument("--portal-url", help="Portal URL.")
    layout_grp = p_cfg_set.add_mutually_exclusive_group()
    layout_grp.add_argument(
        "--day-subfolders",
        action="store_true",
        help="Organize into <Class - Season>/<YYYY-MM-DD>/ subfolders (default).",
    )
    layout_grp.add_argument(
        "--flat-season",
        action="store_true",
        help="Save files directly inside <Class - Season>/ without date subfolders.",
    )
    p_cfg_set.set_defaults(func=_cmd_config)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

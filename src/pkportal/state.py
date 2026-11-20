"""SQLite state tracking for synced albums, files, and runs."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class StateDB:
    """Persistent SQLite database tracking albums, per-file downloads, and sync runs."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode = WAL;")
        self.conn.execute("PRAGMA foreign_keys = ON;")
        self._init_schema()

    def _init_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                status TEXT NOT NULL,
                albums_checked INTEGER NOT NULL DEFAULT 0,
                files_downloaded INTEGER NOT NULL DEFAULT 0,
                files_skipped INTEGER NOT NULL DEFAULT 0,
                error_message TEXT
            );

            CREATE TABLE IF NOT EXISTS albums (
                album_key TEXT PRIMARY KEY,
                url TEXT NOT NULL,
                class_name TEXT,
                season_name TEXT,
                title TEXT,
                album_date TEXT NOT NULL,
                first_seen_at TEXT NOT NULL,
                last_checked_at TEXT NOT NULL,
                item_count INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS media_items (
                media_key TEXT PRIMARY KEY,
                album_key TEXT NOT NULL,
                media_url TEXT NOT NULL,
                filename TEXT NOT NULL,
                media_type TEXT NOT NULL,
                album_date TEXT NOT NULL,
                album_index INTEGER NOT NULL,
                local_path TEXT NOT NULL,
                file_size INTEGER,
                sha256 TEXT,
                downloaded_at TEXT NOT NULL,
                metadata_status TEXT NOT NULL,
                metadata_applied_at TEXT,
                FOREIGN KEY (album_key) REFERENCES albums (album_key)
            );

            CREATE TABLE IF NOT EXISTS gphotos_albums (
                album_title TEXT PRIMARY KEY,
                gphotos_album_id TEXT NOT NULL,
                product_url TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS gphotos_uploads (
                media_key TEXT PRIMARY KEY,
                gphotos_media_id TEXT NOT NULL,
                gphotos_album_id TEXT,
                album_title TEXT,
                product_url TEXT,
                uploaded_at TEXT NOT NULL,
                FOREIGN KEY (media_key) REFERENCES media_items (media_key)
            );
            """
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> StateDB:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def start_run(self) -> int:
        cur = self.conn.execute(
            "INSERT INTO runs (started_at, status) VALUES (?, ?)",
            (_utc_now_iso(), "running"),
        )
        self.conn.commit()
        assert cur.lastrowid is not None
        return int(cur.lastrowid)

    def finish_run(
        self,
        run_id: int,
        *,
        status: str,
        albums_checked: int = 0,
        files_downloaded: int = 0,
        files_skipped: int = 0,
        error_message: str | None = None,
    ) -> None:
        self.conn.execute(
            """
            UPDATE runs
            SET finished_at = ?, status = ?, albums_checked = ?,
                files_downloaded = ?, files_skipped = ?, error_message = ?
            WHERE id = ?
            """,
            (
                _utc_now_iso(),
                status,
                albums_checked,
                files_downloaded,
                files_skipped,
                error_message,
                run_id,
            ),
        )
        self.conn.commit()

    def record_album(
        self,
        *,
        album_key: str,
        url: str,
        class_name: str,
        season_name: str,
        title: str,
        album_date: str,
        item_count: int = 0,
    ) -> None:
        now = _utc_now_iso()
        self.conn.execute(
            """
            INSERT INTO albums (
                album_key, url, class_name, season_name, title,
                album_date, first_seen_at, last_checked_at, item_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(album_key) DO UPDATE SET
                url = excluded.url,
                class_name = excluded.class_name,
                season_name = excluded.season_name,
                title = excluded.title,
                album_date = excluded.album_date,
                last_checked_at = excluded.last_checked_at,
                item_count = excluded.item_count
            """,
            (album_key, url, class_name, season_name, title, album_date, now, now, item_count),
        )
        self.conn.commit()

    def has_media(self, media_key: str, *, verify_exists: bool = False) -> bool:
        row = self.conn.execute(
            "SELECT local_path FROM media_items WHERE media_key = ?",
            (media_key,),
        ).fetchone()
        if row is None:
            return False
        if verify_exists:
            return Path(row["local_path"]).exists()
        return True

    def record_media(
        self,
        *,
        media_key: str,
        album_key: str,
        media_url: str,
        filename: str,
        media_type: str,
        album_date: str,
        album_index: int,
        local_path: str,
        file_size: int,
        sha256: str,
        metadata_status: str,
        metadata_applied_at: str | None = None,
    ) -> None:
        now = _utc_now_iso()
        self.conn.execute(
            """
            INSERT INTO media_items (
                media_key, album_key, media_url, filename, media_type,
                album_date, album_index, local_path, file_size, sha256,
                downloaded_at, metadata_status, metadata_applied_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(media_key) DO UPDATE SET
                media_url = excluded.media_url,
                filename = excluded.filename,
                media_type = excluded.media_type,
                album_date = excluded.album_date,
                album_index = excluded.album_index,
                local_path = excluded.local_path,
                file_size = excluded.file_size,
                sha256 = excluded.sha256,
                downloaded_at = excluded.downloaded_at,
                metadata_status = excluded.metadata_status,
                metadata_applied_at = excluded.metadata_applied_at
            """,
            (
                media_key,
                album_key,
                media_url,
                filename,
                media_type,
                album_date,
                album_index,
                local_path,
                file_size,
                sha256,
                now,
                metadata_status,
                metadata_applied_at or (now if metadata_status == "ok" else None),
            ),
        )
        self.conn.commit()

    def list_albums(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM albums ORDER BY album_date DESC, album_key ASC"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_stats(self) -> dict:
        albums_count = self.conn.execute("SELECT COUNT(*) FROM albums").fetchone()[0]
        media_count = self.conn.execute("SELECT COUNT(*) FROM media_items").fetchone()[0]
        last_run = self.conn.execute(
            "SELECT * FROM runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return {
            "albums_count": albums_count,
            "media_count": media_count,
            "last_run": dict(last_run) if last_run else None,
        }

    def get_gphotos_album(self, album_title: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM gphotos_albums WHERE album_title = ?",
            (album_title,),
        ).fetchone()
        return dict(row) if row else None

    def record_gphotos_album(
        self,
        *,
        album_title: str,
        gphotos_album_id: str,
        product_url: str | None = None,
    ) -> None:
        now = _utc_now_iso()
        self.conn.execute(
            """
            INSERT INTO gphotos_albums (album_title, gphotos_album_id, product_url, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(album_title) DO UPDATE SET
                gphotos_album_id = excluded.gphotos_album_id,
                product_url = COALESCE(excluded.product_url, gphotos_albums.product_url)
            """,
            (album_title, gphotos_album_id, product_url, now),
        )
        self.conn.commit()

    def has_gphotos_upload(self, media_key: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM gphotos_uploads WHERE media_key = ?",
            (media_key,),
        ).fetchone()
        return row is not None

    def record_gphotos_upload(
        self,
        *,
        media_key: str,
        gphotos_media_id: str,
        gphotos_album_id: str | None,
        album_title: str | None,
        product_url: str | None = None,
    ) -> None:
        now = _utc_now_iso()
        self.conn.execute(
            """
            INSERT INTO gphotos_uploads (
                media_key, gphotos_media_id, gphotos_album_id, album_title, product_url, uploaded_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(media_key) DO UPDATE SET
                gphotos_media_id = excluded.gphotos_media_id,
                gphotos_album_id = excluded.gphotos_album_id,
                album_title = excluded.album_title,
                product_url = excluded.product_url,
                uploaded_at = excluded.uploaded_at
            """,
            (media_key, gphotos_media_id, gphotos_album_id, album_title, product_url, now),
        )
        self.conn.commit()

    def list_pending_gphotos_media(
        self,
        *,
        class_name: str | None = None,
        season_name: str | None = None,
        day: str | None = None,
    ) -> list[dict]:
        clauses = ["g.media_key IS NULL"]
        params: list[str] = []
        if class_name is not None:
            clauses.append("a.class_name = ?")
            params.append(class_name)
        if season_name is not None:
            clauses.append("a.season_name = ?")
            params.append(season_name)
        if day is not None:
            clauses.append("m.album_date = ?")
            params.append(day)

        where_sql = " AND ".join(clauses)
        rows = self.conn.execute(
            f"""
            SELECT
                m.media_key,
                m.album_key,
                m.filename,
                m.media_type,
                m.album_date,
                m.album_index,
                m.local_path,
                m.file_size,
                m.sha256,
                a.class_name,
                a.season_name
            FROM media_items m
            JOIN albums a ON a.album_key = m.album_key
            LEFT JOIN gphotos_uploads g ON g.media_key = m.media_key
            WHERE {where_sql}
            ORDER BY m.album_date ASC, m.album_index ASC, m.media_key ASC
            """,
            params,
        ).fetchall()
        return [dict(r) for r in rows]

    def get_gphotos_stats(self) -> dict:
        albums_count = self.conn.execute("SELECT COUNT(*) FROM gphotos_albums").fetchone()[0]
        uploaded_count = self.conn.execute("SELECT COUNT(*) FROM gphotos_uploads").fetchone()[0]
        pending_count = self.conn.execute(
            """
            SELECT COUNT(*)
            FROM media_items m
            LEFT JOIN gphotos_uploads g ON g.media_key = m.media_key
            WHERE g.media_key IS NULL
            """
        ).fetchone()[0]
        return {
            "albums_count": albums_count,
            "uploaded_count": uploaded_count,
            "pending_count": pending_count,
        }

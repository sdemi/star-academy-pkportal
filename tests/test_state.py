"""Unit tests for SQLite state tracking."""

from __future__ import annotations

from pathlib import Path

from pkportal.state import StateDB


def test_state_db_albums_media_and_late_additions(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    photo_file = tmp_path / "photo1.jpg"
    photo_file.write_bytes(b"fake")

    with StateDB(db_path) as db:
        run_id = db.start_run()
        db.record_album(
            album_key="album-2026-11-18",
            url="https://staracademyboston.com/pkportal/album/1",
            class_name="Pre-K Dolphins",
            season_name="Fall 2026",
            title="Morning Art",
            album_date="2026-11-18",
            item_count=1,
        )

        assert db.has_media("media-1") is False

        db.record_media(
            media_key="media-1",
            album_key="album-2026-11-18",
            media_url="https://staracademyboston.com/wp-content/uploads/p1.jpg",
            filename="p1.jpg",
            media_type="photo",
            album_date="2026-11-18",
            album_index=0,
            local_path=str(photo_file),
            file_size=4,
            sha256="abc",
            metadata_status="ok",
        )

        assert db.has_media("media-1") is True
        assert db.has_media("media-1", verify_exists=True) is True
        assert db.has_media("media-2") is False

        # Simulate removing local file and checking verify_exists=True
        photo_file.unlink()
        assert db.has_media("media-1", verify_exists=False) is True
        assert db.has_media("media-1", verify_exists=True) is False

        # Simulate late upload to the same album
        db.record_album(
            album_key="album-2026-11-18",
            url="https://staracademyboston.com/pkportal/album/1",
            class_name="Pre-K Dolphins",
            season_name="Fall 2026",
            title="Morning Art",
            album_date="2026-11-18",
            item_count=2,
        )
        db.record_media(
            media_key="media-2",
            album_key="album-2026-11-18",
            media_url="https://staracademyboston.com/wp-content/uploads/p2.jpg",
            filename="p2.jpg",
            media_type="photo",
            album_date="2026-11-18",
            album_index=1,
            local_path=str(tmp_path / "photo2.jpg"),
            file_size=10,
            sha256="def",
            metadata_status="ok",
        )

        db.finish_run(run_id, status="ok", albums_checked=1, files_downloaded=2, files_skipped=0)
        stats = db.get_stats()
        assert stats["albums_count"] == 1
        assert stats["media_count"] == 2
        assert stats["last_run"]["status"] == "ok"
        assert stats["last_run"]["files_downloaded"] == 2
        assert len(db.list_albums()) == 1


def test_state_db_gphotos_tracking_and_filters(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"

    with StateDB(db_path) as db:
        db.record_album(
            album_key="sp:TD:2026-11-16",
            url="https://example.com/1",
            class_name="Group D",
            season_name="Fall 2026",
            title="2026-11-16",
            album_date="2026-11-16",
            item_count=2,
        )
        db.record_album(
            album_key="sp:TC:2026-11-17",
            url="https://example.com/2",
            class_name="Group C",
            season_name="Winter 2026-2027",
            title="2026-11-17",
            album_date="2026-11-17",
            item_count=1,
        )

        db.record_media(
            media_key="sp:TD:102",
            album_key="sp:TD:2026-11-16",
            media_url="https://example.com/m/102",
            filename="b.jpg",
            media_type="photo",
            album_date="2026-11-16",
            album_index=1,
            local_path=str(tmp_path / "b.jpg"),
            file_size=10,
            sha256="s2",
            metadata_status="ok",
        )
        db.record_media(
            media_key="sp:TD:101",
            album_key="sp:TD:2026-11-16",
            media_url="https://example.com/m/101",
            filename="a.jpg",
            media_type="photo",
            album_date="2026-11-16",
            album_index=0,
            local_path=str(tmp_path / "a.jpg"),
            file_size=10,
            sha256="s1",
            metadata_status="ok",
        )
        db.record_media(
            media_key="sp:TC:201",
            album_key="sp:TC:2026-11-17",
            media_url="https://example.com/m/201",
            filename="c.jpg",
            media_type="photo",
            album_date="2026-11-17",
            album_index=0,
            local_path=str(tmp_path / "c.jpg"),
            file_size=10,
            sha256="s3",
            metadata_status="ok",
        )

        assert db.get_gphotos_album("Group D - Fall 2026") is None
        db.record_gphotos_album(
            album_title="Group D - Fall 2026",
            gphotos_album_id="gp-alb-1",
            product_url="https://photos.google.com/album/gp-alb-1",
        )
        cached = db.get_gphotos_album("Group D - Fall 2026")
        assert cached is not None
        assert cached["gphotos_album_id"] == "gp-alb-1"

        all_pending = db.list_pending_gphotos_media()
        assert [row["media_key"] for row in all_pending] == ["sp:TD:101", "sp:TD:102", "sp:TC:201"]

        assert [row["media_key"] for row in db.list_pending_gphotos_media(class_name="Group D")] == [
            "sp:TD:101",
            "sp:TD:102",
        ]
        assert [row["media_key"] for row in db.list_pending_gphotos_media(season_name="Winter 2026-2027")] == [
            "sp:TC:201",
        ]
        assert [row["media_key"] for row in db.list_pending_gphotos_media(day="2026-11-16")] == [
            "sp:TD:101",
            "sp:TD:102",
        ]

        assert db.has_gphotos_upload("sp:TD:101") is False
        db.record_gphotos_upload(
            media_key="sp:TD:101",
            gphotos_media_id="gp-media-101",
            gphotos_album_id="gp-alb-1",
            album_title="Group D - Fall 2026",
            product_url="https://photos.google.com/photo/gp-media-101",
        )
        assert db.has_gphotos_upload("sp:TD:101") is True

        gp_stats = db.get_gphotos_stats()
        assert gp_stats == {"albums_count": 1, "uploaded_count": 1, "pending_count": 2}
        assert [row["media_key"] for row in db.list_pending_gphotos_media()] == ["sp:TD:102", "sp:TC:201"]

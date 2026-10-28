"""qB 原生状态、最终文件路径、实际入队和重启去重。"""

import importlib
import logging
import threading
from datetime import datetime, timedelta
from pathlib import Path
from queue import Queue
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from video_organizer.core.downloader.qbittorrent_monitor import QBittorrentMonitor
from video_organizer.core.file_handler.handler import VideoFileHandler
from video_organizer.core.monitor.monitor import FileSystemMonitor
from video_organizer.database import session, operations
from video_organizer.database.task_tracking import (
    get_file_task,
    prepare_task,
    recover_interrupted,
)


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(session, "_engine", None)
    monkeypatch.setattr(session, "_SessionLocal", None)
    session.init_db(str(tmp_path / "tasks.db"))
    yield
    session._engine.dispose()


@pytest.fixture
def qb(tmp_path):
    monitor = QBittorrentMonitor(Mock(return_value=True))
    monitor.torrent = {
        "hash": "abc",
        "state": "stalledUP",
        "progress": 1,
        "amount_left": 0,
        "save_path": str(tmp_path),
    }
    monitor.file = {
        "index": 0,
        "name": "Film.mkv",
        "priority": 1,
        "progress": 1,
        "size": 5,
    }
    (tmp_path / "Film.mkv").write_bytes(b"video")
    monitor._get_completed_torrents = Mock(side_effect=lambda: [monitor.torrent])
    monitor._get_torrent_files = Mock(side_effect=lambda _: [monitor.file])
    response = Mock(status_code=200)
    response.json.side_effect = lambda: [monitor.torrent]
    monitor.session.get = Mock(return_value=response)
    return monitor


@pytest.fixture
def handler():
    h = object.__new__(VideoFileHandler)
    h._queue_lock = threading.Lock()
    h._queued_files, h._processing_files, h._uploading_files = set(), set(), set()
    h._uploaded_files, h._failed_files, h._file_downloader_map = set(), {}, {}
    h._upload_queue, h._use_queue = Queue(), True
    h._parent_monitor = SimpleNamespace(_pending_files=set(), _retry_files=set())
    h._runtime_manager = None
    h.upload_targets = ["emos"]
    h.logger = logging.getLogger(__name__)
    return h


@pytest.mark.parametrize(
    "state",
    [
        "uploading",
        "stalledUP",
        "forcedUP",
        "queuedUP",
        "pausedUP",
        "stoppedUP",
    ],
)
def test_qb4_and_qb5_ready_states(qb, state):
    qb.torrent["state"] = state
    qb._poll_once()
    qb.callback.assert_called_once_with(
        str(Path(qb.torrent["save_path"]) / "Film.mkv"), downloader_monitor=qb
    )


@pytest.mark.parametrize(
    "state",
    [
        "checkingUP",
        "checkingDL",
        "checkingResumeData",
        "moving",
        "downloading",
        "metaDL",
        "forcedMetaDL",
        "pausedDL",
        "stoppedDL",
        "queuedDL",
        "stalledDL",
        "forcedDL",
        "error",
        "missingFiles",
        "unknown",
        "newFutureState",
        None,
    ],
)
def test_100_percent_is_insufficient(qb, state):
    qb.torrent["state"] = state
    qb._poll_once()
    qb.callback.assert_not_called()


@pytest.mark.parametrize(
    "field,value",
    [
        ("progress", 0.999),
        ("amount_left", 1),
        ("progress", None),
        ("amount_left", None),
    ],
)
def test_total_completion_required(qb, field, value):
    qb.torrent[field] = value
    qb._poll_once()
    qb.callback.assert_not_called()


def test_tracker_error_does_not_block_completed_torrent(qb):
    qb.torrent["tracker_status"] = "error"
    qb._poll_once()
    qb.callback.assert_called_once()
    assert not any("trackers" in str(call) for call in qb.session.get.call_args_list)


@pytest.mark.parametrize(
    "field,value", [("progress", 0.5), ("priority", 0), ("size", 9), ("size", 0)]
)
def test_only_selected_complete_matching_files(qb, field, value):
    qb.file[field] = value
    qb._poll_once()
    qb.callback.assert_not_called()


def test_qb_logical_filename_waits_for_physical_rename(qb):
    path = Path(qb.torrent["save_path"]) / "Film.mkv"
    temporary = path.with_name(path.name + ".!qB")
    path.rename(temporary)
    qb._poll_once()
    qb.callback.assert_not_called()
    temporary.rename(path)
    qb._poll_once()
    qb.callback.assert_called_once()


def test_multi_file_incomplete_and_deselected_do_not_hide_ready_file(qb):
    files = [
        dict(qb.file, index=1, name="waiting.mkv", progress=0.3),
        dict(qb.file, index=2, name="skipped.mkv", priority=0),
        qb.file,
    ]
    qb._get_torrent_files.side_effect = lambda _: files
    qb._poll_once()
    qb.callback.assert_called_once()
    files[0]["progress"] = 1
    (Path(qb.torrent["save_path"]) / "waiting.mkv").write_bytes(b"video")
    qb._poll_once()
    assert qb.callback.call_count == 3


def test_api_failure_and_callback_rejection_are_revisited(qb):
    qb._get_torrent_files.side_effect = [RuntimeError("offline"), [qb.file], [qb.file]]
    qb._poll_once()
    qb.callback.assert_not_called()
    qb.callback.return_value = False
    qb._poll_once()
    qb.callback.return_value = True
    qb._poll_once()
    assert qb.callback.call_count == 2


def test_one_torrent_failure_does_not_block_next(qb):
    qb._get_completed_torrents.side_effect = lambda: [
        qb.torrent,
        dict(qb.torrent, hash="def"),
    ]
    qb._get_torrent_files.side_effect = [RuntimeError("offline"), [qb.file]]
    qb._poll_once()
    qb.callback.assert_called_once()


def test_exactly_one_mapping_no_url_decoding(qb, tmp_path):
    qb.torrent["save_path"] = "/remote"
    qb.file["name"] = "Literal%20Name.mkv"
    (tmp_path / qb.file["name"]).write_bytes(b"video")
    qb.path_mappings = {"/remote": str(tmp_path), str(tmp_path): "/wrong"}
    qb._poll_once()
    path = str(tmp_path / qb.file["name"])
    qb.callback.assert_called_once_with(path, downloader_monitor=qb)
    assert qb._apply_path_mapping("/remote-other/file.mkv") == "/remote-other/file.mkv"


def test_linux_case_sensitive_filenames_both_processed(qb):
    (Path(qb.torrent["save_path"]) / "film.mkv").write_bytes(b"video")
    qb._get_torrent_files.side_effect = lambda _: [
        qb.file,
        dict(qb.file, index=1, name="film.mkv"),
    ]
    qb._poll_once()
    assert qb.callback.call_count == 2


def bind(qb, handler):
    parent = object.__new__(FileSystemMonitor)
    parent.event_handler = handler
    qb.callback = parent._on_download_completed_snapshot
    return str(Path(qb.torrent["save_path"]) / qb.file["name"])


def finish(handler, path):
    task = handler._upload_queue.get_nowait()[2]
    operations.record_task(path, "completed", task_id=task)
    handler._queued_files.clear()
    handler._processing_files.clear()
    return task


def test_real_queue_acceptance_and_repeated_scans(db, qb, handler):
    path = bind(qb, handler)
    qb._poll_once()
    qb._poll_once()
    assert handler._upload_queue.qsize() == 1
    assert get_file_task(path, ["emos"])["status"] == "queued"
    finish(handler, path)
    qb._poll_once()
    assert handler._upload_queue.empty()
    # 新实例/内存完成记录为空仍由 DB 去重。
    handler._uploaded_files.clear()
    qb._file_sources.clear()
    qb._poll_once()
    assert handler._upload_queue.empty()


def test_queue_failure_does_not_poison_acceptance(db, qb, handler, monkeypatch):
    path = bind(qb, handler)
    original = handler._upload_queue.put_nowait
    monkeypatch.setattr(
        handler._upload_queue, "put_nowait", Mock(side_effect=RuntimeError("full"))
    )
    qb._poll_once()
    assert not handler._queued_files and not handler._processing_files
    assert get_file_task(path, ["emos"])["status"] == "queued"
    monkeypatch.setattr(handler._upload_queue, "put_nowait", original)
    qb._poll_once()
    assert handler._upload_queue.qsize() == 1


def test_database_failure_does_not_poison_acceptance(db, qb, handler, monkeypatch):
    bind(qb, handler)
    module = importlib.import_module(VideoFileHandler.__module__)
    original = module.prepare_task
    monkeypatch.setattr(
        module, "prepare_task", Mock(side_effect=RuntimeError("db locked"))
    )
    qb._poll_once()
    assert not handler._queued_files and handler._upload_queue.empty()
    monkeypatch.setattr(module, "prepare_task", original)
    qb._poll_once()
    assert handler._upload_queue.qsize() == 1


def test_worker_rechecks_native_state_and_requeues_without_fake_failure(
    db, qb, handler
):
    path = bind(qb, handler)
    qb._poll_once()
    qb.torrent["state"] = "checkingUP"
    task = handler._upload_queue.get_nowait()[2]
    assert handler._process_file_internal(path, tracking_task_id=task) is False
    assert get_file_task(path, ["emos"])["status"] == "queued"
    assert get_file_task(path, ["emos"])["attempt_count"] == 0
    handler._queued_files.clear()
    handler._processing_files.clear()
    qb._poll_once()
    assert handler._upload_queue.empty()
    qb.torrent["state"] = "stoppedUP"
    qb._poll_once()
    assert handler._upload_queue.qsize() == 1


def test_changed_file_at_same_path_is_new_task(db, qb, handler):
    path = bind(qb, handler)
    qb._poll_once()
    first = finish(handler, path)
    Path(path).write_bytes(b"replaced")
    qb.file["size"] = 8
    qb._poll_once()
    assert handler._upload_queue.get_nowait()[2] != first


def test_restart_interrupted_task_resumes_only_when_ready(db, qb, handler):
    path = bind(qb, handler)
    task = prepare_task(path, ["emos"], status="uploading")
    with session.get_session_local()() as conn:
        recover_interrupted(conn)
    qb.torrent["state"] = "moving"
    qb._poll_once()
    assert handler._upload_queue.empty()
    qb.torrent["state"] = "queuedUP"
    qb._poll_once()
    assert handler._upload_queue.get_nowait()[2] == task


def test_recognition_failure_not_retried_each_poll(db, qb, handler):
    path = bind(qb, handler)
    task = prepare_task(path, ["emos"])
    operations.record_task(
        path, "failed", task_id=task, error_message="未找到 TMDB 匹配结果"
    )
    qb._poll_once()
    assert handler._upload_queue.empty()


def test_transient_failure_obeys_retry_interval_and_state(db, qb, handler):
    path = bind(qb, handler)
    task = prepare_task(path, ["emos"])
    operations.record_task(
        path,
        "failed",
        task_id=task,
        error_message="超时",
        end_time=datetime.now() - timedelta(seconds=61),
    )
    handler._parent_monitor._retry_files.add(path)
    qb.torrent["state"] = "checkingResumeData"
    qb._poll_once()
    assert handler._upload_queue.empty()
    qb.torrent["state"] = "stalledUP"
    qb._poll_once()
    assert handler._upload_queue.get_nowait()[2] == task
    assert path not in handler._parent_monitor._retry_files


def test_failed_login_does_not_disable_monitor(monkeypatch):
    monitor = QBittorrentMonitor(Mock())
    monkeypatch.setattr(monitor, "is_connected", Mock(return_value=False))
    monkeypatch.setattr(monitor, "_login", Mock(return_value=False))
    monitor.start()
    assert monitor.running
    monitor.stop()
    assert not monitor.monitor_thread.is_alive()


def test_persistent_retry_survives_lost_memory_and_respects_delay(db, qb, handler):
    path = bind(qb, handler)
    task = prepare_task(path, ["emos"])
    operations.record_task(
        path,
        "failed",
        task_id=task,
        auto_retry=True,
        error_message="上传超时",
        end_time=datetime.now(),
    )
    assert not handler._parent_monitor._retry_files
    qb._poll_once()
    assert handler._upload_queue.empty()
    operations.record_task(
        path,
        "failed",
        task_id=task,
        auto_retry=True,
        error_message="上传超时",
        end_time=datetime.now() - timedelta(seconds=61),
    )
    qb._poll_once()
    assert handler._upload_queue.get_nowait()[2] == task


def test_old_database_upgrade_is_idempotent_and_preserves_rows(tmp_path, monkeypatch):
    import sqlite3

    path = tmp_path / "old.db"
    monkeypatch.setattr(session, "_engine", None)
    monkeypatch.setattr(session, "_SessionLocal", None)
    session.init_db(str(path))
    session._engine.dispose()
    conn = sqlite3.connect(path)
    conn.execute("ALTER TABLE upload_task DROP COLUMN auto_retry")
    conn.execute(
        "INSERT INTO upload_task(identity,file_path,targets,status,attempt_count,failure_count,cleared) VALUES('old','/old.mkv','emos','completed',1,0,0)"
    )
    conn.commit()
    session.init_db(str(path))
    assert conn.execute(
        "SELECT identity,status,auto_retry FROM upload_task"
    ).fetchall() == [("old", "completed", 0)]
    session._engine.dispose()
    session.init_db(str(path))
    assert conn.execute("SELECT count(*) FROM upload_task").fetchone() == (1,)
    assert conn.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    conn.close()
    session._engine.dispose()


@pytest.mark.parametrize("update", [{"state": "error"}, {"state": "missingFiles"}])
def test_actual_torrent_errors_wait_and_recover(qb, update):
    qb.torrent.update(update)
    qb._poll_once()
    qb.callback.assert_not_called()
    qb.torrent["state"] = "stalledUP"
    qb._poll_once()
    qb.callback.assert_called_once()


def test_start_version_reports_actual_api_string(qb):
    qb.session.get.return_value.text = "v5.1.2"
    assert qb.is_connected()
    assert qb.app_version == "v5.1.2"


def test_worker_api_error_and_path_change_wait(qb):
    qb._poll_once()
    path = str(Path(qb.torrent["save_path"]) / qb.file["name"])
    assert qb.is_file_ready(path)
    qb.file["name"] = "renamed.mkv"
    assert not qb.is_file_ready(path)
    qb.session.get.side_effect = RuntimeError("offline")
    assert not qb.is_file_ready(path)


def test_one_file_callback_error_does_not_block_same_torrent(qb):
    (Path(qb.torrent["save_path"]) / "second.mkv").write_bytes(b"video")
    qb._get_torrent_files.side_effect = lambda _: [
        qb.file,
        dict(qb.file, index=1, name="second.mkv"),
    ]
    qb.callback.side_effect = [RuntimeError("first file rejected"), True]
    qb._poll_once()
    assert qb.callback.call_count == 2


def test_queued_provenance_retains_accepted_mapping_on_config_change(qb):
    qb._poll_once()
    path = str(Path(qb.torrent["save_path"]) / qb.file["name"])
    replacement = QBittorrentMonitor(Mock())
    replacement._file_sources = qb._file_sources
    replacement.path_mappings = {qb.torrent["save_path"]: "/different"}
    replacement.session = qb.session
    replacement._get_torrent_files = qb._get_torrent_files
    assert replacement.is_file_ready(path)


def test_success_clears_persistent_automatic_retry(db, qb, handler):
    path = bind(qb, handler)
    task = prepare_task(path, ["emos"])
    operations.record_task(
        path,
        "failed",
        task_id=task,
        auto_retry=True,
        error_message="超时",
        end_time=datetime.now() - timedelta(seconds=61),
    )
    qb._poll_once()
    finish(handler, path)
    task = get_file_task(path, ["emos"])
    assert not task["auto_retry"] and task["resolved"]
    qb._poll_once()
    assert handler._upload_queue.empty()

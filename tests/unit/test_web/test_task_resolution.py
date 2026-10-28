"""真实 SQLite、API 和队列边界验证任务合并与解决状态。"""

import importlib
import logging
import sqlite3
import threading
from queue import Queue
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from video_organizer.database import session, operations as ops
from video_organizer.database.models import TaskHistory, UploadTask
from video_organizer.database.task_tracking import (
    prepare_task,
    get_attempts,
    migrate_history,
    recover_interrupted,
    set_task_status,
)
from video_organizer.core.file_handler.handler import VideoFileHandler


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(session, "_engine", None)
    monkeypatch.setattr(session, "_SessionLocal", None)
    session.init_db(str(tmp_path / "tasks.db"))
    yield
    session._engine.dispose()


@pytest.fixture
def file(tmp_path):
    file = tmp_path / "episode.mkv"
    file.write_bytes(b"video")
    return str(file)


def result(file, task, status="failed", error="timeout"):
    assert ops.record_task(
        file, status, error_message=error if status == "failed" else None, task_id=task
    )


def test_repeated_failure_is_one_task_with_all_attempts(db, file):
    task = prepare_task(file, ["emos"])
    for i in range(3):
        assert prepare_task(file, ["emos"]) == task
        result(file, task, error=f"error {i}")
    tasks, total = ops.get_tasks_paginated("failed")
    assert total == 1
    assert tasks[0]["attempt_count"] == tasks[0]["failure_count"] == 3
    assert tasks[0]["error"] == "error 2"
    assert [a["error"] for a in get_attempts(task)["attempts"]] == [
        "error 0",
        "error 1",
        "error 2",
    ]
    assert ops.get_task_counts() == {"completed": 0, "failed": 1, "resolved": 0}


def test_resolution_waits_for_final_success_and_survives_reload(db, file):
    task = prepare_task(file, ["emos"])
    result(file, task)
    prepare_task(file, ["emos"], task, status="queued")
    assert not get_attempts(task)["task"]["resolved"]
    assert ops.get_task_counts()["failed"] == 1
    set_task_status(task, "uploading")
    assert get_attempts(task)["task"]["status"] == "uploading"
    result(file, task, "completed")
    assert ops.get_task_counts() == {"completed": 1, "failed": 0, "resolved": 1}
    tasks, total = ops.get_tasks_paginated("resolved")
    assert total == 1 and tasks[0]["resolved"] and tasks[0]["attempt_count"] == 2
    with session.get_session_local()() as conn:
        migrate_history(conn)
    assert len(get_attempts(task)["attempts"]) == 2
    assert ops.get_failed_tasks() == {}
    assert ops.get_recent_activity_paginated()[0][0]["resolved"]


def test_success_without_failure_not_resolved(db, file):
    task = prepare_task(file, ["emos"])
    result(file, task, "completed")
    assert ops.get_tasks_paginated("resolved")[1] == 0


def test_identity_separates_targets_versions_and_directories(db, file, tmp_path):
    first = prepare_task(file, ["emos"])
    other_target = prepare_task(file, ["p123"])
    duplicate_name = tmp_path / "other" / "episode.mkv"
    duplicate_name.parent.mkdir()
    duplicate_name.write_bytes(b"video")
    other_path = prepare_task(str(duplicate_name), ["emos"])
    from pathlib import Path

    Path(file).write_bytes(b"new content")
    replacement = prepare_task(file, ["emos"])
    assert len({first, other_target, other_path, replacement}) == 4
    with pytest.raises(ValueError, match="文件已经变化"):
        prepare_task(file, ["emos"], first)
    with pytest.raises(ValueError, match="上传目标"):
        prepare_task(file, ["emos"], other_target)


def test_target_order_is_not_new_task(db, file):
    assert prepare_task(file, ["p123", "emos"]) == prepare_task(file, ["emos", "p123"])


def test_clear_preserves_history_and_does_not_resolve(db, file):
    task = prepare_task(file, ["emos"])
    result(file, task)
    assert ops.delete_failed_task(file, task)
    assert ops.get_task_counts() == {"completed": 0, "failed": 0, "resolved": 0}
    assert len(get_attempts(task)["attempts"]) == 1
    prepare_task(file, ["emos"])
    result(file, task)
    assert ops.get_task_counts()["failed"] == 1
    assert get_attempts(task)["task"]["attempt_count"] == 2


def test_restart_marks_interrupted_retry_failed(db, file):
    task = prepare_task(file, ["emos"])
    result(file, task)
    prepare_task(file, ["emos"], task, status="queued")
    with session.get_session_local()() as conn:
        recover_interrupted(conn)
        recover_interrupted(conn)
    detail = get_attempts(task)
    assert detail["task"]["status"] == "failed" and not detail["task"]["resolved"]
    assert len(detail["attempts"]) == 2
    assert "服务重启" in detail["attempts"][-1]["error"]


def test_migration_preserves_raw_rows_and_binds_explicit_legacy_retry(db, file):
    with session.get_session_local()() as conn:
        conn.add_all(
            [
                TaskHistory(file_path=file, status="failed", error_message="first"),
                TaskHistory(file_path=file, status="failed", error_message="second"),
            ]
        )
        conn.commit()
        migrate_history(conn)
        migrate_history(conn)
        assert conn.query(TaskHistory).count() == 2
    tasks, total = ops.get_tasks_paginated("failed")
    assert total == 1 and tasks[0]["targets"] == "unknown"
    task = prepare_task(file, ["emos"], tasks[0]["id"])
    result(file, task, "completed")
    assert len(get_attempts(task)["attempts"]) == 3
    assert ops.get_task_counts()["failed"] == 0
    assert get_attempts(task)["task"]["targets"] == "emos"


def test_migration_handles_historical_success_after_failure(db, file):
    with session.get_session_local()() as conn:
        conn.add(TaskHistory(file_path=file, status="failed"))
        conn.flush()
        conn.add(TaskHistory(file_path=file, status="completed"))
        conn.commit()
        migrate_history(conn)
    assert ops.get_task_counts() == {"completed": 1, "failed": 0, "resolved": 1}


def test_missing_file_does_not_erase_failure(db, file):
    task = prepare_task(file, ["emos"])
    result(file, task)
    from pathlib import Path

    Path(file).unlink()
    with pytest.raises(OSError):
        prepare_task(file, ["emos"], task)
    assert get_attempts(task)["task"]["status"] == "failed"


def handler():
    h = VideoFileHandler.__new__(VideoFileHandler)
    h.logger = logging.getLogger("test")
    h.upload_targets = ["emos"]
    h._uploaded_files = set()
    h._uploading_files = set()
    h._processing_files = set()
    h._queued_files = set()
    h._failed_files = {}
    h._queue_lock = threading.Lock()
    h._use_queue = True
    h._upload_queue = Queue()
    h.max_upload_workers = 1
    return h


@pytest.fixture
def api(db, monkeypatch):
    module = importlib.import_module("video_organizer.web.routers.tasks")
    state = Mock()
    state.get_video_handler.return_value = handler()
    monkeypatch.setattr(module, "get_state_manager", lambda: state)
    app = FastAPI()
    app.include_router(module.router, prefix="/tasks")
    with TestClient(app) as client:
        yield client, state.get_video_handler.return_value


def test_api_retry_keeps_identity_override_and_rejects_double_submit(api, file):
    client, h = api
    task = prepare_task(
        file, ["emos"], tmdb_override={"tmdb_id": 42, "media_type": "tv"}
    )
    result(file, task)
    payload = {"file_path": file, "task_id": task}
    assert client.post("/tasks/retry", json=payload).status_code == 200
    assert client.post("/tasks/retry", json=payload).status_code == 409
    assert h._upload_queue.get_nowait() == (
        file,
        {"tmdb_id": 42, "media_type": "tv"},
        task,
    )
    assert (
        client.get("/tasks/list?status=failed").json()["items"][0]["status"] == "queued"
    )
    detail = client.get(f"/tasks/history/{task}").json()
    assert len(detail["attempts"]) == 1 and "tmdb_override" not in detail


def test_api_does_not_report_missing_or_wrong_target_accepted(api, file):
    client, h = api
    task = prepare_task(file, ["p123"])
    result(file, task)
    assert (
        client.post(
            "/tasks/retry", json={"file_path": file, "task_id": task}
        ).status_code
        == 409
    )
    assert get_attempts(task)["task"]["status"] == "failed"
    response = client.post("/tasks/retry-all").json()
    assert response["retried_count"] == 0 and response["skipped_count"] == 1
    assert client.get("/tasks/history/999999").status_code == 404


def test_current_summary_not_inflated_by_stale_runtime_sets(db, file):
    from video_organizer.web.services.state import StateManager

    task = prepare_task(file, ["emos"])
    result(file, task)
    result(file, task, "completed")
    state = StateManager()
    state._video_handler = handler()
    state._video_handler._failed_files[file] = "stale failure"
    assert state.get_system_status().failed_count == 0
    assert state.get_system_status().resolved_count == 1
    assert state.get_failed_files() == {}


def test_clear_one_target_does_not_hide_other_target(db, file):
    emos = prepare_task(file, ["emos"])
    other = prepare_task(file, ["p123"])
    result(file, emos)
    result(file, other)
    assert ops.delete_failed_task(file, emos)
    assert ops.get_tasks_paginated("failed")[0][0]["id"] == other
    assert len(get_attempts(emos)["attempts"]) == 1


def test_simultaneous_results_do_not_lose_attempts(db, file):
    from concurrent.futures import ThreadPoolExecutor

    task = prepare_task(file, ["emos"])
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda n: result(file, task, error=str(n)), range(8)))
    assert get_attempts(task)["task"]["attempt_count"] == 8
    assert len(get_attempts(task)["attempts"]) == 8
    assert ops.get_task_counts()["failed"] == 1


def test_snapshot_rejects_changed_target_without_uploading(db, file):
    task = prepare_task(file, ["p123"])
    result(file, task)
    h = handler()
    h._requested_task_id = task
    h._manual_tmdb_override = None
    h.renamer = Mock()
    assert h._process_file_snapshot(file) is False
    h.renamer.extract_metadata.assert_not_called()
    assert get_attempts(task)["task"]["status"] == "failed"
    assert "上传目标已变更" in get_attempts(task)["attempts"][-1]["error"]


def test_actual_legacy_sqlite_schema_upgrade(tmp_path, monkeypatch):
    path = tmp_path / "legacy.db"
    raw = sqlite3.connect(path)
    raw.execute(
        "CREATE TABLE task_history(id INTEGER PRIMARY KEY, file_path TEXT, status TEXT, error_message TEXT, start_time DATETIME, end_time DATETIME, retry_count INTEGER)"
    )
    raw.execute(
        "INSERT INTO task_history VALUES(1,'/old/file.mkv','failed','old timeout',NULL,NULL,0)"
    )
    raw.commit()
    raw.close()
    monkeypatch.setattr(session, "_engine", None)
    monkeypatch.setattr(session, "_SessionLocal", None)
    session.init_db(str(path))
    assert ops.get_tasks_paginated("failed")[1] == 1
    with session.get_session_local()() as conn:
        assert conn.query(TaskHistory).one().error_message == "old timeout"
    session._engine.dispose()


def test_dequeued_task_leaves_waiting_list_while_next_task_stays_queued(db, file):
    from video_organizer.web.services.state import StateManager

    h = handler()
    h._queue_running = True
    h._worker_condition = threading.Condition()
    active, release = threading.Event(), threading.Event()
    pending = file + ".next"
    h._queued_files.update([file, pending])
    h._processing_files.update([file, pending])
    h._upload_queue.put(file)
    h._upload_queue.put(pending)

    def process(*args, **kwargs):
        active.set()
        assert release.wait(5)
        h._queue_running = False

    h._process_file_internal = process
    state = StateManager()
    state._video_handler = h
    worker = threading.Thread(target=h._worker_process_queue, args=(1,))
    worker.start()
    try:
        assert active.wait(5)
        assert state.get_queued_files() == [pending]
        assert state.get_processing_files() == [file]
        status = state.get_system_status()
        assert status.queue_size == status.processing_count == 1
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive()
    assert state.get_processing_files() == []
    assert state.get_queued_files() == [pending]


@pytest.mark.parametrize("force", [False, True])
def test_synchronous_processing_is_never_in_waiting_list(db, file, force):
    h = handler()
    h._use_queue = False
    h._is_file_complete = Mock(return_value=True)
    h._is_subtitle_file = Mock(return_value=False)
    h._is_video_file = Mock(return_value=True)

    def process(path, **kwargs):
        assert path not in h._queued_files
        assert path in h._processing_files
        return True

    h._process_file_internal = process
    assert h.force_process_file(file) if force else h._process_file(file)


@pytest.mark.parametrize(
    "other,expected",
    [
        ("unused", "skipped"),
        ({"skipped": True}, "skipped"),
        ({"fileid": "new"}, "completed"),
        (None, "failed"),
    ],
)
def test_duplicate_result_persists_distinct_outcome(
    db, file, monkeypatch, other, expected
):
    from pathlib import Path
    from video_organizer.web.services.state import StateManager
    import video_organizer.upload.emos_uploader as emos

    h = handler()
    if other != "unused":
        h.upload_targets.append("p123")
    h._tracked_task_id = prepare_task(file, h.upload_targets)
    h.emos_auth_token, h.emos_base_url = "test", "https://example.invalid"
    h.emos_chunk_size_mb, h.emos_max_workers = 100, 3
    h.telegram_config, h.emos_file_storage, h.emos_skip_existing_media = {}, "s3", True
    h.p123_token, h.p123_username = "test", ""
    h.p123_uploader = Mock()
    h.p123_uploader.upload_video.return_value = other
    h.renamer = Mock()
    h.renamer.generate_new_path.return_value = Path("Film/film.mkv")
    h.emya_enabled, h.delete_after_upload, h._parent_monitor = False, False, None
    h._cleanup_old_records = Mock()
    uploader = Mock()
    uploader.upload_video.return_value = {"skipped": True, "media_uuid": "existing"}
    monkeypatch.setattr(emos, "RobustEmosVideoUploader", Mock(return_value=uploader))

    h._execute_upload(file, "ve", "42", 1, 42, "tv", "Film", "S01E01", {})
    detail = get_attempts(h._tracked_task_id)
    assert detail["task"]["status"] == expected
    assert detail["attempts"][0]["status"] == expected
    assert ops.get_tasks_paginated(expected)[1] == 1
    state = StateManager()
    state._video_handler = h
    assert bool(state.get_completed_files()) == (expected == "completed")
    assert bool(state.get_completed_with_time()) == (expected == "completed")
    assert state.get_system_status().completed_count == (expected == "completed")
    assert file not in h._uploading_files
    assert ops.get_recent_activity_paginated()[0][0]["status"] == expected


def test_failure_then_duplicate_is_resolved_without_claiming_uploaded(db, file):
    task = prepare_task(file, ["emos"])
    result(file, task)
    result(file, task, "skipped")
    assert get_attempts(task)["task"]["resolved"]
    assert ops.get_tasks_paginated("resolved")[0][0]["status"] == "skipped"
    assert ops.get_task_counts() == {"completed": 0, "failed": 0, "resolved": 1}
    assert ops.get_failed_tasks() == {}


def test_skipped_result_available_in_paginated_api_and_attempt_history(api, file):
    client, h = api
    task = prepare_task(file, ["emos"])
    result(file, task, "skipped")
    assert (
        client.get("/tasks/list?status=skipped").json()["items"][0]["status"]
        == "skipped"
    )
    assert client.get("/tasks/list?status=completed").json()["total"] == 0
    assert (
        client.get(f"/tasks/history/{task}").json()["attempts"][0]["status"]
        == "skipped"
    )

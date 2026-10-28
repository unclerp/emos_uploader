"""真实 SQLite + qB 原生 API 替身验证整种子门槛、移动确认、重启和独立重试。"""

import copy
import importlib
import logging
import threading
from datetime import datetime, timedelta
from pathlib import Path
from queue import Queue
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from video_organizer.core.downloader.qbittorrent_monitor import QBittorrentMonitor
from video_organizer.core.file_handler.handler import VideoFileHandler
from video_organizer.core.post_upload import (
    QBPostUpload,
    list_actions,
    post_upload_policy,
)
from video_organizer.core.effective_config import normalize_config
from video_organizer.database import session, operations
from video_organizer.database.models import (
    DownloadFileBinding,
    PostUploadAction,
    TaskHistory,
)
from video_organizer.database.task_tracking import (
    get_attempts,
    prepare_task,
    recover_interrupted,
)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(session, "_engine", None)
    monkeypatch.setattr(session, "_SessionLocal", None)
    session.init_db(str(tmp_path / "tasks.db"))
    h = object.__new__(VideoFileHandler)
    h._queue_lock = threading.Lock()
    h._queued_files, h._processing_files, h._uploading_files = set(), set(), set()
    h._uploaded_files, h._failed_files, h._file_downloader_map = set(), {}, {}
    h._upload_queue, h._use_queue, h._parent_monitor = Queue(), True, None
    h.upload_targets, h.max_upload_workers = ["emos"], 1
    h.logger = logging.getLogger(__name__)
    root = tmp_path / "in"
    root.mkdir()
    monitor = QBittorrentMonitor(
        lambda path, downloader_monitor: h.enqueue_qb_file(path, downloader_monitor),
        rpc_url="http://qb/api/v2",
        path_mappings={"/qb/incoming": str(root)},
    )
    torrent = dict(
        hash="a" * 40,
        name="Season",
        state="stalledUP",
        progress=1,
        amount_left=0,
        save_path="/qb/incoming",
    )
    files = [
        dict(index=i, name=f"Season/e{i}.mkv", priority=1, progress=1, size=5)
        for i in range(2)
    ]
    for file in files:
        path = root / file["name"]
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(b"video")
    monitor._get_completed_torrents = Mock(side_effect=lambda: [copy.deepcopy(torrent)])
    monitor._get_torrent_files = Mock(side_effect=lambda _: copy.deepcopy(files))
    monitor.session.post = Mock(return_value=Mock(status_code=200))
    response = Mock(status_code=200)
    response.json.side_effect = lambda: [copy.deepcopy(torrent)]
    monitor.session.get = Mock(return_value=response)
    monitor.post_upload = QBPostUpload(
        monitor,
        h,
        {"post_upload_action": "move", "post_upload_move_path": "/qb/archive"},
    )
    env = SimpleNamespace(h=h, qb=monitor, torrent=torrent, files=files, root=root)
    yield env
    session._engine.dispose()


def accept(env):
    env.qb._poll_once()
    return [env.h._upload_queue.get_nowait() for _ in env.files]


def finish(env, queued, status="completed"):
    path, _, task = queued
    operations.record_task(path, status, task_id=task)
    env.h._processing_files.discard(path)
    env.h._queued_files.discard(path)
    return task


def action():
    return list_actions()[0][0]


def test_move_waits_for_all_selected_videos_and_worker_cleanup(env):
    first, second = accept(env)
    finish(env, first)
    env.qb._poll_once()
    env.qb.session.post.assert_not_called()
    finish(env, second, "skipped")
    env.h._processing_files.add(second[0])
    env.qb._poll_once()
    env.qb.session.post.assert_not_called()
    env.h._processing_files.clear()
    env.qb._poll_once()
    env.qb.session.post.assert_called_once_with(
        "http://qb/api/v2/torrents/setLocation",
        data={"hashes": "a" * 40, "location": "/qb/archive"},
        timeout=30,
    )
    assert action()["status"] == "moving"  # HTTP 200 不是移动完成。
    env.qb._poll_once()
    assert env.qb.session.post.call_count == 1
    assert operations.get_task_counts() == {"completed": 1, "failed": 0, "resolved": 0}


def test_failed_video_blocks_move_and_does_not_hide_upload_failure(env):
    first, second = accept(env)
    finish(env, first)
    task = finish(env, second, "failed")
    env.qb._poll_once()
    assert get_attempts(task)["task"]["status"] == "failed"
    env.qb.session.post.assert_not_called()
    assert action()["status"] == "waiting"


def test_unselected_videos_and_non_video_files_do_not_block(env):
    queued = accept(env)
    for item in queued:
        finish(env, item)
    env.files += [
        dict(index=2, name="missing.mkv", size=100, priority=0, progress=0),
        dict(index=3, name="notes.txt", size=10, priority=1, progress=1),
    ]
    env.qb._poll_once()
    assert action()["status"] == "moving"


def test_selected_but_inaccessible_or_incomplete_video_prevents_whole_torrent_action(
    env,
):
    queued = accept(env)
    for item in queued:
        finish(env, item)
    env.files.append(
        dict(index=2, name="missing.mkv", size=100, priority=1, progress=1)
    )
    env.qb._poll_once()
    env.qb.session.post.assert_not_called()
    env.files.pop()
    env.files[1]["progress"] = 0.99
    env.qb._poll_once()
    env.qb.session.post.assert_not_called()


@pytest.mark.parametrize(
    "state", ["checkingUP", "moving", "error", "missingFiles", "stoppedDL"]
)
def test_native_state_changes_block_starting_action(env, state):
    for queued in accept(env):
        finish(env, queued)
    env.torrent["state"] = state
    env.qb._poll_once()
    env.qb.session.post.assert_not_called()


def test_fresh_preflight_rejects_state_change_after_poll_snapshot(env):
    for queued in accept(env):
        finish(env, queued)
    env.qb.session.get.return_value.json.side_effect = lambda: [
        dict(env.torrent, state="moving")
    ]
    env.qb._poll_once()
    env.qb.session.post.assert_not_called()


def test_move_confirmation_updates_paths_without_rewriting_history_and_survives_restart(
    env,
):
    queued = accept(env)
    for item in queued:
        finish(env, item)
    env.qb._poll_once()
    env.torrent.update(save_path="/qb/archive", state="moving")
    env.qb._poll_once()
    assert action()["status"] == "moving"
    env.torrent["state"] = "stoppedUP"
    env.qb._poll_once()
    assert action()["status"] == "moved"
    assert env.h._upload_queue.empty()
    with session.get_session_local()() as db:
        rows = db.query(DownloadFileBinding).all()
        assert all(row.current_path.startswith("/qb/archive/") for row in rows)
        assert all(row.source_path.startswith("/qb/archive/") for row in rows)
        assert all(
            row.file_path.startswith(str(env.root)) for row in db.query(TaskHistory)
        )
        recover_interrupted(db)
    # 归档目录没有挂入上传容器；清掉内存也不能再次回调上传。
    env.h._uploaded_files.clear()
    env.qb._file_sources.clear()
    env.qb.post_upload = QBPostUpload(env.qb, env.h, {"post_upload_action": "keep"})
    env.qb._poll_once()
    assert env.h._upload_queue.empty()
    assert env.qb.session.post.call_count == 1


@pytest.mark.parametrize("http", [403, 409, 500])
def test_move_failure_retry_only_moves_never_reuploads(env, http):
    queued = accept(env)
    for item in queued:
        finish(env, item)
    env.qb.session.post.return_value.status_code = http
    env.qb._poll_once()
    failed = action()
    assert failed["status"] == "failed"
    assert operations.get_task_counts()["failed"] == 0
    assert all(
        get_attempts(q[2])["task"]["post_upload"]["status"] == "failed" for q in queued
    )
    env.qb._poll_once()
    assert env.qb.session.post.call_count == 1
    env.qb.post_upload.retry(failed["id"])
    env.qb.session.post.return_value.status_code = 200
    env.qb._poll_once()
    assert env.qb.session.post.call_count == 2
    assert env.h._upload_queue.empty()
    assert all(get_attempts(q[2])["task"]["attempt_count"] == 1 for q in queued)
    env.torrent["save_path"] = "/qb/archive"
    env.qb._poll_once()
    assert action()["status"] == "moved"
    assert action()["attempt_count"] == 2


def test_timeout_is_confirmed_from_actual_state_without_resending(env):
    for queued in accept(env):
        finish(env, queued)
    env.qb.session.post.side_effect = TimeoutError
    env.qb._poll_once()
    assert action()["status"] == "moving"
    env.torrent["save_path"] = "/qb/archive"
    env.qb._poll_once()
    assert action()["status"] == "moved"
    assert env.qb.session.post.call_count == 1


def test_unconfirmed_move_becomes_retryable_failure_but_native_moving_keeps_waiting(
    env,
):
    for queued in accept(env):
        finish(env, queued)
    env.qb._poll_once()
    with session.get_session_local()() as db:
        db.query(PostUploadAction).first().requested_at = datetime.now() - timedelta(
            hours=3
        )
        db.commit()
    env.torrent["state"] = "moving"
    env.qb._poll_once()
    assert action()["status"] == "moving"
    env.torrent["state"] = "stalledUP"
    env.qb._poll_once()
    assert action()["status"] == "failed"
    assert "尚未确认" in action()["error"]


def test_busy_operation_blocks_manual_reprocessing(env):
    queued = accept(env)
    for item in queued:
        finish(env, item)
    env.qb._poll_once()
    assert not env.h.force_process_file(queued[0][0])
    assert env.h._upload_queue.empty()


def test_old_completed_files_are_not_retroactively_moved(env):
    for file in env.files:
        path = str(env.root / file["name"])
        task = prepare_task(path, ["emos"])
        operations.record_task(path, "completed", task_id=task)
    env.qb._poll_once()
    assert list_actions()[1] == 0
    assert env.h._upload_queue.empty()
    env.qb.session.post.assert_not_called()


def test_keep_has_no_action_and_delete_is_whole_torrent_and_confirmed(env):
    env.qb.post_upload = QBPostUpload(env.qb, env.h, {"post_upload_action": "keep"})
    queued = accept(env)
    for item in queued:
        finish(env, item)
    env.qb._poll_once()
    assert list_actions()[1] == 0
    env.qb.session.post.assert_not_called()
    # 新种子在删除策略下接收；不按文件调用删除，也不从上传容器 unlink。
    env.torrent["hash"] = "b" * 40
    env.qb.post_upload = QBPostUpload(env.qb, env.h, {"post_upload_action": "delete"})
    for file in env.files:
        path = str(env.root / file["name"])
        env.qb._file_sources[path] = (
            env.torrent["hash"],
            file["index"],
            "/qb/incoming/" + file["name"],
        )
        task = prepare_task(path, ["emos"])
        env.qb.post_upload.bind(path, task, schedule=True)
        operations.record_task(path, "completed", task_id=task)
    env.qb._poll_once()
    assert action()["status"] == "deleting"
    assert env.qb.session.post.call_args.kwargs["data"] == {
        "hashes": "b" * 40,
        "deleteFiles": "true",
    }
    env.qb._get_completed_torrents.return_value = []
    env.qb._get_completed_torrents.side_effect = None
    env.qb._poll_once()
    assert action()["status"] == "deleted"
    assert all((env.root / file["name"]).exists() for file in env.files)


def test_retry_api_and_upload_history_remain_separate(env, monkeypatch):
    queued = accept(env)
    for item in queued:
        finish(env, item)
    env.qb.session.post.return_value.status_code = 403
    env.qb._poll_once()
    module = importlib.import_module("video_organizer.web.routers.tasks")
    monkeypatch.setattr(
        module,
        "get_state_manager",
        lambda: SimpleNamespace(get_downloader_monitors=lambda: [env.qb]),
    )
    app = FastAPI()
    app.include_router(module.router, prefix="/tasks")
    with TestClient(app) as client:
        data = client.get("/tasks/post-upload").json()
        action_id = data["items"][0]["id"]
        assert data["total"] == 1
        assert (
            client.get("/tasks/list?status=completed").json()["items"][0][
                "post_upload"
            ]["status"]
            == "failed"
        )
        assert client.post(f"/tasks/post-upload/{action_id}/retry").status_code == 200
        assert client.post(f"/tasks/post-upload/{action_id}/retry").status_code == 409
        assert client.post("/tasks/post-upload/99999/retry").status_code == 409
        assert env.h._upload_queue.empty()


@pytest.mark.parametrize(
    "legacy,explicit,expected",
    [
        (False, "", "keep"),
        (True, "", "delete"),
        (True, "keep", "keep"),
        (False, "delete", "delete"),
    ],
)
def test_legacy_policy_and_explicit_precedence(legacy, explicit, expected):
    config = normalize_config(
        {"processing": {"delete_after_upload": legacy, "post_upload_action": explicit}}
    )
    assert config["processing"]["post_upload_action"] == expected
    assert config["processing"]["delete_after_upload"] == (expected == "delete")


@pytest.mark.parametrize(
    "path", ["", "relative/folder", "../archive", "/archive\nother"]
)
def test_move_requires_absolute_qb_path(path):
    with pytest.raises(ValueError, match="绝对目标目录"):
        post_upload_policy(
            {"post_upload_action": "move", "post_upload_move_path": path}
        )


@pytest.mark.parametrize(
    "path", ["/qb/archive", "D:\\archive", "\\\\server\\share\\archive"]
)
def test_absolute_remote_path_is_not_checked_in_upload_container(path):
    assert post_upload_policy(
        {"post_upload_action": "move", "post_upload_move_path": path}
    ) == ("move", path)


def test_candidate_generation_cannot_move_until_published_and_uses_lease(env):
    from contextlib import contextmanager

    generation = object()
    entered = []

    @contextmanager
    def lease(gen):
        entered.append(gen)
        try:
            yield
        finally:
            entered.remove(gen)

    manager = SimpleNamespace(lock=threading.RLock(), current=object(), lease=lease)
    env.qb.post_upload.runtime_manager = manager
    env.qb.post_upload.generation = generation
    queued = accept(env)
    for item in queued:
        finish(env, item)
    env.qb._poll_once()
    env.qb.session.post.assert_not_called()
    manager.current = generation
    env.qb.session.post.side_effect = lambda *args, **kwargs: (
        assert_leased(entered, generation)
    )
    env.qb._poll_once()
    assert action()["status"] == "moving"
    assert entered == []


def assert_leased(entered, generation):
    assert entered == [generation]
    return Mock(status_code=200)


def test_failed_policy_and_pending_move_survive_hot_config_target_change(env):
    for item in accept(env):
        finish(env, item)
    env.qb.session.post.return_value.status_code = 403
    env.qb._poll_once()
    failed = action()
    env.h.upload_targets = ["p123"]
    env.qb.post_upload = QBPostUpload(env.qb, env.h, {"post_upload_action": "keep"})
    env.qb.post_upload.retry(failed["id"])
    env.qb.session.post.return_value.status_code = 200
    env.qb._poll_once()
    assert env.qb.session.post.call_args.kwargs["data"]["location"] == "/qb/archive"
    assert action()["status"] == "moving"
    assert env.h._upload_queue.empty()


def test_pending_destination_path_is_reserved_before_qb_move_finishes(env):
    from video_organizer.core.post_upload import path_busy

    for item in accept(env):
        finish(env, item)
    env.qb._poll_once()
    assert path_busy("/qb/archive/Season/e0.mkv")
    assert path_busy(str(env.root / "Season/e0.mkv"))


def test_ini_round_trip_and_invalid_config_save_preserve_previous_policy(
    tmp_path, monkeypatch
):
    from video_organizer.core.config_loader import load_config, update_config
    from video_organizer.web.routers import config as routes

    config_path = tmp_path / "config.ini"
    config_path.write_text(
        "[processing]\ndelete_after_upload = True\n", encoding="utf-8"
    )
    cfg = load_config(config_path)
    assert post_upload_policy(cfg["processing"])[0] == "delete"
    cfg["processing"].update(
        post_upload_action="move", post_upload_move_path="/qb/archive"
    )
    update_config(cfg, str(config_path))
    assert post_upload_policy(load_config(config_path)["processing"]) == (
        "move",
        "/qb/archive",
    )
    original = config_path.read_bytes()
    state = SimpleNamespace(
        get_saved_config=lambda: load_config(config_path),
        get_config_path=lambda: config_path,
        apply_config=Mock(side_effect=AssertionError("无效配置不能应用")),
    )
    monkeypatch.setattr(routes, "get_state_manager", lambda: state)
    app = FastAPI()
    app.include_router(routes.router, prefix="/config")
    with TestClient(app) as client:
        response = client.put(
            "/config/section",
            json={
                "section": "processing",
                "values": {"post_upload_action": "move", "post_upload_move_path": ""},
            },
        )
    assert response.status_code == 422
    assert config_path.read_bytes() == original
    state.apply_config.assert_not_called()


def test_operation_status_change_notifies_live_dashboard(env, monkeypatch):
    state_module = importlib.import_module("video_organizer.web.services.state")
    notify = Mock()
    monkeypatch.setattr(
        state_module,
        "get_state_manager",
        lambda: SimpleNamespace(notify_dashboard_update=notify),
    )
    for item in accept(env):
        finish(env, item)
    notify.reset_mock()
    env.qb._poll_once()
    assert notify.called
    assert action()["status"] == "moving"


def test_other_target_group_must_also_finish_before_physical_torrent_moves(env):
    queued = accept(env)
    for item in queued:
        finish(env, item)
    alternate = QBPostUpload(
        env.qb, SimpleNamespace(upload_targets=["p123"]), {"post_upload_action": "keep"}
    )
    path = queued[0][0]
    task = prepare_task(path, ["p123"], status="queued")
    alternate.bind(path, task)
    env.qb._poll_once()
    env.qb.session.post.assert_not_called()
    operations.record_task(path, "failed", task_id=task)
    env.qb._poll_once()
    env.qb.session.post.assert_not_called()
    operations.record_task(path, "completed", task_id=task)
    env.qb._poll_once()
    assert action()["status"] == "moving"
    env.torrent["save_path"] = "/qb/archive"
    env.qb._poll_once()
    with session.get_session_local()() as db:
        assert all(
            b.current_path.startswith("/qb/archive/")
            for b in db.query(DownloadFileBinding)
        )

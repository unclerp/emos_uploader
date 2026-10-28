"""配置热更新的任务隔离、失败回退、回收和 API 回归。"""

import copy
import logging
import threading
import time
from queue import Queue
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from video_organizer.core.config_loader import (
    DEFAULT_CONFIG,
    update_config,
    load_config,
)
from video_organizer.core.effective_config import normalize_config
from video_organizer.web.services import live_config as live
from video_organizer.web.routers import config as routes


def wait_until(function, timeout=3):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if function():
            return
        time.sleep(0.02)
    assert function()


class Cleaner:
    def __init__(self):
        self._run_lock = threading.Lock()
        self._stop = threading.Event()
        self.is_alive = False
        self.started = 0

    def start(self):
        self.started += 1


@pytest.fixture
def manager(monkeypatch):
    handlers, level = logging.getLogger().handlers[:], logging.getLogger().level
    monkeypatch.setattr(live, "with_llm_snapshot", lambda cfg: copy.deepcopy(cfg))
    root = SimpleNamespace(
        _upload_queue=Queue(),
        _file_downloader_map={},
        resize_workers=MagicMock(),
        delete_after_upload=False,
    )
    state = SimpleNamespace(
        _config=copy.deepcopy(DEFAULT_CONFIG), _config_path=None, _video_handler=root
    )
    manager = live.LiveConfig(state)
    manager.attach(copy.deepcopy(DEFAULT_CONFIG), handler=root, cleaner=Cleaner())
    built = []

    def build(config):
        client = SimpleNamespace(close=MagicMock())
        handler = SimpleNamespace(
            delete_after_upload=config["processing"]["delete_after_upload"],
            renamer=SimpleNamespace(tmdb_client=client),
        )
        result = live.Generation(
            manager.current.version + 1,
            config,
            handler,
            cleaner=Cleaner(),
            log_handlers=[],
        )
        built.append(result)
        return result

    monkeypatch.setattr(manager, "_build", build)
    manager.built = built
    yield manager
    logging.getLogger().handlers, logging.getLogger().level = handlers, level


def test_running_task_keeps_delete_policy_until_it_finishes(manager):
    old = manager.current
    old.handler.renamer = SimpleNamespace(
        tmdb_client=SimpleNamespace(close=MagicMock())
    )
    client = old.handler.renamer.tmdb_client
    config = copy.deepcopy(DEFAULT_CONFIG)
    config["processing"]["delete_after_upload"] = True
    with manager.lease() as active:
        result = manager.apply(config)
        assert result["applied"]
        assert active is old
        assert active.handler.delete_after_upload is False
        assert manager.current.handler.delete_after_upload is True
        assert manager.status()["retiring"][0]["active_tasks"] == 1
        client.close.assert_not_called()
    wait_until(lambda: old.closed)
    client.close.assert_called_once()
    assert manager.status()["retiring"] == []


def test_build_failure_keeps_original_component(manager, monkeypatch):
    old = manager.current
    monkeypatch.setattr(
        manager, "_build", MagicMock(side_effect=RuntimeError("bad connection"))
    )
    config = copy.deepcopy(DEFAULT_CONFIG)
    config["processing"]["max_upload_workers"] = 5
    result = manager.apply(config)
    assert not result["applied"]
    assert manager.current is old
    assert not old.closed
    assert manager.status()["status"] == "failed"


def test_invalid_value_never_builds_new_components(manager):
    config = copy.deepcopy(DEFAULT_CONFIG)
    config["processing"]["delete_after_upload"] = "definitely not a boolean"
    old = manager.current
    result = manager.apply(config)
    assert not result["applied"]
    assert not manager.built
    assert manager.current is old


def test_new_scheduler_does_not_start_when_connection_check_fails(manager, monkeypatch):
    config = copy.deepcopy(DEFAULT_CONFIG)
    config["processing"]["max_upload_workers"] = 5
    candidate = manager._build(config)
    monitor = SimpleNamespace(
        start=MagicMock(), is_connected=lambda: False, stop=MagicMock()
    )
    candidate.monitor = SimpleNamespace(downloader_monitors=[monitor])
    monkeypatch.setattr(manager, "_build", lambda cfg: candidate)
    result = manager.apply(config)
    assert not result["applied"]
    assert candidate.cleaner.started == 0
    wait_until(lambda: candidate.closed)
    monitor.stop.assert_called_once()


def test_late_downloader_callback_cannot_enter_new_generation(manager):
    old = manager.current
    old.monitor = SimpleNamespace(
        _on_download_completed_snapshot=MagicMock(return_value=True),
        downloader_monitors=[],
    )
    callback = manager._callback(old)
    assert callback("before.mkv") is True
    config = copy.deepcopy(DEFAULT_CONFIG)
    config["processing"]["max_upload_workers"] = 2
    manager.apply(config)
    assert callback("after.mkv") is False
    old.monitor._on_download_completed_snapshot.assert_called_once_with("before.mkv")


def test_active_recycle_operation_defers_old_component_cleanup(manager):
    old = manager.current
    old.cleaner._run_lock.acquire()
    config = copy.deepcopy(DEFAULT_CONFIG)
    config["processing"]["max_upload_workers"] = 2
    manager.apply(config)
    assert not old.closed
    old.cleaner._run_lock.release()
    wait_until(lambda: old.closed)


def test_old_tracker_queue_must_drain_before_reclamation(manager):
    release = threading.Event()
    thread = threading.Thread(target=release.wait, daemon=True)
    thread.start()
    old = manager.current
    old.tracker = SimpleNamespace(_thread=thread, stop=MagicMock())
    config = copy.deepcopy(DEFAULT_CONFIG)
    config["processing"]["max_upload_workers"] = 2
    manager.apply(config)
    assert not old.closed
    release.set()
    thread.join()
    wait_until(lambda: old.closed)


def test_background_task_reserves_old_generation_before_thread_start(manager):
    old = manager.current
    started, release = threading.Event(), threading.Event()

    def work():
        started.set()
        assert live.borrowed_generation.get() is old
        release.wait(3)

    manager.spawn(old, work)
    assert started.wait(1)
    config = copy.deepcopy(DEFAULT_CONFIG)
    config["processing"]["max_upload_workers"] = 2
    manager.apply(config)
    assert old.users == 1
    assert not old.closed
    release.set()
    wait_until(lambda: old.closed)


def test_unknown_runtime_fields_are_not_exposed_as_database_source():
    result = normalize_config(
        {
            "processing": {
                "delete_after_upload": "false",
                "max_upload_workers": "4",
                "upload_targets": "emos,p123",
            }
        }
    )
    assert result["processing"] == {
        "delete_after_upload": False,
        "max_upload_workers": 4,
        "upload_targets": "emos,p123",
        "post_upload_action": "keep",
        "post_upload_move_path": "",
    }


@pytest.mark.parametrize(
    "key,value",
    [
        ("max_upload_workers", 0),
        ("max_upload_workers", -1),
        ("upload_targets", "unknown"),
        ("delete_after_upload", "typo"),
    ],
)
def test_invalid_runtime_config_is_rejected(key, value):
    config = copy.deepcopy(DEFAULT_CONFIG)
    config["processing"][key] = value
    with pytest.raises(ValueError):
        normalize_config(config)


def test_invalid_manual_rule_cannot_be_published():
    config = copy.deepcopy(DEFAULT_CONFIG)
    config["manual_rules"] = {
        "enabled": True,
        "rules": [{"rule": "invalid rule syntax"}],
    }
    with pytest.raises(ValueError):
        normalize_config(config)


def test_saved_config_is_deep_copy_not_mutable_live_state():
    from video_organizer.web.services.state import StateManager

    state = object.__new__(StateManager)
    state._initialized = False
    state.__init__()
    state.set_config({"processing": {"delete_after_upload": False}})
    config = state.get_saved_config()
    config["processing"]["delete_after_upload"] = True
    assert state.get_saved_config()["processing"]["delete_after_upload"] is False


def test_save_endpoint_calls_application_and_returns_failure_honestly(
    tmp_path, monkeypatch
):
    path = tmp_path / "config.ini"
    config = copy.deepcopy(DEFAULT_CONFIG)
    update_config(config, str(path))
    state = MagicMock()
    state.get_saved_config.return_value = config
    state.get_config_path.return_value = path
    state.apply_config.return_value = {
        "applied": False,
        "message": "已保存但应用失败",
        "application": {"version": 1, "status": "failed"},
    }
    monkeypatch.setattr(routes, "get_state_manager", lambda: state)
    app = FastAPI()
    app.include_router(routes.router, prefix="/api/config")
    with TestClient(app) as client:
        response = client.put(
            "/api/config/section",
            json={"section": "processing", "values": {"delete_after_upload": True}},
        )
    assert response.status_code == 200
    assert response.json()["saved"] is True
    assert response.json()["applied"] is False
    state.apply_config.assert_called_once()
    assert load_config(str(path))["processing"]["delete_after_upload"] is True


def test_api_invalid_config_does_not_touch_ini(tmp_path, monkeypatch):
    path = tmp_path / "config.ini"
    config = copy.deepcopy(DEFAULT_CONFIG)
    update_config(config, str(path))
    before = path.read_bytes()
    state = MagicMock()
    state.get_saved_config.return_value = config
    state.get_config_path.return_value = path
    monkeypatch.setattr(routes, "get_state_manager", lambda: state)
    app = FastAPI()
    app.include_router(routes.router, prefix="/api/config")
    with TestClient(app) as client:
        response = client.put(
            "/api/config/section",
            json={"section": "processing", "values": {"max_upload_workers": 0}},
        )
    assert response.status_code == 422
    assert path.read_bytes() == before
    state.apply_config.assert_not_called()


def test_llm_database_save_triggers_auto_application(monkeypatch):
    state = MagicMock()
    state.get_saved_config.return_value = copy.deepcopy(DEFAULT_CONFIG)
    state.apply_config.return_value = {"applied": True, "message": "已应用"}
    session_factory = MagicMock()
    monkeypatch.setattr(routes, "get_state_manager", lambda: state)
    monkeypatch.setattr(routes, "get_session_local", lambda: session_factory)
    app = FastAPI()
    app.include_router(routes.router, prefix="/api/config")
    with TestClient(app) as client:
        response = client.post(
            "/api/config/db/llm-providers",
            json={"name": "test", "api_url": "https://example.com", "api_key": "test"},
        )
    assert response.status_code == 201
    assert response.json()["applied"] is True
    session_factory.return_value.__enter__.return_value.commit.assert_called_once()
    state.apply_config.assert_called_once()


def test_real_handler_rebuild_keeps_task_policy_and_reuses_only_one_queue(monkeypatch):
    """使用真实处理器和队列，只替换视频任务内容，禁止真实上传与删除。"""
    from video_organizer.web.services.state import StateManager
    from video_organizer.core.file_handler.handler import VideoFileHandler

    original_handlers = logging.getLogger().handlers[:]
    original_level = logging.getLogger().level
    state = object.__new__(StateManager)
    state._initialized = False
    state.__init__()
    config = copy.deepcopy(DEFAULT_CONFIG)
    config["processing"].update(max_upload_workers=1, delete_after_upload=False)
    config["logging"]["file_log"] = False
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    seen = []

    def process(task, path, worker_id=0):
        if path == "old.mkv":
            started.set()
            assert release.wait(5)
        seen.append((path, task.delete_after_upload))
        if path == "new.mkv":
            finished.set()
        return True

    monkeypatch.setattr(VideoFileHandler, "_process_file_snapshot", process)
    monkeypatch.setattr(live, "with_llm_snapshot", lambda cfg: copy.deepcopy(cfg))
    manager = state.enable_live_config(config)
    try:
        assert state.apply_config(config)["applied"]
        # web-only 初始没有处理器，改变设置后才构建完整处理器。
        config["processing"]["max_upload_workers"] = 2
        assert state.apply_config(config)["applied"]
        root = manager.root
        queue = root._upload_queue
        queue.put("old.mkv")
        assert started.wait(2)
        changed = copy.deepcopy(config)
        changed["processing"]["delete_after_upload"] = True
        assert state.apply_config(changed)["applied"]
        assert manager.current.handler._upload_queue is queue
        assert len(root._worker_threads) == 2
        queue.put("new.mkv")
        assert finished.wait(2)
        assert ("new.mkv", True) in seen
        release.set()
        wait_until(lambda: ("old.mkv", False) in seen)
        wait_until(lambda: queue.unfinished_tasks == 0)
    finally:
        release.set()
        if manager.root:
            manager.root.stop_upload_queue()
            for thread in manager.root._worker_threads:
                thread.join(2)
        if manager.current:
            manager._retire(manager.current)
        logging.getLogger().handlers = original_handlers
        logging.getLogger().level = original_level


def test_bootstrap_application_can_attach_actual_monitor_later(monkeypatch):
    from video_organizer.web.services.state import StateManager

    monkeypatch.setattr(live, "with_llm_snapshot", lambda cfg: copy.deepcopy(cfg))
    state = object.__new__(StateManager)
    state._initialized = False
    state.__init__()
    config = copy.deepcopy(DEFAULT_CONFIG)
    manager = state.enable_live_config(config)
    assert manager.root is None
    root = SimpleNamespace(_upload_queue=Queue(), _runtime_manager=None)
    monitor = SimpleNamespace(event_handler=root, downloader_monitors=[])
    state.set_video_handler(root)
    state.enable_live_config(config, monitor=monitor)
    assert manager.monitor is monitor
    assert manager.root is root
    assert root._runtime_manager is manager
    assert manager.current.monitor._runtime_manager is manager


def test_http_stream_keeps_old_clients_until_last_body(monkeypatch, manager):
    import asyncio
    from video_organizer.web.services import state as state_module

    state = SimpleNamespace(_live_config=manager)
    monkeypatch.setattr(state_module, "get_state_manager", lambda: state)
    old = manager.current

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        config = copy.deepcopy(DEFAULT_CONFIG)
        config["processing"]["max_upload_workers"] = 2
        assert manager.apply(config)["applied"]
        assert old.users == 1 and not old.closed
        await send({"type": "http.response.body", "body": b"done", "more_body": False})

    async def receive():
        return {"type": "http.request"}

    messages = []

    async def send(message):
        messages.append(message)

    asyncio.run(live.RuntimeLeaseMiddleware(app)({"type": "http"}, receive, send))
    wait_until(lambda: old.closed)
    assert messages[-1]["body"] == b"done"


def test_long_poll_intervals_and_large_chunk_sizes_are_valid():
    config = copy.deepcopy(DEFAULT_CONFIG)
    config["monitoring"]["polling_interval"] = 600
    config["emos"]["chunk_size_mb"] = 512
    normalized = normalize_config(config)
    assert normalized["monitoring"]["polling_interval"] == 600
    assert normalized["emos"]["chunk_size_mb"] == 512


def test_disabled_downloader_does_not_connect(monkeypatch):
    from video_organizer.core.monitor.monitor import FileSystemMonitor
    from video_organizer.core.monitor import monitor as module

    monitor = object.__new__(FileSystemMonitor)
    monitor.downloader_configs = [{"type": "qbittorrent", "enabled": "False"}]
    monitor.downloader_monitors = []
    create = MagicMock(side_effect=AssertionError("禁用的下载器不应建立连接"))
    monkeypatch.setattr(module.DownloaderMonitorFactory, "create_monitor", create)
    monitor._init_downloader_monitors()
    assert monitor.downloader_monitors == []
    create.assert_not_called()


def test_downloader_upload_completion_records_survive_reconfiguration(monkeypatch):
    from video_organizer.core.monitor.monitor import FileSystemMonitor
    from video_organizer.core.monitor import monitor as module

    class Downloader:
        rpc_url = "http://same-downloader"
        username = "test"

        def __init__(self):
            self._upload_completed_files = set()

    previous, replacement = Downloader(), Downloader()
    previous._upload_completed_files.add("episode-01.mkv")
    monitor = object.__new__(FileSystemMonitor)
    monitor.downloader_monitors = [previous]
    for name in (
        "_directory_processed_files",
        "_directory_processed_dirs",
        "_directory_successfully_processed_files",
    ):
        setattr(monitor, name, set())
    monkeypatch.setattr(
        module.DownloaderMonitorFactory, "create_monitor", lambda *args: replacement
    )
    config = copy.deepcopy(DEFAULT_CONFIG)
    config["downloaders"] = [{"type": "qbittorrent"}]
    candidate = monitor.prepare_live_config(
        config, SimpleNamespace(upload_targets=["emos"])
    )
    assert (
        candidate.downloader_monitors[0]._upload_completed_files
        is previous._upload_completed_files
    )
    assert "episode-01.mkv" in replacement._upload_completed_files

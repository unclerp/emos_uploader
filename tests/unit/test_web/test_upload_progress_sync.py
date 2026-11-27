"""上传完成丢事件、重连快照、终态回收与重试的回归。"""

import asyncio
import threading
from datetime import datetime, timedelta

import pytest
from fastapi import FastAPI, WebSocketDisconnect
from fastapi.testclient import TestClient

from video_organizer.web.routers import tasks
from video_organizer.web.services import state as state_module
from video_organizer.web.services.state import StateManager, UploadProgress


@pytest.fixture
def clock(monkeypatch):
    class Clock(datetime):
        current = datetime(2026, 11, 27, 12)

        @classmethod
        def now(cls):
            return cls.current

        @classmethod
        def advance(cls, seconds):
            cls.current += timedelta(seconds=seconds)

    monkeypatch.setattr(state_module, "datetime", Clock)
    return Clock


@pytest.fixture
def state(monkeypatch):
    manager = object.__new__(StateManager)
    manager._initialized = False
    manager.__init__()
    monkeypatch.setattr(tasks, "get_state_manager", lambda: manager)
    return manager


def progress(status="uploading", percentage=63):
    return UploadProgress(
        file_path="/input/example.mkv",
        filename="example.mkv",
        uploader="emos",
        progress=percentage,
        uploaded_bytes=percentage,
        total_bytes=100,
        speed="1 MB/s",
        status=status,
    )


def test_rest_snapshot_uses_progress_field_and_empty_snapshot_clears(state):
    app = FastAPI()
    app.include_router(tasks.router, prefix="/api/tasks")
    with TestClient(app) as client:
        state.update_upload_progress(progress())
        data = client.get("/api/tasks/progress").json()
        assert data["progress"]["/input/example.mkv"]["progress"] == 63
        assert data["count"] == 1
        assert "progresses" not in data
        state.clear_upload_progress("/input/example.mkv")
        assert client.get("/api/tasks/progress").json() == {
            "success": True,
            "count": 0,
            "progress": {},
        }


@pytest.mark.parametrize("status", ["completed", "skipped", "failed"])
def test_terminal_progress_expires_without_receiving_another_event(
    state, clock, status
):
    state.update_upload_progress(progress(status, 100))
    clock.advance(4)
    assert state.get_upload_progress("/input/example.mkv").status == status
    clock.advance(1)
    assert state.get_all_upload_progress() == {}
    assert state.get_upload_progress("/input/example.mkv") is None


def test_slow_upload_is_not_removed_based_on_age(state, clock):
    state.update_upload_progress(progress())
    clock.advance(3600)
    assert state.get_all_upload_progress()["/input/example.mkv"].progress == 63


def test_late_chunk_does_not_resurrect_completed_upload(state, clock):
    state.update_upload_progress(progress("completed", 100))
    state.update_upload_progress(progress("uploading", 90))
    assert state.get_upload_progress("/input/example.mkv").status == "completed"
    clock.advance(5)
    assert state.get_all_upload_progress() == {}


def test_new_attempt_at_same_path_survives_old_completion_expiry(state, clock):
    state.update_upload_progress(progress("failed", 63))
    clock.advance(1)
    state.update_upload_progress(progress("uploading", 0))
    state.update_upload_progress(progress("uploading", 10))
    clock.advance(10)
    assert state.get_all_upload_progress()["/input/example.mkv"].progress == 10


def test_progress_callbacks_do_not_hold_state_lock(state):
    observed = []

    def callback(_):
        done = threading.Event()
        thread = threading.Thread(
            target=lambda: (state.get_all_upload_progress(), done.set())
        )
        thread.start()
        observed.append(done.wait(1))
        thread.join(timeout=1)

    state.register_progress_callback(callback)
    state.update_upload_progress(progress())
    assert observed == [True]


class Socket:
    def __init__(self, limit=1):
        self.messages = []
        self.limit = limit
        self.closed = False

    async def accept(self):
        pass

    async def send_json(self, message):
        self.messages.append(message)
        if len(self.messages) >= self.limit:
            raise WebSocketDisconnect()

    async def close(self):
        self.closed = True


@pytest.mark.parametrize("active", [True, False])
def test_websocket_connect_immediately_sends_full_snapshot(state, active):
    if active:
        state.update_upload_progress(progress())
    socket = Socket()
    asyncio.run(tasks.websocket_progress(socket))
    snapshot = socket.messages[0]
    assert snapshot["type"] == "snapshot"
    assert snapshot["count"] == int(active)
    assert bool(snapshot["progress"]) is active
    assert socket.closed


def test_periodic_snapshots_repair_missed_completion_and_remove_finished(
    state, clock, monkeypatch
):
    state.update_upload_progress(progress())
    socket = Socket(limit=3)
    sleeps = []

    async def advance(seconds):
        sleeps.append(seconds)
        clock.advance(seconds)
        if len(sleeps) == 1:
            state.update_upload_progress(progress("completed", 100))

    monkeypatch.setattr(tasks.asyncio, "sleep", advance)
    asyncio.run(tasks.websocket_progress(socket))
    assert sleeps == [5, 5]
    assert socket.messages[0]["progress"]["/input/example.mkv"]["status"] == "uploading"
    assert socket.messages[1]["progress"]["/input/example.mkv"]["status"] == "completed"
    assert socket.messages[2]["progress"] == {}
    # 完整快照无需订阅增量回调，不会因慢连接积压历史分片事件。
    assert state._progress_callbacks == []

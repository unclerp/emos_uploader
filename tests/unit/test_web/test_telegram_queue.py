"""队列总览使用真实 SQLite 与假的 Telegram HTTP，不发送测试消息。"""

from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from concurrent.futures import ThreadPoolExecutor

import pytest

from video_organizer.database import session, operations
from video_organizer.database.models import (
    TelegramDelivery,
    TelegramQueueRun,
    TelegramQueueItem,
    UploadTask,
    PostUploadAction,
    DownloadFileBinding,
)
from video_organizer.database.task_tracking import (
    prepare_task,
    set_task_status,
    recover_interrupted,
)
from video_organizer.notifications import telegram as tg

CFG = {"bot_token": "123456:test-secret-token", "chat_id": "-100001", "thread_id": "42"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(session, "_engine", None)
    monkeypatch.setattr(session, "_SessionLocal", None)
    session.init_db(str(tmp_path / "queue.db"))
    d = tg.TelegramDispatcher(auto_start=False)
    monkeypatch.setattr(tg, "get_dispatcher", lambda: d)
    seq = iter(range(100, 10000))

    def respond(url, **kwargs):
        message_id = kwargs["json"].get("message_id", next(seq))
        result = Mock(status_code=200)
        result.json.return_value = {"ok": True, "result": {"message_id": message_id}}
        return result

    post = Mock(side_effect=respond)
    monkeypatch.setattr(tg.requests, "post", post)
    # Web 包在首次导入时创建应用；在接收测试任务之前完成初始化。
    from video_organizer.web.services.state import get_state_manager

    get_state_manager()
    yield SimpleNamespace(d=d, q=d.overview, root=tmp_path, post=post)
    d.stop_event.set()
    session._engine.dispose()


def create(env, name, status="queued", config=None):
    path = env.root / name
    path.write_bytes(b"video")
    return prepare_task(
        str(path),
        ["emos"],
        status=status,
        telegram_config=CFG if config is None else config,
    )


def tick(env, now):
    env.q.next_tick = 0
    env.q.tick(now)


def deliveries():
    with session.get_session_local()() as db:
        return [
            {c.name: getattr(row, c.name) for c in TelegramDelivery.__table__.columns}
            for row in db.query(TelegramDelivery).order_by(TelegramDelivery.updated_at)
        ]


def overview():
    return [r for r in deliveries() if r["scope"].startswith("queue-overview:")][-1]


def runs():
    with session.get_session_local()() as db:
        return [
            {c.name: getattr(row, c.name) for c in TelegramQueueRun.__table__.columns}
            for row in db.query(TelegramQueueRun).order_by(TelegramQueueRun.id)
        ]


def send(env):
    env.d.next_request = 0
    with session.get_session_local()() as db:
        db.query(TelegramDelivery).update(
            {TelegramDelivery.next_attempt_at: datetime.now() - timedelta(seconds=1)}
        )
        db.commit()
    return env.d.send_once()


def finish(env, task, now):
    set_task_status(task, "completed")
    tick(env, now)
    tick(env, now + timedelta(seconds=6))


def test_no_credentials_or_disabled_queue_create_nothing(env):
    for i, config in enumerate(
        ({}, {**CFG, "enabled": False}, {**CFG, "notify_queue": False})
    ):
        create(env, f"{i}.mkv", config=config)
    tick(env, datetime.now())
    assert runs() == [] and deliveries() == []
    env.post.assert_not_called()


def test_round_only_counts_members_and_queue_excludes_processing(env):
    create(env, "historic.mkv", status="completed", config={})
    statuses = ["completed"] * 6 + ["skipped", "failed", "processing"] + ["queued"] * 3
    for i, status in enumerate(statuses):
        create(env, f"{i}.mkv", status)
    tick(env, datetime.now())
    text = overview()["text"]
    for expected in (
        "已处理 8 / 12",
        "上传成功：6",
        "已跳过：1",
        "上传失败：1",
        "正在处理：1",
        "等待处理：3",
    ):
        assert expected in text
    assert "historic" not in text
    assert len(runs()) == 1


def test_new_members_join_and_retry_updates_same_member(env):
    task = create(env, "first.mkv", "failed")
    create(env, "second.mkv")
    tick(env, datetime.now())
    before = overview()
    path = str(env.root / "first.mkv")
    prepare_task(path, ["emos"], task_id=task, telegram_config=CFG)
    assert operations.record_task(
        path, "completed", task_id=task, targets="emos", telegram_config=CFG
    )
    tick(env, datetime.now() + timedelta(seconds=6))
    assert overview()["identity"] == before["identity"]
    assert "已处理 1 / 2" in overview()["text"]
    assert "上传成功：1" in overview()["text"] and "上传失败：0" in overview()["text"]
    with session.get_session_local()() as db:
        assert db.query(TelegramQueueItem).count() == 2


def test_throttle_idle_grace_final_summary_and_next_round(env):
    now = datetime.now()
    task = create(env, "first.mkv")
    tick(env, now)
    rev = overview()["revision"]
    set_task_status(task, "processing")
    tick(env, now + timedelta(seconds=2))
    assert overview()["revision"] == rev
    tick(env, now + timedelta(seconds=5))
    assert "正在处理：1" in overview()["text"]
    finish(env, task, now + timedelta(seconds=6))
    assert overview()["terminal"] and "本轮全部完成" in overview()["text"]
    assert not any(r["scope"].startswith("queue-final:") for r in deliveries())
    assert send(env)
    assert env.post.call_args.kwargs["json"]["disable_notification"] is True
    tick(env, now + timedelta(seconds=13))
    finals = [r for r in deliveries() if r["scope"].startswith("queue-final:")]
    assert len(finals) == 1
    tick(env, now + timedelta(seconds=14))
    assert len([r for r in deliveries() if r["scope"].startswith("queue-final:")]) == 1
    old_text = overview()["text"]
    create(env, "next.mkv")
    tick(env, now + timedelta(seconds=15))
    assert len(runs()) == 2 and "已处理 0 / 1" in overview()["text"]
    assert [r for r in deliveries() if r["scope"] == "queue-overview:1"][0][
        "text"
    ] == old_text


def bind_action(task, status="waiting"):
    with session.get_session_local()() as db:
        db.add(
            DownloadFileBinding(
                identity="binding",
                group_key="group",
                downloader_key="qb",
                torrent_hash="hash",
                task_id=task,
                file_index=0,
                file_size=5,
                source_path="/qb/video.mkv",
                current_path="/input/video.mkv",
            )
        )
        action = PostUploadAction(
            identity="group",
            downloader_key="qb",
            torrent_hash="hash",
            torrent_name="种子",
            targets="emos",
            kind="move",
            destination="/archive",
            status=status,
        )
        db.add(action)
        db.commit()
        return action.id


def test_pending_seed_action_blocks_finish_but_remains_separate_notification(env):
    now = datetime.now()
    task = create(env, "file.mkv", "completed")
    action_id = bind_action(task)
    tick(env, now)
    tick(env, now + timedelta(seconds=10))
    assert runs()[0]["finished_at"] is None and "执行中 1 个种子" in overview()["text"]
    with session.get_session_local()() as db:
        action = db.get(PostUploadAction, action_id)
        action.status = "moved"
        db.commit()
        tg.notify_post_action(CFG, action)
    tick(env, now + timedelta(seconds=11))
    tick(env, now + timedelta(seconds=17))
    assert runs()[0]["finished_at"]
    assert "已完成 1" in overview()["text"]
    separate = [r for r in deliveries() if r["scope"] == f"post-upload:{action_id}"]
    assert len(separate) == 1 and "上传后移动成功" in separate[0]["text"]


@pytest.mark.parametrize(
    "upload_status,action_status,expected",
    [("failed", "waiting", "被上传失败阻塞 1"), ("completed", "failed", "失败 1")],
)
def test_failed_action_or_failed_upload_is_reported_as_unresolved(
    env, upload_status, action_status, expected
):
    now = datetime.now()
    task = create(env, "file.mkv", upload_status)
    bind_action(task, action_status)
    tick(env, now)
    tick(env, now + timedelta(seconds=6))
    assert "本轮结束，有失败待处理" in overview()["text"]
    assert expected in overview()["text"]


def test_progress_available_with_individual_progress_disabled(env):
    config = {**CFG, "notify_progress": False}
    task = create(env, "file.mkv", "processing", config)
    tg.bind_task(config, str(env.root / "file.mkv"), task)
    assert not tg.notify_progress(config, str(env.root / "file.mkv"), "Emos", 65)
    tick(env, datetime.now())
    assert "65.0%" in overview()["text"]
    assert not any(r["scope"].startswith("task:") for r in deliveries())


def test_restart_preserves_run_message_and_marks_interrupted_task_failed(
    env, monkeypatch
):
    now = datetime.now()
    task = create(env, "file.mkv", "processing")
    tick(env, now)
    send(env)
    original = overview()
    with session.get_session_local()() as db:
        recover_interrupted(db)
    d = tg.TelegramDispatcher(auto_start=False)
    monkeypatch.setattr(tg, "get_dispatcher", lambda: d)
    env.d, env.q = d, d.overview
    d.configure(CFG)
    tick(env, now + timedelta(seconds=1))
    tick(env, now + timedelta(seconds=7))
    assert len(runs()) == 1 and "上传失败：1" in overview()["text"]
    send(env)
    assert env.post.call_args.args[0].endswith("/editMessageText")
    assert overview()["message_id"] == original["message_id"]
    assert overview()["identity"] == original["identity"]
    assert task


def control(env):
    env.d.next_request = 0
    with session.get_session_local()() as db:
        db.query(TelegramQueueRun).update({TelegramQueueRun.pin_retry_at: None})
        db.commit()
    return env.q.control_once()


def test_pin_new_then_unpin_only_previous_owned_overview(env):
    now = datetime.now()
    task = create(env, "first.mkv")
    tick(env, now)
    send(env)
    assert control(env)
    first_id = overview()["message_id"]
    assert env.post.call_args.args[0].endswith("/pinChatMessage")
    assert env.post.call_args.kwargs["json"] == {
        "chat_id": CFG["chat_id"],
        "message_id": first_id,
        "disable_notification": True,
    }
    finish(env, task, now + timedelta(seconds=1))
    send(env)
    create(env, "second.mkv")
    tick(env, now + timedelta(seconds=8))
    send(env)
    second_id = overview()["message_id"]
    assert second_id != first_id
    assert control(env)
    assert env.post.call_args.args[0].endswith("/pinChatMessage")
    assert env.post.call_args.kwargs["json"]["message_id"] == second_id
    assert control(env)
    assert env.post.call_args.args[0].endswith("/unpinChatMessage")
    assert env.post.call_args.kwargs["json"]["message_id"] == first_id
    assert runs()[0]["unpin_done"]
    assert all(
        not c.args[0].endswith("/unpinAllChatMessages") for c in env.post.call_args_list
    )


def test_pin_permission_error_does_not_fail_task_and_reload_unblocks(env):
    task = create(env, "file.mkv", "completed")
    tick(env, datetime.now())
    send(env)
    response = Mock(status_code=403)
    response.json.return_value = {"ok": False}
    env.post.side_effect = None
    env.post.return_value = response
    assert control(env)
    assert runs()[0]["pin_blocked"]
    env.post.reset_mock()
    assert not control(env)
    env.post.assert_not_called()
    with session.get_session_local()() as db:
        assert db.get(UploadTask, task).status == "completed"
    env.d.configure(CFG)
    assert not runs()[0]["pin_blocked"]


def test_pin_rate_limit_persists_across_restart_and_redacts_logs(
    env, monkeypatch, caplog
):
    create(env, "file.mkv")
    tick(env, datetime.now())
    send(env)
    response = Mock(status_code=429)
    response.json.return_value = {"ok": False, "parameters": {"retry_after": 120}}
    env.post.side_effect = None
    env.post.return_value = response
    assert control(env)
    assert runs()[0]["pin_retry_at"] > datetime.now() + timedelta(seconds=110)
    d = tg.TelegramDispatcher(auto_start=False)
    monkeypatch.setattr(tg, "get_dispatcher", lambda: d)
    d.configure(CFG)
    env.post.reset_mock()
    assert not d.overview.control_once()
    env.post.assert_not_called()
    assert CFG["bot_token"] not in caplog.text


def test_queue_can_be_disabled_independently_and_pending_queue_stops_after_restart(
    env, monkeypatch
):
    task = create(env, "file.mkv")
    tick(env, datetime.now())
    d = tg.TelegramDispatcher(auto_start=False)
    monkeypatch.setattr(tg, "get_dispatcher", lambda: d)
    config = {**CFG, "notify_queue": False}
    d.configure(config)
    assert not overview()["pending"]
    tg.bind_task(config, str(env.root / "file.mkv"), task)
    assert any(r["scope"] == f"task:{task}" and r["pending"] for r in deliveries())
    assert d.overview.configs == {}


def test_auto_pin_switch_leaves_overview_available(env):
    create(env, "file.mkv", config={**CFG, "auto_pin_queue": False})
    tick(env, datetime.now())
    send(env)
    env.post.reset_mock()
    assert not control(env)
    assert overview()["message_id"]
    env.post.assert_not_called()


def test_concurrent_enqueue_tracks_each_task_once_without_network(env):
    paths = []
    for i in range(12):
        path = env.root / f"{i}.mkv"
        path.write_bytes(b"video")
        paths.append(str(path))
    with ThreadPoolExecutor(max_workers=4) as executor:
        tasks = list(
            executor.map(
                lambda p: prepare_task(
                    p, ["emos"], status="queued", telegram_config=CFG
                ),
                paths,
            )
        )
    tick(env, datetime.now())
    assert len(set(tasks)) == 12 and len(runs()) == 1
    assert "已处理 0 / 12" in overview()["text"]
    env.post.assert_not_called()


def test_queue_overview_not_starved_by_pending_file_results(env):
    create(env, "file.mkv")
    env.d.submit(CFG, "task:1000", "成功", event="success")
    tick(env, datetime.now())
    assert send(env)
    assert "本轮队列总览" in env.post.call_args.kwargs["json"]["text"]
    assert env.post.call_args.kwargs["json"]["message_thread_id"] == 42


def test_queue_storage_failure_does_not_prevent_file_or_result(env, monkeypatch):
    monkeypatch.setattr(
        env.q, "track", Mock(side_effect=RuntimeError("storage failure"))
    )
    task = create(env, "file.mkv")
    assert task
    assert operations.record_task(
        str(env.root / "file.mkv"),
        "completed",
        task_id=task,
        targets="emos",
        telegram_config=CFG,
    )
    with session.get_session_local()() as db:
        assert db.get(UploadTask, task).status == "completed"
    assert any(r["scope"] == f"task:{task}" for r in deliveries())
    assert not runs()


def test_transmission_100_percent_does_not_count_upload_success(env):
    config = {**CFG, "notify_progress": False}
    task = create(env, "file.mkv", "uploading", config)
    path = str(env.root / "file.mkv")
    tg.bind_task(config, path, task)
    tg.notify_progress(config, path, "Emos", 100)
    tick(env, datetime.now())
    assert "等待最终确认" in overview()["text"]
    assert "上传成功：0" in overview()["text"]
    assert not overview()["terminal"]

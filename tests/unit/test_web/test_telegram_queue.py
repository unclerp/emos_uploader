"""队列总览使用真实 SQLite 与假的 Telegram HTTP，不发送测试消息。"""

import logging
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

CFG = {
    "queue_idle_seconds": 5,
    "bot_token": "123456:test-secret-token",
    "chat_id": "-100001",
    "thread_id": "42",
}


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


def test_final_summary_lists_only_successful_media_from_its_round(env, monkeypatch):
    compact_config = {**CFG, "compact_mode": True}
    old_task = create(env, "first.mkv", config={})

    def result(task, name, media, status="completed", end_time=None):
        assert operations.record_task(
            str(env.root / name),
            status,
            task_id=task,
            targets="emos",
            end_time=end_time or datetime.now(),
            media=media,
        )

    show = {"tmdb_id": "10", "media_type": "tv", "title": "第一部剧"}
    result(old_task, "first.mkv", {**show, "season_episode": "S01E99"})
    prepare_task(
        str(env.root / "first.mkv"),
        ["emos"],
        task_id=old_task,
        telegram_config=compact_config,
    )
    result(old_task, "first.mkv", {**show, "season_episode": "S01E01"})

    for index, episodes in enumerate(
        ("S01E02", "S01E03", "S01E02", "S02E04", "S02E05-7", "S02E09", "S00E01")
    ):
        name = f"release-{index}.mkv"
        task = create(env, name, config=compact_config)
        result(task, name, {**show, "season_episode": episodes})

    other_show = {"tmdb_id": "11", "media_type": "tv", "title": "第二部剧"}
    retry = create(env, "retry.mkv", config=compact_config)
    result(retry, "retry.mkv", {**other_show, "season_episode": "S03E01"}, "failed")
    result(retry, "retry.mkv", {**other_show, "season_episode": "S03E01"})

    movie = {
        "tmdb_id": "10",
        "media_type": "movie",
        "title": "电影名",
        "season_episode": "",
    }
    for name in ("movie-release-a.mkv", "movie-release-b.mkv"):
        result(create(env, name, config=compact_config), name, movie)
    for status in ("failed", "skipped"):
        name = f"{status}.mkv"
        result(
            create(env, name, config=compact_config),
            name,
            {**movie, "tmdb_id": status, "title": f"忽略-{status}"},
            status,
        )

    now = datetime.now()
    tick(env, now)
    tick(env, now + timedelta(seconds=6))
    assert "本轮已上传片单" not in overview()["text"]
    assert send(env)

    # 等待结束通知投递时重启，并在下一轮复用同一个任务 ID。
    d = tg.TelegramDispatcher(auto_start=False)
    monkeypatch.setattr(tg, "get_dispatcher", lambda: d)
    env.d, env.q = d, d.overview
    d.configure(compact_config)
    prepare_task(
        str(env.root / "first.mkv"),
        ["emos"],
        task_id=old_task,
        telegram_config=compact_config,
    )
    result(
        old_task,
        "first.mkv",
        {**show, "season_episode": "S04E88"},
        end_time=now + timedelta(seconds=7),
    )
    tick(env, now + timedelta(seconds=8))

    final = [row for row in deliveries() if row["scope"] == "queue-final:1"][0]
    assert final["text"].split("🎬 本轮已上传片单\n")[1].splitlines() == [
        "第一部剧: S00E01, S01E01-S01E03, S02E04-S02E07, S02E09",
        "第二部剧: S03E01",
        "电影名",
    ]


def test_long_final_media_list_sends_continuation_silently(env, monkeypatch):
    lines = [f"电影 {i:02d} {'🎞' * 60}" for i in range(70)]
    monkeypatch.setattr(
        "video_organizer.notifications.queue_overview.completed_media_lines",
        lambda db, run: lines,
    )
    now = datetime.now()
    task = create(env, "file.mkv")
    finish(env, task, now)
    assert send(env)
    tick(env, now + timedelta(seconds=7))
    assert send(env)
    first = env.post.call_args.kwargs["json"]
    assert "disable_notification" not in first
    pages = [first["text"]]
    while send(env):
        payload = env.post.call_args.kwargs["json"]
        assert payload["disable_notification"] is True
        pages.append(payload["text"])
    assert len(pages) > 1
    assert all(len(page.encode("utf-16-le")) <= 8000 for page in pages)
    listed = [
        line for page in pages for line in page.splitlines() if line.startswith("电影 ")
    ]
    assert listed == lines


def test_upload_worker_uses_matched_title_for_final_media_list(env, monkeypatch):
    from video_organizer.core.file_handler.handler import VideoFileHandler
    from video_organizer.core.renamer import VideoRenamer

    config = {**CFG, "compact_mode": True}
    name = "Local.Release.S00E01.mkv"
    path = str(env.root / name)
    task = create(env, name, config=config)
    renamer = VideoRenamer(tmdb_api_key="", config={})
    renamer.tmdb_client = Mock()
    renamer.tmdb_client.get_tv_details.return_value = {"id": 10, "name": "TMDB 匹配剧名"}
    metadata = renamer.extract_metadata_with_tmdb(path, 10, "tv")
    uploader = Mock()
    uploader.upload_video.return_value = {"media_uuid": "test"}
    monkeypatch.setattr(
        "video_organizer.upload.emos_uploader.RobustEmosVideoUploader",
        Mock(return_value=uploader),
    )
    h = VideoFileHandler.__new__(VideoFileHandler)
    h.logger = logging.getLogger(__name__)
    h._tracked_task_id = task
    h._uploaded_files = set()
    h._uploading_files = set()
    h._failed_files = {}
    h._cleanup_old_records = Mock()
    h.upload_targets = ["emos"]
    h.emos_auth_token = "fake"
    h.emos_base_url = "https://invalid.example"
    h.emos_chunk_size_mb = h.emos_max_workers = 1
    h.emos_file_storage = "test"
    h.emos_skip_existing_media = True
    h.telegram_config = config
    h.emya_enabled = h.delete_after_upload = False
    h._execute_upload(path, "ve", 1, 1, "10", "tv", "Local Release", "S00E01", metadata)
    uploader.upload_video.assert_called_once()

    now = datetime.now()
    tick(env, now)
    tick(env, now + timedelta(seconds=6))
    assert send(env)
    tick(env, now + timedelta(seconds=7))
    final = next(row["text"] for row in deliveries() if row["scope"] == "queue-final:1")
    assert final.endswith("\nTMDB 匹配剧名: S00E01")
    assert "Local Release" not in final


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


def test_compact_only_overview_with_five_second_progress(env):
    cfg = {**CFG, "compact_mode": True}
    task = create(env, "compact.mkv", "uploading", cfg)
    path = str(env.root / "compact.mkv")
    tg.bind_task(cfg, path, task)
    tg.notify_progress(cfg, path, "Emos", 60, "已上传：6 GB / 10 GB")
    now = datetime.now()
    tick(env, now)
    assert len(deliveries()) == 1
    assert "60.0%" in overview()["text"] and "▰▰▰▰▰▰▱▱▱▱" in overview()["text"]
    assert "6 GB / 10 GB" in overview()["text"]
    tg.notify_progress(cfg, path, "Emos", 80)
    tick(env, now + timedelta(seconds=4))
    assert "60.0%" in overview()["text"]
    tick(env, now + timedelta(seconds=5))
    assert "80.0%" in overview()["text"]
    tg.notify_task_result(cfg, task, path, "completed")
    assert len(deliveries()) == 1


def test_default_idle_is_120_seconds(env):
    cfg = {**CFG, "compact_mode": True}
    cfg.pop("queue_idle_seconds")
    task = create(env, "idle.mkv", "completed", cfg)
    now = datetime.now()
    tick(env, now)
    tick(env, now + timedelta(seconds=119))
    assert not runs()[0]["finished_at"]
    tick(env, now + timedelta(seconds=120))
    assert runs()[0]["finished_at"]
    assert task


def test_failure_window_combines_and_resolved_updates_original(env):
    cfg = {**CFG, "compact_mode": True}
    now = datetime.now()
    tasks = [create(env, f"failed{i}.mkv", "failed", cfg) for i in range(2)]
    for task in tasks:
        assert env.d.failures.record(cfg, task, now)
    env.d.failures.tick(now + timedelta(seconds=59))
    assert not deliveries()
    env.d.failures.tick(now + timedelta(seconds=60))
    assert len(deliveries()) == 1 and "共 2 项" in deliveries()[0]["text"]
    send(env)
    message_id = deliveries()[0]["message_id"]
    for task in tasks:
        set_task_status(task, "completed")
    env.d.failures.tick(now + timedelta(seconds=61))
    assert "全部解决" in deliveries()[0]["text"]
    send(env)
    assert deliveries()[0]["message_id"] == message_id
    assert env.post.call_args.args[0].endswith("/editMessageText")


def test_transient_failure_resolved_before_window_never_notifies(env):
    cfg = {**CFG, "compact_mode": True}
    now = datetime.now()
    task = create(env, "transient.mkv", "failed", cfg)
    env.d.failures.record(cfg, task, now)
    set_task_status(task, "completed")
    env.d.failures.tick(now + timedelta(seconds=10))
    env.d.failures.tick(now + timedelta(seconds=70))
    assert not deliveries()


def test_failure_digest_survives_restart_and_repeat_retry_does_not_duplicate(
    env, monkeypatch
):
    from video_organizer.database.models import (
        TelegramFailureGroup,
        TelegramFailureItem,
    )

    cfg = {**CFG, "compact_mode": True}
    now = datetime.now()
    task = create(env, "restart.mkv", "failed", cfg)
    env.d.failures.record(cfg, task, now)
    d = tg.TelegramDispatcher(auto_start=False)
    monkeypatch.setattr(tg, "get_dispatcher", lambda: d)
    d.configure(cfg)
    d.failures.record(cfg, task, now + timedelta(seconds=61))
    d.failures.tick(now + timedelta(seconds=61))
    assert len(deliveries()) == 1
    with session.get_session_local()() as db:
        assert db.query(TelegramFailureGroup).count() == 1
        assert db.query(TelegramFailureItem).count() == 1


def test_compact_post_success_suppressed_but_failure_resolution_is_edited(env):
    cfg = {**CFG, "compact_mode": True}
    task = create(env, "move.mkv", "completed", cfg)
    action_id = bind_action(task, "moved")
    with session.get_session_local()() as db:
        action = db.get(PostUploadAction, action_id)
        tg.notify_post_action(cfg, action)
        assert not deliveries()
        action.status = "failed"
        db.commit()
        tg.notify_post_action(cfg, action)
        assert len(deliveries()) == 1
        action.status = "moved"
        db.commit()
        tg.notify_post_action(cfg, action)
    assert len(deliveries()) == 1 and "移动成功" in deliveries()[0]["text"]


def test_compact_disabled_queue_falls_back_to_detailed_and_reload_stops_pending(env):
    cfg = {**CFG, "compact_mode": True, "notify_queue": False}
    task = create(env, "fallback.mkv", "processing", cfg)
    path = str(env.root / "fallback.mkv")
    tg.bind_task(cfg, path, task)
    assert deliveries()[0]["pending"]
    env.d.configure({**cfg, "notify_queue": True})
    assert not [r for r in deliveries() if r["scope"].startswith("task:")][0]["pending"]

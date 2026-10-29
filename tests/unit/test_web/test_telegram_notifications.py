"""真实通知表 + HTTP 替身：不向 Telegram 或其他频道发送测试消息。"""

import inspect
import logging
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests

from video_organizer.database import session, operations
from video_organizer.database.models import TelegramDelivery, UploadTask
from video_organizer.database.task_tracking import prepare_task, recover_interrupted
from video_organizer.notifications import telegram as tg
from video_organizer.upload.emos_uploader import RobustEmosVideoUploader
from video_organizer.upload.p123_uploader import P123Uploader
from video_organizer.upload.yun139_uploader import Yun139Uploader
from video_organizer.upload.cloud189_uploader import Cloud189Uploader
from video_organizer.core.effective_config import normalize_config

CFG = {
    "bot_token": "123456:private-test-token",
    "chat_id": "-100001",
    "thread_id": "42",
}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(session, "_engine", None)
    monkeypatch.setattr(session, "_SessionLocal", None)
    session.init_db(str(tmp_path / "notifications.db"))
    dispatcher = tg.TelegramDispatcher(auto_start=False)
    monkeypatch.setattr(tg, "get_dispatcher", lambda: dispatcher)
    monkeypatch.setattr(
        tg.requests, "post", Mock(side_effect=AssertionError("禁止真实网络"))
    )
    file = tmp_path / "file_[中文]`<&>.mkv"
    file.write_bytes(b"video")
    yield SimpleNamespace(d=dispatcher, file=str(file))
    dispatcher.stop_event.set()
    session._engine.dispose()


def rows():
    with session.get_session_local()() as db:
        result = db.query(TelegramDelivery).order_by(TelegramDelivery.updated_at).all()
        return [
            {c.name: getattr(row, c.name) for c in TelegramDelivery.__table__.columns}
            for row in result
        ]


def due(dispatcher):
    dispatcher.next_request = 0
    with session.get_session_local()() as db:
        db.query(TelegramDelivery).update(
            {TelegramDelivery.next_attempt_at: datetime.now() - timedelta(seconds=1)}
        )
        db.commit()


def response(status=200, message_id=71, description="", parameters=None):
    r = Mock(status_code=status)
    r.json.return_value = {
        "ok": status == 200,
        "result": {"message_id": message_id},
        "description": description,
        "parameters": parameters or {},
    }
    return r


def test_disabled_or_missing_credentials_never_store_or_start(env):
    for config in (
        {},
        {"bot_token": "x"},
        {**CFG, "enabled": False},
        {**CFG, "notify_success": False},
    ):
        assert not env.d.submit(config, "task:1", "finished", event="success")
    assert rows() == [] and env.d.thread is None


def test_final_bypasses_progress_throttle_and_late_progress_cannot_overwrite(
    env, monkeypatch
):
    tg.bind_task(CFG, env.file, 1)
    tg.notify_progress(CFG, env.file, "Emos", 50)
    tg.notify_progress(CFG, env.file, "Emos", 100)
    assert "等待最终确认" in rows()[0]["text"]
    tg.notify_task_result(CFG, 1, env.file, "completed", targets="emos")
    assert "上传成功" in rows()[0]["text"] and rows()[0]["terminal"]
    tg.notify_progress(CFG, env.file, "Emos", 10)
    assert "上传成功" in rows()[0]["text"]
    post = Mock(return_value=response())
    monkeypatch.setattr(tg.requests, "post", post)
    assert env.d.send_once()
    payload = post.call_args.kwargs["json"]
    assert payload["message_thread_id"] == 42
    assert "parse_mode" not in payload and Path(env.file).name in payload["text"]


def test_retry_and_restart_update_original_message(env, monkeypatch):
    task = prepare_task(env.file, ["emos"])
    tg.bind_task(CFG, env.file, task)
    assert operations.record_task(
        env.file, "failed", task_id=task, error_message="timeout", telegram_config=CFG
    )
    post = Mock(return_value=response(message_id=71))
    monkeypatch.setattr(tg.requests, "post", post)
    env.d.send_once()
    assert "上传失败" in rows()[0]["text"]
    tg.unbind_task(env.file)
    prepare_task(env.file, ["emos"], task)
    tg.bind_task(CFG, env.file, task)
    operations.record_task(env.file, "completed", task_id=task, telegram_config=CFG)
    restarted = tg.TelegramDispatcher(auto_start=False)
    restarted.configure(CFG)
    due(restarted)
    assert restarted.send_once()
    assert post.call_args.args[0].endswith("/editMessageText")
    assert post.call_args.kwargs["json"]["message_id"] == 71
    assert "原失败已解决" in post.call_args.kwargs["json"]["text"]
    assert "message_thread_id" not in post.call_args.kwargs["json"]
    assert len(rows()) == 1
    assert operations.get_task_counts() == {"completed": 1, "failed": 0, "resolved": 1}


def test_recover_interrupted_task_does_not_resume_stale_uploading_message(env):
    task = prepare_task(env.file, ["emos"])
    tg.bind_task(CFG, env.file, task)
    with session.get_session_local()() as db:
        recover_interrupted(db)
    restarted = tg.TelegramDispatcher(auto_start=False)
    restarted.configure(CFG)
    assert rows()[0]["terminal"]
    assert "上传失败" in rows()[0]["text"] and "服务重启" in rows()[0]["text"]


def test_429_retry_after_survives_new_status_and_restart(env, monkeypatch):
    env.d.submit(CFG, "task:1", "progress", terminal=False)
    monkeypatch.setattr(
        tg.requests,
        "post",
        Mock(return_value=response(429, parameters={"retry_after": 30})),
    )
    before = datetime.now()
    assert env.d.send_once()
    assert rows()[0]["next_attempt_at"] >= before + timedelta(seconds=29)
    env.d.submit(CFG, "task:1", "final", event="success")
    assert not env.d.send_once()
    restarted = tg.TelegramDispatcher(auto_start=False)
    restarted.configure(CFG)
    assert not restarted.send_once()
    assert rows()[0]["delivered_revision"] == 0
    post = Mock(return_value=response())
    monkeypatch.setattr(tg.requests, "post", post)
    due(restarted)
    assert restarted.send_once()
    assert post.call_args.kwargs["json"]["text"] == "final"


def test_exceptions_do_not_leak_token_and_notification_failure_does_not_fail_upload(
    env, monkeypatch, caplog
):
    task = prepare_task(env.file, ["emos"])
    operations.record_task(env.file, "completed", task_id=task, telegram_config=CFG)
    monkeypatch.setattr(
        tg.requests,
        "post",
        Mock(
            side_effect=requests.ConnectionError(
                "https://api.telegram.org/bot" + CFG["bot_token"] + "/sendMessage"
            )
        ),
    )
    with caplog.at_level(logging.WARNING):
        assert env.d.send_once()
    assert (
        CFG["bot_token"] not in caplog.text
        and "api.telegram.org/bot" not in caplog.text
    )
    assert operations.get_task_counts()["completed"] == 1
    assert rows()[0]["failure_count"] == 1 and rows()[0]["delivered_revision"] == 0


def test_missing_message_resends_once_in_correct_topic(env, monkeypatch):
    env.d.submit(CFG, "task:1", "first")
    monkeypatch.setattr(tg.requests, "post", Mock(return_value=response()))
    env.d.send_once()
    env.d.submit(CFG, "task:1", "updated")
    due(env.d)
    post = Mock(
        side_effect=[
            response(400, description="Bad Request: message to edit not found"),
            response(message_id=99),
        ]
    )
    monkeypatch.setattr(tg.requests, "post", post)
    env.d.send_once()
    assert post.call_count == 2
    assert post.call_args.args[0].endswith("/sendMessage")
    assert post.call_args.kwargs["json"]["message_thread_id"] == 42
    assert rows()[0]["message_id"] == 99


def test_not_modified_is_success_and_permission_failure_waits_for_reconfiguration(
    env, monkeypatch
):
    env.d.submit(CFG, "task:1", "first")
    monkeypatch.setattr(tg.requests, "post", Mock(return_value=response()))
    env.d.send_once()
    env.d.submit(CFG, "task:1", "changed")
    due(env.d)
    monkeypatch.setattr(
        tg.requests,
        "post",
        Mock(
            return_value=response(
                400, description="Bad Request: message is not modified"
            )
        ),
    )
    env.d.send_once()
    assert rows()[0]["revision"] == rows()[0]["delivered_revision"]
    env.d.submit(CFG, "task:1", "another")
    due(env.d)
    monkeypatch.setattr(tg.requests, "post", Mock(return_value=response(403)))
    env.d.send_once()
    assert rows()[0]["blocked"]
    env.d.configure(CFG)
    assert not rows()[0]["blocked"]


def test_submit_does_not_wait_for_network_and_ack_preserves_new_revision(
    env, monkeypatch
):
    env.d.submit(CFG, "task:1", "old progress", terminal=False)
    entered, release = threading.Event(), threading.Event()

    def slow_post(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return response()

    monkeypatch.setattr(tg.requests, "post", slow_post)
    sender = threading.Thread(target=env.d.send_once)
    sender.start()
    try:
        assert entered.wait(3)
        started = time.monotonic()
        env.d.submit(CFG, "task:1", "final success", event="success")
        assert time.monotonic() - started < 0.5
    finally:
        release.set()
        sender.join(3)
    assert rows()[0]["revision"] > rows()[0]["delivered_revision"]
    due(env.d)
    post = Mock(return_value=response())
    monkeypatch.setattr(tg.requests, "post", post)
    env.d.send_once()
    assert post.call_args.kwargs["json"]["text"] == "final success"
    assert post.call_args.kwargs["json"]["message_id"] == 71


def test_context_keeps_parallel_files_and_captured_destinations_separate(env):
    second = str(Path(env.file).with_name("second.mkv"))
    tg.bind_task(CFG, env.file, 1)
    other = {**CFG, "chat_id": "-200002", "thread_id": "43"}
    tg.bind_task(other, second, 2)
    tg.notify_progress({}, env.file, "Emos", 50)
    tg.notify_progress({}, second, "123云盘", 70)
    assert {row["chat_id"] for row in rows()} == {"-100001", "-200002"}
    assert len({row["scope"] for row in rows()}) == 2


@pytest.mark.parametrize(
    "mode", ["success", "confirm_failure", "duplicate", "existing_token"]
)
def test_emos_final_is_from_confirmation_or_aggregate_task_result(
    env, monkeypatch, mode
):
    task = prepare_task(env.file, ["emos"])
    tg.bind_task(CFG, env.file, task)
    uploader = RobustEmosVideoUploader(
        "fake", telegram_config=CFG, cache_dir=str(Path(env.file).parent / "cache")
    )
    uploader.check_existing_media = Mock(
        return_value={"media_id": "existing"} if mode == "duplicate" else None
    )
    uploader.step1_init_video = Mock(return_value={"title": "Film"})
    uploader._get_cache_key = Mock(return_value=None)
    uploader.step2_get_upload_token = Mock(
        return_value=(
            {"message": "此资源您之前上传过"}
            if mode == "existing_token"
            else {"file_id": "id"}
        )
    )

    def transfer(*args, **kwargs):
        uploader.upload_stats["total_uploaded"] = 5
        uploader._send_tg_progress(Path(env.file).name, 0, 1, 5, 5, 5, 5, 1)
        return True

    uploader.step3_chunk_upload = transfer
    uploader.step4_confirm_upload = (
        Mock(side_effect=RuntimeError("confirm failed"))
        if mode == "confirm_failure"
        else Mock(return_value={"media_id": "new"})
    )
    result = uploader.upload_video(env.file, "ve", "1", skip_existing_media=True)
    assert not rows()[0]["terminal"]  # 单目标结果还不能替整个任务下结论。
    status = (
        "skipped"
        if mode in ("duplicate", "existing_token")
        else "completed" if result else "failed"
    )
    operations.record_task(
        env.file,
        status,
        task_id=task,
        telegram_config=CFG,
        error_message="confirm failed" if not result else None,
    )
    expected = (
        "已跳过"
        if status == "skipped"
        else "上传成功" if status == "completed" else "上传失败"
    )
    assert expected in rows()[0]["text"]
    assert rows()[0]["terminal"] and len(rows()) == 1


def test_multiple_targets_wait_for_aggregate_result(env):
    task = prepare_task(env.file, ["emos", "p123"])
    tg.bind_task(CFG, env.file, task)

    class Provider:
        telegram_config = CFG

        @tg.notified_upload("Emos")
        def upload_video(self, path):
            return {"media_id": "ok"}

    assert Provider().upload_video(env.file)
    assert not rows()[0]["terminal"]
    operations.record_task(
        env.file,
        "failed",
        task_id=task,
        telegram_config=CFG,
        error_message="p123 failed",
    )
    assert "上传失败" in rows()[0]["text"] and "p123 failed" in rows()[0]["text"]


def test_standalone_uploader_reports_both_kinds_of_skip(env):
    class Provider:
        telegram_config = CFG

        @tg.notified_upload("Emos")
        def upload_video(self, path):
            return {"skipped": True}

    Provider().upload_video(env.file)
    assert rows()[0]["terminal"] and "已跳过" in rows()[0]["text"]


def test_extra_channel_is_explicit_only_and_logs_never_contain_token(env, capsys):
    assert (
        inspect.signature(P123Uploader).parameters["tg_channel_123fslink"].default == ""
    )
    uploader = object.__new__(P123Uploader)
    uploader.telegram_config = CFG
    uploader.tg_bot_token = CFG["bot_token"]
    result = {"etag": "md5", "size": 5, "name": "film.mkv"}
    assert not uploader.send_123fslinkv2_to_tg(env.file, "film.mkv", result)
    assert rows() == []
    assert uploader.send_123fslinkv2_to_tg(
        env.file, "film.mkv", result, tg_channel="@my_explicit_channel"
    )
    assert rows()[0]["chat_id"] == "@my_explicit_channel"
    assert "123FSLinkV2$md5#5#film.mkv" in rows()[0]["text"]
    for provider in (Cloud189Uploader, Yun139Uploader):
        obj = object.__new__(provider)
        obj.telegram_config = CFG
        assert not obj._send_tg_channel_message("info")
        obj.telegram_config = {**CFG, "channel_chat_id": "@my_explicit_channel"}
        assert obj._send_tg_channel_message("info " + provider.__name__)
    assert CFG["bot_token"] not in capsys.readouterr().out


def test_post_upload_result_and_retry_update_one_separate_message(env, monkeypatch):
    action = SimpleNamespace(
        id=1,
        kind="move",
        status="failed",
        torrent_name="Season",
        torrent_hash="abc",
        destination="/archive",
        error="permission denied",
    )
    tg.notify_post_action(CFG, action)
    monkeypatch.setattr(tg.requests, "post", Mock(return_value=response()))
    env.d.send_once()
    assert (
        "上传后移动失败" in rows()[0]["text"]
        and "上传结果保持不变" in rows()[0]["text"]
    )
    action.status, action.error = "moved", None
    tg.notify_post_action(CFG, action)
    due(env.d)
    post = Mock(return_value=response())
    monkeypatch.setattr(tg.requests, "post", post)
    env.d.send_once()
    assert post.call_args.kwargs["json"]["message_id"] == 71
    assert "上传后移动成功" in rows()[0]["text"]


def test_event_toggles_topic_validation_and_message_length(env):
    assert not tg.notify_task_result(
        {**CFG, "notify_skipped": False}, 1, env.file, "skipped"
    )
    with pytest.raises(ValueError, match="话题"):
        normalize_config({"telegram": {"thread_id": "not a number"}})
    assert (
        normalize_config({"telegram": {"thread_id": 42}})["telegram"]["thread_id"]
        == "42"
    )
    env.d.submit(CFG, "long", "😀" * 5000, event="success")
    assert len(rows()[0]["text"].encode("utf-16-le")) <= 8000
    assert CFG["bot_token"] not in repr(rows())


def test_post_action_db_commit_precedes_notification_and_delivery_errors_are_isolated(
    env, monkeypatch
):
    from video_organizer.database.models import PostUploadAction
    from video_organizer.core.post_upload import _commit

    spy = Mock()
    monkeypatch.setattr(tg, "notify_post_action", spy)
    with session.get_session_local()() as db:
        action = PostUploadAction(
            identity="x",
            downloader_key="qb",
            torrent_hash="hash",
            targets="emos",
            kind="move",
            destination="/archive",
            status="waiting",
        )
        db.add(action)
        _commit(db, CFG)
        spy.assert_not_called()
        action.status = "failed"
        action.error = "move failed"
        _commit(db, CFG)
        assert spy.call_args.args[1].status == "failed"
        spy.side_effect = RuntimeError("notification unavailable")
        action.status = "moved"
        _commit(db, CFG)
        assert db.get(PostUploadAction, action.id).status == "moved"


def test_request_debug_logging_redacts_bot_path(env, caplog):
    env.d.register(CFG)
    with caplog.at_level(logging.DEBUG):
        logging.getLogger("urllib3.connectionpool").debug(
            "POST /bot%s/sendMessage", CFG["bot_token"]
        )
    assert CFG["bot_token"] not in caplog.text
    assert "[REDACTED]" in caplog.text


def test_configure_failure_cannot_break_application_configuration(
    env, monkeypatch, caplog
):
    monkeypatch.setattr(
        tg, "get_session_local", Mock(side_effect=RuntimeError("database locked"))
    )
    env.d.configure(CFG)
    assert "配置和上传继续运行" in caplog.text


def test_channel_only_configuration_resumes_explicit_channel(env):
    config = {"bot_token": CFG["bot_token"], "channel_chat_id": "my_channel"}
    assert tg.notify_channel(config, "uploaded", "file")
    restarted = tg.TelegramDispatcher(auto_start=False)
    restarted.configure(config)
    assert restarted.tokens
    assert rows()[0]["chat_id"] == "@my_channel"


def test_prepared_log_handlers_redact_before_being_published(env):
    from video_organizer.web.services.live_config import prepare_logging

    handlers, level = prepare_logging({"console_log": True, "file_log": False})
    try:
        record = logging.LogRecord(
            "urllib3.connectionpool",
            logging.DEBUG,
            "",
            1,
            "POST /bot%s/sendMessage",
            (CFG["bot_token"],),
            None,
        )
        assert all(handler.filter(record) for handler in handlers)
        assert CFG["bot_token"] not in record.getMessage()
    finally:
        for handler in handlers:
            handler.close()


def test_resubmitting_same_pending_payload_registers_bot_after_restart(env):
    env.d.submit(CFG, "same", "pending", event="success")
    restarted = tg.TelegramDispatcher(auto_start=False)
    assert restarted.submit(CFG, "same", "pending", event="success")
    assert restarted.tokens and len(rows()) == 1
    assert rows()[0]["pending"] and rows()[0]["revision"] == 1

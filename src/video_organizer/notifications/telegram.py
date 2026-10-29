"""异步 Telegram 通知：合并进度、持久消息 ID、失败补发，网络请求不占上传线程。"""

import hashlib
import logging
import re
import threading
import time
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path
from typing import Dict

import requests

from sqlalchemy import and_, or_

from ..database.models import TelegramDelivery
from ..database.session import get_session_local

logger = logging.getLogger(__name__)


class TelegramSecretFilter(logging.Filter):
    """也遮盖 requests/urllib3 调试日志中的 Bot 请求路径。"""

    def filter(self, record):
        message = record.getMessage()
        safe = re.sub(r"(/bot)\d+:[A-Za-z0-9_-]+", r"\1[REDACTED]", message)
        if safe != message:
            record.msg, record.args = safe, ()
        return True


def install_log_redaction():
    for handler in logging.getLogger().handlers:
        if not any(isinstance(item, TelegramSecretFilter) for item in handler.filters):
            handler.addFilter(TelegramSecretFilter())


def enabled(config, event="progress"):
    return (
        str(config.get("enabled", True)).lower() not in ("false", "0", "off", "no")
        and bool(str(config.get("bot_token", "") or "").strip())
        and bool(str(config.get("chat_id", "") or "").strip())
        and str(config.get("notify_" + event, True)).lower()
        not in ("false", "0", "off", "no")
    )


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def safe_text(config, text):
    token = str(config.get("bot_token", "") or "").strip()
    text = str(text).replace(token, "[REDACTED]") if token else str(text)
    return text.encode("utf-16-le")[:8000].decode("utf-16-le", errors="ignore")


class TelegramDispatcher:
    def __init__(self, auto_start=True):
        self.auto_start = auto_start
        self.lock = threading.RLock()
        self.send_lock = threading.Lock()
        self.wake = threading.Event()
        self.stop_event = threading.Event()
        self.thread = None
        self.tokens: Dict[str, str] = {}
        self.contexts = {}
        self.progress_times = {}
        self.next_request = 0.0

    def register(self, config):
        if not enabled(config):
            # 即使关闭进度通知，最终通知也需要启动投递线程。
            if not any(
                enabled(config, e)
                for e in ("success", "failure", "skipped", "post_upload")
            ):
                return
        token = str(config["bot_token"]).strip()
        install_log_redaction()
        with self.lock:
            self.tokens[digest(token)] = token
        if self.auto_start:
            self.start()

    def configure(self, config):
        try:
            self._configure(config)
        except Exception:
            logger.warning("Telegram 通知恢复暂不可用，配置和上传继续运行")

    def _configure(self, config):
        channel_config = dict(config, chat_id=channel_destination(config), thread_id="")
        if not any(
            enabled(config, e)
            for e in ("progress", "success", "failure", "skipped", "post_upload")
        ) and not enabled(channel_config, "success"):
            return
        from ..database.models import UploadTask

        token = str(config["bot_token"]).strip()
        chat, thread = (
            str(config.get("chat_id", "") or "").strip(),
            str(config.get("thread_id", "") or "").strip(),
        )
        with self.lock, get_session_local()() as db:
            destinations = [
                and_(
                    TelegramDelivery.chat_id == chat,
                    TelegramDelivery.thread_id == thread,
                )
            ]
            if channel_destination(config):
                destinations.append(
                    and_(
                        TelegramDelivery.chat_id == channel_destination(config),
                        TelegramDelivery.thread_id == "",
                    )
                )
            rows = (
                db.query(TelegramDelivery)
                .filter(TelegramDelivery.bot_key == digest(token), or_(*destinations))
                .all()
            )
            for row in rows:
                if row.blocked:
                    row.blocked, row.next_attempt_at = False, datetime.now()
                if not row.terminal and row.scope.startswith("task:"):
                    task = db.get(UploadTask, int(row.scope.split(":", 1)[1]))
                    if task and task.status in ("completed", "skipped", "failed"):
                        text, event = task_result_text(
                            task.file_path,
                            task.status,
                            task.error_message,
                            task.failure_count,
                            task.attempt_count,
                            task.targets,
                        )
                        if enabled(config, event):
                            row.pending = True
                            row.text, row.terminal, row.revision = (
                                safe_text(config, text),
                                True,
                                row.revision + 1,
                            )
                        else:
                            row.delivered_revision = row.revision
                            row.pending = False
            db.commit()
        self.register(config)
        if enabled(channel_config, "success"):
            self.register(channel_config)

    def start(self):
        with self.lock:
            if self.thread and self.thread.is_alive():
                return
            self.thread = threading.Thread(
                target=self._run, name="telegram-notifications", daemon=True
            )
            self.thread.start()

    def context(self, config, path):
        path = str(Path(path).absolute())
        with self.lock:
            if path in self.contexts:
                return self.contexts[path]
        try:
            stat = Path(path).stat()
            version = f"{stat.st_size}:{stat.st_mtime_ns}"
        except OSError:
            version = "missing"
        return dict(config), "file:" + digest(path + "\0" + version), False

    def bind(self, config, path, task_id):
        with self.lock:
            self.contexts[str(Path(path).absolute())] = (
                dict(config),
                f"task:{task_id}",
                True,
            )
        self.submit(
            config,
            f"task:{task_id}",
            f"🔎 正在处理\n文件：{Path(path).name}",
            terminal=False,
            restart=True,
        )

    def unbind(self, path):
        with self.lock:
            self.contexts.pop(str(Path(path).absolute()), None)

    def submit(
        self, config, scope, text, terminal=True, event="progress", restart=False
    ):
        if not enabled(config, event):
            return False
        try:
            token = str(config["bot_token"]).strip()
            bot_key = digest(token)
            chat_id, thread_id = (
                str(config["chat_id"]).strip(),
                str(config.get("thread_id", "") or "").strip(),
            )
            if thread_id and (not thread_id.isdigit() or int(thread_id) <= 0):
                raise ValueError("thread_id 必须为正整数")
            identity = digest("\0".join((bot_key, chat_id, thread_id, scope)))
            # 保守控制 UTF-16 长度，文件名中的特殊字符作为纯文本发送。
            text = safe_text(config, text)
            now = datetime.now()
            with self.lock, get_session_local()() as db:
                row = db.get(TelegramDelivery, identity)
                if row is not None and row.terminal and not terminal and not restart:
                    return False  # 同次尝试迟到的进度不能覆盖最终状态。
                if row is None:
                    row = TelegramDelivery(
                        identity=identity,
                        bot_key=bot_key,
                        scope=scope,
                        chat_id=chat_id,
                        thread_id=thread_id,
                        text=text,
                        revision=0,
                        delivered_revision=0,
                    )
                    db.add(row)
                if row.text == text and row.revision and row.terminal == terminal:
                    self.register(config)
                    self.wake.set()
                    return True
                row.text, row.terminal, row.updated_at = text, terminal, now
                row.revision = (row.revision or 0) + 1
                row.pending = True
                # 新状态不绕过 Telegram 已给出的 retry_after，也不解除权限错误的暂停。
                if row.next_attempt_at is None:
                    row.next_attempt_at = now
                db.commit()
            self.register(config)
            self.wake.set()
            return True
        except Exception:
            logger.warning("Telegram 通知暂存失败，上传状态不受影响")
            return False

    def send_once(self):
        """单独投递一个快照，便于无网络测试；只由后台线程调用。"""
        if not self.send_lock.acquire(blocking=False):
            return False
        try:
            now = datetime.now()
            with self.lock, get_session_local()() as db:
                if not self.tokens or time.monotonic() < self.next_request:
                    return False
                row = (
                    db.query(TelegramDelivery)
                    .filter(
                        TelegramDelivery.bot_key.in_(list(self.tokens)),
                        TelegramDelivery.pending.is_(True),
                        TelegramDelivery.blocked.is_(False),
                        TelegramDelivery.next_attempt_at <= now,
                    )
                    .order_by(
                        TelegramDelivery.terminal.desc(), TelegramDelivery.updated_at
                    )
                    .first()
                )
                if row is None:
                    return False
                snapshot = {
                    key: getattr(row, key)
                    for key in (
                        "identity",
                        "bot_key",
                        "chat_id",
                        "thread_id",
                        "message_id",
                        "text",
                        "revision",
                        "failure_count",
                    )
                }
                token = self.tokens[row.bot_key]
            payload = {
                "chat_id": snapshot["chat_id"],
                "text": snapshot["text"],
                "link_preview_options": {"is_disabled": True},
            }
            if snapshot["thread_id"] and not snapshot["message_id"]:
                payload["message_thread_id"] = int(snapshot["thread_id"])
            if snapshot["message_id"]:
                payload["message_id"] = snapshot["message_id"]
            ok, message_id, retry_after, permanent = (
                False,
                snapshot["message_id"],
                None,
                False,
            )
            try:
                method = "editMessageText" if message_id else "sendMessage"
                response = requests.post(
                    f"https://api.telegram.org/bot{token}/{method}",
                    json=payload,
                    timeout=10,
                )
                data = response.json()
                description = str(data.get("description", "")).lower()
                # 已删除的消息只重发一次；格式使用纯文本，不做盲目的格式重试。
                if (
                    message_id
                    and response.status_code == 400
                    and any(
                        s in description
                        for s in (
                            "message to edit not found",
                            "message can't be edited",
                        )
                    )
                ):
                    payload.pop("message_id", None)
                    if snapshot["thread_id"]:
                        payload["message_thread_id"] = int(snapshot["thread_id"])
                    response = requests.post(
                        f"https://api.telegram.org/bot{token}/sendMessage",
                        json=payload,
                        timeout=10,
                    )
                    data = response.json()
                    description = str(data.get("description", "")).lower()
                ok = bool(data.get("ok")) or (
                    response.status_code == 400
                    and "message is not modified" in description
                )
                if ok:
                    result = data.get("result")
                    if isinstance(result, dict):
                        message_id = result.get("message_id", message_id)
                else:
                    retry_after = data.get("parameters", {}).get("retry_after")
                    permanent = response.status_code in (400, 401, 403, 404)
                    logger.warning(
                        "Telegram 通知未送达（HTTP %s），%s",
                        response.status_code,
                        "请检查配置和 Bot 权限" if permanent else "将在后台重试",
                    )
            except Exception:
                # requests 异常通常包含带 Token 的 URL，不能直接写日志。
                logger.warning("Telegram 请求失败，将在后台重试")
            delay = max(2, min(300, 2 ** min(snapshot["failure_count"] + 1, 8)))
            if retry_after is not None:
                try:
                    delay = max(2, float(retry_after))
                except (ValueError, TypeError):
                    pass
            with self.lock, get_session_local()() as db:
                row = db.get(TelegramDelivery, snapshot["identity"])
                if ok:
                    row.pending = row.revision != snapshot["revision"]
                    row.message_id, row.delivered_revision, row.failure_count = (
                        message_id,
                        snapshot["revision"],
                        0,
                    )
                else:
                    row.failure_count += 1
                    row.blocked = permanent
                row.next_attempt_at = datetime.now() + timedelta(
                    seconds=2 if ok else delay
                )
                db.commit()
                self.next_request = time.monotonic() + (3 if ok else delay)
            return True
        finally:
            self.send_lock.release()

    def _run(self):
        while not self.stop_event.is_set():
            try:
                self.send_once()
            except Exception:
                logger.warning("Telegram 投递暂不可用，稍后重试")
            self.wake.wait(1)
            self.wake.clear()


_dispatcher = TelegramDispatcher()


def get_dispatcher():
    return _dispatcher


def bind_task(config, path, task_id):
    get_dispatcher().bind(config, path, task_id)


def unbind_task(path):
    get_dispatcher().unbind(path)


def notify_progress(config, path, uploader, percent, details=""):
    dispatcher = get_dispatcher()
    config, scope, managed = dispatcher.context(config, path)
    if not enabled(config, "progress"):
        return False
    percent = max(0, min(100, float(percent)))
    with dispatcher.lock:
        now = time.monotonic()
        previous = dispatcher.progress_times.get(scope, 0)
        if percent < 100 and now - previous < 2:
            return False
        dispatcher.progress_times[scope] = now
        if len(dispatcher.progress_times) > 5000:
            for old in sorted(
                dispatcher.progress_times, key=dispatcher.progress_times.get
            )[:2500]:
                dispatcher.progress_times.pop(old, None)
    status = "文件传输完成，等待最终确认" if percent >= 100 else "上传中"
    return dispatcher.submit(
        config,
        scope,
        f"📤 {status} [{uploader}]\n文件：{Path(path).name}\n进度：{percent:.1f}%\n{details}",
        terminal=False,
    )


def task_result_text(path, status, error="", failures=0, attempts=1, targets=""):
    labels = {
        "completed": "✅ 重试成功，原失败已解决" if failures else "✅ 上传成功",
        "skipped": "⏭️ 已跳过，原失败已解决" if failures else "⏭️ 已跳过",
        "failed": "❌ 重试失败，尚未解决" if failures > 1 else "❌ 上传失败",
    }
    event = {"completed": "success", "skipped": "skipped", "failed": "failure"}.get(
        status
    )
    text = f"{labels.get(status,status)}\n文件：{Path(path).name}\n目标：{targets}\n尝试：{attempts} 次"
    if error:
        text += f"\n说明：{error}"
    return text, event


def notify_task_result(
    config, task_id, path, status, error="", failures=0, attempts=1, targets=""
):
    text, event = task_result_text(path, status, error, failures, attempts, targets)
    if event:
        return get_dispatcher().submit(
            config, f"task:{task_id}", text, terminal=True, event=event
        )
    return False


def notified_upload(uploader):
    """独立调用上传器也有终态；应用任务的终态统一由任务记录提交。"""

    def decorate(function):
        @wraps(function)
        def run(self, file_path, *args, **kwargs):
            dispatcher = get_dispatcher()
            config, scope, managed = dispatcher.context(self.telegram_config, file_path)
            if uploader == "Emos":
                self._telegram_file_path = str(file_path)
            if not managed:
                dispatcher.submit(
                    config,
                    scope,
                    f"📤 准备上传 [{uploader}]\n文件：{Path(file_path).name}",
                    terminal=False,
                    restart=True,
                )
            result = None
            error = ""
            try:
                result = function(self, file_path, *args, **kwargs)
                return result
            except Exception as exc:
                error = str(exc)
                raise
            finally:
                if not managed:
                    skipped = isinstance(result, dict) and result.get("skipped")
                    event = "skipped" if skipped else "success" if result else "failure"
                    label = (
                        "⏭️ 已跳过"
                        if skipped
                        else "✅ 上传成功" if result else "❌ 上传失败"
                    )
                    dispatcher.submit(
                        config,
                        scope,
                        f"{label} [{uploader}]\n文件：{Path(file_path).name}"
                        + (f"\n说明：{error}" if error else ""),
                        event=event,
                    )

        return run

    return decorate


def channel_destination(config):
    channel = str(config.get("channel_chat_id", "") or "").strip()
    if channel and not channel.startswith(("@", "-")) and not channel.isdigit():
        channel = "@" + channel
    return channel


def notify_channel(config, text, scope):
    config = dict(config)
    channel = channel_destination(config)
    if not channel:
        return False
    config["chat_id"], config["thread_id"] = channel, ""
    return get_dispatcher().submit(config, "channel:" + scope, text, event="success")


def notify_post_action(config, action):
    if action.status not in ("moved", "deleted", "failed"):
        return
    kind = "移动" if action.kind == "move" else "删除"
    state = "成功" if action.status in ("moved", "deleted") else "失败"
    text = f"{'✅' if state == '成功' else '⚠️'} 上传后{kind}{state}\n种子：{action.torrent_name or action.torrent_hash}"
    if action.destination:
        text += f"\n目标目录：{action.destination}"
    if action.error:
        text += f"\n说明：{action.error}"
    text += "\n上传结果保持不变。" if state == "失败" else ""
    get_dispatcher().submit(
        config, f"post-upload:{action.id}", text, event="post_upload"
    )

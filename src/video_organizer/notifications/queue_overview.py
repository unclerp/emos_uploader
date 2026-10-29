"""持久的本轮队列总览；置顶仅操作本应用自己的总览消息。"""

import logging
import time
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

import requests

from ..database.models import (
    DownloadFileBinding,
    PostUploadAction,
    TelegramDelivery,
    TelegramQueueItem,
    TelegramQueueRun,
    UploadTask,
)
from ..database.session import get_session_local
from .telegram import digest, enabled

logger = logging.getLogger(__name__)
TERMINAL = {"completed", "skipped", "failed"}


def destination(config):
    return digest(
        "\0".join(
            str(config.get(k, "") or "").strip()
            for k in ("bot_token", "chat_id", "thread_id")
        )
    )


def flag(config, key):
    return str(config.get(key, True)).lower() not in ("false", "0", "off", "no")


class QueueOverview:
    def __init__(self, dispatcher):
        self.dispatcher = dispatcher
        self.configs = {}
        self.next_tick = 0.0

    def configure(self, config):
        """热更新后停止旧目的地的总览投递，不补发启用前的历史任务。"""
        with self.dispatcher.lock, get_session_local()() as db:
            previous = self.configs
            self.configs = (
                {destination(config): dict(config)} if enabled(config, "queue") else {}
            )
            for key, old in previous.items():
                if key not in self.configs:
                    self._deliveries(db, old).filter(
                        TelegramDelivery.scope.like("queue-%")
                    ).update(
                        {TelegramDelivery.pending: False}, synchronize_session=False
                    )
            if (
                not enabled(config, "queue")
                and config.get("bot_token")
                and config.get("chat_id")
            ):
                self._deliveries(db, config).filter(
                    TelegramDelivery.scope.like("queue-%")
                ).update({TelegramDelivery.pending: False}, synchronize_session=False)
            if enabled(config, "queue"):
                runs = (
                    db.query(TelegramQueueRun)
                    .filter_by(destination=destination(config))
                    .all()
                )
                for run in runs:
                    run.pin_blocked = False
                    if not run.finished_at:
                        run.last_text = ""
                active = [
                    row[0]
                    for row in db.query(UploadTask.id).filter(
                        UploadTask.status.in_(["queued", "processing", "uploading"])
                    )
                ]
            else:
                active = []
            db.commit()
        for task_id in active:
            self.track(config, task_id)
        self.next_tick = 0

    def track(self, config, task_id):
        if not enabled(config, "queue"):
            return
        with self.dispatcher.lock, get_session_local()() as db:
            if db.get(UploadTask, task_id) is None:
                return
            key = destination(config)
            self.configs[key] = dict(config)
            run = (
                db.query(TelegramQueueRun)
                .filter_by(destination=key, finished_at=None)
                .first()
            )
            if run is None:
                run = TelegramQueueRun(destination=key)
                db.add(run)
                db.flush()
            if db.get(TelegramQueueItem, (run.id, task_id)) is None:
                db.add(TelegramQueueItem(run_id=run.id, task_id=task_id))
            run.idle_since = None
            db.commit()
        self.dispatcher.register(config)
        self.dispatcher.wake.set()

    @staticmethod
    def _deliveries(db, config):
        return db.query(TelegramDelivery).filter_by(
            bot_key=digest(str(config["bot_token"]).strip()),
            chat_id=str(config["chat_id"]).strip(),
            thread_id=str(config.get("thread_id", "") or "").strip(),
        )

    def _state(self, db, run):
        tasks = (
            db.query(UploadTask)
            .join(TelegramQueueItem, TelegramQueueItem.task_id == UploadTask.id)
            .filter(TelegramQueueItem.run_id == run.id)
            .order_by(UploadTask.id)
            .all()
        )
        counts = Counter(task.status for task in tasks)
        active = [task for task in tasks if task.status not in TERMINAL | {"queued"}]
        actions = (
            db.query(PostUploadAction)
            .join(
                DownloadFileBinding,
                DownloadFileBinding.group_key == PostUploadAction.identity,
            )
            .join(
                TelegramQueueItem,
                TelegramQueueItem.task_id == DownloadFileBinding.task_id,
            )
            .filter(TelegramQueueItem.run_id == run.id)
            .distinct()
            .all()
        )
        operations = Counter()
        for action in actions:
            if action.status == "waiting":
                failed = (
                    db.query(UploadTask.id)
                    .join(
                        DownloadFileBinding,
                        DownloadFileBinding.task_id == UploadTask.id,
                    )
                    .filter(
                        DownloadFileBinding.group_key == action.identity,
                        UploadTask.status == "failed",
                    )
                    .first()
                )
                operations["blocked" if failed else "pending"] += 1
            elif action.status in ("moving", "deleting"):
                operations["pending"] += 1
            elif action.status == "failed":
                operations["failed"] += 1
            elif action.status in ("moved", "deleted"):
                operations["completed"] += 1
        return tasks, counts, active, operations

    def _text(self, run, tasks, counts, active, operations):
        total = len(tasks)
        ended = sum(counts[s] for s in TERMINAL)
        issues = counts["failed"] + operations["failed"] + operations["blocked"]
        label = (
            ("⚠️ 本轮结束，有失败待处理" if issues else "✅ 本轮全部完成")
            if run.finished_at
            else "📋 本轮队列总览"
        )
        filled = int(10 * ended / total) if total else 0
        lines = [
            label,
            f"已处理 {ended} / {total}",
            "▰" * filled + "▱" * (10 - filled),
            f"✅ 上传成功：{counts['completed']}",
            f"⏭️ 已跳过：{counts['skipped']}",
            f"❌ 上传失败：{counts['failed']}",
            f"⬆️ 正在处理：{len(active)}",
            f"⏳ 等待处理：{counts['queued']}",
        ]
        for task in active[:3]:
            progress = self.dispatcher.latest_progress.get(f"task:{task.id}")
            detail = (
                "100% · 等待最终确认"
                if progress is not None and progress >= 100
                else (
                    f"{progress:.1f}%"
                    if progress is not None
                    else "上传中" if task.status == "uploading" else "识别／处理中"
                )
            )
            lines.append(f"当前：{Path(task.file_path).name[:160]} · {detail}")
        if len(active) > 3:
            lines.append(f"另有 {len(active) - 3} 个文件正在处理")
        if operations:
            lines.extend(
                [
                    f"📦 上传后操作：等待／执行中 {operations['pending']} 个种子",
                    f"已完成 {operations['completed']} · 失败 {operations['failed']} · 被上传失败阻塞 {operations['blocked']}",
                ]
            )
        lines.append(f"本轮开始：{run.started_at:%m-%d %H:%M}")
        return "\n".join(lines)

    def tick(self, now=None):
        """后台刷新，单条总览最多每五秒编辑一次，结束立即更新。"""
        if not self.configs:
            return
        now = now or datetime.now()
        if time.monotonic() < self.next_tick:
            return
        self.next_tick = time.monotonic() + 1
        with self.dispatcher.lock, get_session_local()() as db:
            for key, config in list(self.configs.items()):
                runs = (
                    db.query(TelegramQueueRun)
                    .filter_by(destination=key)
                    .filter(
                        (TelegramQueueRun.finished_at.is_(None))
                        | (TelegramQueueRun.summary_queued.is_(False))
                    )
                    .all()
                )
                for run in runs:
                    if run.finished_at:
                        delivery = (
                            self._deliveries(db, config)
                            .filter_by(scope=f"queue-overview:{run.id}")
                            .first()
                        )
                        # 完整总览已更新成功才发底部总结；两个消息各自独立重试。
                        if delivery and delivery.message_id and not delivery.pending:
                            text = run.last_text.split("\n")
                            summary = "\n".join(
                                [
                                    text[0],
                                    text[1],
                                    *[
                                        line
                                        for line in text
                                        if line.startswith(
                                            (
                                                "✅ 上传",
                                                "⏭️",
                                                "❌",
                                                "已完成 ",
                                                "本轮开始",
                                            )
                                        )
                                    ],
                                    "完整统计见本轮总览消息。",
                                ]
                            )
                            if self.dispatcher.submit(
                                config, f"queue-final:{run.id}", summary, event="queue"
                            ):
                                run.summary_queued = True
                        continue
                    tasks, counts, active, operations = self._state(db, run)
                    busy = active or counts["queued"] or operations["pending"]
                    if busy:
                        run.idle_since = None
                    elif run.idle_since is None:
                        run.idle_since = now
                    elif (now - run.idle_since).total_seconds() >= 5:
                        run.finished_at = now
                    text = self._text(run, tasks, counts, active, operations)
                    if text != run.last_text and (
                        run.finished_at
                        or run.last_published_at is None
                        or (now - run.last_published_at).total_seconds() >= 5
                    ):
                        if self.dispatcher.submit(
                            config,
                            f"queue-overview:{run.id}",
                            text + f"\n更新于 {now:%H:%M:%S}",
                            terminal=bool(run.finished_at),
                            event="queue",
                            restart=True,
                        ):
                            run.last_text, run.last_published_at = text, now
            db.commit()

    def control_once(self):
        """后台逐条置顶／取消旧置顶，不清除用户或其他机器人的置顶。"""
        if not self.configs:
            return False
        d = self.dispatcher
        if not d.send_lock.acquire(blocking=False):
            return False
        try:
            if time.monotonic() < d.next_request:
                return False
            with d.lock, get_session_local()() as db:
                operation = None
                for key, config in list(self.configs.items()):
                    if not flag(config, "auto_pin_queue"):
                        continue
                    run = (
                        db.query(TelegramQueueRun)
                        .filter_by(destination=key)
                        .order_by(TelegramQueueRun.id.desc())
                        .first()
                    )
                    if not run:
                        continue
                    message = (
                        self._deliveries(db, config)
                        .filter_by(scope=f"queue-overview:{run.id}")
                        .first()
                    )
                    if not message or not message.message_id:
                        continue
                    if run.pin_blocked or (
                        run.pin_retry_at and run.pin_retry_at > datetime.now()
                    ):
                        continue
                    if run.pinned_message_id != message.message_id:
                        operation = (
                            run.id,
                            "pinChatMessage",
                            message.message_id,
                            dict(config),
                        )
                    else:
                        old = (
                            db.query(TelegramQueueRun)
                            .filter(
                                TelegramQueueRun.destination == key,
                                TelegramQueueRun.id < run.id,
                                TelegramQueueRun.pinned_message_id.is_not(None),
                                TelegramQueueRun.unpin_done.is_(False),
                            )
                            .order_by(TelegramQueueRun.id)
                            .first()
                        )
                        if old:
                            if old.pin_blocked or (
                                old.pin_retry_at and old.pin_retry_at > datetime.now()
                            ):
                                continue
                            operation = (
                                old.id,
                                "unpinChatMessage",
                                old.pinned_message_id,
                                dict(config),
                            )
                    if operation:
                        break
            if not operation:
                return False
            run_id, method, message_id, config = operation
            payload = {
                "chat_id": str(config["chat_id"]).strip(),
                "message_id": message_id,
            }
            if method == "pinChatMessage":
                payload["disable_notification"] = True
            ok, blocked, delay = False, False, 3
            try:
                response = requests.post(
                    f"https://api.telegram.org/bot{str(config['bot_token']).strip()}/{method}",
                    json=payload,
                    timeout=10,
                )
                data = response.json()
                ok = bool(data.get("ok"))
                if not ok:
                    blocked = response.status_code in (400, 401, 403, 404)
                    delay = max(
                        3, float(data.get("parameters", {}).get("retry_after", 10))
                    )
                    logger.warning(
                        "Telegram 总览置顶操作未完成（HTTP %s），请检查 Bot 权限或等待重试",
                        response.status_code,
                    )
            except Exception:
                logger.warning("Telegram 总览置顶请求失败，稍后重试")
                delay = 10
            with d.lock, get_session_local()() as db:
                run = db.get(TelegramQueueRun, run_id)
                if ok:
                    if method == "pinChatMessage":
                        run.pinned_message_id = message_id
                    else:
                        run.unpin_done = True
                    run.pin_failures = 0
                else:
                    run.pin_failures += 1
                    run.pin_blocked = blocked
                    delay = max(delay, min(300, 2 ** min(run.pin_failures, 8)))
                run.pin_retry_at = datetime.now() + timedelta(seconds=delay)
                db.commit()
                d.next_request = time.monotonic() + delay
            return True
        finally:
            d.send_lock.release()

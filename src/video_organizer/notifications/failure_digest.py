"""按时间窗口合并失败；投递、限流和消息 ID 仍由公共发件箱管理。"""

from datetime import datetime
from pathlib import Path

from ..database.models import (
    TelegramFailureGroup,
    TelegramFailureItem,
    UploadTask,
    TelegramDelivery,
)
from ..database.session import get_session_local
from .queue_overview import destination
from .telegram import enabled


class FailureDigest:
    def __init__(self, dispatcher):
        self.dispatcher = dispatcher
        self.configs = {}

    def configure(self, config):
        from .telegram import compact

        self.configs = (
            {destination(config): dict(config)}
            if compact(config) and enabled(config, "failure")
            else {}
        )

        if not self.configs and config.get("bot_token") and config.get("chat_id"):
            with self.dispatcher.lock, get_session_local()() as db:
                self.dispatcher.overview._deliveries(db, config).filter(
                    TelegramDelivery.scope.like("failure-digest:%")
                ).update({"pending": False}, synchronize_session=False)
                db.commit()

    def record(self, config, task_id, now=None):
        if not enabled(config, "failure"):
            return False
        now = now or datetime.now()
        with self.dispatcher.lock, get_session_local()() as db:
            task = db.get(UploadTask, task_id)
            if task is None or task.status != "failed":
                return False
            key = destination(config)
            self.configs[key] = dict(config)
            # 同一任务的重复失败优先归回尚未解决的原提醒。
            group = (
                db.query(TelegramFailureGroup)
                .join(
                    TelegramFailureItem,
                    TelegramFailureItem.group_id == TelegramFailureGroup.id,
                )
                .filter(
                    TelegramFailureGroup.destination == key,
                    TelegramFailureGroup.resolved.is_(False),
                    TelegramFailureItem.task_id == task_id,
                )
                .first()
            )
            if group is None:
                group = (
                    db.query(TelegramFailureGroup)
                    .filter_by(destination=key, resolved=False)
                    .order_by(TelegramFailureGroup.id.desc())
                    .first()
                )
                if group is None or (now - group.opened_at).total_seconds() >= int(
                    config.get("failure_window_seconds", 60)
                ):
                    group = TelegramFailureGroup(destination=key, opened_at=now)
                    db.add(group)
                    db.flush()
                if db.get(TelegramFailureItem, (group.id, task_id)) is None:
                    db.add(TelegramFailureItem(group_id=group.id, task_id=task_id))
            db.commit()
        self.dispatcher.register(config)
        return True

    def tick(self, now=None):
        now = now or datetime.now()
        with self.dispatcher.lock, get_session_local()() as db:
            for key, config in list(self.configs.items()):
                window = int(config.get("failure_window_seconds", 60))
                for group in db.query(TelegramFailureGroup).filter_by(
                    destination=key, resolved=False
                ):
                    tasks = (
                        db.query(UploadTask)
                        .join(
                            TelegramFailureItem,
                            TelegramFailureItem.task_id == UploadTask.id,
                        )
                        .filter(TelegramFailureItem.group_id == group.id)
                        .order_by(UploadTask.id)
                        .all()
                    )
                    if not tasks:
                        continue
                    solved = [t for t in tasks if t.status in ("completed", "skipped")]
                    resolved = len(solved) == len(tasks)
                    if (
                        now - group.opened_at
                    ).total_seconds() < window and not resolved:
                        continue
                    # 窗口内已经解决的短暂失败不再新发提醒。
                    if resolved and not group.last_text:
                        group.resolved = True
                        continue
                    lines = [
                        "✅ 上传失败已全部解决" if resolved else "⚠️ 上传失败汇总",
                        f"共 {len(tasks)} 项 · 已解决 {len(solved)} · 待处理 {len(tasks)-len(solved)}",
                    ]
                    for task in tasks[:15]:
                        label = (
                            "✅ 已解决"
                            if task in solved
                            else (
                                "🔄 重试中"
                                if task.status in ("queued", "processing", "uploading")
                                else "❌ 失败"
                            )
                        )
                        lines.append(f"{label}：{Path(task.file_path).name[:100]}")
                        if task.status == "failed" and task.error_message:
                            lines.append(f"原因：{task.error_message[:120]}")
                    if len(tasks) > 15:
                        lines.append(f"另有 {len(tasks)-15} 项，详情见网页失败列表。")
                    text = "\n".join(lines)
                    if text != group.last_text and (
                        resolved
                        or group.last_published_at is None
                        or (now - group.last_published_at).total_seconds() >= window
                    ):
                        if self.dispatcher.submit(
                            config,
                            f"failure-digest:{group.id}",
                            text,
                            terminal=resolved,
                            event="failure",
                            restart=True,
                        ):
                            group.last_text, group.last_published_at, group.resolved = (
                                text,
                                now,
                                resolved,
                            )
            db.commit()

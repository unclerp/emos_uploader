"""上传任务汇总和尝试历史；原始 task_history 始终保留。"""

import hashlib
import json
import threading
from datetime import datetime
from pathlib import Path

from .models import TaskHistory, UploadTask, UploadAttempt
from .session import get_session_local

# SQLite 写入与同一进程内的首次任务创建串行化。
lock = threading.RLock()


def _identity(path, targets):
    path = Path(path).absolute()
    stat = path.stat()
    fingerprint = f"{stat.st_size}:{stat.st_mtime_ns}"
    target = ",".join(
        sorted(set(targets.split(",") if isinstance(targets, str) else targets))
    )
    key = hashlib.sha256(f"{path}\0{fingerprint}\0{target}".encode()).hexdigest()
    return str(path), target, fingerprint, key


def _append(db, task, history, auto_retry=False):
    db.flush()
    db.add(UploadAttempt(task_id=task.id, history_id=history.id))
    task.auto_retry = history.status == "failed" and auto_retry
    task.status = history.status
    task.error_message = history.error_message
    task.updated_at = history.end_time or history.start_time or datetime.now()
    task.attempt_count = (task.attempt_count or 0) + 1
    task.failure_count = (task.failure_count or 0) + (history.status == "failed")
    task.cleared = False


def migrate_history(db):
    """幂等导入旧记录；历史无目标和文件指纹，明确标为未知。"""
    rows = (
        db.query(TaskHistory)
        .outerjoin(UploadAttempt, UploadAttempt.history_id == TaskHistory.id)
        .filter(UploadAttempt.id.is_(None))
        .order_by(TaskHistory.id)
        .all()
    )
    for row in rows:
        key = hashlib.sha256(("legacy\0" + row.file_path).encode()).hexdigest()
        task = db.query(UploadTask).filter_by(identity=key).first()
        if task is None:
            task = UploadTask(identity=key, file_path=row.file_path, targets="unknown")
            db.add(task)
            db.flush()
        _append(db, task, row)
    db.commit()


def prepare_task(path, targets, task_id=None, status="processing", tmdb_override=None):
    path, target, fingerprint, key = _identity(path, targets)
    with lock, get_session_local()() as db:
        task = (
            db.get(UploadTask, task_id)
            if task_id
            else db.query(UploadTask).filter_by(identity=key).first()
        )
        if task_id and task is None:
            raise ValueError("任务不存在")
        if task is not None:
            if str(Path(task.file_path).absolute()) != path:
                raise ValueError("任务路径不匹配")
            if task.targets not in ("unknown", target):
                raise ValueError("上传目标已变更，请按当前配置新建任务")
            if task.fingerprint and task.fingerprint != fingerprint:
                raise ValueError("文件已经变化，请新建任务，不能覆盖原文件的失败记录")
            collision = db.query(UploadTask).filter_by(identity=key).first()
            if collision and collision.id != task.id:
                raise ValueError("当前文件已有独立任务，请重试对应任务")
            task.identity, task.targets, task.fingerprint = key, target, fingerprint
        else:
            task = UploadTask(
                identity=key, file_path=path, targets=target, fingerprint=fingerprint
            )
            db.add(task)
        if tmdb_override:
            task.tmdb_override = json.dumps(tmdb_override)
        task.status, task.updated_at, task.cleared = status, datetime.now(), False
        db.commit()
        return task.id


def get_file_task(path, targets):
    """查询同一文件版本和目标的任务，供下载器重扫和重启去重使用。"""
    _, _, _, key = _identity(path, targets)
    with lock, get_session_local()() as db:
        task = db.query(UploadTask).filter_by(identity=key).first()
        if task is None:
            return None
        result = serialize(task)
        result["updated_at"] = task.updated_at
        result["auto_retry"] = task.auto_retry
        return result


def append_result(db, history, task_id=None, targets="unknown", auto_retry=False):
    task = db.get(UploadTask, task_id) if task_id else None
    if task is None:
        try:
            path, target, fingerprint, key = _identity(history.file_path, targets)
        except OSError:
            path, target, fingerprint = history.file_path, targets, None
            key = hashlib.sha256(f"missing\0{path}\0{target}".encode()).hexdigest()
        task = db.query(UploadTask).filter_by(identity=key).first()
        if task is None:
            task = UploadTask(
                identity=key, file_path=path, targets=target, fingerprint=fingerprint
            )
            db.add(task)
            db.flush()
    _append(db, task, history, auto_retry=auto_retry)
    return task


def serialize(task):
    from ..core.post_upload import task_action

    return {
        "post_upload": task_action(task),
        "id": task.id,
        "path": task.file_path,
        "status": task.status,
        "error": task.error_message,
        "time": task.updated_at.isoformat() if task.updated_at else None,
        "targets": task.targets,
        "attempt_count": task.attempt_count,
        "failure_count": task.failure_count,
        "resolved": task.status in ("completed", "skipped") and task.failure_count > 0,
    }


def query_tasks(db, status=None, search=None):
    query = db.query(UploadTask).filter_by(cleared=False)
    if status == "resolved":
        query = query.filter(
            UploadTask.status.in_(["completed", "skipped"]),
            UploadTask.failure_count > 0,
        )
    elif status == "issues":
        query = query.filter(UploadTask.failure_count > 0)
    elif status == "failed":
        query = query.filter(
            UploadTask.status.in_(["failed", "queued", "processing", "uploading"]),
            UploadTask.failure_count > 0,
        )
    elif status:
        query = query.filter(UploadTask.status == status)
    if search:
        query = query.filter(UploadTask.file_path.contains(search))
    return query


def get_attempts(task_id):
    with get_session_local()() as db:
        task = db.get(UploadTask, task_id)
        if task is None:
            return None
        rows = (
            db.query(TaskHistory)
            .join(UploadAttempt, UploadAttempt.history_id == TaskHistory.id)
            .filter(UploadAttempt.task_id == task_id)
            .order_by(TaskHistory.id)
            .all()
        )
        return {
            "task": serialize(task),
            "tmdb_override": (
                json.loads(task.tmdb_override) if task.tmdb_override else None
            ),
            "attempts": [
                {
                    "status": r.status,
                    "error": r.error_message,
                    "time": (
                        (r.end_time or r.start_time).isoformat()
                        if r.end_time or r.start_time
                        else None
                    ),
                }
                for r in rows
            ],
        }


def recover_interrupted(db):
    """进程重启后不把中断中的重试误报成已解决。"""
    for task in db.query(UploadTask).filter(
        UploadTask.status.in_(["queued", "processing", "uploading"])
    ):
        history = TaskHistory(
            file_path=task.file_path,
            status="failed",
            error_message="服务重启，前次处理未完成，请重试",
            end_time=datetime.now(),
        )
        db.add(history)
        _append(db, task, history, auto_retry=True)
    db.commit()


def set_task_status(task_id, status):
    with lock, get_session_local()() as db:
        task = db.get(UploadTask, task_id)
        if task:
            task.status, task.updated_at = status, datetime.now()
            db.commit()

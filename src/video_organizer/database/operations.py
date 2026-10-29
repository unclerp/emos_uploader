import logging
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from .session import get_session_local, init_db
from .models import TaskHistory

logger = logging.getLogger(__name__)


def record_task(
    file_path: str,
    status: str,
    error_message: Optional[str] = None,
    start_time: Optional[datetime] = None,
    end_time: Optional[datetime] = None,
    task_id: Optional[int] = None,
    targets: str = "unknown",
    auto_retry: bool = False,
    telegram_config: Optional[Dict] = None,
) -> bool:
    """
    记录任务完成或失败到数据库

    Returns:
        是否成功写入
    """
    try:
        session_local = get_session_local()
    except Exception:
        return False

    try:
        from .task_tracking import lock, append_result

        with lock, session_local() as db:
            record = TaskHistory(
                file_path=file_path,
                status=status,
                error_message=error_message,
                start_time=start_time or datetime.now(),
                end_time=end_time,
            )
            db.add(record)
            task = append_result(db, record, task_id, targets, auto_retry=auto_retry)
            db.commit()
            notification = (
                task.id,
                task.file_path,
                task.status,
                task.error_message,
                task.failure_count,
                task.attempt_count,
                task.targets,
            )
        if telegram_config:
            try:
                from ..notifications.telegram import (
                    notify_task_result,
                    track_queue_task,
                )

                track_queue_task(telegram_config, notification[0])
                notify_task_result(telegram_config, *notification)
            except Exception:
                logger.warning("任务已保存，但通知暂存失败")
        # 通知仪表盘更新（惰性导入避免循环依赖）
        try:
            from ..web.services.state import get_state_manager

            get_state_manager().notify_dashboard_update()
        except Exception:
            pass
        return True
    except Exception as e:
        logger.warning(f"写入任务历史到数据库失败: {e}")
        return False


def get_tasks_paginated(status=None, search=None, offset=0, limit=20):
    from .task_tracking import query_tasks, serialize

    with get_session_local()() as db:
        query = query_tasks(db, status, search)
        total = query.count()
        from .models import UploadTask

        tasks = (
            query.order_by(UploadTask.updated_at.desc(), UploadTask.id.desc())
            .offset(offset)
            .limit(limit)
            .all()
        )
        return [serialize(t) for t in tasks], total


def get_completed_tasks(limit=200):
    return get_tasks_paginated(status="completed", limit=limit)[0]


def get_failed_tasks(limit=200):
    return {
        t["path"]: {"error": t["error"] or "", "time": t["time"], "id": t["id"]}
        for t in reversed(get_tasks_paginated(status="failed", limit=limit)[0])
    }


def get_task_counts():
    from .task_tracking import query_tasks

    result = {}
    with get_session_local()() as db:
        for status in ("completed", "failed", "resolved"):
            result[status] = query_tasks(db, status).count()
    return result


def get_all_tasks_for_dashboard(limit=20):
    return get_tasks_paginated(limit=limit)[0]


def delete_failed_task(file_path, task_id=None):
    from .task_tracking import query_tasks
    from .models import UploadTask

    with get_session_local()() as db:
        query = query_tasks(db, "failed").filter(
            UploadTask.status == "failed", UploadTask.file_path == file_path
        )
        if task_id is not None:
            query = query.filter(UploadTask.id == task_id)
        count = query.update({UploadTask.cleared: True}, synchronize_session=False)
        db.commit()
        return count > 0


def delete_all_failed_tasks():
    from .task_tracking import query_tasks
    from .models import UploadTask

    with get_session_local()() as db:
        count = (
            query_tasks(db, "failed")
            .filter(UploadTask.status == "failed")
            .update({UploadTask.cleared: True}, synchronize_session=False)
        )
        db.commit()
        return count


def get_recent_activity_paginated(page=1, page_size=20, search=None):
    return get_tasks_paginated(
        search=search, offset=(page - 1) * page_size, limit=page_size
    )

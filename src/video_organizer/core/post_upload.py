"""按 qB 种子协调上传后的移动/删除，只有下载器实际状态能确认完成。"""

import hashlib
import logging
import threading
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from sqlalchemy import or_
from sqlalchemy.orm import object_session

from ..database.models import DownloadFileBinding, PostUploadAction, UploadTask
from ..database.session import get_session_local

logger = logging.getLogger(__name__)
# 热更新的新旧监控器不能对同一种子同时发起操作。
_action_lock = threading.RLock()
TERMINAL_UPLOAD = ("completed", "skipped")
BUSY_ACTION = ("moving", "deleting")


def post_upload_policy(processing):
    """新配置优先；未设置时兼容旧版上传后删除开关。"""
    legacy = str(processing.get("delete_after_upload", False)).lower() in (
        "true",
        "1",
        "yes",
        "on",
    )
    kind = str(processing.get("post_upload_action", "")).strip() or (
        "delete" if legacy else "keep"
    )
    destination = str(processing.get("post_upload_move_path", "")).strip()
    if kind not in ("keep", "move", "delete"):
        raise ValueError("上传后操作必须为 keep、move 或 delete")
    if kind == "move" and (
        not destination
        or not (
            PurePosixPath(destination).is_absolute()
            or PureWindowsPath(destination).is_absolute()
        )
        or any(c in destination for c in ("\n", "\r", "\0"))
    ):
        raise ValueError("通过 qB 移动需要填写 qB 可访问的绝对目标目录")
    return kind, destination


def _commit(db, telegram_config=None):
    from sqlalchemy import inspect

    results = [
        obj
        for obj in db.dirty
        if isinstance(obj, PostUploadAction)
        and inspect(obj).attrs.status.history.has_changes()
        and obj.status in ("moved", "deleted", "failed")
    ]
    db.commit()
    if telegram_config:
        from ..notifications.telegram import notify_post_action

        for action in results:
            try:
                notify_post_action(telegram_config, action)
            except Exception:
                logger.warning("上传后操作已保存，但通知暂存失败")
    try:
        from ..web.services.state import get_state_manager

        get_state_manager().notify_dashboard_update()
    except Exception:
        logger.debug("上传后操作已保存，等待网页下一次刷新")


def _hash(*parts):
    return hashlib.sha256("\0".join(map(str, parts)).encode()).hexdigest()


def _path(path):
    # 只比较 qB 原样返回的路径，不 URL 解码、不转小写、不解析容器文件系统。
    return str(PurePosixPath(path.replace("\\", "/")))


def action_json(action):
    return {
        k: getattr(action, k)
        for k in (
            "id",
            "torrent_hash",
            "torrent_name",
            "kind",
            "destination",
            "status",
            "error",
            "attempt_count",
        )
    } | {"time": action.updated_at.isoformat() if action.updated_at else None}


def list_actions(offset=0, limit=20, search=""):
    with get_session_local()() as db:
        query = db.query(PostUploadAction)
        if search:
            query = query.filter(PostUploadAction.torrent_name.contains(search))
        total = query.count()
        rows = (
            query.order_by(
                PostUploadAction.updated_at.desc(), PostUploadAction.id.desc()
            )
            .offset(offset)
            .limit(limit)
            .all()
        )
        return [action_json(row) for row in rows], total


def task_action(task):
    db = object_session(task)
    if db is None:
        return None
    row = (
        db.query(PostUploadAction)
        .join(
            DownloadFileBinding,
            DownloadFileBinding.group_key == PostUploadAction.identity,
        )
        .filter(DownloadFileBinding.task_id == task.id)
        .order_by(PostUploadAction.id.desc())
        .first()
    )
    return action_json(row) if row else None


def path_busy(path):
    """手动重新处理也不能与正在移动/删除同一路径交叉。"""
    with get_session_local()() as db:
        return (
            db.query(PostUploadAction.id)
            .join(
                DownloadFileBinding,
                DownloadFileBinding.group_key == PostUploadAction.identity,
            )
            .filter(
                or_(
                    DownloadFileBinding.current_path == path,
                    DownloadFileBinding.pending_path == path,
                ),
                PostUploadAction.status.in_(BUSY_ACTION),
            )
            .first()
            is not None
        )


class QBPostUpload:
    def __init__(self, monitor, handler, processing):
        self.monitor, self.handler = monitor, handler
        self.kind, self.destination = post_upload_policy(processing)
        self.targets = ",".join(sorted(handler.upload_targets))
        self.downloader_key = _hash(monitor.rpc_url.rstrip("/"), monitor.username)

    def group_key(self, torrent_hash):
        return _hash(self.downloader_key, torrent_hash, self.targets)

    def file_key(self, torrent_hash, index, size):
        return _hash(self.group_key(torrent_hash), index, size)

    def bind(self, path, task_id, schedule=False):
        source = self.monitor._file_sources.get(path)
        if source is None:
            raise ValueError("缺少 qB 文件来源，不能安排上传后操作")
        torrent_hash, index, raw = source
        size = Path(path).stat().st_size
        with get_session_local()() as db:
            key = self.file_key(torrent_hash, index, size)
            row = db.get(DownloadFileBinding, key)
            if row is None:
                row = DownloadFileBinding(
                    identity=key,
                    group_key=self.group_key(torrent_hash),
                    downloader_key=self.downloader_key,
                    torrent_hash=torrent_hash,
                    file_index=index,
                    file_size=size,
                    task_id=task_id,
                    source_path=raw,
                    current_path=path,
                )
                db.add(row)
            else:
                row.task_id, row.current_path, row.source_path = task_id, path, raw
            # 旧的已完成记录只建立关联；切换配置不能追溯移动以前的种子。
            if schedule and self.kind != "keep":
                action = (
                    db.query(PostUploadAction).filter_by(identity=row.group_key).first()
                )
                if action is None:
                    db.add(
                        PostUploadAction(
                            identity=row.group_key,
                            downloader_key=self.downloader_key,
                            torrent_hash=torrent_hash,
                            targets=self.targets,
                            kind=self.kind,
                            destination=self.destination,
                            status="waiting",
                        )
                    )
            _commit(db, getattr(self.handler, "telegram_config", {}))

    def handled(self, torrent, file):
        key = self.file_key(torrent["hash"], file.get("index"), file.get("size"))
        with get_session_local()() as db:
            row = db.get(DownloadFileBinding, key)
            task = db.get(UploadTask, row.task_id) if row else None
            return task is not None and task.status in TERMINAL_UPLOAD

    def _selected(self, files):
        return [
            f
            for f in files
            if f.get("priority", 0) > 0
            and Path(f.get("name", "")).suffix.lower()
            in self.monitor.supported_extensions
        ]

    def _all_done(self, db, torrent, files, action):
        selected = self._selected(files)
        if not selected or not self.monitor._torrent_ready(torrent):
            return False
        paths = []
        for file in selected:
            if file.get("progress") != 1 or not isinstance(file.get("index"), int):
                return False
            row = db.get(
                DownloadFileBinding,
                _hash(action.identity, file["index"], file.get("size")),
            )
            task = db.get(UploadTask, row.task_id) if row else None
            if (
                task is None
                or task.status not in TERMINAL_UPLOAD
                or task.targets != action.targets
            ):
                return False
            paths.extend(
                [
                    row.current_path,
                    task.file_path,
                    self.monitor._apply_path_mapping(
                        self.monitor._source_path(torrent, file)
                    ),
                ]
            )
        # 同一物理种子在配置变更后可能有另一组目标；也不能中途移动或删除。
        bindings = (
            db.query(DownloadFileBinding)
            .filter_by(downloader_key=self.downloader_key, torrent_hash=torrent["hash"])
            .all()
        )
        for binding in bindings:
            task = db.get(UploadTask, binding.task_id)
            if task is None or task.status not in TERMINAL_UPLOAD:
                return False
            paths.extend([binding.current_path, task.file_path])
        paths.extend(
            self.monitor._apply_path_mapping(self.monitor._source_path(torrent, f))
            for f in files
        )
        # 完成记录早于 worker 最后的资源清理；必须等它们都离开工作区。
        return not any(
            path in self.handler._processing_files
            or path in self.handler._queued_files
            or path in self.handler._uploading_files
            for path in paths
        )

    def _finish_move(self, db, torrent, files, action):
        for file in files:
            bindings = (
                db.query(DownloadFileBinding)
                .filter_by(
                    downloader_key=self.downloader_key,
                    torrent_hash=torrent["hash"],
                    file_index=file.get("index"),
                    file_size=file.get("size"),
                )
                .all()
            )
            for row in bindings:
                old_path = row.current_path
                raw = self.monitor._source_path(torrent, file)
                new_path = self.monitor._apply_path_mapping(raw)
                row.source_path, row.current_path, row.pending_path = (
                    raw,
                    new_path,
                    None,
                )
                self.monitor._file_sources.pop(old_path, None)
                self.monitor._file_sources[new_path] = (
                    torrent["hash"],
                    file["index"],
                    raw,
                )
                self.handler._file_downloader_map.pop(old_path, None)
                self.handler._file_downloader_map[new_path] = self.monitor
                self.handler._uploaded_files.add(new_path)
        action.status, action.error = "moved", None
        action.updated_at = datetime.now()
        _commit(db, getattr(self.handler, "telegram_config", {}))

    def reconcile(self, torrents):
        """每次原生轮询推进状态；HTTP 200 只表示接收，不能直接标记移动成功。"""
        by_hash = {t["hash"]: t for t in torrents if t.get("hash")}
        with ExitStack() as leases:
            manager = getattr(self, "runtime_manager", None)
            if manager:
                with manager.lock:
                    if manager.current is not self.generation:
                        return
                    leases.enter_context(manager.lease(self.generation))
            self._reconcile_snapshot(by_hash)

    def _reconcile_snapshot(self, by_hash):
        with _action_lock, get_session_local()() as db:
            actions = (
                db.query(PostUploadAction)
                .filter_by(downloader_key=self.downloader_key)
                .filter(PostUploadAction.status.notin_(["moved", "deleted"]))
                .all()
            )
            for action in actions:
                try:
                    self._reconcile_action(db, action, by_hash.get(action.torrent_hash))
                except Exception as exc:
                    db.rollback()
                    logger.warning("上传后操作检查失败，下一轮继续: %s", exc)

    def _reconcile_action(self, db, action, torrent):
        now = datetime.now()
        if torrent is None:
            if action.kind == "delete" and action.requested_at:
                action.status, action.error = "deleted", None
            elif action.status != "failed":
                action.status, action.error = (
                    "failed",
                    "qB 中已找不到该种子，未执行操作",
                )
            action.updated_at = now
            _commit(db, getattr(self.handler, "telegram_config", {}))
            return
        name = torrent.get("name", action.torrent_hash)
        if action.torrent_name != name:
            action.torrent_name = name
            _commit(db, getattr(self.handler, "telegram_config", {}))
        files = self.monitor._get_torrent_files(action.torrent_hash)
        # 即使上次请求超时或报错，先确认实际结果，避免盲目重复请求。
        if (
            action.kind == "move"
            and action.requested_at
            and (
                _path(torrent.get("save_path", "")) == _path(action.destination)
                and self.monitor._torrent_ready(torrent)
                and bool(self._selected(files))
                and all(f.get("progress") == 1 for f in self._selected(files))
            )
        ):
            self._finish_move(db, torrent, files, action)
            return
        if action.status in BUSY_ACTION:
            if torrent.get("state") == "moving":
                return
            if torrent.get("state") in ("error", "missingFiles"):
                action.status, action.error = (
                    "failed",
                    "qB 报告文件错误，请检查目标目录和磁盘后重试",
                )
            elif (
                action.requested_at
                and (now - action.requested_at).total_seconds() >= 60
            ):
                action.status, action.error = (
                    "failed",
                    "qB 尚未确认操作完成，请检查保存位置、权限及剩余空间后重试",
                )
            else:
                return
            action.updated_at = now
            _commit(db, getattr(self.handler, "telegram_config", {}))
            return
        if action.status == "failed":
            return
        # 发起操作前重新读取原生状态，不能用长队列前半轮的旧快照。
        response = self.monitor.session.get(
            f"{self.monitor.rpc_url}/torrents/info",
            params={"hashes": action.torrent_hash},
            timeout=30,
        )
        response.raise_for_status()
        fresh = next(
            (t for t in response.json() if t.get("hash") == action.torrent_hash), None
        )
        if fresh is None:
            return
        files = self.monitor._get_torrent_files(action.torrent_hash)
        with self.handler._queue_lock:
            other_busy = (
                db.query(PostUploadAction.id)
                .filter(
                    PostUploadAction.downloader_key == self.downloader_key,
                    PostUploadAction.torrent_hash == action.torrent_hash,
                    PostUploadAction.id != action.id,
                    PostUploadAction.status.in_(BUSY_ACTION),
                )
                .first()
            )
            if other_busy or not self._all_done(db, fresh, files, action):
                return
            if action.kind == "move":
                for file in self._selected(files):
                    binding = db.get(
                        DownloadFileBinding,
                        _hash(action.identity, file["index"], file.get("size")),
                    )
                    binding.pending_path = self.monitor._apply_path_mapping(
                        str(Path(action.destination) / file["name"])
                    )
            action.status = "moving" if action.kind == "move" else "deleting"
            action.attempt_count += 1
            action.requested_at = action.updated_at = now
            action.error = None
            _commit(db, getattr(self.handler, "telegram_config", {}))
        endpoint = "setLocation" if action.kind == "move" else "delete"
        data = {"hashes": action.torrent_hash}
        data.update(
            {"location": action.destination}
            if action.kind == "move"
            else {"deleteFiles": "true"}
        )
        try:
            response = self.monitor.session.post(
                f"{self.monitor.rpc_url}/torrents/{endpoint}", data=data, timeout=30
            )
            if response.status_code != 200:
                action.status, action.error = (
                    "failed",
                    f"qB 拒绝操作（HTTP {response.status_code}），请检查目录及权限",
                )
        except Exception:
            # 超时不能证明 qB 没有接收；持续查询实际位置，暂不重发。
            action.error = "请求结果待确认，正在查询 qB 实际状态"
        action.updated_at = datetime.now()
        _commit(db, getattr(self.handler, "telegram_config", {}))

    def retry(self, action_id):
        with _action_lock, get_session_local()() as db:
            action = db.get(PostUploadAction, action_id)
            if action is None or action.downloader_key != self.downloader_key:
                raise ValueError("找不到对应下载器或上传目标的操作")
            if action.status != "failed":
                raise ValueError("当前操作没有失败，无需重复提交")
            # 保留 requested_at 供下一轮先确认已完成的移动，仍未完成才重新发起。
            action.status, action.error, action.updated_at = (
                "waiting",
                None,
                datetime.now(),
            )
            _commit(db, getattr(self.handler, "telegram_config", {}))

"""
状态管理服务

管理 VideoFileHandler 实例和全局状态，
为 Web API 提供统一的状态访问接口。
"""

import threading
import copy
from contextlib import contextmanager
import logging
import asyncio
from typing import Any, Dict, List, Optional, Set, Callable
from pathlib import Path
from dataclasses import dataclass, field
from datetime import datetime

from ...database.operations import get_completed_tasks as db_get_completed
from ...database.operations import get_failed_tasks as db_get_failed
from ...database.operations import get_task_counts as db_get_task_counts


@dataclass
class TaskInfo:
    """任务信息"""

    file_path: str
    status: str  # queued, processing, uploading, completed, skipped, failed
    error_message: Optional[str] = None
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None


@dataclass
class UploadProgress:
    """上传进度"""

    file_path: str
    filename: str
    uploader: str  # cloud189, yun139, p123, emos
    progress: float  # 0-100
    uploaded_bytes: int
    total_bytes: int
    speed: str  # 如 "5.2 MB/s"
    status: str  # uploading, completed, skipped, failed
    error: Optional[str] = None
    start_time: Optional[datetime] = None
    last_update: Optional[datetime] = None


@dataclass
class SystemStatus:
    """系统状态"""

    queue_size: int = 0
    processing_count: int = 0
    uploading_count: int = 0
    completed_count: int = 0
    failed_count: int = 0
    resolved_count: int = 0
    is_running: bool = False
    last_update: Optional[datetime] = None


class StateManager:
    """
    状态管理器（单例模式）

    管理 VideoFileHandler 实例和全局状态，
    为 Web API 提供统一的状态访问接口。
    """

    _instance: Optional["StateManager"] = None
    _lock: threading.Lock = threading.Lock()

    def __new__(cls) -> "StateManager":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True

        self._logger = logging.getLogger(__name__)

        # VideoFileHandler 引用
        self._video_handler: Any = None
        self._config: Dict[str, Any] = {}
        self._config_path: Optional[Path] = None

        # 下载器监控器列表
        self._downloader_monitors: List[Any] = []

        # TMDB 共享客户端（懒初始化，进程内复用限速预算与缓存）
        self._tmdb_client: Any = None

        # 系统运行状态
        self._system_running: bool = False

        # 任务历史记录（最近 1000 条）
        self._task_history: List[TaskInfo] = []
        self._max_history: int = 1000

        # 上传进度追踪
        self._upload_progress: Dict[str, UploadProgress] = (
            {}
        )  # file_path -> UploadProgress
        self._progress_callbacks: List[Callable] = []  # 进度更新回调列表

        # 仪表盘更新回调
        self._dashboard_callbacks: List[Callable] = []

        # 状态锁
        self._state_lock = threading.RLock()
        self._live_config = None

    def set_video_handler(self, handler: Any) -> None:
        """设置 VideoFileHandler 实例"""
        with self._state_lock:
            self._video_handler = handler
            self._logger.info("VideoHandler 实例已设置")

    def get_video_handler(self) -> Optional[Any]:
        """获取 VideoFileHandler 实例"""
        from .live_config import borrowed_generation

        generation = borrowed_generation.get()
        if generation is None and self._live_config:
            generation = self._live_config.current
        return generation.handler if generation else self._video_handler

    def set_config(
        self, config: Dict[str, Any], config_path: Optional[Path] = None
    ) -> None:
        """设置配置"""
        with self._state_lock:
            self._config = copy.deepcopy(config)
            if config_path is not None:
                self._config_path = Path(config_path)

    def get_config(self) -> Dict[str, Any]:
        """获取配置"""
        from .live_config import borrowed_generation

        generation = borrowed_generation.get()
        return copy.deepcopy(generation.config if generation else self._config)

    def get_saved_config(self):
        """编辑页展示已保存配置；与当前仍在运行的配置代明确区分。"""
        with self._state_lock:
            return copy.deepcopy(self._config)

    def enable_live_config(
        self, config, config_path=None, monitor=None, cleaner=None, tracker=None
    ):
        from .live_config import LiveConfig
        from ...core.config_loader import get_default_config_path

        self.set_config(config, Path(config_path or get_default_config_path()))
        if self._live_config is None:
            self._live_config = LiveConfig(self)
            self._live_config.attach(
                config, self._video_handler, monitor, cleaner, tracker
            )
        elif (monitor is not None and self._live_config.monitor is None) or (
            self._live_config.root is None and self._video_handler is not None
        ):
            # web.app 的导入会先创建无处理器的应用；主程序随后注册真实运行组件。
            self._live_config.attach(
                config, self._video_handler, monitor, cleaner, tracker
            )
        return self._live_config

    def apply_config(self, config, config_path=None):
        manager = self._live_config or self.enable_live_config(
            self._config, self._config_path
        )
        # 持久化值可以与旧任务使用的配置不同，失败时也不能误报已生效。
        self.set_config(config, config_path)
        return manager.apply(config, self._config_path)

    def get_config_application_status(self):
        return (
            self._live_config.status()
            if self._live_config
            else {"status": "idle", "version": 0, "retiring": [], "error": ""}
        )

    @contextmanager
    def borrow_components(self):
        if self._live_config and self._live_config.current:
            with self._live_config.lease() as generation:
                yield generation
        else:
            yield None

    def make_task_renamer(self, config):
        from ...core.renamer import VideoRenamer
        from ...core.manual_rule_engine import ManualRuleEngine

        handler = self.get_video_handler()
        if handler and getattr(handler, "renamer", None):
            renamer = copy.copy(handler.renamer)
            settings = config.get("manual_rules", {})
            renamer.rule_engine = ManualRuleEngine(
                settings.get("rules", []), enabled=settings.get("enabled", False)
            )
            return renamer
        return VideoRenamer(
            tmdb_api_key=config.get("tmdb", {}).get("api_key", ""),
            naming_rules=config.get("naming_rules"),
            config=config,
        )

    def get_config_path(self) -> Optional[Path]:
        """获取配置文件路径"""
        return self._config_path

    def get_tmdb_client(self):
        """
        获取共享 TMDB 客户端（懒初始化单例）。

        复用限速预算与请求缓存，避免每个请求新建客户端导致限速计数互不共享。
        无 api_key 或初始化失败时返回 None。
        """
        handler = self.get_video_handler()
        if handler and getattr(handler, "renamer", None):
            return handler.renamer.tmdb_client
        if self._tmdb_client is not None:
            return self._tmdb_client
        from ...core.tmdb_client import TMDBClient

        with self._state_lock:
            if self._tmdb_client is not None:
                return self._tmdb_client
            tmdb_config = self._config.get("tmdb", {})
            if not tmdb_config.get("api_key"):
                self._logger.debug("未配置 TMDB api_key，跳过共享客户端初始化")
                return None
            try:
                self._tmdb_client = TMDBClient(
                    api_key=tmdb_config["api_key"],
                    retry_count=tmdb_config.get("retry_count", 3),
                    timeout=tmdb_config.get("timeout", 30),
                    base_url=tmdb_config.get("base_url"),
                    rate_limit_per_sec=tmdb_config.get("rate_limit_per_sec"),
                )
                self._logger.info("共享 TMDB 客户端初始化成功")
            except Exception as e:
                self._logger.error(f"初始化共享 TMDB 客户端失败: {e}")
                self._tmdb_client = None
            return self._tmdb_client

    def set_downloader_monitors(self, monitors: List[Any]) -> None:
        """设置下载器监控器列表"""
        with self._state_lock:
            self._downloader_monitors = monitors

    def get_downloader_monitors(self) -> List[Any]:
        """获取下载器监控器列表"""
        from .live_config import borrowed_generation

        generation = borrowed_generation.get()
        if generation is None and self._live_config:
            generation = self._live_config.current
        return (
            generation.monitor.downloader_monitors
            if generation and generation.monitor
            else self._downloader_monitors
        )

    def get_cloud_client(self, provider: str):
        """
        获取云盘客户端实例

        Args:
            provider: 云盘类型 ("cloud189" 或 "yun139")

        Returns:
            云盘客户端实例，如果不可用则返回 None
        """
        handler = self.get_video_handler()
        if not handler:
            return None
        if provider == "cloud189" and hasattr(handler, "cloud189_uploader"):
            uploader = handler.cloud189_uploader
            return uploader.client if uploader else None
        if provider == "yun139" and hasattr(handler, "yun139_uploader"):
            uploader = handler.yun139_uploader
            return uploader.client if uploader else None
        return None

    def set_system_running(self, running: bool) -> None:
        """设置系统运行状态"""
        self._system_running = running

    def is_system_running(self) -> bool:
        """获取系统运行状态"""
        return self._system_running

    def get_system_status(self) -> SystemStatus:
        """获取系统状态"""
        status = SystemStatus(
            is_running=self._system_running, last_update=datetime.now()
        )

        if self._video_handler is None:
            counts = db_get_task_counts()
            status.completed_count = counts["completed"]
            status.failed_count = counts["failed"]
            status.resolved_count = counts["resolved"]
            return status

        try:
            handler = self._video_handler

            # 获取队列状态
            if hasattr(handler, "_upload_queue"):
                status.queue_size = handler._upload_queue.qsize()

            # 获取处理状态
            if hasattr(handler, "_processing_files"):
                status.processing_count = len(self.get_processing_files())

            if hasattr(handler, "_uploading_files"):
                status.uploading_count = len(handler._uploading_files)

            runtime_failed = (
                len(handler._failed_files) if hasattr(handler, "_failed_files") else 0
            )

            # 从数据库获取汇总计数，避免旧内存失败集合把已解决任务重新计入
            try:
                db_counts = db_get_task_counts()
                status.completed_count = db_counts.get("completed", 0)
                status.failed_count = db_counts.get("failed", 0)
                status.resolved_count = db_counts.get("resolved", 0)
            except Exception:
                status.completed_count = len(self.get_completed_files())
                status.failed_count = runtime_failed

        except Exception as e:
            self._logger.error(f"获取系统状态失败: {e}")

        return status

    def get_queued_files(self) -> List[str]:
        """获取队列中的文件列表"""
        if self._video_handler is None:
            return []

        try:
            handler = self._video_handler
            if hasattr(handler, "_queued_files"):
                return list(handler._queued_files)
        except Exception as e:
            self._logger.error(f"获取队列文件失败: {e}")

        return []

    def get_processing_files(self) -> List[str]:
        """获取正在处理的文件列表"""
        if self._video_handler is None:
            return []

        try:
            handler = self._video_handler
            if hasattr(handler, "_processing_files"):
                return list(
                    handler._processing_files.difference(
                        getattr(handler, "_queued_files", set())
                    )
                )
        except Exception as e:
            self._logger.error(f"获取处理中文件失败: {e}")

        return []

    def get_uploading_files(self) -> List[str]:
        """获取正在上传的文件列表"""
        if self._video_handler is None:
            return []

        try:
            handler = self._video_handler
            if hasattr(handler, "_uploading_files"):
                return list(handler._uploading_files)
        except Exception as e:
            self._logger.error(f"获取上传中文件失败: {e}")

        return []

    def get_completed_files(self) -> List[str]:
        """按持久任务状态返回实际完成的文件，避免把排重跳过算作完成。"""
        return list(
            dict.fromkeys(item["path"] for item in db_get_completed(limit=10000))
        )

    def get_failed_files(self) -> Dict[str, str]:
        """获取失败的文件及错误信息"""
        return {
            path: item["error"] for path, item in db_get_failed(limit=10000).items()
        }

    def get_completed_with_time(self) -> List[Dict]:
        """完成列表以持久结果为准，内存中的已处理集合也包含跳过的文件。"""
        seen = {}
        for item in db_get_completed(limit=500):
            seen.setdefault(item["path"], item["time"])
        return [{"path": path, "time": time} for path, time in seen.items()][:200]

    def get_failed_with_time(self) -> Dict[str, Dict]:
        """
        获取失败的文件列表（含时间戳）

        Returns:
            {path: {"error": str, "time": str or None}, ...}
        """
        return db_get_failed(limit=200)

    def add_task_history(self, task: TaskInfo) -> None:
        """添加任务历史记录"""
        with self._state_lock:
            self._task_history.append(task)
            if len(self._task_history) > self._max_history:
                self._task_history = self._task_history[-self._max_history :]

    def get_task_history(self, limit: int = 100) -> List[TaskInfo]:
        """获取任务历史记录"""
        return self._task_history[-limit:]

    # ========== 上传进度管理 ==========

    def register_progress_callback(self, callback: Callable) -> None:
        """注册进度更新回调"""
        with self._state_lock:
            self._progress_callbacks.append(callback)

    def unregister_progress_callback(self, callback: Callable) -> None:
        """注销进度更新回调"""
        with self._state_lock:
            if callback in self._progress_callbacks:
                self._progress_callbacks.remove(callback)

    def update_upload_progress(self, progress: UploadProgress) -> None:
        """
        更新上传进度

        Args:
            progress: 上传进度对象
        """
        with self._state_lock:
            self._upload_progress[progress.file_path] = progress

            # 调用所有回调
            for callback in self._progress_callbacks:
                try:
                    callback(progress)
                except Exception as e:
                    self._logger.error(f"进度回调执行失败: {e}")

    def get_upload_progress(self, file_path: str) -> Optional[UploadProgress]:
        """获取指定文件的上传进度"""
        return self._upload_progress.get(file_path)

    def get_all_upload_progress(self) -> Dict[str, UploadProgress]:
        """获取所有上传进度"""
        return self._upload_progress.copy()

    def clear_upload_progress(self, file_path: str) -> None:
        """清除指定文件的上传进度"""
        with self._state_lock:
            if file_path in self._upload_progress:
                del self._upload_progress[file_path]

    # ========== 仪表盘实时更新 ==========

    def register_dashboard_callback(self, callback: Callable) -> None:
        """注册仪表盘更新回调"""
        with self._state_lock:
            self._dashboard_callbacks.append(callback)

    def unregister_dashboard_callback(self, callback: Callable) -> None:
        """注销仪表盘更新回调"""
        with self._state_lock:
            if callback in self._dashboard_callbacks:
                self._dashboard_callbacks.remove(callback)

    def notify_dashboard_update(self) -> None:
        """
        通知所有仪表盘监听器：有新的任务完成/失败
        回调在独立线程中执行，不阻塞调用方。
        """
        callbacks = []
        with self._state_lock:
            callbacks = list(self._dashboard_callbacks)
        for callback in callbacks:
            try:
                callback()
            except Exception as e:
                self._logger.error(f"仪表盘回调执行失败: {e}")


def get_state_manager() -> StateManager:
    """获取状态管理器单例"""
    return StateManager()


# ========== 全局进度更新函数（供上传器调用） ==========


def report_upload_progress(
    file_path: str,
    filename: str,
    uploader: str,
    progress: float,
    uploaded_bytes: int,
    total_bytes: int,
    speed: str = "",
    status: str = "uploading",
    error: Optional[str] = None,
):
    """
    报告上传进度（全局函数，供上传器调用）

    Args:
        file_path: 文件路径
        filename: 文件名
        uploader: 上传器名称 (cloud189, yun139, p123, emos)
        progress: 进度百分比 (0-100)
        uploaded_bytes: 已上传字节数
        total_bytes: 总字节数
        speed: 上传速度字符串
        status: 状态 (uploading, completed, skipped, failed)
        error: 错误信息（如果失败）
    """
    state = get_state_manager()
    progress_obj = UploadProgress(
        file_path=file_path,
        filename=filename,
        uploader=uploader,
        progress=progress,
        uploaded_bytes=uploaded_bytes,
        total_bytes=total_bytes,
        speed=speed,
        status=status,
        error=error,
        start_time=datetime.now() if progress == 0 else None,
        last_update=datetime.now(),
    )
    state.update_upload_progress(progress_obj)

"""配置代的构建、发布、借用与回收。任务开始后始终持有同一代组件。"""

import copy
import logging
import logging.handlers
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from ...core.effective_config import normalize_config, with_llm_snapshot

logger = logging.getLogger(__name__)
borrowed_generation = ContextVar("video_organizer_generation", default=None)


@dataclass
class Generation:
    version: int
    config: Dict
    handler: Any
    monitor: Any = None
    cleaner: Any = None
    tracker: Any = None
    users: int = 0
    retired_at: Optional[float] = None
    closed: bool = False
    closing: bool = False
    error: str = ""
    log_handlers: Any = None
    log_level: int = logging.INFO


def close_resources(handler, downloaders=()):
    """关闭独占 HTTP 会话及 emya 连接池，不销毁共享的任务数据库。"""
    seen = set()

    def close(obj, depth=0):
        if obj is None or id(obj) in seen or depth > 6:
            return
        seen.add(id(obj))
        for name in (
            "session",
            "_session",
            "client",
            "tmdb_client",
            "llm_translator",
            "service",
            "db",
            "_engine",
        ):
            close(getattr(obj, name, None), depth + 1)
        for child in (
            getattr(obj, "providers", [])
            if isinstance(getattr(obj, "providers", None), list)
            else []
        ):
            close(child, depth + 1)
        method = getattr(obj, "dispose", None) or getattr(obj, "close", None)
        if callable(method):
            method()

    for name in (
        "renamer",
        "p123_uploader",
        "cloud189_uploader",
        "yun139_uploader",
        "emya_controller",
    ):
        close(getattr(handler, name, None))
    for downloader in downloaders:
        close(downloader)


def prepare_logging(config):
    """先打开新日志文件，失败时保留旧处理器。"""
    from ...utils.logging_utils import (
        TaskIdFormatter,
        TaskIdFilter,
        LOG_FORMAT,
        DATE_FORMAT,
    )
    import sys

    level = str(config.get("log_level", config.get("level", "INFO"))).upper()
    if level not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
        raise ValueError("日志级别无效")
    level = getattr(logging, level)
    handlers = []
    try:
        if config.get("console_log", True):
            handlers.append(logging.StreamHandler(sys.stdout))
        if config.get("file_log", False):
            path = Path(config.get("log_file") or "logs/video-organizer.log")
            path.parent.mkdir(parents=True, exist_ok=True)
            handlers.append(
                logging.handlers.RotatingFileHandler(
                    path,
                    maxBytes=int(config.get("log_max_bytes", 10485760)),
                    backupCount=int(config.get("log_backup_count", 5)),
                    encoding="utf-8",
                )
            )
        for handler in handlers:
            handler.setLevel(level)
            handler.setFormatter(TaskIdFormatter(LOG_FORMAT, DATE_FORMAT))
            handler.addFilter(TaskIdFilter())
        return handlers, level
    except Exception:
        for handler in handlers:
            handler.close()
        raise


class LiveConfig:
    def __init__(self, state):
        self.state = state
        self.lock = threading.RLock()
        self.apply_lock = threading.RLock()
        self.current = None
        self.root = None
        self.monitor = None
        self.retired = []
        self.last_error = ""
        self.last_status = "idle"
        self._reaper_running = False
        self._next_version = 2

    def attach(self, config, handler=None, monitor=None, cleaner=None, tracker=None):
        with self.lock:
            self.monitor, self.root = monitor, handler
            template = copy.copy(handler) if handler else None
            self.current = Generation(
                1,
                with_llm_snapshot(config),
                template,
                copy.copy(monitor) if monitor else None,
                cleaner,
                tracker,
            )
            if handler:
                handler._runtime_manager = self
            if monitor:
                monitor._runtime_manager = self
                self.current.monitor.event_handler = template
                self.current.monitor._runtime_manager = self
                self.current.monitor._live_generation = self.current
                for downloader in monitor.downloader_monitors:
                    downloader.callback = self._callback(self.current)
                template.downloaders = monitor.downloader_monitors
            self.last_status = "applied"

    @contextmanager
    def lease(self, generation=None):
        with self.lock:
            generation = generation or self.current
            if generation is None or generation.closed or generation.closing:
                raise RuntimeError("配置组件尚未就绪或已经关闭")
            generation.users += 1
        token = borrowed_generation.set(generation)
        try:
            yield generation
        finally:
            borrowed_generation.reset(token)
            with self.lock:
                generation.users -= 1
            self._schedule_reaper()

    def spawn(self, generation, function, *args):
        # 在启动线程前计数，防止旧组件在线程尚未获得调度时被关闭。
        with self.lock:
            if generation.closed or generation.closing:
                raise RuntimeError("不能向已关闭组件投递任务")
            generation.users += 1

        def run():
            token = borrowed_generation.set(generation)
            try:
                function(*args)
            finally:
                borrowed_generation.reset(token)
                with self.lock:
                    generation.users -= 1
                self._schedule_reaper()

        try:
            threading.Thread(target=run, daemon=True).start()
        except Exception:
            with self.lock:
                generation.users -= 1
            raise

    def status(self):
        with self.lock:
            return {
                "status": self.last_status,
                "version": self.current.version if self.current else 0,
                "error": self.last_error,
                "retiring": [
                    {
                        "version": g.version,
                        "active_tasks": g.users,
                        "waiting_seconds": round(time.monotonic() - g.retired_at, 1),
                        "status": "cleanup_failed" if g.error else "draining",
                        "error": g.error,
                    }
                    for g in self.retired
                    if not g.closed
                ],
            }

    @staticmethod
    def _background_stop(generation):
        if generation.cleaner:
            generation.cleaner._stop.set()
        if generation.tracker:
            generation.tracker.stop()
        for downloader in (
            generation.monitor.downloader_monitors if generation.monitor else []
        ):
            # 回调已经停止投递，连接仍供旧上传任务做下载任务清理。
            downloader.running = False
            threading.Thread(target=downloader.stop, daemon=True).start()

    def _retire(self, generation):
        generation.retired_at = time.monotonic()
        self._background_stop(generation)
        with self.lock:
            self.retired.append(generation)
        self._schedule_reaper()

    def _schedule_reaper(self):
        with self.lock:
            if self._reaper_running or not self.retired:
                return
            self._reaper_running = True
        threading.Thread(
            target=self._reap, name="config-component-retirement", daemon=True
        ).start()

    def _reap(self):
        try:
            while True:
                with self.lock:
                    pending = list(self.retired)
                if not pending:
                    return
                for generation in pending:
                    with self.lock:
                        if generation.users or generation.error:
                            continue
                        cleaner = generation.cleaner
                        if cleaner and (cleaner._run_lock.locked() or cleaner.is_alive):
                            continue
                        tracker = generation.tracker
                        if tracker and tracker._thread and tracker._thread.is_alive():
                            continue
                        if generation.monitor and any(
                            getattr(d, name, None) and getattr(d, name).is_alive()
                            for d in generation.monitor.downloader_monitors
                            for name in (
                                "monitor_thread",
                                "_ws_thread",
                                "_webhook_thread",
                            )
                        ):
                            continue
                        generation.closing = True
                    try:
                        close_resources(
                            generation.handler,
                            (
                                generation.monitor.downloader_monitors
                                if generation.monitor
                                else ()
                            ),
                        )
                        for handler in generation.log_handlers or []:
                            handler.close()
                        generation.closed = True
                    except Exception:
                        logger.exception(
                            "旧配置组件释放失败，版本 %s", generation.version
                        )
                        generation.error = "旧组件资源释放失败，请检查服务日志"
                    finally:
                        generation.closing = False
                with self.lock:
                    self.retired[:] = [g for g in self.retired if not g.closed]
                    if not self.retired or all(g.error for g in self.retired):
                        return
                # 长任务不会强制中止；网页可看到等待时长和剩余使用者。
                time.sleep(1)
        finally:
            with self.lock:
                self._reaper_running = False

    def _build(self, config):
        from ...core.file_handler import VideoFileHandler
        from ...core.recycle_cleaner import RecycleCleaner
        from ...utils.cache import ThreadSafeLRUCache

        monitoring = config.get("monitoring", {})
        with self.lock:
            version = self._next_version
            self._next_version += 1
        generation = Generation(version, config, None)
        try:
            # 构造失败也保留部分对象供统一回收。
            handler = VideoFileHandler.__new__(VideoFileHandler)
            generation.handler = handler
            handler_config = copy.deepcopy(config)
            handler_config["_runtime_preparing"] = True
            handler.__init__(
                output_dir=monitoring.get("output_dir", ""),
                supported_extensions=monitoring.get("supported_extensions", []),
                naming_rules=config.get("naming_rules", config.get("naming", {})),
                tmdb_config=config.get("tmdb"),
                emos_config=config.get("emos"),
                p123_config=config.get("p123"),
                cloud189_config=config.get("cloud189"),
                yun139_config=config.get("yun139"),
                processing_config=config.get("processing"),
                path_mappings=monitoring.get("path_mappings"),
                telegram_config=config.get("telegram"),
                config=handler_config,
                emya_db_config=config.get("emya_db"),
                start_workers=False,
                strict=True,
            )
            for name in (
                "_tmdb_cache",
                "_tmdb_name_to_id",
                "_search_cache",
                "_season_year_boost_cache",
                "_type_resolve_cache",
                "_llm_parse_cache",
                "_season_infer_cache",
            ):
                setattr(handler.renamer, name, ThreadSafeLRUCache(maxsize=2000))
            handler.renamer._llm_inflight = {}
            handler.renamer._llm_cache_lock = threading.Lock()
            handler.renamer._search_empty_ttl = {}
            if self.root and hasattr(self.root, "_upload_queue"):
                for name in (
                    "_processing_files",
                    "_uploading_files",
                    "_uploaded_files",
                    "_failed_files",
                    "_queued_files",
                    "_queue_lock",
                    "_file_downloader_map",
                    "_upload_queue",
                ):
                    setattr(handler, name, getattr(self.root, name))
                handler._parent_monitor = self.monitor
            if self.monitor:
                generation.monitor = self.monitor.prepare_live_config(config, handler)
                generation.monitor._live_generation = generation
                handler.downloaders = generation.monitor.downloader_monitors
                for downloader in handler.downloaders:
                    downloader.callback = self._callback(generation)
            generation.cleaner = RecycleCleaner(config)
            generation.cleaner._hot_reloaded = True
            if self.current and self.current.cleaner:
                generation.cleaner._run_lock = self.current.cleaner._run_lock
                generation.cleaner.last_run_at = self.current.cleaner.last_run_at
            if config.get("media_tracker", {}).get("enabled", False):
                from ...core.media_tracker_client import MediaTrackerClient

                if not handler.yun139_uploader:
                    raise ValueError(
                        "启用 Media Tracker 前需要配置可用的 139 云盘客户端"
                    )
                generation.tracker = MediaTrackerClient(
                    config.get("media_tracker", {}),
                    renamer=handler.renamer,
                    yun139_uploader=handler.yun139_uploader,
                    emya_controller=handler.emya_controller,
                    emya_db_config=config.get("emya_db", {}),
                )
            generation.log_handlers, generation.log_level = prepare_logging(
                config.get("logging", {})
            )
            return generation
        except Exception:
            self._retire(generation)
            raise

    def _callback(self, generation):
        def callback(*args, **kwargs):
            # 锁覆盖接收边界，过期下载器回调不会进入新代的路径映射逻辑。
            with self.lock:
                if self.current is not generation:
                    return False
                with self.lease(generation):
                    return generation.monitor._on_download_completed_snapshot(
                        *args, **kwargs
                    )

        return callback

    def apply(self, config, path=None):
        with self.apply_lock:
            candidate = None
            try:
                config = with_llm_snapshot(normalize_config(config))
                if self.current and config == self.current.config:
                    self.last_status, self.last_error = "applied", ""
                    return {
                        "applied": True,
                        "application": self.status(),
                        "message": "配置已生效",
                    }
                self.last_status = "preparing"
                candidate = self._build(config)
                if candidate.monitor:
                    for downloader in candidate.monitor.downloader_monitors:
                        downloader.start()
                        if not downloader.is_connected():
                            raise ValueError("新下载器连接验证失败")
                # 线程预启动，接收/调度门在发布前保持关闭。
                if candidate.tracker:
                    candidate.tracker._activation = threading.Event()
                    candidate.tracker.start()
                candidate.cleaner._activation = threading.Event()
                candidate.cleaner.start()
                # worker 数量先扩容，任务仍借用旧代；缩容不会中断已运行任务。
                if self.root and hasattr(self.root, "resize_workers"):
                    self.root.resize_workers(
                        int(config.get("processing", {}).get("max_upload_workers", 1))
                    )
                with self.lock:
                    old = self.current
                    if old:
                        # 先关闭旧调度入口，再开启新的调度入口，执行中的任务不受影响。
                        if old.cleaner:
                            old.cleaner._stop.set()
                        if old.tracker:
                            old.tracker.stop()
                    self.current = candidate
                    if self.monitor and candidate.monitor:
                        self.monitor.config = config
                        self.monitor.event_handler = candidate.handler
                        self.monitor.supported_extensions = (
                            candidate.monitor.supported_extensions
                        )
                        self.monitor.downloader_monitors = (
                            candidate.monitor.downloader_monitors
                        )
                        self.monitor.downloader_configs = (
                            candidate.monitor.downloader_configs
                        )
                    if self.root is None or not hasattr(self.root, "_upload_queue"):
                        # web-only 首次启用手动处理时创建唯一队列，后续配置代复用它。
                        self.root = candidate.handler
                        self.root._runtime_manager = self
                        candidate.handler = copy.copy(self.root)
                        candidate.handler._runtime_manager = None
                        self.state._video_handler = self.root
                        self.root._start_upload_queue()
                    self.state._config = {
                        k: copy.deepcopy(v)
                        for k, v in config.items()
                        if not k.startswith("_runtime_")
                    }
                    self.state._config_path = path or self.state._config_path
                    # 清理排队任务的旧下载器引用；已开始任务有自己的映射快照。
                    mapping = getattr(self.root, "_file_downloader_map", {})
                    new_downloaders = (
                        candidate.monitor.downloader_monitors
                        if candidate.monitor
                        else []
                    )
                    for key, downloader in list(mapping.items()):
                        replacement = next(
                            (
                                d
                                for d in new_downloaders
                                if type(d) is type(downloader)
                                and getattr(d, "rpc_url", None)
                                == getattr(downloader, "rpc_url", None)
                            ),
                            None,
                        )
                        if replacement is None:
                            mapping.pop(key, None)
                        else:
                            mapping[key] = replacement
                    candidate.cleaner._activation.set()
                    if candidate.tracker:
                        candidate.tracker._activation.set()
                    from ...core.recycle_cleaner import set_cleaner

                    set_cleaner(candidate.cleaner)
                    from ...core.tmdb_client import (
                        _global_rate_limiter,
                        DEFAULT_RATE_LIMIT_PER_SEC,
                    )

                    with _global_rate_limiter._lock:
                        _global_rate_limiter._rate_limit_per_sec = int(
                            config.get("tmdb", {}).get("rate_limit_per_sec")
                            or DEFAULT_RATE_LIMIT_PER_SEC
                        )
                    root_logger = logging.getLogger()
                    old_handlers = root_logger.handlers
                    root_logger.handlers = candidate.log_handlers
                    root_logger.setLevel(candidate.log_level)
                    candidate.log_handlers = (
                        None  # 全局 logger 接管所有权，旧代回收不能关闭新日志。
                    )
                    self.last_status, self.last_error = "applied", ""
                for handler in old_handlers:
                    handler.close()
                if old:
                    self._retire(old)
                return {
                    "applied": True,
                    "message": "配置已保存并应用；新任务使用新配置，已有任务继续完成",
                    "application": self.status(),
                }
            except Exception:
                logger.exception("配置组件构建或连接验证失败")
                if candidate and self.current is candidate:
                    # 发布后只可能是资源收尾失败，不能误报为已回退。
                    self.last_status = "applied"
                    self.last_error = "新配置已应用，旧资源回收异常，请检查服务日志"
                    return {
                        "applied": True,
                        "message": self.last_error,
                        "application": self.status(),
                    }
                if candidate:
                    self._retire(candidate)
                self.last_status = "failed"
                self.last_error = "配置应用失败，旧配置继续运行；请检查配置及服务日志"
                return {
                    "applied": False,
                    "message": "配置已保存，但应用失败；旧配置继续运行",
                    "apply_error": self.last_error,
                    "application": self.status(),
                }


class RuntimeLeaseMiddleware:
    """覆盖整个 HTTP 响应（包含流式内容），防止 API 使用的旧连接提前关闭。"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        from .state import get_state_manager

        manager = getattr(get_state_manager(), "_live_config", None)
        if scope["type"] == "http" and manager and manager.current:
            with manager.lease():
                await self.app(scope, receive, send)
        else:
            await self.app(scope, receive, send)

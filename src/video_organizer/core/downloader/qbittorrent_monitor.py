"""qBittorrent 下载监控器"""

import os
import time
import logging
import threading
import requests
from pathlib import Path
from typing import Optional, Callable, Dict

from .base import DownloaderMonitor

logger = logging.getLogger(__name__)


class QBittorrentMonitor(DownloaderMonitor):
    """
    Monitor for qBittorrent downloader.
    """

    # qB 4.x 使用 pausedUP，5.x 使用 stoppedUP；未知状态一律等待。
    READY_STATES = frozenset(
        {"uploading", "stalledUP", "forcedUP", "queuedUP", "pausedUP", "stoppedUP"}
    )
    requires_native_readiness = True

    def __init__(
        self,
        callback: Callable[[str], None],
        rpc_url: str = "http://localhost:8080/api/v2",
        username: str = "admin",
        password: str = "adminadmin",
        supported_extensions: tuple = (".mp4", ".mkv", ".avi", ".mov", ".wmv", ".strm"),
        path_mappings: Optional[Dict[str, str]] = None,
    ):
        """
        Initialize the qBittorrent monitor.

        Args:
            callback: Callback function to call when a download is completed.
            rpc_url: RPC URL of the qBittorrent instance.
            username: Username for qBittorrent web UI.
            password: Password for qBittorrent web UI.
            supported_extensions: Tuple of supported file extensions.
            path_mappings: 路径映射字典，将下载器返回的路径映射到主机实际路径
        """
        super().__init__(callback)
        self.rpc_url = rpc_url.rstrip("/")
        self.username = username
        self.password = password
        self.supported_extensions = supported_extensions
        self.path_mappings = path_mappings or {}
        self.session = requests.Session()  # 使用 Session 管理 Cookie
        # 本地路径 -> (种子哈希, 文件索引, qB 原始路径)，只记录来源。
        self._file_sources = {}
        self._stop_event = threading.Event()
        self.app_version = None
        self.running = False
        self.monitor_thread = None
        self._upload_completed_files = (
            set()
        )  # 存储真正上传完成的文件路径，用于判定是否可删种子

    def _apply_path_mapping(self, file_path: str) -> str:
        """只匹配完整目录前缀，选择最长规则，保留文件名大小写和百分号。"""
        normalized = file_path.replace("\\", "/")
        for prefix in sorted(self.path_mappings, key=len, reverse=True):
            source = prefix.replace("\\", "/").rstrip("/")
            if normalized == source or normalized.startswith(source + "/"):
                relative = normalized[len(source) :].lstrip("/")
                return str(Path(self.path_mappings[prefix]) / relative)
        return str(Path(normalized))

    def start(self):
        """连接暂不可用也启动轮询，让下载器恢复后可以自动继续。"""
        if self.running:
            return
        # 热更新会在 start 返回后验证连接，首次登录必须先完成。
        # 登录暂时失败仍启动轮询，让初始启动时可在 qB 恢复后重连。
        self._login()
        self.running = True
        self._stop_event.clear()
        self.monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self.monitor_thread.start()
        logger.info("Started qBittorrent monitor: %s", self.rpc_url)

    def stop(self):
        self.running = False
        self._stop_event.set()
        if self.monitor_thread:
            self.monitor_thread.join()
        logger.info("Stopped qBittorrent monitor")

    def is_connected(self) -> bool:
        """
        Check if connected to qBittorrent Web UI.

        Returns:
            bool: True if connected, False otherwise.
        """
        try:
            # 尝试获取应用版本信息
            url = f"{self.rpc_url}/app/version"
            response = self.session.get(url, timeout=30)
            if response.status_code != 200:
                return False
            version = response.text.strip()
            if version != self.app_version:
                self.app_version = version
                logger.info(
                    "qBittorrent 版本: %s（兼容 pausedUP / stoppedUP）", version
                )
            return True
        except Exception as e:
            logger.error(f"Failed to connect to qBittorrent: {e}")
            return False

    def remove_download(self, file_path: str) -> bool:
        """
        从 qBittorrent 中删除指定文件的种子任务 (只有当所有视频都处理完后才真正执行)

        Args:
            file_path: 文件路径 (可能是主机映射后的路径)

        Returns:
            bool: 触发了检查动作返回 True，如果真正执行了删除也返回 True
        """
        try:
            # 获取所有已完成的种子
            completed_torrents = self._get_completed_torrents()

            # 标准化输入路径
            norm_input_path = os.path.normpath(file_path).lower()

            # 查找包含该文件的种子
            target_torrent = None
            for torrent in completed_torrents:
                save_path = torrent.get("save_path", "")
                files = self._get_torrent_files(torrent["hash"])

                for f in files:
                    # 组合完整路径进行比对
                    f_name = f["name"]
                    full_torrent_file_path = os.path.normpath(
                        os.path.join(save_path, f_name)
                    ).lower()

                    # 匹配逻辑：绝对路径一致，或者输入路径是以种子内文件路径结尾的（处理映射点差异）
                    if (
                        full_torrent_file_path == norm_input_path
                        or norm_input_path.endswith(os.path.normpath(f_name).lower())
                    ):
                        target_torrent = torrent
                        break
                if target_torrent:
                    break

            if not target_torrent:
                logger.debug(f"在 qBittorrent 中未找到对应文件的任务: {file_path}")
                return False

            torrent_hash = target_torrent["hash"]

            # 标记当前文件已上传完成（使用标准化路径）
            norm_input_path = os.path.normpath(file_path).lower()
            self._upload_completed_files.add(norm_input_path)

            # 检查种子内是否还有其他待处理的视频文件
            all_files = self._get_torrent_files(torrent_hash)
            remaining_videos = []

            for f in all_files:
                f_name = f["name"]
                # 检查是否是视频文件
                if f_name.lower().endswith(self.supported_extensions):
                    f_full_path = str(
                        Path(os.path.join(target_torrent.get("save_path", ""), f_name))
                    )

                    # 检查文件是否实际存在（区分"未下载"和"未处理"）
                    if not os.path.exists(f_full_path):
                        # 文件未下载，跳过检查
                        continue

                    # 检查该文件是否已上传完成
                    norm_f_full = os.path.normpath(f_full_path).lower()
                    if norm_f_full not in self._upload_completed_files:
                        remaining_videos.append(f_name)

            if remaining_videos:
                logger.info(
                    f"种子 {torrent_hash} 仍有 {len(remaining_videos)} 个视频未处理完毕，将种子暂时保留在下载器中。剩余: {remaining_videos[:2]}..."
                )
                return False  # 返回 False，外层处理器将不会打印"已从下载器中删除"

            # 所有视频都已处理，执行删除
            delete_url = f"{self.rpc_url}/torrents/delete"
            data = {"hashes": torrent_hash, "deleteFiles": "true"}

            response = self.session.post(delete_url, data=data, timeout=30)
            if response.status_code == 200:
                logger.info(
                    f"🎉 种子内所有视频已处理完毕，已删除 qBittorrent 任务: {target_torrent.get('name')} ({torrent_hash})"
                )
                return True
            else:
                logger.warning(
                    f"从 qBittorrent 删除任务失败: {torrent_hash}, 状态码: {response.status_code}"
                )
                return False

        except Exception as e:
            logger.error(f"从 qBittorrent 清理任务时发生错误: {e}")
            return False

    def force_remove_download(self, file_path: str) -> bool:
        """
        强制从 qBittorrent 中删除种子任务及其文件 (用于文件删除失败时的清理)

        Args:
            file_path: 文件路径 (可能是主机映射后的路径)

        Returns:
            bool: 是否成功删除
        """
        try:
            # 获取所有种子 (包括未完成的)
            url = f"{self.rpc_url}/torrents/info"
            response = self.session.get(url, timeout=30)

            if response.status_code != 200:
                logger.error(f"获取种子列表失败: {response.status_code}")
                return False

            all_torrents = response.json()

            # 标准化输入路径
            norm_input_path = os.path.normpath(file_path).lower()

            # 查找包含该文件的种子
            target_torrent = None
            for torrent in all_torrents:
                save_path = torrent.get("save_path", "")
                files = self._get_torrent_files(torrent["hash"])

                for f in files:
                    f_name = f["name"]
                    full_torrent_file_path = os.path.normpath(
                        os.path.join(save_path, f_name)
                    ).lower()

                    if (
                        full_torrent_file_path == norm_input_path
                        or norm_input_path.endswith(os.path.normpath(f_name).lower())
                    ):
                        target_torrent = torrent
                        break
                if target_torrent:
                    break

            if not target_torrent:
                logger.debug(f"在 qBittorrent 中未找到对应文件的任务: {file_path}")
                return False

            torrent_hash = target_torrent["hash"]
            torrent_name = target_torrent.get("name", "")

            # 检查种子内是否还有其他待处理的视频文件
            all_files = self._get_torrent_files(torrent_hash)
            remaining_videos = []

            for f in all_files:
                f_name = f["name"]
                if f_name.lower().endswith(self.supported_extensions):
                    f_full_path = str(
                        Path(os.path.join(target_torrent.get("save_path", ""), f_name))
                    )

                    # 检查文件是否实际存在（区分"未下载"和"未处理"）
                    if not os.path.exists(f_full_path):
                        # 文件未下载，跳过检查
                        continue

                    if f_full_path not in self._processed_files:
                        norm_f_full = os.path.normpath(f_full_path).lower()
                        is_processed = False
                        for p_file in self._processed_files:
                            if os.path.normpath(p_file).lower() == norm_f_full:
                                is_processed = True
                                break
                        if not is_processed:
                            remaining_videos.append(f_name)

            # 如果还有其他视频待处理，不能强制删除整个种子
            if remaining_videos:
                logger.info(
                    f"种子 {torrent_name} 仍有 {len(remaining_videos)} 个视频待处理，不执行强制删除。剩余: {remaining_videos[:3]}..."
                )
                return False

            # 所有视频都已处理，执行删除 (包含文件)
            delete_url = f"{self.rpc_url}/torrents/delete"
            data = {"hashes": torrent_hash, "deleteFiles": "true"}
            response = self.session.post(delete_url, data=data, timeout=30)

            if response.status_code == 200:
                logger.info(
                    f"已强制删除 qBittorrent 任务及文件: {torrent_name} ({torrent_hash})"
                )
                return True
            else:
                logger.warning(
                    f"强制删除 qBittorrent 任务失败: {torrent_hash}, 状态码: {response.status_code}"
                )
                return False

        except Exception as e:
            logger.error(f"强制从 qBittorrent 删除任务时发生错误: {e}")
            return False

    def pause_torrent_for_file(self, file_path: str) -> bool:
        """
        暂停包含该文件的种子，以释放文件句柄 (用于处理文件被占用的问题)

        Args:
            file_path: 文件路径

        Returns:
            bool: 是否成功暂停了种子
        """
        try:
            # 获取所有种子
            url = f"{self.rpc_url}/torrents/info"
            response = self.session.get(url, timeout=30)

            if response.status_code != 200:
                return False

            all_torrents = response.json()
            norm_input_path = os.path.normpath(file_path).lower()

            # 查找种子
            for torrent in all_torrents:
                # 只暂停正在做种的种子
                if torrent.get("state") not in ["uploading", "stalledUP", "forcedUP"]:
                    continue

                save_path = torrent.get("save_path", "")
                try:
                    files = self._get_torrent_files(torrent["hash"])
                except Exception:
                    continue

                for f in files:
                    f_name = f["name"]
                    full_path = os.path.normpath(
                        os.path.join(save_path, f_name)
                    ).lower()

                    if full_path == norm_input_path or norm_input_path.endswith(
                        os.path.normpath(f_name).lower()
                    ):
                        # 找到目标种子，暂停它
                        torrent_hash = torrent["hash"]
                        pause_url = f"{self.rpc_url}/torrents/pause"
                        data = {"hashes": torrent_hash}
                        pause_response = self.session.post(
                            pause_url, data=data, timeout=30
                        )

                        if pause_response.status_code == 200:
                            logger.info(
                                f"已暂停种子以释放文件句柄: {torrent.get('name')} ({torrent_hash})"
                            )
                            return True
                        break

            return False
        except Exception as e:
            logger.error(f"暂停种子时发生错误: {e}")
            return False

    @classmethod
    def _torrent_ready(cls, torrent):
        """先看原生状态，再核对所选内容的总进度，tracker 状态不参与。"""
        return (
            torrent.get("state") in cls.READY_STATES
            and torrent.get("progress") == 1
            and torrent.get("amount_left") == 0
        )

    def _ready_file_path(self, torrent, file, local_path=None):
        """原生文件进度 + 最终路径可读；未重命名或未移动完成就留待下轮。"""
        name = file.get("name", "")
        if (
            not isinstance(name, str)
            or Path(name).suffix.lower() not in self.supported_extensions
            or file.get("priority", 0) <= 0
            or file.get("progress") != 1
            or not torrent.get("save_path")
        ):
            return None
        # API 的 name 是逻辑文件名，不能 URL 解码，也不能自行剥离 .!qB。
        path = Path(
            local_path or self._apply_path_mapping(self._source_path(torrent, file))
        )
        size = file.get("size")
        if not isinstance(size, int) or size <= 0:
            return None
        try:
            with path.open("rb") as stream:
                if not path.is_file() or os.fstat(stream.fileno()).st_size != size:
                    return None
                stream.seek(-1, os.SEEK_END)
                if not stream.read(1):
                    return None
        except OSError:
            return None
        return str(path)

    @staticmethod
    def _source_path(torrent, file):
        return str(
            Path(torrent["save_path"].replace("\\", "/"))
            / file["name"].replace("\\", "/")
        )

    def is_file_ready(self, file_path):
        """出队时再次查询 qB，防止排队期间进入校验、移动或丢失文件状态。"""
        source = self._file_sources.get(file_path)
        if source is None:
            return False
        torrent_hash, index, raw_path = source
        try:
            response = self.session.get(
                f"{self.rpc_url}/torrents/info",
                params={"hashes": torrent_hash},
                timeout=30,
            )
            response.raise_for_status()
            torrents = response.json()
            torrent = next((t for t in torrents if t.get("hash") == torrent_hash), None)
            if not torrent or not self._torrent_ready(torrent):
                return False
            for file in self._get_torrent_files(torrent_hash):
                if file.get("index") == index:
                    # 队列保留接收时的本地映射；热更新不能对旧队列再次映射。
                    return (
                        self._source_path(torrent, file) == raw_path
                        and self._ready_file_path(torrent, file, local_path=file_path)
                        == file_path
                    )
        except Exception as exc:
            logger.warning("qB 文件就绪检查失败，等待重试: %s", exc)
        return False

    def _poll_once(self):
        """每轮检查所有已完成任务；去重和最终成功由持久任务记录决定。"""
        torrents = self._get_completed_torrents()
        post_upload = getattr(self, "post_upload", None)
        if post_upload:
            post_upload.reconcile(torrents)
        for torrent in torrents:
            if self._stop_event.is_set():
                break
            if not self._torrent_ready(torrent):
                continue
            torrent_hash = torrent.get("hash")
            if not torrent_hash:
                continue
            try:
                files = self._get_torrent_files(torrent_hash)
            except Exception as exc:
                logger.warning("qB 种子 %s 扫描失败，等待重试: %s", torrent_hash, exc)
                continue
            for file in files:
                if self._stop_event.is_set():
                    break
                try:
                    # 移动后的目录可以不挂入上传容器，稳定来源标识仍能防止重复上传。
                    if post_upload and post_upload.handled(torrent, file):
                        continue
                    file_path = self._ready_file_path(torrent, file)
                    if file_path is None or not isinstance(file.get("index"), int):
                        continue
                    self._file_sources[file_path] = (
                        torrent_hash,
                        file["index"],
                        self._source_path(torrent, file),
                    )
                    # 必须明确返回 True 才表示已经接收；绝不永久屏蔽整个种子。
                    if self.callback(file_path, downloader_monitor=self) is not True:
                        logger.debug("qB 文件尚未接收，下轮继续检查: %s", file_path)
                except Exception as exc:
                    # 单文件接收失败不阻塞同种子的其他文件。
                    logger.warning(
                        "qB 种子 %s 文件接收失败，等待重试: %s", torrent_hash, exc
                    )

    def _monitor_loop(self):
        while self.running:
            interval = 5
            try:
                if not self.is_connected() and not self._login():
                    interval = 10
                else:
                    self._poll_once()
            except Exception as exc:
                logger.error("qBittorrent 轮询失败，稍后重试: %s", exc)
                interval = 10
            self._stop_event.wait(interval)

    def _login(self) -> bool:
        """
        Login to qBittorrent Web UI.

        Returns:
            bool: True if login successful, False otherwise.
        """
        try:
            url = f"{self.rpc_url}/auth/login"
            data = {"username": self.username, "password": self.password}

            # 使用 session 登录，Cookie 会自动管理
            response = self.session.post(url, data=data, timeout=30)
            if response.status_code == 200 and response.text == "Ok.":
                # Session 会自动保存 Cookie
                logger.info("qBittorrent 登录成功")
                return True
            logger.error(
                f"Login failed with status {response.status_code}: {response.text}"
            )
            return False
        except Exception as e:
            logger.error(f"Failed to login to qBittorrent: {e}")
            return False

    def _get_completed_torrents(self):
        """
        Get completed torrents from qBittorrent.

        Returns:
            list: List of completed torrents.
        """
        url = f"{self.rpc_url}/torrents/info"
        params = {"filter": "all"}  # 改用 all，手动过滤进度，避免状态误判

        max_retries = 3
        for i in range(max_retries):
            try:
                logger.debug(
                    f"Requesting qBittorrent torrents/info (attempt {i+1}/{max_retries})"
                )
                response = self.session.get(url, params=params, timeout=30)
                logger.debug(f"qBittorrent response status: {response.status_code}")
                if response.status_code != 200:
                    logger.warning(
                        f"qBittorrent returned non-200 status: {response.status_code}, body: {response.text[:200]}"
                    )
                response.raise_for_status()
                result = response.json()
                logger.debug(f"qBittorrent returned {len(result)} torrents")
                return result
            except Exception as e:
                logger.warning(
                    f"获取 qBittorrent 种子列表失败 ({i+1}/{max_retries}): {e}"
                )
                if i < max_retries - 1:
                    time.sleep(2)
                else:
                    raise

    def _get_torrent_files(self, torrent_hash: str):
        """
        Get files in a torrent.

        Args:
            torrent_hash: Hash of the torrent.

        Returns:
            list: List of files in the torrent.
        """
        url = f"{self.rpc_url}/torrents/files"
        params = {"hash": torrent_hash}

        try:
            response = self.session.get(url, params=params, timeout=30)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.error(f"Failed to get files for torrent {torrent_hash}: {e}")
            raise

import datetime
from typing import Optional

from sqlalchemy import Column, Integer, String, DateTime, Text, Boolean, Float
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class TaskHistory(Base):
    __tablename__ = "task_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    file_path: Mapped[str] = mapped_column(String(1024), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    start_time: Mapped[Optional[datetime.datetime]] = mapped_column(
        DateTime, nullable=True
    )
    end_time: Mapped[Optional[datetime.datetime]] = mapped_column(
        DateTime, nullable=True
    )
    retry_count: Mapped[int] = mapped_column(Integer, default=0)


class ManualRule(Base):
    __tablename__ = "config_manual_rule"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    rule_text: Mapped[str] = mapped_column(Text, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[Optional[datetime.datetime]] = mapped_column(
        DateTime, nullable=True
    )
    updated_at: Mapped[Optional[datetime.datetime]] = mapped_column(
        DateTime, nullable=True
    )


class ReleaseGroupMapping(Base):
    __tablename__ = "config_release_group"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_name: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    content_type: Mapped[str] = mapped_column(
        String(32), nullable=False
    )  # anime, drama, movie
    created_at: Mapped[Optional[datetime.datetime]] = mapped_column(
        DateTime, nullable=True
    )
    updated_at: Mapped[Optional[datetime.datetime]] = mapped_column(
        DateTime, nullable=True
    )


class LlmProvider(Base):
    __tablename__ = "config_llm_provider"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    api_url: Mapped[str] = mapped_column(String(1024), nullable=False)
    api_key: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    model: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    weight: Mapped[int] = mapped_column(Integer, default=1)
    timeout: Mapped[int] = mapped_column(Integer, default=30)
    max_retries: Mapped[int] = mapped_column(Integer, default=2)
    created_at: Mapped[Optional[datetime.datetime]] = mapped_column(
        DateTime, nullable=True
    )
    updated_at: Mapped[Optional[datetime.datetime]] = mapped_column(
        DateTime, nullable=True
    )


class AuthUser(Base):
    __tablename__ = "config_auth_user"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(String(256), nullable=False)
    role: Mapped[str] = mapped_column(String(32), default="admin")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[Optional[datetime.datetime]] = mapped_column(
        DateTime, nullable=True
    )
    updated_at: Mapped[Optional[datetime.datetime]] = mapped_column(
        DateTime, nullable=True
    )


class AuthApiKey(Base):
    __tablename__ = "config_auth_api_key"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    api_key_hash: Mapped[str] = mapped_column(String(256), nullable=False, unique=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_used_at: Mapped[Optional[datetime.datetime]] = mapped_column(
        DateTime, nullable=True
    )
    created_at: Mapped[Optional[datetime.datetime]] = mapped_column(
        DateTime, nullable=True
    )
    updated_at: Mapped[Optional[datetime.datetime]] = mapped_column(
        DateTime, nullable=True
    )


class RuntimeConfig(Base):
    __tablename__ = "config_runtime"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    description: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    updated_at: Mapped[Optional[datetime.datetime]] = mapped_column(
        DateTime, nullable=True
    )


class UploadTask(Base):
    """一个文件版本及目标组合的当前状态，历史尝试单独保留。"""

    __tablename__ = "upload_task"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    identity: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    file_path: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    targets: Mapped[str] = mapped_column(String(128), default="unknown")
    fingerprint: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="failed", index=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    failure_count: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[Optional[datetime.datetime]] = mapped_column(
        DateTime, nullable=True
    )
    cleared: Mapped[bool] = mapped_column(Boolean, default=False)
    tmdb_override: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    auto_retry: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")


class UploadAttempt(Base):
    """关联原始历史表，不删除或改写已有的失败原因。"""

    __tablename__ = "upload_attempt"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    history_id: Mapped[int] = mapped_column(Integer, nullable=False, unique=True)


class DownloadFileBinding(Base):
    """qB 种子文件与上传任务的稳定关联，移动不改变上传历史。"""

    __tablename__ = "download_file_binding"
    identity: Mapped[str] = mapped_column(String(64), primary_key=True)
    group_key: Mapped[str] = mapped_column(String(64), index=True)
    downloader_key: Mapped[str] = mapped_column(String(64), index=True)
    torrent_hash: Mapped[str] = mapped_column(String(128), index=True)
    task_id: Mapped[int] = mapped_column(Integer, index=True)
    file_index: Mapped[int] = mapped_column(Integer)
    file_size: Mapped[int] = mapped_column(Integer)
    source_path: Mapped[str] = mapped_column(Text)
    current_path: Mapped[str] = mapped_column(Text, index=True)
    pending_path: Mapped[Optional[str]] = mapped_column(Text, nullable=True, index=True)


class PostUploadAction(Base):
    """整种子的上传后操作，失败与上传结果分开持久保存。"""

    __tablename__ = "post_upload_action"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    identity: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    downloader_key: Mapped[str] = mapped_column(String(64), index=True)
    torrent_hash: Mapped[str] = mapped_column(String(128))
    torrent_name: Mapped[str] = mapped_column(Text, default="")
    targets: Mapped[str] = mapped_column(String(128))
    kind: Mapped[str] = mapped_column(String(16))
    destination: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(32), default="waiting", index=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    requested_at: Mapped[Optional[datetime.datetime]] = mapped_column(
        DateTime, nullable=True
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=datetime.datetime.now
    )


class TelegramDelivery(Base):
    """通知最新状态和投递进度；只保存 Bot 指纹，不保存 Token。"""

    __tablename__ = "telegram_delivery"
    identity: Mapped[str] = mapped_column(String(64), primary_key=True)
    bot_key: Mapped[str] = mapped_column(String(64), index=True)
    scope: Mapped[str] = mapped_column(String(160), index=True)
    chat_id: Mapped[str] = mapped_column(String(128))
    thread_id: Mapped[str] = mapped_column(String(64), default="")
    message_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    text: Mapped[str] = mapped_column(Text)
    terminal: Mapped[bool] = mapped_column(Boolean, default=False)
    revision: Mapped[int] = mapped_column(Integer, default=0)
    delivered_revision: Mapped[int] = mapped_column(Integer, default=0)
    pending: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    failure_count: Mapped[int] = mapped_column(Integer, default=0)
    blocked: Mapped[bool] = mapped_column(Boolean, default=False)
    next_attempt_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=datetime.datetime.now
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=datetime.datetime.now
    )

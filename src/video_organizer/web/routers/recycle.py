"""网盘回收站清理 API

提供：
- ``GET  /api/recycle/status``            清理器配置、调度时间与最近结果
- ``GET  /api/recycle/{provider}/stats``  实时查询回收站条目数与占用
- ``POST /api/recycle/{provider}/run``    立即清理（支持 dry_run 试运行）

说明：这些接口都会发起阻塞的网络请求，因此处理函数用 ``def`` 声明，
由 FastAPI 自动放入线程池执行，避免卡住事件循环。
"""

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..services.state import get_state_manager
from ...core.recycle_cleaner import (
    RecycleCleaner,
    get_cleaner,
    list_provider_names,
    set_cleaner,
)
from ...core.recycle_cleaner.base import get_provider_class

logger = logging.getLogger(__name__)

router = APIRouter()


class RunRequest(BaseModel):
    """清理请求体"""

    dry_run: bool = False


def _get_config() -> Dict[str, Any]:
    """获取当前配置"""
    state = get_state_manager()
    return state.get_config()


def _attach_existing_clients(cleaner: RecycleCleaner, config: Dict[str, Any]) -> None:
    """复用主流程已登录的云盘客户端，避免重复登录触发风控"""
    handler = get_state_manager().get_video_handler()
    uploader = getattr(handler, "p123_uploader", None) if handler else None
    client = getattr(uploader, "client", None) if uploader else None
    if client is not None:
        from ...core.recycle_cleaner import P123RecycleProvider

        cleaner.use_provider("p123", P123RecycleProvider(config, client=client))


def _get_cleaner() -> RecycleCleaner:
    """获取（或按需创建）全局清理器，并同步最新配置"""
    from ..services.live_config import borrowed_generation

    generation = borrowed_generation.get()
    if generation and generation.cleaner:
        return generation.cleaner
    config = _get_config()
    cleaner = get_cleaner()
    if cleaner is None:
        cleaner = RecycleCleaner(config)
        _attach_existing_clients(cleaner, config)
        set_cleaner(cleaner)
    else:
        cleaner.apply_config(config)
    return cleaner


def _check_provider(provider: str) -> None:
    """校验 provider 是否已注册"""
    if provider not in list_provider_names():
        raise HTTPException(
            status_code=400,
            detail=f"不支持的网盘类型: {provider}，可选: {', '.join(list_provider_names())}",
        )


@router.get("/status")
def recycle_status():
    """清理器配置与最近一次执行结果"""
    try:
        cleaner = _get_cleaner()
        providers: List[Dict[str, Any]] = []
        for name in list_provider_names():
            cls = get_provider_class(name)
            try:
                available = bool(cls(config=_get_config()).is_available())
            except Exception as exc:  # 单个 Provider 异常不影响整体状态
                logger.warning(f"检查回收站 Provider {name} 可用性失败: {exc}")
                available = False
            providers.append(
                {
                    "name": name,
                    "label": getattr(cls, "label", name),
                    "available": available,
                }
            )
        return {
            "success": True,
            "cleaner": cleaner.get_status(),
            "providers": providers,
        }
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"获取回收站清理状态失败: {exc}")
        raise HTTPException(status_code=500, detail=f"获取状态失败: {exc}")


@router.get("/{provider}/stats")
def recycle_stats(provider: str, max_items: Optional[int] = None):
    """实时查询指定网盘回收站的条目数与占用空间"""
    _check_provider(provider)
    cleaner = _get_cleaner()
    limit = int(max_items) if max_items else cleaner.max_items
    try:
        stats = cleaner.stats_for(provider, max_items=limit)
    except Exception as exc:
        logger.error(f"[{provider}] 查询回收站统计失败: {exc}")
        raise HTTPException(status_code=500, detail=f"查询失败: {exc}")
    return {"success": stats.available, "provider": provider, "stats": stats.to_dict()}


@router.post("/{provider}/run")
def recycle_run(provider: str, req: RunRequest):
    """立即清理指定网盘的回收站（不可恢复；dry_run=true 只统计）"""
    _check_provider(provider)
    cleaner = _get_cleaner()
    result = cleaner.clean_one(provider, dry_run=req.dry_run, reason="web")
    if result.get("skipped") and not result.get("success"):
        return {
            "success": False,
            "result": result,
            "message": result.get("message", ""),
        }
    return {"success": bool(result.get("success")), "result": result}

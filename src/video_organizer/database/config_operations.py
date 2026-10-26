import logging
import secrets
import string
from datetime import datetime
from typing import Any, Dict, List, Optional

from .models import (
    ManualRule, ReleaseGroupMapping, LlmProvider, AuthUser, RuntimeConfig,
)
from .session import get_session_local

logger = logging.getLogger(__name__)

# 存储首次运行生成的临时密码（仅内存，不落盘）
_first_run_password: str = ""
_first_run_username: str = ""


def get_first_run_credentials() -> tuple:
    """获取首次运行生成的临时账号密码"""
    return _first_run_username, _first_run_password


def seed_from_ini(config: Dict[str, Any]) -> bool:
    """初始化登录账号和 LLM 提供商；其他重复业务配置不再导入。"""
    try:
        session_local = get_session_local()
    except Exception:
        return False

    seeded = False
    try:
        with session_local() as db:
            now = datetime.now()

            # 手动规则、字幕组映射和运行时配置暂停重复导入，历史数据保留。
            # 3. LLM Providers
            if db.query(LlmProvider).count() == 0:
                for section, values in config.items():
                    if section.startswith("llm_provider_"):
                        db.add(LlmProvider(
                            name=str(values.get("name", "")),
                            api_url=str(values.get("api_url", "")),
                            api_key=str(values.get("api_key", "")),
                            model=str(values.get("model", "")),
                            enabled=str(values.get("enabled", "true")).lower() == "true",
                            weight=int(values.get("weight", 1)),
                            timeout=int(values.get("timeout", 30)),
                            max_retries=int(values.get("max_retries", 2)),
                            created_at=now,
                        ))
                logger.info("已从 INI 导入 LLM Provider 到数据库")
                seeded = True


            # 4. Auth Users — 首次运行自动生成随机密码（不依赖 auth.enabled）
            if db.query(AuthUser).count() == 0:
                from ..web.auth import hash_password

                global _first_run_username, _first_run_password
                auth_config = config.get("auth", {})
                chars = string.ascii_letters + string.digits + "!@#$%^&*"
                raw_pw = ''.join(secrets.choice(chars) for _ in range(12))
                _first_run_username = str(auth_config.get("username", "admin"))
                _first_run_password = raw_pw
                db.add(AuthUser(
                    username=_first_run_username,
                    password_hash=hash_password(raw_pw),
                    role="admin",
                    enabled=True,
                    created_at=now,
                ))
                logger.info("=" * 60)
                logger.info(f"首次运行，已生成随机密码")
                logger.info(f"用户名: {_first_run_username}")
                logger.info(f"密码: {raw_pw}")
                logger.info("=" * 60)
                print("=" * 60, flush=True)
                print(f"⚠  首次运行，已生成随机密码", flush=True)
                print(f"   用户名: {_first_run_username}", flush=True)
                print(f"   密码: {raw_pw}", flush=True)
                print("=" * 60, flush=True)
                seeded = True

            if seeded:
                db.commit()
        return seeded
    except Exception as e:
        logger.warning(f"从 INI 导入配置到数据库失败: {e}")
        return False


def get_manual_rule_dicts() -> List[Dict]:
    """获取手动规则（引擎需要的 List[Dict] 格式）"""
    try:
        with get_session_local()() as db:
            rules = db.query(ManualRule).filter(ManualRule.enabled == True).order_by(ManualRule.sort_order).all()
            return [{"rule": r.rule_text, "enabled": r.enabled} for r in rules]
    except Exception:
        return []


def get_release_groups() -> Dict[str, str]:
    """获取字幕组映射 {name: content_type}"""
    try:
        with get_session_local()() as db:
            items = db.query(ReleaseGroupMapping).all()
            return {r.group_name: r.content_type for r in items}
    except Exception:
        return {}


def get_llm_providers() -> List[Dict]:
    """获取所有 LLM Provider"""
    try:
        with get_session_local()() as db:
            providers = db.query(LlmProvider).filter(LlmProvider.enabled == True).order_by(LlmProvider.weight.desc()).all()
            return [
                {
                    "name": p.name,
                    "api_url": p.api_url,
                    "api_key": p.api_key,
                    "model": p.model,
                    "weight": p.weight,
                    "timeout": p.timeout,
                    "max_retries": p.max_retries,
                }
                for p in providers
            ]
    except Exception:
        return []


def get_auth_users() -> Dict[str, str]:
    """获取用户列表 {username: password_hash}"""
    try:
        with get_session_local()() as db:
            users = db.query(AuthUser).filter(AuthUser.enabled == True).all()
            return {u.username: u.password_hash for u in users}
    except Exception:
        return {}


def get_runtime_config(key: str, default: str = "") -> str:
    """获取运行时配置项"""
    try:
        with get_session_local()() as db:
            item = db.query(RuntimeConfig).filter(RuntimeConfig.key == key).first()
            return item.value if item else default
    except Exception:
        return default

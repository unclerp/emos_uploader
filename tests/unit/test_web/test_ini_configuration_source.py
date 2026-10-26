"""INI 为业务配置来源，历史数据库配置不可编辑也不覆盖 INI。"""
from copy import deepcopy
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from video_organizer.core.config_loader import DEFAULT_CONFIG, load_config, update_config
from video_organizer.core.renamer import VideoRenamer
from video_organizer.database import config_operations
from video_organizer.database.models import (
    Base, AuthUser, ManualRule, ReleaseGroupMapping, LlmProvider, RuntimeConfig,
)
from video_organizer.web.routers import config as config_routes


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(config_routes.router, prefix="/api/config")
    with TestClient(app) as instance:
        yield instance


@pytest.mark.parametrize("method,path,body", [
    ("GET", "/db/manual-rules", None),
    ("POST", "/db/manual-rules", {"rule_text": "replace: A -> B"}),
    ("PUT", "/db/manual-rules/1", {"rule_text": "replace: A -> B"}),
    ("DELETE", "/db/manual-rules/1", None),
    ("GET", "/db/release-groups", None),
    ("POST", "/db/release-groups", {"group_name": "test", "content_type": "tv"}),
    ("PUT", "/db/release-groups/1", {"group_name": "test", "content_type": "tv"}),
    ("DELETE", "/db/release-groups/1", None),
    ("GET", "/db/runtime", None),
    ("PUT", "/db/runtime/delete_after_upload", {"value": "true"}),
])
def test_duplicate_endpoints_never_touch_database(client, monkeypatch, method, path, body):
    database = MagicMock(side_effect=AssertionError("不应访问历史配置表"))
    monkeypatch.setattr(config_routes, "get_session_local", database)
    response = client.request(method, "/api/config" + path, json=body)
    assert response.status_code == 409
    assert "INI" in response.json()["detail"]
    database.assert_not_called()


def test_llm_provider_still_prefers_database(monkeypatch):
    import sys
    from types import SimpleNamespace
    from video_organizer.core.renamer import renamer as module
    provider = {"name": "database-provider", "api_url": "https://example.com", "api_key": "db-key", "model": "db-model"}
    database = MagicMock(return_value=[provider])
    monkeypatch.setattr(config_operations, "get_llm_providers", database)
    factory = MagicMock()
    factory.return_value.enabled = True
    factory.return_value.providers = [provider]
    factory.return_value.strategy.value = "round_robin"
    monkeypatch.setattr(module, "LLMTranslator", factory)
    VideoRenamer(tmdb_api_key="", config={"llm_fallback": {"enabled": True}, "llm_provider_1": {"enabled": True, "name": "ini-provider"}})
    assert factory.call_args.kwargs["providers"] == [provider]
    database.assert_called_once()


def test_llm_database_api_still_accessible(client, monkeypatch):
    sessions = MagicMock()
    sessions.return_value.return_value.__enter__.return_value.query.return_value.order_by.return_value.all.return_value = []
    monkeypatch.setattr(config_routes, "get_session_local", sessions)
    response = client.get("/api/config/db/llm-providers")
    assert response.status_code == 200
    assert response.json()["providers"] == []
    sessions.assert_called_once()


def test_seed_keeps_existing_data_and_only_initializes_auth(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'config.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    monkeypatch.setattr(config_operations, "get_session_local", lambda: sessions)
    with sessions() as db:
        db.add(ManualRule(rule_text="old rule", sort_order=0))
        db.add(RuntimeConfig(key="delete_after_upload", value="True"))
        db.commit()
    config = {"manual_rules": {"enabled": True, "rules": [{"rule": "new rule"}]},
              "release_group_mapping": {"NewGroup": "drama"},
              "llm_provider_1": {"enabled": True, "name": "new provider"},
              "processing": {"delete_after_upload": False}}
    assert config_operations.seed_from_ini(config)
    assert not config_operations.seed_from_ini(config)
    with sessions() as db:
        assert db.query(AuthUser).count() == 1
        assert [r.rule_text for r in db.query(ManualRule)] == ["old rule"]
        assert db.get(RuntimeConfig, "delete_after_upload").value == "True"
        assert db.query(ReleaseGroupMapping).count() == 0
        assert db.query(LlmProvider).count() == 1
    engine.dispose()


def test_removed_ini_rules_do_not_reappear_after_save(tmp_path):
    path = tmp_path / "config.ini"
    config = deepcopy(DEFAULT_CONFIG)
    config["manual_rules"].update(enabled=True, rule1="replace: Old -> New", rules=[])
    update_config(config, str(path))
    assert load_config(str(path))["manual_rules"]["rules"] == []
    config["manual_rules"]["rules"] = [{"rule": "when: Show => {[tmdbid=123;type=tv]}"}]
    update_config(config, str(path))
    rules = load_config(str(path))["manual_rules"]["rules"]
    assert rules == [{"rule": "when: Show => {[tmdbid=123;type=tv]}", "enabled": True}]


def test_ini_runtime_endpoint_remains_editable(client, monkeypatch, tmp_path):
    config = deepcopy(DEFAULT_CONFIG)
    path = tmp_path / "config.ini"
    update_config(config, str(path))
    state = MagicMock()
    state.get_config.return_value = config
    state.get_saved_config.return_value = config
    state.apply_config.return_value = {"applied": True, "message": "配置已保存并应用"}
    state.get_config_path.return_value = path
    monkeypatch.setattr(config_routes, "get_state_manager", lambda: state)
    response = client.put("/api/config/section", json={"section": "processing", "values": {"delete_after_upload": True}})
    assert response.status_code == 200
    assert load_config(str(path))["processing"]["delete_after_upload"] is True


def test_mapping_replacement_removes_old_entries(client, monkeypatch, tmp_path):
    config = deepcopy(DEFAULT_CONFIG)
    config["release_group_mapping"] = {"OldGroup": "anime"}
    path = tmp_path / "config.ini"
    update_config(config, str(path))
    state = MagicMock()
    state.get_config.return_value = config
    state.get_saved_config.return_value = config
    state.apply_config.return_value = {"applied": True, "message": "配置已保存并应用"}
    state.get_config_path.return_value = path
    monkeypatch.setattr(config_routes, "get_state_manager", lambda: state)
    response = client.put("/api/config/section", json={"section": "release_group_mapping", "values": {"NewGroup": "drama"}, "replace": True})
    assert response.status_code == 200
    assert load_config(str(path))["release_group_mapping"] == {"newgroup": "drama"}

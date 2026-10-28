"""验证网页提交、INI 持久化和下载器收到的是同一份路径映射。"""

import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from video_organizer.core.config_loader import (
    DEFAULT_CONFIG,
    load_config,
    update_config,
)
from video_organizer.core.downloader.factory import DownloaderMonitorFactory
from video_organizer.core.downloader.qbittorrent_monitor import QBittorrentMonitor
from video_organizer.core.effective_config import normalize_config
from video_organizer.core.path_mappings import parse_path_mappings
from video_organizer.web.routers import config as routes

MAPPING = {"/home/unclerp/torrents/deluge/emos_ready": "/input"}


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, {}),
        ("", {}),
        ("  ", {}),
        ("{}", {}),
        ({}, {}),
        ([], {}),
        (json.dumps(MAPPING), MAPPING),
        (MAPPING, MAPPING),
        ("/downloads:/input", {"/downloads": "/input"}),
        (
            "/downloads:/input, /other:/second",
            {"/downloads": "/input", "/other": "/second"},
        ),
        (
            ["/downloads:/input", "/other:/second"],
            {"/downloads": "/input", "/other": "/second"},
        ),
        ("{'/downloads': '/input'}", {"/downloads": "/input"}),
        (r"C:\Downloads:D:\Videos", {r"C:\Downloads": r"D:\Videos"}),
        ("/downloads:F:/Videos", {"/downloads": "F:/Videos"}),
    ],
)
def test_supported_representations(value, expected):
    assert parse_path_mappings(value) == expected
    assert DownloaderMonitorFactory._parse_path_mappings(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        '{"/downloads":}',
        '{"/downloads": null}',
        '{"/downloads": 1}',
        '{"": "/input"}',
        '{"/downloads": ""}',
        "invalid-entry",
        42,
        {"/downloads": ["/input"]},
        {3: "/input"},
    ],
)
def test_invalid_mapping_is_not_silently_split(value):
    with pytest.raises(ValueError):
        parse_path_mappings(value)


@pytest.mark.parametrize(
    "mapping",
    [
        MAPPING,
        {"/downloads": "/input", "/other": "/second"},
        {r"C:\Downloads": r"D:\Videos"},
        {'/目录/含,冒号:和"引号': '/input/含,冒号:和"引号'},
        {},
    ],
)
def test_repeated_ini_round_trip_preserves_json_structure(tmp_path, mapping):
    path = tmp_path / "config.ini"
    config = deepcopy(DEFAULT_CONFIG)
    config["monitoring"]["path_mappings"] = mapping
    for _ in range(4):
        update_config(config, str(path))
        config = load_config(str(path))
        assert config["monitoring"]["path_mappings"] == mapping
    monitor = QBittorrentMonitor(
        Mock(), path_mappings=config["monitoring"]["path_mappings"]
    )
    if mapping == MAPPING:
        assert (
            monitor._apply_path_mapping(next(iter(mapping)) + "/Film.mkv")
            == "/input/Film.mkv"
        )


def test_legacy_ini_still_reads_then_saves_as_json(tmp_path):
    path = tmp_path / "config.ini"
    path.write_text(
        "[monitoring]\npath_mappings = /downloads:/input,/other:/second\n",
        encoding="utf-8",
    )
    config = load_config(str(path))
    expected = {"/downloads": "/input", "/other": "/second"}
    assert config["monitoring"]["path_mappings"] == expected
    update_config(config, str(path))
    assert load_config(str(path))["monitoring"]["path_mappings"] == expected


@pytest.mark.parametrize("kind", ["qbittorrent", "aria2"])
@pytest.mark.parametrize(
    "value",
    [MAPPING, json.dumps(MAPPING), "/home/unclerp/torrents/deluge/emos_ready:/input"],
)
def test_downloader_specific_mapping_round_trips(tmp_path, kind, value):
    path = tmp_path / "config.ini"
    config = deepcopy(DEFAULT_CONFIG)
    section = "downloader." + kind
    config[section] = {"type": kind, "path_mappings": value, "enabled": False}
    for _ in range(3):
        update_config(normalize_config(config), str(path))
        config = load_config(str(path))
        assert config[section]["path_mappings"] == MAPPING
        assert config["downloaders"][0]["path_mappings"] == MAPPING
    monitor = DownloaderMonitorFactory.create_monitor(
        kind, Mock(), config["downloaders"][0]
    )
    assert monitor.path_mappings == MAPPING


@pytest.fixture
def api(tmp_path, monkeypatch):
    path = tmp_path / "config.ini"
    update_config(deepcopy(DEFAULT_CONFIG), str(path))
    applied = []

    def apply(config, filename):
        applied.append(deepcopy(config))
        return {"applied": True, "message": "配置已应用"}

    state = SimpleNamespace(
        get_saved_config=lambda: load_config(str(path)),
        get_config_path=lambda: path,
        apply_config=apply,
    )
    monkeypatch.setattr(routes, "get_state_manager", lambda: state)
    app = FastAPI()
    app.include_router(routes.router, prefix="/api/config")
    with TestClient(app) as client:
        yield client, path, applied


def test_browser_save_read_reload_and_save_again(api):
    client, path, applied = api
    for _ in range(3):
        response = client.put(
            "/api/config/section",
            json={
                "section": "monitoring",
                "values": {"path_mappings": json.dumps(MAPPING)},
            },
        )
        assert response.status_code == 200 and response.json()["applied"]
        assert response.json()["config"]["monitoring"]["path_mappings"] == MAPPING
        assert applied[-1]["monitoring"]["path_mappings"] == MAPPING
        assert (
            client.get("/api/config").json()["config"]["monitoring"]["path_mappings"]
            == MAPPING
        )
        assert client.post("/api/config/reload").json()["applied"]
        assert load_config(str(path))["monitoring"]["path_mappings"] == MAPPING
    # 同时验证从配置页读取对象、原样保存的流程。
    saved = client.get("/api/config").json()["config"]["monitoring"]
    assert client.put(
        "/api/config/section", json={"section": "monitoring", "values": saved}
    ).json()["applied"]
    assert applied[-1]["monitoring"]["path_mappings"] == MAPPING


@pytest.mark.parametrize(
    "value", ['{"/downloads":}', {"/downloads": 1}, {"/downloads": ""}]
)
def test_invalid_web_mapping_never_overwrites_ini(api, value):
    client, path, applied = api
    original = path.read_bytes()
    response = client.put(
        "/api/config/section",
        json={"section": "monitoring", "values": {"path_mappings": value}},
    )
    assert response.status_code == 422
    assert path.read_bytes() == original
    assert not applied


def test_qb_start_authenticates_before_validation(monkeypatch):
    monitor = QBittorrentMonitor(Mock())
    authenticated = []
    monkeypatch.setattr(monitor, "_login", lambda: authenticated.append(True) or True)
    monkeypatch.setattr(monitor, "_monitor_loop", Mock())
    monitor.start()
    try:
        assert authenticated == [True]
    finally:
        monitor.stop()

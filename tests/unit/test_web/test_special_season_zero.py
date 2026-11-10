"""特别篇季号必须贯穿真实解析、指定条目、预览和 emos 请求。"""

import importlib
import logging
from urllib.parse import parse_qs, urlsplit
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from video_organizer.core.renamer import VideoRenamer
from video_organizer.core.file_handler.handler import VideoFileHandler

ORIGINAL = (
    "【幻月字幕组】【23年日剧番外】重启人生番外篇.Another.Story.s00EP01【中文字幕】.mp4"
)
UNNUMBERED_SP = "Kurokawa.no.Techo.SP.2021.Amazon.WEB-DL.1080p.H264.DDP-HDCTV.mkv"


@pytest.mark.parametrize("guessit", [False, True])
@pytest.mark.parametrize("prefer", [False, True])
@pytest.mark.parametrize(
    "name,season,episode",
    [
        (ORIGINAL, 0, 1),
        ("重启人生.S00E01.mp4", 0, 1),
        ("Show.s00ep02.mkv", 0, 2),
        ("Show.S02EP03.mkv", 2, 3),
        ("Show.S02E03.mkv", 2, 3),
        ("Show.S02E03.SP.mkv", 2, 3),
    ],
)
def test_real_parser_and_explicit_tmdb_preserve_season(
    name, season, episode, guessit, prefer
):
    r = VideoRenamer(
        tmdb_api_key="",
        config={
            "guessit": {"enabled": guessit, "prefer_guessit": prefer},
            "llm_fallback": {"enabled": False},
        },
    )
    parsed = r.extract_metadata(name, media_type_hint="tv")
    assert (int(parsed["season"]), int(parsed["episode"])) == (season, episode)
    assert parsed["original_filename"] == name
    client = Mock()
    client.get_tv_details.return_value = {
        "id": 42,
        "name": "指定剧集",
        "genres": [],
        "first_air_date": "2023-01-01",
    }
    client.get_tv_episode_details.return_value = {"name": "特别篇"}
    client.search_tv.side_effect = AssertionError("手动指定不能回退搜索")
    r.tmdb_client = client
    result = r.extract_metadata_with_tmdb(name, 42, "tv")
    assert (int(result["season"]), int(result["episode"])) == (season, episode)
    client.get_tv_episode_details.assert_called_with(42, season, episode)


@pytest.mark.parametrize("season", [0, "0", "00"])
def test_zero_not_missing_and_never_inferred(season):
    r = VideoRenamer.__new__(VideoRenamer)
    r._infer_season_from_year = Mock(return_value=2)
    data = {"season": season, "episode": 1, "year": "2023", "tmdb_id": 42}
    r._ensure_season(data)
    assert data["season"] == season
    r._infer_season_from_year.assert_not_called()


@pytest.mark.parametrize("season", [None, ""])
def test_missing_season_still_defaults_to_one(season):
    r = VideoRenamer.__new__(VideoRenamer)
    data = {"season": season, "episode": 1}
    r._ensure_season(data)
    assert data["season"] == 1


@pytest.fixture
def upload_handler(monkeypatch):
    mod = importlib.import_module("video_organizer.core.file_handler.handler")
    monkeypatch.setattr(mod, "prepare_task", Mock(return_value=1))
    monkeypatch.setattr("video_organizer.notifications.telegram.bind_task", Mock())
    h = VideoFileHandler.__new__(VideoFileHandler)
    h.logger = logging.getLogger("season-zero-test")
    h._manual_tmdb_override = {"tmdb_id": 42, "media_type": "tv"}
    h._failed_files = {}
    h._uploading_files = set()
    h._processing_files = set()
    h.upload_targets = ["emos"]
    h.emos_base_url = "https://invalid.example"
    h.emos_auth_token = "test-only"
    h._execute_upload = Mock()
    h._record_task = Mock()
    response = Mock()
    response.json.return_value = {
        "video_type": "tv",
        "item_id": 10,
        "season_info": {"item_id": 20},
        "episode_info": {"item_id": 30, "item_type": "ve"},
    }
    get = Mock(return_value=response)
    monkeypatch.setattr(mod.requests, "get", get)
    return h, get


@pytest.mark.parametrize("season", [0, "00"])
@pytest.mark.parametrize("episode", [None, 1])
def test_actual_handler_sends_specials_to_emos(upload_handler, season, episode):
    h, get = upload_handler
    h.renamer = Mock()
    h.renamer.extract_metadata_with_tmdb.return_value = {
        "tmdb_id": 42,
        "media_type": "tv",
        "title": "指定剧集",
        "season": season,
        "episode": episode,
    }
    h._process_file_snapshot("/input/" + ORIGINAL)
    assert get.call_count == 1
    assert "season_number=0&episode_number=1" in get.call_args.args[0]
    h._execute_upload.assert_called_once()
    h._record_task.assert_not_called()


@pytest.mark.parametrize("filename, episode", [(UNNUMBERED_SP, 1), ("Show.SP03.mkv", 3)])
def test_special_validation_matches_emos_lookup(
    monkeypatch, tmp_path, upload_handler, filename, episode
):
    path = tmp_path / filename
    path.touch()
    h, get = upload_handler
    renamer = VideoRenamer(tmdb_api_key="", config={})
    tmdb = Mock()
    tmdb.get_tv_details.return_value = {"id": 42, "name": "指定剧集", "genres": []}
    tmdb.get_tv_episode_details.return_value = {"name": "特别篇"}
    h.renamer = renamer
    state = Mock()
    state.get_config.return_value = {}
    state.get_tmdb_client.return_value = tmdb
    state.make_task_renamer.return_value = renamer
    manual = importlib.import_module("video_organizer.web.routers.manual")
    monkeypatch.setattr(manual, "get_state_manager", lambda: state)
    app = FastAPI()
    app.include_router(manual.router, prefix="/manual")
    with TestClient(app) as client:
        data = client.post(
            "/manual/validate", json={"file_path": str(path), **h._manual_tmdb_override}
        ).json()

    h._process_file_snapshot(str(path))
    query = parse_qs(urlsplit(get.call_args.args[0]).query)
    assert query["season_number"] == ["0"]
    assert query["episode_number"] == [str(episode)]
    assert (data["season"], data["episode"]) == (0, episode)
    season_episode = f"S00E{episode:02d}"
    assert season_episode in data["suggested_name"]
    assert data["episode_title"] == "特别篇"
    assert tmdb.get_tv_episode_details.call_count == 2
    tmdb.get_tv_episode_details.assert_called_with(42, 0, episode)
    assert h._execute_upload.call_args.args[2] == 30
    assert h._execute_upload.call_args.args[7] == season_episode


@pytest.mark.parametrize("marker", ["s00ep01", "S00E01", "S01EP01", "S01E01"])
def test_explicit_episode_season_not_inferred_from_year(marker):
    r = VideoRenamer.__new__(VideoRenamer)
    r._infer_season_from_year = Mock(return_value=3)
    season = int(marker[1:3])
    data = {
        "original_filename": "剧名." + marker + "【中文字幕】.mp4",
        "season": season,
        "episode": 1,
        "year": "2023",
        "tmdb_id": 42,
    }
    r._ensure_season(data)
    assert data["season"] == season
    r._infer_season_from_year.assert_not_called()


@pytest.mark.parametrize("season", [0, "0", "00"])
def test_generated_path_preserves_zero_even_without_special_keywords(season):
    from pathlib import Path

    r = VideoRenamer(tmdb_api_key="", config={})
    result = r.generate_new_path(
        {
            "show_name": "Show",
            "media_type": "tv",
            "season": season,
            "episode": 1,
            "year": "2023",
        },
        original_path=Path("Show.S00EP01.mkv"),
    )
    assert "S00E01" in str(result)
    assert "Season 00" in str(result)

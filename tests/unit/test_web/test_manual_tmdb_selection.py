"""指定条目在验证、预览与排队上传之间保持一致。"""

import importlib
import logging
import threading
from queue import Queue
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from video_organizer.core.renamer import VideoRenamer
from video_organizer.core.file_handler.handler import VideoFileHandler

manual = importlib.import_module("video_organizer.web.routers.manual")


@pytest.fixture
def api(monkeypatch):
    state = Mock()
    state.get_config.return_value = {}
    state.get_tmdb_client.return_value = None
    state.get_queued_files.return_value = []
    state.get_processing_files.return_value = []
    renamer = state.make_task_renamer.return_value
    metadata = {
        "tmdb_id": 1399,
        "media_type": "tv",
        "show_name": "指定剧集",
        "season": 2,
        "episode": 3,
    }
    renamer.extract_metadata_with_tmdb.return_value = metadata
    renamer.extract_metadata.return_value = metadata
    renamer.generate_new_path.return_value = __import__("pathlib").Path(
        "指定剧集/S02E03.mkv"
    )
    monkeypatch.setattr(manual, "get_state_manager", lambda: state)
    app = FastAPI()
    app.include_router(manual.router, prefix="/manual")
    with TestClient(app) as client:
        yield client, state, renamer


@pytest.mark.parametrize("endpoint", ["preview", "validate"])
def test_preview_and_validate_use_explicit_id(api, tmp_path, endpoint):
    client, _, renamer = api
    path = tmp_path / "wrong.S02E03.mkv"
    path.touch()
    response = client.post(
        "/manual/" + endpoint,
        json={"file_path": str(path), "tmdb_id": 1399, "media_type": "tv"},
    )
    assert response.status_code == 200 and response.json()["success"]
    renamer.extract_metadata_with_tmdb.assert_called_once_with(
        str(path), tmdb_id=1399, media_type="tv"
    )
    renamer.extract_metadata.assert_not_called()


@pytest.mark.parametrize(
    "payload",
    [
        {"tmdb_id": 0, "media_type": "tv"},
        {"tmdb_id": -1, "media_type": "movie"},
        {"tmdb_id": 1},
        {"tmdb_id": 1, "media_type": "unknown"},
        {"tmdb_id": 1.5, "media_type": "tv"},
    ],
)
@pytest.mark.parametrize("endpoint", ["preview", "validate", "process"])
def test_invalid_selection_is_rejected(api, payload, endpoint):
    response = api[0].post(
        "/manual/" + endpoint, json={"file_path": "any.mkv", **payload}
    )
    assert response.status_code == 422


def test_queue_single_and_batch_forward_selection(api, tmp_path):
    client, state, _ = api
    path = tmp_path / "file.mkv"
    path.touch()
    selection = {"tmdb_id": 550, "media_type": "movie"}
    assert client.post(
        "/manual/process", json={"file_path": str(path), **selection}
    ).json()["success"]
    state.get_video_handler().force_process_file.assert_called_with(
        str(path), tmdb_override=selection
    )
    state.get_video_handler().force_process_file.reset_mock()
    response = client.post(
        "/manual/process-batch", json={"file_paths": [str(path)], **selection}
    )
    assert response.json()["added_count"] == 1
    state.get_video_handler().force_process_file.assert_called_once_with(
        str(path), tmdb_override=selection
    )


def test_legacy_batch_and_automatic_validation_still_work(api, tmp_path):
    client, state, renamer = api
    path = tmp_path / "file.mkv"
    path.touch()
    assert (
        client.post("/manual/process-batch", json=[str(path)]).json()["added_count"]
        == 1
    )
    state.get_video_handler().force_process_file.assert_called_with(str(path))
    assert client.post("/manual/validate", json={"file_path": str(path)}).json()[
        "success"
    ]
    renamer.extract_metadata.assert_called_once_with(str(path), media_type_hint=None)


@pytest.mark.parametrize("media_type", ["tv", "movie"])
def test_explicit_details_override_filename_and_do_not_use_search(
    monkeypatch, media_type
):
    renamer = VideoRenamer.__new__(VideoRenamer)
    renamer.tmdb_client = Mock()
    details = {
        "id": 42,
        "name": "指定剧集",
        "title": "指定电影",
        "first_air_date": "2024-01-01",
        "release_date": "2023-01-01",
        "genres": [],
    }
    renamer.tmdb_client.get_tv_details.return_value = details
    renamer.tmdb_client.get_movie_details.return_value = details
    renamer.tmdb_client.get_tv_episode_details.return_value = {"name": "第三集"}
    renamer._ensure_season = Mock()
    renamer._determine_category = lambda metadata: "TV Shows/其他"

    def parse(parser, path, media_type_hint=None):
        assert parser.tmdb_client is None
        return {
            "tmdb_id": 999,
            "media_type": "tv",
            "show_name": "错误片名",
            "season": 2,
            "episode": 3,
            "quality_tags": "2160p",
        }

    monkeypatch.setattr(VideoRenamer, "extract_metadata", parse)
    result = renamer.extract_metadata_with_tmdb("wrong.S02E03.mkv", 42, media_type)
    assert result["tmdb_id"] == 42 and result["media_type"] == media_type
    assert result["quality_tags"] == "2160p"
    if media_type == "tv":
        assert (
            result["season"] == 2
            and result["episode"] == 3
            and result["episode_title"] == "第三集"
        )
    else:
        assert "season" not in result and "episode" not in result
    renamer.tmdb_client.search_tv.assert_not_called()
    renamer.tmdb_client.search_movie.assert_not_called()


def test_invalid_explicit_id_never_falls_back(monkeypatch):
    renamer = VideoRenamer.__new__(VideoRenamer)
    renamer.tmdb_client = Mock()
    renamer.tmdb_client.get_movie_details.return_value = None
    monkeypatch.setattr(VideoRenamer, "extract_metadata", lambda *args, **kwargs: {})
    with pytest.raises(ValueError, match="无法获取指定"):
        renamer.extract_metadata_with_tmdb("file.mkv", 999999, "movie")
    renamer.tmdb_client.search_movie.assert_not_called()


def test_queue_selection_is_task_local(monkeypatch, tmp_path):
    module = importlib.import_module("video_organizer.core.file_handler.handler")
    monkeypatch.setattr(module, "prepare_task", Mock(side_effect=[101, 102]))
    handler = VideoFileHandler.__new__(VideoFileHandler)
    handler.logger = logging.getLogger("test")
    handler._uploaded_files = set()
    handler._uploading_files = set()
    handler.upload_targets = ["emos"]
    handler._failed_files = {}
    handler._queued_files = set()
    handler._processing_files = set()
    handler._queue_lock = threading.Lock()
    handler._upload_queue = Queue()
    handler._use_queue = True
    handler.max_upload_workers = 1
    handler._runtime_manager = None
    handler._is_supported_file = lambda path: True
    seen = []
    monkeypatch.setattr(
        VideoFileHandler,
        "_process_file_snapshot",
        lambda task, path, worker_id=0: seen.append((path, task._manual_tmdb_override)),
    )
    selection = {"tmdb_id": 42, "media_type": "tv"}
    first = tmp_path / "a.mkv"
    first.touch()
    second = tmp_path / "b.mkv"
    second.touch()
    handler.force_process_file(str(first), tmdb_override=selection)
    selection["tmdb_id"] = 99
    handler.force_process_file(str(second))
    queued = handler._upload_queue.get_nowait()
    handler._process_file_internal(
        queued[0], tmdb_override=queued[1], tracking_task_id=queued[2]
    )
    queued_second = handler._upload_queue.get_nowait()
    handler._process_file_internal(
        queued_second[0],
        tmdb_override=queued_second[1],
        tracking_task_id=queued_second[2],
    )
    assert seen == [
        (str(first), {"tmdb_id": 42, "media_type": "tv"}),
        (str(second), None),
    ]
    assert not hasattr(handler, "_manual_tmdb_override")


def test_real_filename_parser_keeps_episode_without_search():
    renamer = VideoRenamer(
        tmdb_api_key="",
        config={"guessit": {"enabled": True}, "llm_fallback": {"enabled": False}},
    )
    client = Mock()
    client.get_tv_details.return_value = {
        "id": 42,
        "name": "指定剧集",
        "genres": [],
        "first_air_date": "2024-01-01",
    }
    client.get_tv_episode_details.return_value = {"name": "第三集"}
    client.search_tv.side_effect = AssertionError("explicit selection must not search")
    client.search_movie.side_effect = AssertionError(
        "explicit selection must not search"
    )
    renamer.tmdb_client = client
    result = renamer.extract_metadata_with_tmdb(
        "/input/Wrong.S02E03.2160p.WEB-DL.mkv", 42, "tv"
    )
    assert (result["tmdb_id"], int(result["season"]), int(result["episode"])) == (
        42,
        2,
        3,
    )
    assert result["show_name"] == "指定剧集"
    client.search_tv.assert_not_called()
    client.search_movie.assert_not_called()


def test_failed_override_is_not_automatically_retried_without_id(monkeypatch):
    module = importlib.import_module("video_organizer.core.file_handler.handler")
    monkeypatch.setattr(module, "record_task", Mock())
    monkeypatch.setattr(module, "prepare_task", Mock(return_value=101))
    handler = VideoFileHandler.__new__(VideoFileHandler)
    handler.logger = logging.getLogger("test")
    handler.upload_targets = ["emos"]
    handler.renamer = Mock()
    handler.renamer.extract_metadata_with_tmdb.side_effect = ValueError("指定编号无效")
    handler._manual_tmdb_override = {"tmdb_id": 42, "media_type": "tv"}
    handler._failed_files = {}
    handler._processing_files = set()
    handler._parent_monitor = Mock()
    handler._process_file_snapshot("/input/wrong.mkv")
    handler.renamer.extract_metadata.assert_not_called()
    handler._parent_monitor._retry_files.add.assert_not_called()
    assert "/input/wrong.mkv" in handler._failed_files

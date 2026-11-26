"""上游原生解析器、分段命名与 custom 手动入口的组合回归。"""

from pathlib import Path
from unittest.mock import Mock

from video_organizer.core.renamer import VideoRenamer


def test_native_parser_honors_manual_filename_replacement():
    renamer = VideoRenamer(
        None,
        config={
            "manual_rules": {
                "enabled": True,
                "rules": [{"enabled": True, "rule": "replace: 旧标题 -> 新标题"}],
            },
        },
    )
    metadata = renamer.extract_metadata("旧标题01.mp4")
    assert metadata["show_name"] == "新标题"
    assert int(metadata["episode"]) == 1


def test_manual_tmdb_movie_keeps_part_marker_in_generated_filename():
    renamer = VideoRenamer("test-key")
    renamer.tmdb_client = Mock()
    renamer.tmdb_client.get_movie_details.return_value = {
        "id": 42,
        "title": "指定电影",
        "release_date": "2026-01-01",
    }
    filename = "Film.Part.2.2026.1080p.mkv"
    metadata = renamer.extract_metadata_with_tmdb(filename, 42, "movie")
    output = renamer.generate_new_path(metadata, original_path=Path(filename))
    assert metadata["tmdb_title"] == "指定电影"
    assert metadata["part"] == "Part 2"
    assert "Part 2" in output.name
    assert "Part 2" not in str(output.parent)
    renamer.tmdb_client.get_movie_details.assert_called_once_with(42)
    renamer.tmdb_client.search_all_pages.assert_not_called()

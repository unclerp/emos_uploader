"""Emos 各排重出口必须报告跳过，不能报告实际上传完成。"""

from unittest.mock import Mock

import pytest

from video_organizer.upload import emos_uploader as emos


@pytest.mark.parametrize("stage", ["precheck", "token", "refresh_token"])
def test_duplicate_progress_is_skipped_at_every_exit(tmp_path, monkeypatch, stage):
    file = tmp_path / "film.mkv"
    file.write_bytes(b"video")
    uploader = emos.RobustEmosVideoUploader("test", cache_dir=str(tmp_path / "cache"))
    duplicate = {"media_id": "existing", "media_name": "Film"}
    uploader.check_existing_media = Mock(
        return_value=duplicate if stage == "precheck" else None
    )
    uploader.send_tg_notification = Mock()
    uploader.step1_init_video = Mock(return_value={})
    uploader.step2_get_upload_token = Mock(
        return_value={"message": "此资源您之前上传过"}
    )
    uploader.step3_chunk_upload = Mock(return_value=False)
    uploader._get_cache_key = Mock(return_value="cache")
    uploader._load_upload_cache = Mock(
        return_value=(
            {"step2_result": {"file_id": "expired"}}
            if stage == "refresh_token"
            else None
        )
    )
    uploader._clear_upload_cache = Mock()
    progress = Mock()
    monkeypatch.setattr(emos, "_report_upload_progress", progress)
    result = uploader.upload_video(
        file,
        "ve",
        "42",
        video_list_id=1,
        video_season_id=2,
        video_episode_id=42,
        skip_existing_media=True,
    )
    assert result["skipped"]
    assert progress.call_args.kwargs["status"] == "skipped"
    assert progress.call_args.kwargs["uploaded_bytes"] == 0
    assert not any(
        call.kwargs["status"] == "completed" for call in progress.call_args_list
    )
    assert uploader.step3_chunk_upload.call_count == (stage == "refresh_token")

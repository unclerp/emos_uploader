import os
import sys
import unittest
from unittest.mock import MagicMock, patch
from pathlib import Path
import tempfile
import shutil

# 添加项目根目录到Python路径
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))

from src.video_organizer.upload.emos_uploader import RobustEmosVideoUploader
from src.video_organizer.core.config_loader import DEFAULT_CONFIG


class TestEmosDuplicateCheck(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp())
        self.test_file = self.temp_dir / "test_video.mp4"
        # 写入 1024 字节的数据
        self.test_file.write_bytes(b"0" * 1024)
        self.file_size = 1024

        self.uploader = RobustEmosVideoUploader(
            auth_token="fake_token",
            base_url="https://emos.best",
            cache_dir=str(self.temp_dir / "cache")
        )

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_config_default_skip_existing(self):
        """测试默认配置中包含 skip_existing_media 开关且为 True"""
        self.assertIn("skip_existing_media", DEFAULT_CONFIG["emos"])
        self.assertTrue(DEFAULT_CONFIG["emos"]["skip_existing_media"])

    @patch("requests.Session.get")
    def test_check_existing_media_found(self, mock_get):
        """测试存在完全相同大小且 status 为 complete 的资源"""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = [
            {
                "media_id": "m1_diff_size",
                "media_status": "complete",
                "media_file_size": 2048,
                "media_name": "1080p",
            },
            {
                "media_id": "m2_same_size_pending",
                "media_status": "pending",
                "media_file_size": 1024,
                "media_name": "4K Pending",
            },
            {
                "media_id": "m3_same_size_complete",
                "media_status": "complete",
                "media_file_size": 1024,
                "media_name": "4K HDR - User Upload",
                "user_pseudonym": "TestUser",
            },
        ]
        mock_get.return_value = mock_response

        res = self.uploader.check_existing_media(
            file_path=self.test_file,
            video_list_id=1008,
            video_season_id=1025,
            video_episode_id=2949532,
        )

        self.assertIsNotNone(res)
        self.assertEqual(res["media_id"], "m3_same_size_complete")

        # 验证请求参数
        mock_get.assert_called_once()
        args, kwargs = mock_get.call_args
        self.assertEqual(
            kwargs["params"],
            {
                "video_list_id": "1008",
                "video_season_id": "1025",
                "video_episode_id": "2949532",
            }
        )

    @patch("requests.Session.get")
    def test_check_existing_media_not_found(self, mock_get):
        """测试不存在相同大小的已完成资源"""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = [
            {
                "media_id": "m1",
                "media_status": "complete",
                "media_file_size": 2048,  # 大小不同
            }
        ]
        mock_get.return_value = mock_response

        res = self.uploader.check_existing_media(
            file_path=self.test_file,
            video_list_id=1008,
            video_season_id=1025,
            video_episode_id=2949532,
        )
        self.assertIsNone(res)

    @patch.object(RobustEmosVideoUploader, "send_tg_notification")
    @patch.object(RobustEmosVideoUploader, "check_existing_media")
    def test_upload_video_skip_when_duplicate(self, mock_check, mock_send_tg):
        """测试 upload_video 开启 skip_existing_media 时直接返回跳过并触发 TG 通知"""
        mock_check.return_value = {
            "media_id": "exist_uuid_123",
            "media_name": "Existing",
            "media_file_size": 1024,
            "user_pseudonym": "TestUploader",
        }

        res = self.uploader.upload_video(
            file_path=self.test_file,
            item_type="ve",
            item_id="2949532",
            video_list_id=1008,
            video_season_id=1025,
            video_episode_id=2949532,
            skip_existing_media=True,
        )

        self.assertIsNotNone(res)
        self.assertTrue(res.get("skipped"))
        self.assertEqual(res.get("media_uuid"), "exist_uuid_123")
        self.assertEqual(res.get("reason"), "duplicate_media_file_size")
        # 跳过通知改为由统一任务结果/独立上传装饰器提交，不再重复发送旧 HTML 消息。
        mock_send_tg.assert_not_called()


if __name__ == "__main__":
    unittest.main()

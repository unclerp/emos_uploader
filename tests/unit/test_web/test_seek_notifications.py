"""覆盖求片轮询快照、全量提醒、失败回滚和独立通知目的地。"""

from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from video_organizer.core.config_loader import load_config, update_config
from video_organizer.core.telegram_config import cloud_config, link_config
from video_organizer.database import session
from video_organizer.database.models import SeekSnapshot, TelegramDelivery
from video_organizer.notifications import seek, telegram

SEEK = {"enabled": True, "bot_token": "123:seek", "chat_id": "-101", "thread_id": "12"}
EMOS = {"base_url": "https://emos.invalid", "auth_token": "auth"}


@pytest.fixture
def dispatcher(tmp_path, monkeypatch):
    monkeypatch.setattr(session, "_engine", None)
    monkeypatch.setattr(session, "_SessionLocal", None)
    session.init_db(str(tmp_path / "seek.db"))
    dispatcher = telegram.TelegramDispatcher(auto_start=False)
    dispatcher.configure_application({"telegram_seek": SEEK, "emos": EMOS})
    yield dispatcher
    session._engine.dispose()


def row(identity, when, title="三餐四季", work=5, season=2, episode=1):
    return {
        "id": identity,
        "updated_at": f"2026-11-15T{when}Z",
        "video_title": title,
        "video_title_display": title,
        "video_list_id": work,
        "video_type": "movie" if season is None else "tv",
        "video_season_number": season,
        "video_episode_number": episode,
    }


def response(items, page=1, total=None, size=15):
    return Mock(
        status_code=200,
        json=Mock(
            return_value={
                "items": items,
                "page": page,
                "page_size": size,
                "total": len(items) if total is None else total,
            }
        ),
    )


def stub_emos(monkeypatch, *responses):
    http = Mock()
    http.post.side_effect = responses
    context = Mock()
    context.__enter__ = Mock(return_value=http)
    context.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(seek.requests, "Session", lambda: context)
    return http


def snapshot_and_messages():
    with session.get_session_local()() as db:
        snapshot = db.get(SeekSnapshot, 1)
        messages = (
            db.query(TelegramDelivery).order_by(TelegramDelivery.updated_at).all()
        )
        return snapshot.items if snapshot else None, [m.text for m in messages]


def test_full_pages_snapshot_silent_first_run_and_persistent_markers(
    dispatcher, monkeypatch
):
    older = row(1, "10:05:20", episode=4)
    original = row(2, "12:00:02", episode=8)
    unchanged = row(3, "11:30:00", title="完美世界", work=6, season=0, episode=2)
    http = stub_emos(
        monkeypatch,
        response([original, unchanged], size=2, total=3),
        response([older], page=2, size=2, total=3),
    )
    dispatcher.seek.poll()
    assert snapshot_and_messages() == ([original, unchanged, older], [])
    assert [c.kwargs["params"] for c in http.post.call_args_list] == [
        {"page": 1},
        {"page": 2},
    ]
    assert http.post.call_args.kwargs["json"]["status"] == ["default"]

    dispatcher = telegram.TelegramDispatcher(auto_start=False)
    dispatcher.configure_application({"telegram_seek": SEEK, "emos": EMOS})
    updated = row(2, "12:12:45", episode=8)
    movie = row(4, "12:08:00", title="电影", work=7, season=None)
    new = [row(i + 10, "12:05:05", episode=i) for i in (1, 2, 3, 5)]
    new.append(row(17, "12:05:05", season=3, episode=1))
    full = [older, original, unchanged]  # 模拟原列表再次返回
    listed = [updated, movie, *new, unchanged, older]
    stub_emos(monkeypatch, response(listed), response(listed), response(full))
    dispatcher.seek.poll()
    assert snapshot_and_messages() == (
        listed,
        [
            "🎬 EMOS 求片提醒\n\n三餐四季\n"
            "  🔄 S02E08 · 11 月 15 日 20:12\n"
            "  🆕 S02E01–S02E03、S02E05、S03E01 · 11 月 15 日 20:05\n"
            "     S02E04 · 11 月 15 日 18:05\n\n"
            "电影\n  🆕 11 月 15 日 20:08\n\n"
            "完美世界\n     S00E02 · 11 月 15 日 19:30",
        ],
    )
    dispatcher.seek.poll()
    assert len(snapshot_and_messages()[1]) == 1
    assert dispatcher.seek.status()["last_success_at"]


def test_failed_page_and_outbox_failure_keep_prior_snapshot(dispatcher, monkeypatch):
    first, second = row(1, "12:00:00"), row(2, "12:30:00", episode=2)
    stub_emos(
        monkeypatch,
        response([first]),
        response([second], size=1, total=2),
        Mock(status_code=503),
        response([second, first]),
        response([second, first]),
    )
    dispatcher.seek.poll()
    dispatcher.seek.poll()
    assert snapshot_and_messages() == ([first], [])
    assert "503" in dispatcher.seek.status()["query_error"]
    stage = dispatcher.stage
    pages = seek.render_pages
    monkeypatch.setattr(seek, "render_pages", lambda *_: ["first", "second"])

    def fail_second(db, config, scope, text):
        if text == "second":
            raise RuntimeError("interrupted")
        return stage(db, config, scope, text)

    dispatcher.stage = fail_second
    dispatcher.seek.poll()
    assert snapshot_and_messages() == ([first], [])
    dispatcher.stage = stage
    monkeypatch.setattr(seek, "render_pages", pages)
    dispatcher.seek.poll()
    assert len(snapshot_and_messages()[1]) == 1
    assert not dispatcher.seek.status()["query_error"]


def test_routing_preserves_legacy_targets_and_cloud_is_independent(
    dispatcher, tmp_path, monkeypatch
):
    path = tmp_path / "config.ini"
    update_config(
        {
            "telegram": {
                "enabled": True,
                "bot_token": "1:upload",
                "chat_id": "-1",
                "thread_id": "11",
                "channel_chat_id": "-2",
                "notify_post_upload": False,
            },
            "recycle_clean": {"notify_telegram": True},
        },
        path,
    )
    config = load_config(str(path))
    assert "telegram_cloud" not in config
    legacy = cloud_config(config)
    assert legacy["notify_recycle"] is False
    assert link_config(legacy)["chat_id"] == "-2"
    assert link_config(legacy)["thread_id"] == ""

    config["telegram"]["enabled"] = False
    config["telegram_cloud"] = {
        "enabled": True,
        "bot_token": "2:cloud",
        "chat_id": "-3",
        "thread_id": "33",
        "notify_links": True,
        "notify_recycle": True,
    }
    dispatcher.configure_application(config)
    monkeypatch.setattr(telegram, "get_dispatcher", lambda: dispatcher)
    with session.get_session_local()() as db:
        old = dict(config["telegram"], enabled=True, chat_id="-2", thread_id="")
        dispatcher.stage(db, old, "channel:legacy", "old")
        db.commit()
    assert telegram.notify_channel({"_cloud": cloud_config(config)}, "cloud", "new")
    request = Mock(
        return_value=Mock(
            status_code=200,
            json=Mock(
                return_value={
                    "ok": True,
                    "result": {"message_id": 12},
                }
            ),
        )
    )
    monkeypatch.setattr(telegram.requests, "post", request)
    assert dispatcher.send_once()
    assert request.call_args.kwargs["json"]["chat_id"] == "-3"
    assert request.call_args.kwargs["json"]["message_thread_id"] == 33
    dispatcher.next_request = 0
    assert not dispatcher.send_once()


def test_long_digest_preserves_order_and_all_titles():
    items = [
        row(i, "12:00:00", title=f"片名 {i:03d} " + "🎞" * 120, work=i, season=None)
        for i in range(1, 80)
    ]
    pages = seek.render_pages(items, {1: "new"})
    assert len(pages) > 1
    assert all(len(p.encode("utf-16-le")) <= 7800 for p in pages)
    assert [
        line for page in pages for line in page.splitlines() if line.startswith("片名")
    ] == [item["video_title"] for item in items]


def test_seek_test_message_reports_delivery_using_current_form(dispatcher, monkeypatch):
    from video_organizer.web.routers.config import router

    app = FastAPI()
    app.include_router(router, prefix="/api/config")
    sender = Mock(return_value="")
    monkeypatch.setattr(seek, "send_test", sender)
    with TestClient(app) as client:
        result = client.post(
            "/api/config/telegram-seek/test",
            json={"bot_token": "999:test", "chat_id": "-404", "thread_id": "42"},
        )
        assert result.status_code == 200
        sender.assert_called_once_with("999:test", "-404", "42")
        result = client.post(
            "/api/config/telegram-seek/test",
            json={"bot_token": "999:test", "chat_id": "-404", "thread_id": "0"},
        )
        assert result.status_code == 422

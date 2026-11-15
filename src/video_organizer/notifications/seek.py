"""轮询 EMOS 未认领求片，对比最近一份完整快照。"""

import logging
import re
import threading
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from textwrap import wrap
from uuid import uuid4

import requests

from ..database.models import SeekSnapshot, TelegramDelivery
from ..database.session import get_session_local
from .telegram import digest, enabled, install_log_redaction

logger = logging.getLogger(__name__)
BEIJING = timezone(timedelta(hours=8))
HEADING = "🎬 EMOS 求片提醒"
MARKERS = {"new": "🆕 ", "updated": "🔄 ", "existing": "   "}


def _timestamp(item):
    timestamp = datetime.fromisoformat(item["updated_at"].replace("Z", "+00:00"))
    if timestamp.tzinfo is None:
        raise ValueError("EMOS 更新时间缺少时区")
    return timestamp


def fetch_items(url, token):
    """只有页数、条数、ID 和时间戳全部通过校验时才返回结果。"""
    filters = {
        "sort_by": "updated_at",
        "sort_order": "desc",
        "status": ["default"],
        "with_user": False,
    }
    items = []
    seen = set()
    page = 1
    expected = None
    with requests.Session() as session:
        while True:
            response = session.post(
                url,
                params={"page": page},
                json=filters,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json",
                },
                timeout=(5, 15),
            )
            if response.status_code != 200:
                raise ValueError(f"EMOS 查询失败（HTTP {response.status_code}）")
            data = response.json()
            rows, total, size = data["items"], data["total"], data["page_size"]
            if (
                data["page"] != page
                or type(total) is not int
                or total < 0
                or type(size) is not int
                or size < 1
                or not isinstance(rows, list)
                or len(rows) != min(size, max(0, total - (page - 1) * size))
                or (expected is not None and expected != (total, size))
            ):
                raise ValueError("EMOS 返回的分页不完整或轮询期间发生变化")
            expected = (total, size)
            for item in rows:
                identity = item["id"]
                if type(identity) is not int or identity <= 0 or identity in seen:
                    raise ValueError("EMOS 返回了无效或重复的求片 ID")
                _timestamp(item)
                seen.add(identity)
                items.append(item)
            if len(items) == total:
                return items
            page += 1


def _title(item):
    title = item.get("video_title") or item.get("video_title_display") or "未知片名"
    if not item.get("video_title") and item.get("video_type") == "tv":
        title = re.sub(r"\s+#\s+S\d+(?:E\d+)?\s*$", "", title)
    return " ".join(title.split())


def _episode_labels(rows):
    seasons = defaultdict(set)
    other = {}
    for row in rows:
        season, episode = row.get("video_season_number"), row.get(
            "video_episode_number"
        )
        if (
            type(season) is int
            and type(episode) is int
            and season >= 0
            and episode >= 0
        ):
            seasons[season].add(episode)
        elif row.get("video_type") == "tv":
            label = (
                f"S{season:02d}"
                if type(season) is int and season >= 0
                else (row.get("video_title_display") or "")
            )
            if label and label != _title(row):
                other[" ".join(label.split())] = None
    labels = []
    for season, episodes in sorted(seasons.items()):
        end = None
        for episode in sorted(episodes):
            if end is None or episode != end + 1:
                if end is not None:
                    labels.append(_episode_range(season, start, end))
                start = episode
            end = episode
        labels.append(_episode_range(season, start, end))
    return "、".join([*labels, *other])


def _episode_range(season, start, end):
    first = f"S{season:02d}E{start:02d}"
    return first if start == end else f"{first}–S{season:02d}E{end:02d}"


def render_pages(items, changes):
    works = defaultdict(list)
    # 倒序插入各作品，第一次出现的即该作品更新时间最新的记录。
    for item in sorted(items, key=_timestamp, reverse=True):
        key = next(
            (
                (field, item[field])
                for field in ("video_list_id", "todb_id", "tmdb_id")
                if item.get(field)
            ),
            ("seek", item["id"]),
        )
        works[(item.get("video_type"), key)].append(item)
    blocks = []
    for rows in works.values():
        lines = [_title(rows[0])]
        groups = defaultdict(list)
        for row in rows:
            minute = (
                _timestamp(row).astimezone(BEIJING).replace(second=0, microsecond=0)
            )
            groups[(minute, changes.get(row["id"], "existing"))].append(row)
        for (minute, change), entries in groups.items():
            episodes = _episode_labels(entries)
            date = f"{minute.month} 月 {minute.day} 日 {minute:%H:%M}"
            lines.append(
                f"  {MARKERS[change]}{episodes + ' · ' if episodes else ''}{date}"
            )
        blocks.append(lines)

    pages = [HEADING]
    for lines in blocks:
        block = "\n".join(lines)
        if len((HEADING + "\n\n" + block).encode("utf-16-le")) <= 7800:
            if len((pages[-1] + "\n\n" + block).encode("utf-16-le")) > 7800:
                pages.append(HEADING + "（续）")
            pages[-1] += "\n\n" + block
            continue
        title = lines[0][:200]
        # 这个作品单独占页仍放不下时，按行续发并重复剧名。
        prefix = HEADING if pages == [HEADING] else HEADING + "（续）"
        if pages == [HEADING]:
            pages.pop()
        pages.append(f"{prefix}\n\n{title}")
        contents = lines[1:] if title == lines[0] else [lines[0][200:], *lines[1:]]
        for line in contents:
            for segment in wrap(
                line, 1700, break_on_hyphens=False, drop_whitespace=False
            ):
                if len((pages[-1] + "\n" + segment).encode("utf-16-le")) > 7800:
                    pages.append(f"{HEADING}（续）\n\n{title}")
                pages[-1] += "\n" + segment
    return pages


class SeekMonitor:
    def __init__(self, dispatcher):
        self.dispatcher = dispatcher
        self.lock = dispatcher.lock
        self.wake = threading.Event()
        self.thread = None
        self.settings = {}
        self.url = ""
        self.token = ""
        self.source = ""
        self.last_error = ""

    def configure(self, settings, emos):
        url = str(emos.get("base_url", "https://emos.best")).rstrip("/") + "/api/seek"
        token = (
            str(emos.get("auth_token") or "").split("#", 1)[0].split(";", 1)[0].strip()
        )
        with self.lock:
            if (settings, url, token) != (self.settings, self.url, self.token):
                self.settings, self.url, self.token = dict(settings), url, token
                self.source = digest(url + "\0" + token)
                self.last_error = ""
                self.wake.set()
            if (
                self.dispatcher.auto_start
                and enabled(settings, "seek")
                and (self.thread is None or not self.thread.is_alive())
            ):
                self.thread = threading.Thread(
                    target=self._run, daemon=True, name="emos-seek"
                )
                self.thread.start()

    def _run(self):
        while not self.dispatcher.stop_event.is_set():
            with self.lock:
                self.wake.clear()
                settings = self.settings
            if enabled(settings, "seek"):
                self.poll()
                self.wake.wait(settings.get("poll_interval", 120))
            else:
                self.wake.wait(1)

    def poll(self):
        with self.lock:
            settings, source, url, token = (
                self.settings,
                self.source,
                self.url,
                self.token,
            )
        if not enabled(settings, "seek"):
            return
        try:
            if not token:
                raise ValueError("缺少 EMOS 认证令牌")
            items = fetch_items(url, token)
            with self.lock, get_session_local()() as db:
                if settings is not self.settings:
                    return
                snapshot = db.get(SeekSnapshot, 1)
                if snapshot and snapshot.source == source:
                    previous = {item["id"]: _timestamp(item) for item in snapshot.items}
                    changes = {
                        item["id"]: "updated" if item["id"] in previous else "new"
                        for item in items
                        if _timestamp(item) != previous.get(item["id"])
                    }
                    if changes:
                        round_id = uuid4().hex
                        for index, page in enumerate(render_pages(items, changes)):
                            if not self.dispatcher.stage(
                                db, settings, f"seek:{round_id}:{index:06d}", page
                            ):
                                raise RuntimeError("求片通知入队失败")
                if snapshot is None:
                    snapshot = SeekSnapshot(id=1)
                    db.add(snapshot)
                snapshot.source, snapshot.items = source, items
                snapshot.checked_at = datetime.now(timezone.utc).replace(tzinfo=None)
                db.commit()
                self.last_error = ""
            self.dispatcher.wake.set()
        except Exception as exc:
            with self.lock:
                if settings is self.settings:
                    if isinstance(exc, requests.RequestException):
                        self.last_error = "EMOS 请求失败，请检查连接和认证"
                    elif isinstance(exc, ValueError) and str(exc).startswith(
                        ("EMOS ", "缺少 EMOS")
                    ):
                        self.last_error = str(exc)
                    else:
                        self.last_error = "求片查询或保存失败"
                    logger.warning("%s", self.last_error)

    def status(self):
        with self.lock, get_session_local()() as db:
            snapshot = db.get(SeekSnapshot, 1)
            checked_at = None
            if snapshot and snapshot.source == self.source:
                checked_at = snapshot.checked_at.replace(
                    tzinfo=timezone.utc
                ).isoformat()
            active = enabled(self.settings, "seek")
            delivery_error = ""
            if active:
                bot, chat, thread = self.dispatcher.delivery_target(self.settings)
                failed = (
                    db.query(TelegramDelivery)
                    .filter(
                        TelegramDelivery.scope.startswith("seek:"),
                        TelegramDelivery.bot_key == bot,
                        TelegramDelivery.chat_id == chat,
                        TelegramDelivery.thread_id == thread,
                        TelegramDelivery.pending.is_(True),
                        TelegramDelivery.failure_count > 0,
                    )
                    .order_by(TelegramDelivery.updated_at.desc())
                    .first()
                )
                if failed:
                    delivery_error = (
                        "Telegram 拒绝投递，请检查 Bot 和接收权限"
                        if failed.blocked
                        else "Telegram 投递失败，等待重试"
                    )
            return {
                "enabled": active,
                "last_success_at": checked_at,
                "query_error": self.last_error,
                "delivery_error": delivery_error,
            }


def send_test(bot_token, chat_id, thread_id):
    """使用表单当前值验证求片 Bot，测试结果直接返回给网页。"""
    install_log_redaction()
    payload = {"chat_id": chat_id, "text": HEADING + "\n这是一条测试消息。"}
    if thread_id:
        payload["message_thread_id"] = int(thread_id)
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json=payload,
            timeout=(5, 8),
        )
        if response.ok and response.json().get("ok") is True:
            return ""
        return f"Telegram 测试消息发送失败（HTTP {response.status_code}）"
    except (requests.RequestException, ValueError):
        return "Telegram 测试请求失败，请检查连接和 Bot 配置"

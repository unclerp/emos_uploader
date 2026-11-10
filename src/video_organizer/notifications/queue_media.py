"""将本轮成功上传的影片按 TMDB 条目汇总。"""

import re
from collections import defaultdict
from textwrap import wrap

from sqlalchemy import func

from ..database.models import (
    TaskHistory,
    TelegramQueueItem,
    UploadAttempt,
    UploadMediaResult,
)

_EPISODE = re.compile(r"S(\d+)E(\d+)(?:-(\d+))?")


def _episode_ranges(results):
    seasons = defaultdict(list)
    for result in results:
        episode = _EPISODE.fullmatch(result.season_episode)
        if episode:
            season, start, end = episode.groups()
            start = int(start)
            seasons[int(season)].append((start, max(start, int(end or start))))

    ranges = []
    for season, episodes in sorted(seasons.items()):
        merged = []
        for start, end in sorted(episodes):
            if merged and start <= merged[-1][1] + 1:
                merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
            else:
                merged.append((start, end))
        for start, end in merged:
            first = f"S{season:02d}E{start:02d}"
            ranges.append(first if start == end else f"{first}-S{season:02d}E{end:02d}")
    return ", ".join(ranges)


def completed_media_lines(db, run):
    # 统计本轮时间窗口内的成功尝试。下一轮重试同一任务时，旧统计依旧稳定。
    rows = (
        db.query(UploadMediaResult)
        .join(TaskHistory, TaskHistory.id == UploadMediaResult.history_id)
        .join(UploadAttempt, UploadAttempt.history_id == TaskHistory.id)
        .join(TelegramQueueItem, TelegramQueueItem.task_id == UploadAttempt.task_id)
        .filter(
            TelegramQueueItem.run_id == run.id,
            TaskHistory.status == "completed",
            func.coalesce(TaskHistory.end_time, TaskHistory.start_time).between(
                run.started_at, run.finished_at
            ),
        )
        .order_by(TaskHistory.id)
    )
    groups = defaultdict(list)
    for row in rows:
        groups[row.media_type, row.tmdb_id].append(row)

    lines = []
    for (media_type, _), results in groups.items():
        title = " ".join(results[0].title.split())
        if not title:
            continue
        episodes = _episode_ranges(results) if media_type == "tv" else ""
        lines.append(f"{title}: {episodes}" if episodes else title)
    return lines


def summary_pages(summary, media_lines):
    """超长片单分页，给 Telegram 的 UTF-16 长度限制留出余量。"""
    if not media_lines:
        return [summary]
    pages = []
    page = f"{summary}\n\n🎬 本轮已上传片单"
    for line in media_lines:
        segments = (
            [line]
            if len(line.encode("utf-16-le")) <= 7200
            else wrap(line, width=1700, subsequent_indent="  ", break_on_hyphens=False)
        )
        for segment in segments:
            if len(f"{page}\n{segment}".encode("utf-16-le")) > 7800:
                pages.append(page)
                page = "🎬 本轮已上传片单（续）"
            page += f"\n{segment}"
    pages.append(page)
    return pages

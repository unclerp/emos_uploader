"""将本轮成功上传的影片按 TMDB 条目汇总。"""

import re
from collections import Counter, defaultdict
from textwrap import wrap

from sqlalchemy import func

from ..database.models import (
    TaskHistory,
    TelegramQueueItem,
    UploadAttempt,
    UploadMediaResult,
    UploadTask,
)

_EPISODE = re.compile(r"S(\d+)E(\d+)(?:-(\d+))?")


def _episode_label(season, first, last):
    label = f"S{season:02d}E{first:02d}"
    return label if first == last else f"{label}-S{season:02d}E{last:02d}"


def _episode_ranges(results):
    seasons = defaultdict(Counter)
    for result in results:
        episode = _EPISODE.fullmatch(result.season_episode)
        if episode:
            season, start, end = episode.groups()
            start = int(start)
            end = max(start, int(end or start))
            changes = seasons[int(season)]
            changes[start] += 1
            changes[end + 1] -= 1

    ranges = []
    multi_versions = []
    for season, changes in sorted(seasons.items()):
        count = range_start = count_start = 0
        # 只在版本数改变时切段，合集中跨多集的同一个文件每集计一次。
        for episode, delta in sorted(changes.items()):
            if not delta:
                continue
            if count > 1:
                label = _episode_label(season, count_start, episode - 1)
                multi_versions.append(f"{label} ×{count}")
            if not count:
                range_start = episode
            count += delta
            if not count:
                ranges.append(_episode_label(season, range_start, episode - 1))
            count_start = episode
    return ", ".join(ranges), "，".join(multi_versions)


def completed_media_lines(db, run):
    # 统计本轮时间窗口内的成功尝试。下一轮重试同一任务时，旧统计依旧稳定。
    rows = (
        db.query(UploadMediaResult, UploadTask.file_path, UploadTask.fingerprint)
        .join(TaskHistory, TaskHistory.id == UploadMediaResult.history_id)
        .join(UploadAttempt, UploadAttempt.history_id == TaskHistory.id)
        .join(UploadTask, UploadTask.id == UploadAttempt.task_id)
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
    groups = defaultdict(dict)
    for row, file_path, fingerprint in rows:
        # 同一文件版本可能有多次成功记录或对应多个上传目标。
        groups[row.media_type, row.tmdb_id][file_path, fingerprint] = row

    lines = []
    for (media_type, _), files in groups.items():
        title = " ".join(next(iter(files.values())).title.split())
        if not title:
            continue
        if media_type == "tv":
            episodes, multi_versions = _episode_ranges(files.values())
            line = f"{title}: {episodes}" if episodes else title
            if multi_versions:
                line += f"\n  多版本：{multi_versions}"
        else:
            line = f"{title} ×{len(files)}" if len(files) > 1 else title
        lines.append(line)
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
            else [
                segment
                for entry in line.splitlines()
                for segment in wrap(
                    entry, width=1700, subsequent_indent="  ", break_on_hyphens=False
                )
            ]
        )
        for segment in segments:
            if len(f"{page}\n{segment}".encode("utf-16-le")) > 7800:
                pages.append(page)
                page = "🎬 本轮已上传片单（续）"
            page += f"\n{segment}"
    pages.append(page)
    return pages

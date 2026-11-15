"""兼容旧版 Telegram 配置，并解析独立的网盘通知目的地。"""


def cloud_config(config):
    if "telegram_cloud" in config:
        return dict(config["telegram_cloud"])
    return legacy_cloud_config(
        config.get("telegram", {}), config.get("recycle_clean", {})
    )


def legacy_cloud_config(upload, recycle=None):
    channel = upload.get("channel_chat_id", "")
    return {
        "enabled": upload.get("enabled", True),
        "bot_token": upload.get("bot_token", ""),
        "chat_id": upload.get("chat_id", ""),
        "thread_id": upload.get("thread_id", ""),
        "channel_chat_id": channel,
        "notify_links": upload.get("notify_success", True) if channel else False,
        "notify_recycle": (
            (recycle or {}).get("notify_telegram", True)
            and upload.get("notify_post_upload", True)
        ),
    }


def link_config(cloud):
    channel = str(cloud.get("channel_chat_id") or "").strip()
    if channel:
        if not channel.startswith(("@", "-")) and not channel.isdigit():
            channel = "@" + channel
        return dict(cloud, chat_id=channel, thread_id="")
    return cloud

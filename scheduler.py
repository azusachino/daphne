import os
from typing import Optional
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from exchange import fetch_and_save_rates, format_rates_message
from image_fetch import fetch_popular_image


async def update_and_report_rates(bot, db_path: str, notification_channel: str) -> None:
    """
    Fetch and save the rates, format the message, and send the message
    to the specified notification_channel using the telegram bot client.
    """
    rates = await fetch_and_save_rates(db_path)
    message = format_rates_message(rates)
    await bot.send_message(chat_id=notification_channel, text=message)


def setup_scheduler(
    bot,
    db_path: str,
    notification_channel: str,
    scheduler: Optional[AsyncIOScheduler] = None,
) -> AsyncIOScheduler:
    """
    Set up the scheduled job on the provided AsyncIOScheduler,
    or create and return a new one.
    """
    if scheduler is None:
        scheduler = AsyncIOScheduler()
    scheduler.add_job(
        update_and_report_rates,
        trigger="cron",
        minute=59,
        second=39,
        args=[bot, db_path, notification_channel],
    )

    yandere_channel = os.environ.get("YANDERE_CHANNEL", "@yandere_daily_popular")

    # Yandere job at hour=23, minute=39, second=39
    scheduler.add_job(
        fetch_popular_image,
        trigger="cron",
        hour=23,
        minute=39,
        second=39,
        args=[bot, "yandere", yandere_channel],
    )

    # Danbooru job at hour=13, minute=29, second=39
    scheduler.add_job(
        fetch_popular_image,
        trigger="cron",
        hour=13,
        minute=29,
        second=39,
        args=[bot, "danbooru", yandere_channel],
    )

    return scheduler

import os
import logging
import datetime
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from rbac import RbacService, get_rbac_config_path
from database import (
    get_db_path,
    get_latest_exchange_rate,
    get_exchange_rate_history,
    save_exchange_rate,
)
from exchange import fetch_rate

logger = logging.getLogger(__name__)

# Initialize RbacService
rbac_path = get_rbac_config_path()
rbac_service = RbacService.load(rbac_path)


async def check_access_and_reply(update: Update, command: str) -> bool:
    user_id = update.effective_user.id if update.effective_user else 0
    chat_id = update.effective_chat.id if update.effective_chat else 0

    access = rbac_service.check_access(user_id, chat_id, command)
    if not access.is_allowed():
        if access.is_rate_limited():
            await update.message.reply_text(
                "⚠️ Rate limit exceeded for public commands. Please wait."
            )
        else:
            await update.message.reply_text(f"❌ Permission denied: {access.reason}")
        return False
    return True


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await check_access_and_reply(update, "help"):
        return

    help_text = (
        "daphne - Wise Exchange Rate Bot\n\n"
        "Commands:\n"
        "/rate - Show the latest JPY/CNY exchange rate\n"
        "/rate <source> <target> - Check exchange rate for any pair on-demand\n"
        "/rate history [N] - Show history of JPY/CNY rates (last N entries, default 7)\n"
        "/help - Show this help message"
    )
    await update.message.reply_text(help_text)


async def rate_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await check_access_and_reply(update, "rate"):
        return

    text = update.message.text or ""
    parts = text.split()
    args = parts[1:]

    db_path = get_db_path()

    if len(args) == 0:
        # Case 1: No args -> Get latest rate from SQLite db (source="JPY", target="CNY")
        try:
            rate_entry = await get_latest_exchange_rate(db_path, "JPY", "CNY")
            if rate_entry is None:
                # If no rate in DB, fetch live rate from Wise, save, then reply
                rate = await fetch_rate("JPY", "CNY")
                fetched_at = datetime.datetime.now(datetime.timezone.utc)
                await save_exchange_rate(db_path, "JPY", "CNY", rate, fetched_at)
                rate_entry = {"rate": rate, "fetched_at": fetched_at}

            rate = rate_entry["rate"]
            fetched_at = rate_entry["fetched_at"]

            if isinstance(fetched_at, str):
                try:
                    fetched_at = datetime.datetime.fromisoformat(
                        fetched_at.replace(" ", "T")
                    )
                except ValueError:
                    pass

            # Convert timezone-aware fetched_at to local time for format stability if needed,
            # but preserve datetime type for format string helper
            if (
                isinstance(fetched_at, datetime.datetime)
                and fetched_at.tzinfo is not None
            ):
                fetched_at = fetched_at.astimezone()

            reply_text = f"JPY/CNY: {10000.0 * rate:.3f} (×10000)\nFetched: {fetched_at:%Y-%m-%d %H:%M}"
            await update.message.reply_text(reply_text)
        except Exception:
            logger.exception("Error in JPY/CNY rate command")
            await update.message.reply_text(
                "Could not fetch or retrieve exchange rate."
            )

    elif args[0].lower() == "history":
        # Case 2: History
        count = 7
        if len(args) >= 2:
            try:
                count = int(args[1])
                if count < 1:
                    count = 7
                elif count > 30:
                    count = 30
            except ValueError:
                count = 7

        try:
            # Fetch overall history and filter JPY/CNY entries in python
            history_all = await get_exchange_rate_history(db_path, count * 5 + 10)
            jpy_cny_history = [
                r
                for r in history_all
                if r["source_currency"].upper() == "JPY"
                and r["target_currency"].upper() == "CNY"
            ][:count]

            if not jpy_cny_history:
                await update.message.reply_text("No rate history available.")
            else:
                lines = []
                for entry in jpy_cny_history:
                    rate = entry["rate"]
                    fetched_at = entry["fetched_at"]
                    if isinstance(fetched_at, str):
                        try:
                            fetched_at = datetime.datetime.fromisoformat(
                                fetched_at.replace(" ", "T")
                            )
                        except ValueError:
                            pass
                    if (
                        isinstance(fetched_at, datetime.datetime)
                        and fetched_at.tzinfo is not None
                    ):
                        fetched_at = fetched_at.astimezone()
                    lines.append(f"{fetched_at:%m-%d %H:%M}: {10000.0 * rate:.3f}")

                reply_text = "JPY/CNY history (×10000):\n" + "\n".join(lines)
                await update.message.reply_text(reply_text)
        except Exception:
            logger.exception("Error fetching rate history")
            await update.message.reply_text("Error retrieving rate history.")

    elif len(args) == 2:
        # Case 3: On-demand `/rate <source> <target>`
        source = args[0]
        target = args[1]
        try:
            rate = await fetch_rate(source, target)
            await update.message.reply_text(
                f"{source.upper()}/{target.upper()}: {rate:.6f}"
            )
        except Exception:
            logger.exception(f"Error fetching live rate for {source}/{target}")
            await update.message.reply_text(
                f"Could not fetch rate for {source.upper()}/{target.upper()}"
            )

    else:
        # Invalid arguments
        await update.message.reply_text(
            "Invalid arguments.\n"
            "Usage:\n"
            "/rate - Show JPY/CNY rate\n"
            "/rate <source> <target> - Show rate for currency pair\n"
            "/rate history [N] - Show JPY/CNY history (default 7, max 30)"
        )


def build_application() -> Application:
    token = os.environ.get("HARUS_BOT_TOKEN")
    if not token:
        raise ValueError("HARUS_BOT_TOKEN environment variable not set")

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("rate", rate_command))
    app.add_handler(CommandHandler("help", help_command))
    return app

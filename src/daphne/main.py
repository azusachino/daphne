import asyncio
import os
import sys
import logging
import argparse

# Setup basic logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger("daphne.main")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

ENV_BOT_TOKEN = "DAPHNE_BOT_TOKEN"


def load_env_file(filepath: str) -> None:
    """
    Manually load environment variables from a file if it exists.
    """
    if not os.path.exists(filepath):
        return
    logger.info(f"Loading environment variables from {filepath}")
    with open(filepath, "r") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, val = line.split("=", 1)
                key = key.strip()
                val = val.strip()
                # Remove surrounding quotes if present
                if (val.startswith('"') and val.endswith('"')) or (
                    val.startswith("'") and val.endswith("'")
                ):
                    val = val[1:-1]
                os.environ[key] = val


# Manually load environment variables from standard locations
load_env_file(".env")
load_env_file(os.path.expanduser("~/.config/daphne/daphne.env"))

# Import remaining modules after loading environment variables
from daphne.bot import build_dispatcher, register_bot_commands  # noqa: E402
from daphne.tg import build_bot  # noqa: E402


async def run_bot() -> None:
    token = os.environ[ENV_BOT_TOKEN]
    bot = build_bot(token)
    dispatcher = build_dispatcher()
    await register_bot_commands(bot)
    try:
        await dispatcher.start_polling(bot.raw)
    finally:
        await bot.session.close()


def run_init(local: bool = False) -> None:
    """
    Craft default files and configuration for Daphne.
    """
    if local:
        config_dir = "./config"
        env_path = "./.env"
    else:
        home = os.path.expanduser("~")
        config_dir = os.path.join(home, ".config", "daphne")
        env_path = os.path.join(config_dir, "daphne.env")

    # 1. Create config directory
    os.makedirs(config_dir, exist_ok=True)
    print(f"Created config directory at: {config_dir}")

    # 2. Write default config.toml
    config_path = os.path.join(config_dir, "config.toml")
    config_content = """# Daphne configuration
[app]
# telegram_api_url = "http://localhost:8081"
video_upload_limit_mb = 256
# Heavy-download concurrency guard (yt-dlp / gallery-dl):
# max_concurrent_downloads = 3        # across the whole bot
# max_user_concurrent_downloads = 1   # per user

[rbac]
public_commands = ["help"]

[rbac.roles.admin]
permissions = ["*"]

# Example non-admin roles, graduated from minimal to full. Available
# permissions include: convert_link, fetch_metadata, preview_video,
# download_video, extract_audio, download_gallery, inline_convert. NOTE:
# inline_convert is user-level only — inline queries carry no chat_id, so it
# must be granted via [rbac.users], not [rbac.chats].
# [rbac.roles.default]
# permissions = ["convert_link"]
# [rbac.roles.power_user]
# permissions = ["convert_link", "preview_video", "fetch_metadata", "extract_audio", "download_video"]

[rbac.users]
# Add user IDs mapping to roles here. Example:
# 111111111 = "admin"  # Replace with your Telegram user ID

[rbac.chats]
# Add chat/group IDs mapping to roles here. Example:
# -1001111111111 = "power_user"  # Replace with your chat ID
"""
    if os.path.exists(config_path):
        print(f"Skipping config.toml (already exists at {config_path})")
    else:
        with open(config_path, "w") as f:
            f.write(config_content)
        print(f"Wrote default config.toml to: {config_path}")

    # 3. Write template environment file
    env_content = """# Environment variables for Daphne Bot
DAPHNE_BOT_TOKEN=your_telegram_bot_token_here
TZ=Asia/Tokyo
"""
    if os.path.exists(env_path):
        print(f"Skipping environment file (already exists at {env_path})")
    else:
        with open(env_path, "w") as f:
            f.write(env_content)
        print(f"Wrote template environment file to: {env_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Daphne - Telegram Media Converter")
    subparsers = parser.add_subparsers(dest="command")

    # Subcommand 'init'
    parser_init = subparsers.add_parser(
        "init", help="Craft default config and env files"
    )
    parser_init.add_argument(
        "--local",
        action="store_true",
        help="Craft files in the current working directory (./config/config.toml and .env) instead of ~/.config/daphne/",
    )

    args = parser.parse_args()

    if args.command == "init":
        run_init(local=args.local)
    else:
        # Check token existence
        token = os.environ.get(ENV_BOT_TOKEN)
        if not token:
            print(
                f"Error: {ENV_BOT_TOKEN} environment variable not set.",
                file=sys.stderr,
            )
            print(
                "Please set it in your environment or run 'daphne init' to set up a template env file.",
                file=sys.stderr,
            )
            sys.exit(1)

        try:
            logger.info("Starting Daphne bot polling...")
            asyncio.run(run_bot())
        except (KeyboardInterrupt, SystemExit):
            logger.info("Daphne bot received exit signal. Shutting down gracefully...")
        except Exception as e:
            logger.critical(
                "Daphne bot crashed with unhandled exception: %s", e, exc_info=True
            )
            sys.exit(1)


if __name__ == "__main__":
    main()

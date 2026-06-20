import os
import sys
import logging
import argparse

# Setup basic logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger("daphne.main")

DEFAULT_IMAGE_CHANNEL = "@yandere_daily_popular"
DEFAULT_NOTIFICATION_CHANNEL = "@harus_notification"
ENV_BOT_TOKEN = "DAPHNE_BOT_TOKEN"
ENV_NOTIFICATION_CHANNEL = "DAPHNE_NOTIFICATION_CHANNEL"


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
from daphne.database import init_db, get_db_path  # noqa: E402
from daphne.bot import build_application  # noqa: E402
from daphne.scheduler import setup_scheduler  # noqa: E402


async def post_init(app) -> None:
    """
    Perform database initialization and scheduler setup within the running event loop.
    """
    db_path = get_db_path()
    # Initialize DB
    await init_db(db_path)
    logger.info(f"Database initialized at: {db_path}")

    # Setup scheduler
    notification_channel = os.environ.get(
        ENV_NOTIFICATION_CHANNEL, DEFAULT_NOTIFICATION_CHANNEL
    )
    setup_scheduler(app.bot, db_path, notification_channel)
    logger.info("Scheduler setup completed.")


def run_init() -> None:
    """
    Craft default files and configuration for Daphne.
    """
    home = os.path.expanduser("~")
    config_dir = os.path.join(home, ".config", "daphne")

    # 1. Create config directory
    os.makedirs(config_dir, exist_ok=True)
    print(f"Created config directory at: {config_dir}")

    # 2. Write default rbac.toml
    rbac_path = os.path.join(config_dir, "rbac.toml")
    rbac_content = """# Daphne RBAC Configuration
public_commands = ["help", "rate"]

[roles.admin]
permissions = ["*"]

[users]
# Add user IDs mapping to roles here. Example:
# 123456789 = "admin"

[chats]
# Add chat/group IDs mapping to roles here. Example:
# -1002058191932 = "standard_group"
"""
    with open(rbac_path, "w") as f:
        f.write(rbac_content)
    print(f"Wrote default rbac.toml to: {rbac_path}")

    # 3. Write template environment file
    env_path = os.path.join(config_dir, "daphne.env")
    env_content = f"""# Environment variables for Daphne Bot
DAPHNE_BOT_TOKEN=your_telegram_bot_token_here
DAPHNE_DATABASE_URL=sqlite:///{os.path.join(home, ".local", "share", "daphne", "daphne.db")}
DAPHNE_RBAC_CONFIG_PATH={rbac_path}
DAPHNE_NOTIFICATION_CHANNEL={DEFAULT_NOTIFICATION_CHANNEL}
DAPHNE_IMAGE_CHANNEL={DEFAULT_IMAGE_CHANNEL}
"""
    with open(env_path, "w") as f:
        f.write(env_content)
    print(f"Wrote template environment file to: {env_path}")

    # 4. Write systemd user service file
    systemd_dir = os.path.join(home, ".config", "systemd", "user")
    os.makedirs(systemd_dir, exist_ok=True)

    import shutil

    daphne_bin = shutil.which("daphne")
    if not daphne_bin:
        daphne_bin = os.path.join(home, ".local", "bin", "daphne")

    service_path = os.path.join(systemd_dir, "daphne.service")
    service_content = f"""[Unit]
Description=Daphne - Wise Exchange Rate Bot
After=network.target

[Service]
Type=simple
ExecStart={daphne_bin}
EnvironmentFile={env_path}
Restart=always

[Install]
WantedBy=default.target
"""
    with open(service_path, "w") as f:
        f.write(service_content)
    print(f"Wrote systemd user service file to: {service_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Daphne - Wise Exchange Rate Bot")
    subparsers = parser.add_subparsers(dest="command")

    # Subcommand 'init'
    subparsers.add_parser(
        "init", help="Craft default config files, env, and systemd service"
    )

    args = parser.parse_args()

    if args.command == "init":
        run_init()
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

        # Build application and pass post_init
        app = build_application()

        # Configure post_init to initialize database and scheduler
        # We hook into post_init because it runs on the application's event loop
        app.post_init = post_init

        logger.info("Starting Daphne bot polling...")
        app.run_polling()


if __name__ == "__main__":
    main()

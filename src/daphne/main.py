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
from daphne.bot import build_application  # noqa: E402


async def post_init(app) -> None:
    """Reserved for future startup hooks."""
    return None


def run_init() -> None:
    """
    Craft default files and configuration for Daphne.
    """
    home = os.path.expanduser("~")
    config_dir = os.path.join(home, ".config", "daphne")

    # 1. Create config directory
    os.makedirs(config_dir, exist_ok=True)
    print(f"Created config directory at: {config_dir}")

    # 2. Write default config.toml
    config_path = os.path.join(config_dir, "config.toml")
    config_content = """# Daphne configuration
[app]
# telegram_api_url = "http://localhost:8081"
video_upload_limit_mb = 512

[rbac]
public_commands = ["help"]

[rbac.roles.admin]
permissions = ["*"]

[rbac.users]
# Add user IDs mapping to roles here. Example:
# 123456789 = "admin"

[rbac.chats]
# Add chat/group IDs mapping to roles here. Example:
# -1002058191932 = "standard_group"
"""
    with open(config_path, "w") as f:
        f.write(config_content)
    print(f"Wrote default config.toml to: {config_path}")

    # 3. Write template environment file
    env_path = os.path.join(config_dir, "daphne.env")
    env_content = """# Environment variables for Daphne Bot
DAPHNE_BOT_TOKEN=your_telegram_bot_token_here
TZ=Asia/Tokyo
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
Description=Daphne - Telegram Media Converter
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
    parser = argparse.ArgumentParser(description="Daphne - Telegram Media Converter")
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

        app = build_application()
        app.post_init = post_init

        logger.info("Starting Daphne bot polling...")
        app.run_polling()


if __name__ == "__main__":
    main()

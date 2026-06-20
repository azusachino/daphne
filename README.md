# daphne

Daphne（ダフニー）沈丁花（月桂）

## Description

Daphne is a Telegram bot for converting raw media links into Telegram-friendly media messages.

## Configuration

Secrets stay in environment variables:

- `DAPHNE_BOT_TOKEN`
- `TELEGRAM_API_ID`
- `TELEGRAM_API_HASH`

Runtime settings live in `config.toml`:

```toml
[app]
# telegram_api_url = "http://localhost:8081"
video_upload_limit_mb = 512

[rbac]
public_commands = ["help"]
```

`video_upload_limit_mb` controls the maximum video Daphne will upload to Telegram. If a detected video exceeds the limit, Daphne replies with a cleaned and decorated HTML card instead of uploading the file.

## Project-local run

Use this path for a separate local Telegram bot without touching the currently running bot:

```bash
cp .daphne.local.env.example .daphne.local.env
cp .daphne.config.local.toml.example .daphne.config.local.toml
```

Edit `.daphne.local.env` with a separate BotFather token and Telegram API credentials. Edit `.daphne.config.local.toml` with your Telegram user ID, the test chat/group ID, local Bot API URL, and upload limit.

Then start the local container:

```bash
podman compose -f docker-compose.local.yml up --build
```

Local secrets and runtime state stay untracked:

- `.daphne.local.env`
- `.daphne.config.local.toml`

Daphne v0.1 is stateless for local media conversion; the compose file does not mount a database.

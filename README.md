# daphne

Daphne（ダフニー）沈丁花（月桂）

## Description

Daphne is a Telegram bot for personal automation: exchange-rate reporting, media link conversion, video download/upload, and daily image posts.

## Configuration

Runtime environment variables are all prefixed with `DAPHNE_`:

- `DAPHNE_BOT_TOKEN`
- `DAPHNE_DATABASE_URL`
- `DAPHNE_RBAC_CONFIG_PATH`
- `DAPHNE_NOTIFICATION_CHANNEL`
- `DAPHNE_IMAGE_CHANNEL`

Existing channel names/IDs can stay unchanged; only the environment variable keys are renamed.

## Project-local run

Use this path for a separate local Telegram bot without touching the currently running bot:

```bash
cp .daphne.local.env.example .daphne.local.env
cp .daphne.rbac.local.toml.example .daphne.rbac.local.toml
```

Edit `.daphne.local.env` with a separate BotFather token and test chat/channel IDs. Edit `.daphne.rbac.local.toml` with your Telegram user ID and the test chat/group ID.

Then start the local container:

```bash
podman compose -f docker-compose.local.yml up --build
```

Local secrets and runtime state stay untracked:

- `.daphne.local.env`
- `.daphne.rbac.local.toml`
- `.daphne-data/`

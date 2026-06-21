# Daphne (ダフニー - 沈丁花)

Daphne is a fast, stateless Telegram bot designed to convert raw media links (Twitter, Pixiv, Bluesky, TikTok, Instagram, Bilibili, YouTube, etc.) into Telegram-friendly native media messages.

**Current Version:** `0.1.3`

## Core Features

- **Platform Media Extractor & Converter**:
  - **YouTube & Bilibili**: Video downloads utilizing standard `yt-dlp`/`you-get`/`lux` fallback engines, with automatic duration and dimensions probing.
  - **Twitter / X**: Fetches tweets using the `FxTwitter` API, rendering photos, animations (GIFs), and videos natively.
  - **Pixiv**: Resolves Pixiv artwork/galleries and sends them as clean photo/media groups.
  - **Bluesky**: Resolves handles via XRPC identity endpoints, parsing native image carousels and HLS playlist video URLs.
  - **TikTok / Douyin**: Direct high-speed video downloads via the public `TikWM` API, bypassing `yt-dlp` login blocks, with a graceful fallback to `yt-dlp`.
  - **Instagram**: Public image, carousel, and video/reel downloads using `parth-dl` (GraphQL parsing), bypassing login walls and server IP blockages.
- **Audio Extraction**:
  - `/audio <link>` command to extract the audio track from video URLs, automatically encoding it to MP3 with performer and title metadata.
- **Access Control & RBAC**:
  - Multi-tenant Role-Based Access Control (RBAC) configured in `config.toml` matching specific user and chat ID permissions. See [RBAC.md](RBAC.md) for details on authorization flows and fallback mechanics.
- **Interactive UX Cues**:
  - Visually updates the chat actions (e.g. `uploading_video`, `uploading_photo`, `uploading_audio`) to give visual feedback during download/transcoding.
  - HTML captions highlighting original post title, uploader, duration, source link, platform tag, and attribution to the requesting user.
- **Safety First**:
  - Messages are only deleted if the download, conversion, and upload to Telegram succeed, preventing links from being lost on error.

## Configuration

Secrets are loaded from environment variables:
- `DAPHNE_BOT_TOKEN`: The bot token from `@BotFather`.
- `TELEGRAM_API_ID` & `TELEGRAM_API_HASH`: API credentials required by the Telegram Bot API sidecar.

Runtime settings are loaded from `config.toml`:
```toml
[app]
# telegram_api_url = "http://localhost:8081"
video_upload_limit_mb = 512

[rbac]
public_commands = ["help"]
```

`video_upload_limit_mb` controls the maximum video size Daphne will upload to Telegram. If a detected video exceeds the limit, Daphne replies with a decorated HTML info card instead of uploading the file.

## Development & Local Testing

Daphne uses [uv](https://github.com/astral-sh/uv) for dependency management and runs cleanly in containerized stacks (Podman / Docker).

1. Initialize configurations locally:
   ```bash
   make init-local
   ```
   This generates untracked local templates:
   - `.daphne.local.env`
   - `.daphne.config.local.toml`

2. Edit `.daphne.local.env` and `.daphne.config.local.toml` with your test bot credentials and user IDs.

3. Spin up the container stack (bot + local Telegram Bot API server sidecar):
   ```bash
   make up
   ```

4. Tear down the stack:
   ```bash
   make down
   ```

5. Run checks and tests:
   ```bash
   make fmt    # Format code
   make lint   # Run lint check
   make test   # Run unit tests
   make ready  # Format, lint, and run tests together
   ```

## License

This project is licensed under the [MIT License](LICENSE).

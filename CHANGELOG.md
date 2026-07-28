# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.3.3] - 2026-07-28

### Added
- **Author Hashtags**: Twitter/X, YouTube (and the other yt-dlp-backed video platforms sharing its caption builder: Bilibili, TikTok, Bluesky), and Instagram captions now include an extra hashtag for the post's author/uploader (e.g. `#waterloo_intern`), alongside the existing platform tag. Names are Unicode-normalized (NFKC) and folded to a single underscore-joined slug — multi-word and non-Latin names collapse into one tag instead of splitting or being dropped, and a missing/"unknown" author is skipped rather than producing an empty or `#unknown` tag.

## [0.3.2] - 2026-07-28

### Fixed
- `twitter.py`: X Articles (long-form posts) have an empty `text` field (just the raw `t.co` short link) and a `media: null` field — real content lives under a separate `article` object (`title`, `preview_text`, `cover_media`). `tweet.get("media", {})` doesn't fall back to `{}` when the key exists with a `null` value, so the article branch crashed with an `AttributeError` swallowed by the handler's broad `except`, silently degrading to a bare fallback link. Articles now render as a cover-photo message with the title and preview snippet as caption (falling back to a text message when there's no cover image).

## [0.3.1] - 2026-07-23

### Fixed
- `instagram.py`: when `parth-dl` failed to extract a post, the handler fell back to `handle_video_link`'s full yt-dlp/you-get/lux engine chain — but yt-dlp's own Instagram extractor hits the same public endpoints parth-dl already failed against, so this just retried into the same wall at real cost (~15s across 4 doomed engines) before failing with a confusing "There is no video in this post" error on ordinary image posts. Extraction failure now fails fast instead: a clear message + 😢 reaction, no engine chain.
- `instagram.py` / `downloader.py`: added a `fetch_instagram_fallback_media()` recovery step between the two — yt-dlp's Instagram extractor can still read a post anonymously even when it has no video, it just needs `--ignore-no-formats-error` to hand back the metadata (thumbnail/image URLs) it already extracted instead of raising. Recovers the common case (parth-dl fails on an image/carousel post) without needing yt-dlp's video engine chain or any Instagram login/cookies.

## [0.3.0] - 2026-07-17

### Added
- **`/start` Command**: Added a welcome response, shown in the Telegram command menu, that answers even for users/chats not yet whitelisted by RBAC.
- **Live RBAC via Valkey**: RBAC can now optionally sync from a Valkey instance (`[rbac] valkey_url` or `DAPHNE_VALKEY_URL`), refreshing every 30 seconds so role edits don't require a redeploy. Falls back to fully static `config.toml` RBAC when unset.
- **`/grant`, `/revoke`, `/roles` Admin Commands**: Admins can grant or revoke a role for a user (reply to their message) or the current chat (no reply), and list configured roles — all hardcoded to the `admin` role check and left out of the public command menu.
- **Update Logging**: Every incoming Telegram update is now logged at the earliest dispatch point, before RBAC or routing — makes it possible to tell "never received" apart from "received but denied/failed" during troubleshooting.

### Changed
- **Richer Captions**: Long tag lists (e.g. Pixiv artworks with a dozen+ tags) now collapse into a tap-to-expand `<blockquote expandable>` instead of a single long hashtag line.
- **Richer Video Downloads**: yt-dlp passes now embed metadata, thumbnails, and subtitles (`--embed-metadata --embed-thumbnail --embed-subs`) into downloaded videos, and embed metadata/cover art into extracted MP3s.
- **Command Menu Registration**: Daphne now registers its command list with Telegram (`/help`, `/audio`, `/gallery`, `/start`) so they appear in the native "/" picker.
- **Failure Reaction**: Switched the failed-conversion reaction from 👎 to 😢 — a failed conversion is a system apology, not a downvote of the user's content.

## [0.2.0] - 2026-07-10

### Added
- **Inline Mode**: Added inline conversion for Twitter/X, Instagram, and YouTube/Bilibili links, with user-level RBAC authorization.
- **Gallery Downloads**: Added the `/gallery` command using `gallery-dl`, with Telegram media-group chunking.
- **Concurrency Guard**: Added global and per-user limits for heavy downloads, including queued status feedback.
- **Message Reactions**: Added working, completed, and failed reaction feedback for supported link and command flows.

### Changed
- **More Reliable Video Downloads**: Detect truncated downloads using expected duration and retry through the fallback engines.
- **Instagram Media Handling**: Added direct image, carousel, and reel resolution for in-chat and inline delivery.
- **Twitter Fallbacks**: Replaced bare fallback URLs with rich HTML link messages when no native media is available.

## [0.1.3] - 2026-06-21

### Added
- **On-Demand Video Download**: Introduced a manual "Download Video" callback button inside generic video preview cards to optimize server bandwidth and CPU.
- **`fetch_metadata` Permission**: Created a distinct RBAC permission for generic video link detection to check limits prior to fetching metadata.
- **Telegram Hard Size Limits**: Enforced a strict 2GiB upload check before download/upload of generic videos.

### Changed
- **Swapped Video Quotas**: Exchanged default hourly quotas between auto-preview (`preview_video_limit` set to 10/hr) and callback download (`download_video_limit` set to 5/hr).
- **Reduced Default Upload Limit**: Decreased the default automatic preview size limit from 512MB to 256MB.
- **Updated Configurations**: Adjusted default initialization config templates and K3s deployment manifest RBAC permissions.

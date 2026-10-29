# Bilibili download chain

Bilibili links (`bilibili.com`, `b23.tv`) have their own chain. YouTube and every other
site keep the generic path (yt-dlp `pass 1` → `pass 2` → you-get → lux) and never reach
the Bilibili engines.

## Video order

```mermaid
flowchart TD
    A[Bilibili link] --> B[canonical link]
    B --> C{disk cache hit?}
    C -- yes --> Z[serve cached mp4]
    C -- no --> D[1 native player API]
    D --> E[2 yt-dlp]
    E --> F[3 you-get]
    F --> G[4 lux]
    G --> H[5 yutto, last]
    D -- complete --> S[store in cache, serve]
    E -- complete --> S
    F -- complete --> S
    G -- complete --> S
    H -- complete --> S
    H -- nothing complete --> P{any partial file?}
    P -- yes --> L[longest partial, not cached]
    P -- no --> X[curated error]
```

## Engines

| Order | Engine | What it does | Typical failure |
| --- | --- | --- | --- |
| 0 | Disk cache | Serves a finished mp4 by `bvid` and part for 6 hours (2 GiB cap, oldest evicted). Only files that passed the duration check are stored. | None; a miss falls through. |
| 1 | Native player API (`bilibili.py`) | `view` for metadata, `playurl` with `fnval=4048` for DASH. Downloads the video and audio tracks and muxes them with ffmpeg, without re-encoding. Anonymous, so quality is capped at 480p. | HTTP 412 or code -412 risk control. Retried with backoff, once with a fresh visitor session. |
| 2 | yt-dlp (`uvx yt-dlp`) | One pass with the site's headers. | Bilibili extractor broken upstream (`Unable to extract initial state`). |
| 3 | you-get (`uvx you-get`) | Writes its own file into the engine folder. | Varies by extractor. |
| 4 | lux (image binary) | Writes its own file into the engine folder. | Varies by extractor. |
| 5 | yutto (`uvx yutto`), **last** | Bilibili-specific downloader with its own API client. Its exit code is ignored; the output file decides. | Anonymous requests get the same risk control. |

Metadata uses engine 1 first, and yt-dlp `--dump-json` only if the native call fails.

Audio order: native audio track → yt-dlp audio extract → lux `--audio-only` → yutto
`--audio-only` (last). you-get has no audio-only mode, so it is skipped for audio.

## Sequence

```mermaid
sequenceDiagram
    autonumber
    participant Bot as bot.py handler
    participant Chain as downloader.py chain
    participant Cache as disk cache
    participant Engine as engines, in order

    Bot->>Chain: download_video(url, out_dir, expected_duration)
    Chain->>Chain: canonical_url(url), expands b23.tv once
    Chain->>Cache: cached_video(link)
    alt cache hit
        Cache-->>Bot: finished mp4
    else cache miss
        loop native, yt-dlp, you-get, lux, yutto
            Chain->>Engine: run the next engine
            alt file covers the expected duration
                Engine-->>Chain: complete file
                Chain->>Cache: store_video (video only)
                Chain-->>Bot: path
            else error, rate limit, or truncated file
                Engine-->>Chain: reason or short file, kept as partial
            end
        end
        Note over Chain,Bot: no engine complete: longest partial (not cached), or a curated error
    end
```

## Decision rules

- **Complete** means the probed duration covers at least 95% of the expected duration
  from metadata. Without an expected duration, a file counts as complete.
- Only complete files are cached, so a truncated download is never served again.
- If no engine is complete, the longest partial file is returned and marked as partial in
  the logs. It is never cached.
- A 900-second budget stops new engines from starting, so one link cannot hold a download
  slot for the sum of every engine's timeout. Each engine also has its own timeout.
- Engine errors are logged with a reason (host only, never the full link). The chat
  message is curated: a rate-limit message when risk control blocked every attempt, and a
  generic failure otherwise. Raw tool output never reaches the chat.

## Limits

- **Quality:** anonymous requests cap at 480p in native, yutto, and the other engines.
  Higher quality needs a logged-in cookie (`SESSDATA`), which is not wired in yet.
- **Risk control:** Bilibili answers `-412 request was banned` per IP. On 2026-10-29 the
  test host got it from the first `view` call, with and without visitor cookies, and yutto
  failed the same way. The chain can only recover once the ban lifts.
- **Bangumi and paid content:** not handled natively. The native engine refuses them, and
  the chain falls through.
- **Versions:** yt-dlp, you-get, and yutto run at the latest PyPI release, refreshed at
  bot start. lux is the latest GitHub release, resolved at each image build.
- **Cache:** lives in `$TMPDIR/daphne-bilibili`. This is the only state Daphne keeps.

## Where it lives

- `src/daphne/bilibili.py`: native API client, visitor session, backoff, disk cache.
- `src/daphne/downloader.py`: `_download_bilibili_video`, `_download_bilibili_audio`,
  `_run_bilibili_chain`, the per-engine helpers, and the generic yt-dlp → you-get → lux path.
- `Dockerfile`: installs lux from its latest release.
- `tests/test_downloader.py` (`TestBilibiliChain`) and `tests/test_bilibili.py`.

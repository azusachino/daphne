---
name: daphne-build-and-deploy
description: Build the Daphne image and run it with compose (local or production) or on the harus-k3s cluster, then verify and roll back. Use when shipping or running Daphne, or when a deploy step fails.
---

# Build and deploy Daphne

Daphne is a Telegram bot. It needs a bot token, a Telegram Bot API server (the sidecar),
and a `config.toml`. Pick one of three routes:

| Route | Use when | Build | Run |
| --- | --- | --- | --- |
| Local compose | Testing on a laptop or dev box | `make up` | `docker-compose.local.yml` |
| Production compose | A plain Docker or Podman host | `docker compose build` | `docker-compose.yml` |
| harus-k3s | The workstation cluster | `make image-local` on the node | `05-tools/daphne` in `harus-k3s` |

Work from the repository root (`vendor/daphne/`). The container and compose commands
use `CONTAINER_TOOL`, which the Makefile sets to `podman` when it is installed.

## 1. Gate before any build

```bash
make ready        # format check, lint, and the unit tests, all offline
```

Do not build or ship on a red gate. Then note the commit you are building:

```bash
git status --short   # must be empty for a reproducible image
git rev-parse --short HEAD
```

## 2. Build the image

```bash
make image-build     # builds daphne:latest with the Dockerfile
make verify          # runs the image's --help and checks the banner
```

The Dockerfile builds from public registries only. Two things are fetched at build
time: the lux release (latest, checked against its checksums file) and the Python
environment from `uv.lock`. yt-dlp, yutto and you-get are not in the image's lock; they
run through `uvx` and are refreshed from PyPI at each bot start. A bot that starts
without network can still run, with the last cached tool versions.

## 3. Run with compose

### Local (`make up`)

`make up` runs `docker-compose.local.yml` and reads two untracked files:

```bash
cp .daphne.local.env.example .daphne.local.env
cp .daphne.config.local.toml.example .daphne.config.local.toml
# fill in DAPHNE_BOT_TOKEN, TELEGRAM_API_ID, TELEGRAM_API_HASH and the user/chat IDs
make up
make down
```

Both files are gitignored. The image is `daphne:local`.

### Production (`docker-compose.yml`)

This file reads `./.env` and mounts `./config/config.toml`. Create both with the
production init, which writes them in the layout the file expects:

```bash
make init-local      # writes ./.env and ./config/config.toml (both gitignored)
$EDITOR .env config/config.toml
docker compose up -d --build
```

`./config/` is gitignored, so the live config does not get committed.

## 4. Deploy to harus-k3s

Run this on `harus-mini`, the single-node cluster host. Never SSH to the host from
itself. The cluster runs the image straight from its containerd store, so there is no
registry push.

1. **Confirm the pin.** `vendor/harus-k3s/SERVICE_VERSIONS.yaml` has a `daphne` entry
   with `skip: true`, and `05-tools/daphne/deployment.yaml` names the same tag.
2. **Keep a rollback tag.** Re-tag the image that is running now, before importing:
   ```bash
   sudo k3s ctr images tag azusachino.com/daphne:0.5.0 azusachino.com/daphne:0.5.0-prev<N>
   ```
3. **Build and import** with the repo target, on the commit you gated:
   ```bash
   make image-local     # podman build, then `k3s ctr images import` (needs sudo)
   sudo k3s ctr images ls | rg "azusachino.com/daphne:0.5.0 "   # check the new digest
   ```
4. **Check drift** in the cluster repo: `make -C vendor/harus-k3s check-drift`. It must
   report 0 drift.
5. **Restart.** The tag stays the same, and `imagePullPolicy: Never` means the pod keeps
   the old image until it is restarted:
   ```bash
   kubectl rollout restart deployment/daphne -n harus-tools
   kubectl rollout status deployment/daphne -n harus-tools
   ```

Changing the version is different. Bump `SERVICE_VERSIONS.yaml` and the manifest's image
tag together, import the new tag, then apply the manifests:
`make -C vendor/harus-k3s apply-tools`.

### Secrets

The cluster accepts only SealedSecrets. Seal the env file from the cluster repo; never
put a token on a command line:

```bash
make -C vendor/harus-k3s seal NAME=daphne-secret NS=harus-tools ENV=05-tools/daphne/secret.env
```

The secret carries `DAPHNE_BOT_TOKEN`, the Telegram API id and hash, and optionally
`DAPHNE_PIXIV_REFRESH_TOKEN` (see below).

## 5. Verify

```bash
kubectl get pods -n harus-tools -l app=daphne
kubectl logs -n harus-tools deploy/daphne -c daphne --tail=50 | rg -i "polling|error|traceback"
```

A healthy start logs `Starting Daphne bot polling`, registers two commands (`/start` and
`/help`), and reports the three tools ready. Then check the user path in the group:

- A Twitter/X, Pixiv, Bluesky, Instagram or TikTok link converts in the chat.
- A YouTube or Bilibili link gets a card with a Download button.
- A yande.re, konachan.net, lolibooru or Danbooru post link comes back as photos.
- A tag or search page gets no reply.

Compose: `docker compose logs -f daphne` shows the same lines.

## 6. Roll back

```bash
sudo k3s ctr images tag azusachino.com/daphne:0.5.0-prev<N> azusachino.com/daphne:0.5.0
kubectl rollout restart deployment/daphne -n harus-tools
```

Compose: `docker compose down`, then start the previous image tag. Keep the previous
image before every import, as in step 4.2.

## Configuration

Environment (`.env`, `.daphne.local.env`, or the sealed secret):

| Variable | Required | Purpose |
| --- | --- | --- |
| `DAPHNE_BOT_TOKEN` | yes | Bot token from @BotFather |
| `TELEGRAM_API_ID`, `TELEGRAM_API_HASH` | yes | For the local Bot API sidecar |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_LOCAL` | cluster | The sidecar's own settings (see the manifest) |
| `DAPHNE_PIXIV_REFRESH_TOKEN` | no | A Pixiv login. Enables the login route for age-restricted works. Never logged |

`config.toml` (mounted at `/app/config.toml`): `[app]` for the upload limit and the
concurrency guard, `[rbac]` for public commands, quotas and roles. The permission names
are `convert_link`, `fetch_metadata`, `preview_video`, `download_video`, and `help`. See
`RBAC.md`.

## Pitfalls

- **`imagePullPolicy: Never` and the same tag.** A re-import does nothing until the
  deployment is restarted (step 4.5).
- **Two users.** The image runs as UID 10001. The cluster manifest runs the container as
  UID 1000 and sets `HOME=/tmp`. The app must be able to write to `/tmp` (the Bilibili
  cache and uv tool environments live there).
- **Tool refresh is in the background.** Polling starts before the yt-dlp, yutto and
  you-get refresh finishes, so the first downloads may use cached tool versions.
- **`make init-local` is not the local compose layout.** It writes `./.env` and
  `./config/config.toml`. `make up` reads `.daphne.local.env` and
  `.daphne.config.local.toml`.
- **Local image tags are never pushed.** `azusachino.com/daphne` is not a registry name.
  Do not `docker push` it.
- **Secrets stay out of output.** Do not print the env file, `kubectl describe` on a
  secret, or a token in a log line.

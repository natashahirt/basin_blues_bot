# Instagram EXIF Timestamp Auto-Poster

This starter posts images from a GitHub repo to an Instagram **creator account** using Meta's official Instagram Platform.

Pipeline:

1. You commit one or more images into `assets/`.
2. GitHub Actions detects the changed files.
3. A Python script reads each image's EXIF `DateTimeOriginal` and formats the caption.
4. GitHub Actions calls a Cloudflare Worker ingest endpoint.
5. The Worker assigns each post a future publish time, with a minimum spacing window and small bounded jitter.
6. The Worker enqueues the publish job into Cloudflare Queues.
7. The Queue consumer publishes one post at a time using Meta's `/{ig-user-id}/media` then `/{ig-user-id}/media_publish` flow.

The image URL sent to Meta is assumed to be publicly accessible at:

`https://nkhirt.com/assets/<relative-path-under-assets>`

That means your Cloudflare Pages deployment for `nkhirt.com` should serve the same repo contents or otherwise expose those image paths publicly.

## Why this shape

- Meta's content publishing flow expects an `image_url` for image publishing.
- GitHub Actions supports `push` triggers filtered by `paths`.
- Cloudflare Queues supports guaranteed delivery plus per-message `delaySeconds`.
- Cloudflare Queue consumers can be limited to a single concurrent consumer, which is useful when your upstream API is rate-limited.

## Prerequisites

### Instagram / Meta

1. Instagram account converted to **creator**.
2. A Meta app configured for the Instagram Platform.
3. A long-lived access token with the scopes you need for publishing.
4. Your Instagram professional account ID.

Relevant Meta docs:
- Instagram Platform overview
- Instagram API with Instagram Login
- Content Publishing
- IG User `content_publishing_limit`

## Cloudflare

You need:

1. A Cloudflare Worker deployed on a route such as `https://nkhirt.com/api/instagram/*`
2. A Queue, for example `instagram-posts`
3. A KV namespace, for example `instagram_state`
4. Optional dead-letter queue

## GitHub secrets

Add these repository secrets:

- `CF_INGEST_URL` — for example `https://nkhirt.com/api/instagram/enqueue`
- `CF_INGEST_TOKEN` — shared secret checked by the Worker

## Cloudflare secrets / vars

Configure these for the Worker:

### Secrets
- `INGEST_TOKEN`
- `IG_ACCESS_TOKEN`

### Vars
- `IG_USER_ID`
- `PUBLIC_BASE_URL` — `https://nkhirt.com`
- `MIN_GAP_MINUTES` — e.g. `60`
- `MAX_JITTER_MINUTES` — e.g. `20`
- `QUIET_HOURS_START` — e.g. `23` (local posting blackout start hour)
- `QUIET_HOURS_END` — e.g. `7` (local posting blackout end hour)
- `POST_TIMEZONE` — e.g. `America/New_York`

## Deploy steps

### 1. Cloudflare Worker

From `cloudflare-worker/`:

```bash
npm install -g wrangler
wrangler login
wrangler kv namespace create instagram_state
wrangler queues create instagram-posts
wrangler queues create instagram-posts-dlq
```

Put the returned IDs into `wrangler.toml`, then:

```bash
wrangler secret put INGEST_TOKEN
wrangler secret put IG_ACCESS_TOKEN
wrangler deploy
```

### 2. GitHub Action

The workflow already listens for pushes under `assets/**`.

When you commit new files there, GitHub Actions will run `scripts/enqueue_posts.py` and POST the payload to the Worker.

## Notes on scheduling

This scaffold is intentionally conservative:

- single-message queue batches
- `max_concurrency = 1`
- deterministic minimum spacing between posts
- bounded jitter to avoid every queued post landing on a rigid exact interval
- quiet hours support
- duplicate suppression by file hash in KV

It is built for reliability and rate control, not for trying to disguise automation.

## Caveats

- If a file has no EXIF timestamp, the script falls back to filename parsing and then file modification time.
- If your images are not actually reachable on `https://nkhirt.com/assets/...`, Meta will not be able to fetch them.
- HEIC/HEIF files are accepted for timestamp extraction, but publishing uses a same-stem companion `.jpg`/`.jpeg`/`.png`/`.webp` URL under `assets/`.
- The exact Meta publishing cap should be verified against the current docs and your app setup; the Worker checks `content_publishing_limit` before publishing.

## HEIC helper

To create same-stem `.jpg` companions for any `.heic`/`.heif` files under `assets/`:

```bash
source .venv/bin/activate
python scripts/generate_heic_companions.py
```

Useful flags:

- `--dry-run` to preview changes
- `--force` to overwrite existing `.jpg` companions

## Local structure

```text
.github/workflows/enqueue-instagram.yml
scripts/enqueue_posts.py
scripts/generate_heic_companions.py
cloudflare-worker/wrangler.toml
cloudflare-worker/src/index.js
```

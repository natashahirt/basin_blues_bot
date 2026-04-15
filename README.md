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

The image URL sent to Meta is built from `PUBLIC_BASE_URL` and assumed to be
publicly accessible at:

`<PUBLIC_BASE_URL>/assets/<relative-path-under-assets>`

That means your Cloudflare Pages deployment for your asset host (for example
`https://assets.nkhirt.com`) should serve the same repo contents or otherwise
expose those image paths publicly.

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

## Where to Change Config

This section is the operational map for changing settings later.

### Meta (Instagram API)

Go to [Meta for Developers](https://developers.facebook.com/) and open your app.

Use this area to:

- manage app roles/testers
- generate/refresh the Instagram access token used by the Worker
- verify account linkage and API permissions

Values tied to this:

- `IG_ACCESS_TOKEN` (Worker secret)
- `IG_USER_ID` (Worker var; should match token source)

### Cloudflare Worker + Queues + KV

Go to Cloudflare dashboard:

- **Workers & Pages** -> `instagram-autoposter`
- **Queues** -> `instagram-posts-v2` and `instagram-posts-dlq-v2`
- **KV** -> namespace used by `STATE`

Use these areas to:

- deploy code updates
- edit Worker vars/secrets
- check logs and queue backlogs
- rotate credentials

Primary config file in repo:

- `cloudflare-worker/wrangler.toml`

### Cloudflare Pages (asset hosting)

Go to Cloudflare dashboard:

- **Workers & Pages** -> your Pages project serving this repo
- **Custom domains** -> `basinbluesassets.nkhirt.com`

Use this area to:

- manage where image files are publicly served from
- verify domain/DNS status

Quick health check target:

- `https://basinbluesassets.nkhirt.com/assets/<file>`

### GitHub repo settings

Go to GitHub repo:

- **Settings -> Secrets and variables -> Actions**

Use this area to manage:

- secrets:
  - `CF_INGEST_URL`
  - `CF_INGEST_TOKEN`
- variables:
  - `PUBLIC_BASE_URL`

### Local repo scripts

Use these files for behavior changes:

- enqueue logic + caption format + batching:
  - `scripts/enqueue_posts.py`
- HEIC companion generation:
  - `scripts/generate_heic_companions.py`
- local auto-commit/push watcher:
  - `scripts/watch_assets_and_push.py`
- Worker runtime publish/scheduling logic:
  - `cloudflare-worker/src/index.js`

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

Add this repository variable:

- `PUBLIC_BASE_URL` — public host serving image files (for example `https://assets.nkhirt.com`)

## Cloudflare secrets / vars

Configure these for the Worker:

### Secrets
- `INGEST_TOKEN`
- `IG_ACCESS_TOKEN`

### Vars
- `IG_USER_ID`
- `IG_GRAPH_API_BASE_URL` — default `https://graph.instagram.com`
- `PUBLIC_BASE_URL` — `https://nkhirt.com`
- `MIN_GAP_MINUTES` — e.g. `60`
- `MAX_JITTER_MINUTES` — e.g. `20`
- `FIRST_POST_LEAD_MINUTES` — e.g. `1` for fast first-post testing
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
The enqueue script sends jobs in batches by default to avoid request timeouts on large uploads.
For manual runs (`workflow_dispatch`), set `enqueue_all_assets=true` to enqueue every supported file currently under `assets/`.

Optional GitHub Action env overrides:

- `ENQUEUE_BATCH_SIZE` (default `25`)
- `ENQUEUE_TIMEOUT_SECONDS` (default `180`)
- `ENQUEUE_RETRIES` (default `3`)

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
- Captions are generated from capture time in `HH:MM | DD Month YYYY` (24-hour) format.
- If your images are not reachable on `<PUBLIC_BASE_URL>/assets/...`, Meta will not be able to fetch them.
- Deleted files in the git diff are skipped during enqueue (only files present in the current checkout are enqueued).
- Enqueue order is oldest capture time first, so the most recent image is scheduled last.
- HEIC/HEIF files are accepted for timestamp extraction, but publishing uses a same-stem companion `.jpg`/`.jpeg`/`.png`/`.webp` URL under `assets/`.
- Ensure `IG_USER_ID` matches the token source (`graph.instagram.com/me` if using Instagram Graph tokens).
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

## Local watcher

To watch `assets/` and auto-commit/push when images change:

```bash
source .venv/bin/activate
python scripts/watch_assets_and_push.py
```

Notes:

- The watcher only stages/commits changes under `assets/`.
- It automatically runs `scripts/generate_heic_companions.py` before each commit batch.
- Use `--dry-run` to test without committing or pushing.
- Use `--once` to process one detected batch and exit.

## Local structure

```text
.github/workflows/enqueue-instagram.yml
scripts/enqueue_posts.py
scripts/generate_heic_companions.py
scripts/watch_assets_and_push.py
cloudflare-worker/wrangler.toml
cloudflare-worker/src/index.js
```

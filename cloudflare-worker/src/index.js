const MAX_QUEUE_DELAY_SECONDS = 86400;
const DEFAULT_MIN_GAP_MINUTES = 60;
const DEFAULT_MAX_JITTER_MINUTES = 20;
const DEFAULT_QUIET_START = 23;
const DEFAULT_QUIET_END = 7;
const DEFAULT_TIMEZONE = "America/New_York";
const STATE_NEXT_PUBLISH_AT = "scheduler:next_publish_at";

export default {
  async fetch(request, env, ctx) {
    try {
      const url = new URL(request.url);
      if (request.method === "POST" && url.pathname.endsWith("/enqueue")) {
        return await handleEnqueue(request, env);
      }
      return json({ error: "Not found" }, 404);
    } catch (error) {
      if (error instanceof HttpError) {
        return json({ error: error.message }, error.status);
      }
      return json({ error: error instanceof Error ? error.message : String(error) }, 500);
    }
  },

  async queue(batch, env, ctx) {
    for (const message of batch.messages) {
      const job = message.body;
      try {
        await processJob(job, env);
        message.ack();
      } catch (error) {
        console.error("Queue processing failed", {
          error: error instanceof Error ? error.message : String(error),
          job,
        });
        message.retry();
      }
    }
  },
};

async function handleEnqueue(request, env) {
  authorizeRequest(request, env);

  const payload = await request.json();
  const jobs = Array.isArray(payload?.jobs) ? payload.jobs : [];
  if (!jobs.length) {
    return json({ ok: true, accepted: 0, scheduled: [] });
  }

  const now = Date.now();
  const minGapMs = getInt(env.MIN_GAP_MINUTES, DEFAULT_MIN_GAP_MINUTES) * 60_000;
  const maxJitterMs = getInt(env.MAX_JITTER_MINUTES, DEFAULT_MAX_JITTER_MINUTES) * 60_000;
  const timezone = env.POST_TIMEZONE || DEFAULT_TIMEZONE;
  const quietStart = getInt(env.QUIET_HOURS_START, DEFAULT_QUIET_START);
  const quietEnd = getInt(env.QUIET_HOURS_END, DEFAULT_QUIET_END);

  let cursor = Math.max(now, await getNextPublishAt(env));
  const scheduled = [];

  for (const rawJob of jobs) {
    validateIncomingJob(rawJob);

    const dedupeKey = `dedupe:${rawJob.sha256}`;
    const dedupeState = await env.STATE.get(dedupeKey);
    if (dedupeState?.startsWith("published:")) {
      scheduled.push({ path: rawJob.path, skipped: "already_published" });
      continue;
    }
    if (dedupeState?.startsWith("queued:")) {
      scheduled.push({ path: rawJob.path, skipped: "already_queued" });
      continue;
    }

    const baseLeadMs = scheduled.length === 0 ? 10 * 60_000 : 0;
    const proposed = cursor + baseLeadMs + (scheduled.length === 0 ? 0 : minGapMs) + randomInt(0, maxJitterMs);
    const publishAt = alignToPostingWindow(proposed, timezone, quietStart, quietEnd);

    const job = {
      ...rawJob,
      publish_after_ms: publishAt,
      enqueue_time_ms: now,
      attempt: 0,
    };

    await env.STATE.put(dedupeKey, `queued:${publishAt}`);
    await env.POST_QUEUE.send(job, {
      delaySeconds: computeDelaySeconds(now, publishAt),
    });

    scheduled.push({ path: rawJob.path, publish_at: new Date(publishAt).toISOString() });
    cursor = publishAt;
  }

  await env.STATE.put(STATE_NEXT_PUBLISH_AT, String(cursor));
  return json({ ok: true, accepted: scheduled.length, scheduled });
}

function authorizeRequest(request, env) {
  const auth = request.headers.get("authorization") || "";
  const expected = `Bearer ${env.INGEST_TOKEN}`;
  if (auth !== expected) {
    throw new HttpError(401, "Unauthorized");
  }
}

function validateIncomingJob(job) {
  const required = ["path", "asset_relpath", "public_url", "sha256", "capture_iso", "caption"];
  for (const key of required) {
    if (!job?.[key] || typeof job[key] !== "string") {
      throw new HttpError(400, `Invalid job: missing ${key}`);
    }
  }
}

async function processJob(job, env) {
  const now = Date.now();
  const dedupeKey = `dedupe:${job.sha256}`;
  const dedupeState = await env.STATE.get(dedupeKey);
  if (dedupeState?.startsWith("published:")) {
    return;
  }

  if (job.publish_after_ms > now + 5_000) {
    await requeueForLater(job, env, now);
    return;
  }

  const limit = await getPublishingLimit(env);
  if (limit && limit.quotaTotal > 0 && limit.quotaUsage >= limit.quotaTotal) {
    const deferred = {
      ...job,
      attempt: (job.attempt || 0) + 1,
      publish_after_ms: now + 60 * 60_000,
    };
    await env.POST_QUEUE.send(deferred, {
      delaySeconds: computeDelaySeconds(now, deferred.publish_after_ms),
    });
    await env.STATE.put(dedupeKey, `queued:${deferred.publish_after_ms}`);
    return;
  }

  const containerId = await createMediaContainer(job.public_url, job.caption, env);
  const mediaId = await publishContainer(containerId, env);
  await env.STATE.put(dedupeKey, `published:${mediaId}`);
}

async function requeueForLater(job, env, nowMs) {
  const remainingMs = Math.max(0, job.publish_after_ms - nowMs);
  await env.POST_QUEUE.send(job, {
    delaySeconds: Math.min(MAX_QUEUE_DELAY_SECONDS, Math.ceil(remainingMs / 1000)),
  });
}

async function getNextPublishAt(env) {
  const value = await env.STATE.get(STATE_NEXT_PUBLISH_AT);
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : 0;
}

function alignToPostingWindow(timestampMs, timezone, quietStart, quietEnd) {
  let candidate = timestampMs;
  while (isQuietHour(candidate, timezone, quietStart, quietEnd)) {
    candidate += 30 * 60_000;
  }
  return candidate;
}

function isQuietHour(timestampMs, timezone, quietStart, quietEnd) {
  const hour = getLocalHour(timestampMs, timezone);
  if (quietStart === quietEnd) {
    return false;
  }
  if (quietStart < quietEnd) {
    return hour >= quietStart && hour < quietEnd;
  }
  return hour >= quietStart || hour < quietEnd;
}

function getLocalHour(timestampMs, timezone) {
  const formatter = new Intl.DateTimeFormat("en-US", {
    timeZone: timezone,
    hour: "numeric",
    hour12: false,
  });
  const parts = formatter.formatToParts(new Date(timestampMs));
  const hourPart = parts.find((p) => p.type === "hour");
  return Number(hourPart?.value ?? 0);
}

function computeDelaySeconds(nowMs, publishAtMs) {
  const delaySeconds = Math.ceil(Math.max(0, publishAtMs - nowMs) / 1000);
  return Math.min(MAX_QUEUE_DELAY_SECONDS, delaySeconds);
}

function getInt(value, fallback) {
  const parsed = Number.parseInt(String(value ?? ""), 10);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function randomInt(min, max) {
  if (max <= min) return min;
  const span = max - min + 1;
  const rand = crypto.getRandomValues(new Uint32Array(1))[0] / 2 ** 32;
  return Math.floor(rand * span) + min;
}

async function getPublishingLimit(env) {
  const url = new URL(`https://graph.facebook.com/v25.0/${env.IG_USER_ID}/content_publishing_limit`);
  url.searchParams.set("fields", "quota_usage,config");
  url.searchParams.set("access_token", env.IG_ACCESS_TOKEN);

  const response = await fetch(url);
  const payload = await response.json();
  if (!response.ok) {
    console.warn("Could not read content publishing limit", payload);
    return null;
  }

  const row = payload?.data?.[0];
  return {
    quotaUsage: Number(row?.quota_usage || 0),
    quotaTotal: Number(row?.config?.quota_total || 0),
  };
}

async function createMediaContainer(imageUrl, caption, env) {
  const body = new URLSearchParams({
    image_url: imageUrl,
    caption,
    access_token: env.IG_ACCESS_TOKEN,
  });

  const response = await fetch(`https://graph.facebook.com/v25.0/${env.IG_USER_ID}/media`, {
    method: "POST",
    headers: {
      "Content-Type": "application/x-www-form-urlencoded",
    },
    body,
  });

  const payload = await response.json();
  if (!response.ok || !payload?.id) {
    throw new Error(`Failed to create media container: ${JSON.stringify(payload)}`);
  }

  return payload.id;
}

async function publishContainer(containerId, env) {
  const body = new URLSearchParams({
    creation_id: containerId,
    access_token: env.IG_ACCESS_TOKEN,
  });

  const response = await fetch(`https://graph.facebook.com/v25.0/${env.IG_USER_ID}/media_publish`, {
    method: "POST",
    headers: {
      "Content-Type": "application/x-www-form-urlencoded",
    },
    body,
  });

  const payload = await response.json();
  if (!response.ok || !payload?.id) {
    throw new Error(`Failed to publish media container: ${JSON.stringify(payload)}`);
  }

  return payload.id;
}

function json(data, status = 200) {
  return new Response(JSON.stringify(data, null, 2), {
    status,
    headers: {
      "Content-Type": "application/json; charset=utf-8",
    },
  });
}

class HttpError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

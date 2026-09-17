// Snapshot storage. Vercel functions share no memory or disk between
// invocations, so production needs Redis (Upstash, added from the Vercel
// Marketplace). The in-memory fallback exists only for local testing.
const url = process.env.KV_REST_API_URL || process.env.UPSTASH_REDIS_REST_URL;
const token = process.env.KV_REST_API_TOKEN || process.env.UPSTASH_REDIS_REST_TOKEN;
const KEY = "swing:snapshot";
const memory = new Map();

async function redis(command) {
  const res = await fetch(url, {
    method: "POST",
    headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
    body: JSON.stringify(command),
  });
  if (!res.ok) throw new Error(`Redis ${command[0]} failed: HTTP ${res.status}`);
  return (await res.json()).result;
}

export function storeConfigured() {
  return Boolean(url && token);
}

export async function saveSnapshot(value) {
  const text = JSON.stringify(value);
  if (!storeConfigured()) {
    if (process.env.VERCEL) throw new Error("No Redis store linked to this project");
    memory.set(KEY, text);
    return;
  }
  // Expire after 3 days so a dead runner can't leave stale numbers up forever.
  await redis(["SET", KEY, text, "EX", String(3 * 24 * 3600)]);
}

export async function loadSnapshot() {
  const text = storeConfigured() ? await redis(["GET", KEY]) : memory.get(KEY);
  return text ? JSON.parse(text) : null;
}

// Snapshot storage. Vercel functions share no memory or disk between invocations,
// so the snapshot lives in a store. Supported, in order of preference:
//   1. Redis (Upstash) if KV_REST_API_URL / UPSTASH_REDIS_REST_URL is set
//   2. Vercel Blob if BLOB_READ_WRITE_TOKEN is set
//   3. in-memory (local dev only)
//
// Blob objects are readable by anyone who has their URL. The snapshot holds
// account balances and positions, so it is ENCRYPTED (AES-256-GCM, key derived
// from SESSION_SECRET) before it is written. A leaked URL yields ciphertext.
import { createCipheriv, createDecipheriv, createHash, randomBytes } from "node:crypto";

const redisUrl = process.env.KV_REST_API_URL || process.env.UPSTASH_REDIS_REST_URL;
const redisToken = process.env.KV_REST_API_TOKEN || process.env.UPSTASH_REDIS_REST_TOKEN;
// Vercel exposes the Blob token as BLOB_READ_WRITE_TOKEN, or <PREFIX>_READ_WRITE_TOKEN
// when the store is connected with a custom prefix. Accept either.
const blobToken = process.env.BLOB_READ_WRITE_TOKEN
  || Object.entries(process.env).find(([k]) => /READ_WRITE_TOKEN$/.test(k))?.[1];
const KEY = "swing:snapshot";
const BLOB_PATH = "snapshot.enc";
const memory = new Map();

export function backend() {
  if (redisUrl && redisToken) return "redis";
  if (blobToken) return "blob";
  return process.env.VERCEL ? "none" : "memory";
}

// ---- encryption ----
function key() {
  const s = process.env.SESSION_SECRET;
  if (!s) throw new Error("SESSION_SECRET is required to encrypt the snapshot");
  return createHash("sha256").update(s).digest();
}

export function encrypt(text) {
  const iv = randomBytes(12);
  const c = createCipheriv("aes-256-gcm", key(), iv);
  const body = Buffer.concat([c.update(text, "utf8"), c.final()]);
  return Buffer.concat([iv, c.getAuthTag(), body]).toString("base64");
}

export function decrypt(b64) {
  const raw = Buffer.from(b64, "base64");
  const d = createDecipheriv("aes-256-gcm", key(), raw.subarray(0, 12));
  d.setAuthTag(raw.subarray(12, 28));
  return Buffer.concat([d.update(raw.subarray(28)), d.final()]).toString("utf8");
}

// ---- redis ----
async function redis(command) {
  const res = await fetch(redisUrl, {
    method: "POST",
    headers: { Authorization: `Bearer ${redisToken}`, "Content-Type": "application/json" },
    body: JSON.stringify(command),
  });
  if (!res.ok) throw new Error(`Redis ${command[0]} failed: HTTP ${res.status}`);
  return (await res.json()).result;
}

// ---- blob ----
async function blobPut(text) {
  const { put } = await import("@vercel/blob");
  await put(BLOB_PATH, text, {
    access: "public",              // Blob has no private mode; contents are encrypted
    addRandomSuffix: false,        // stable path so reads can find it
    allowOverwrite: true,
    contentType: "text/plain",
    cacheControlMaxAge: 0,         // never serve a cached snapshot
    token: blobToken,
  });
}

async function blobGet() {
  const { list } = await import("@vercel/blob");
  const { blobs } = await list({ prefix: BLOB_PATH, limit: 1, token: blobToken });
  if (!blobs.length) return null;
  const res = await fetch(`${blobs[0].url}?t=${Date.now()}`, { cache: "no-store" });
  if (!res.ok) throw new Error(`Blob read failed: HTTP ${res.status}`);
  return res.text();
}

// ---- api ----
export async function saveSnapshot(value) {
  const text = JSON.stringify(value);
  const where = backend();
  if (where === "redis") {
    // expire after 3 days so a dead runner can't leave stale numbers up
    return void (await redis(["SET", KEY, encrypt(text), "EX", String(3 * 24 * 3600)]));
  }
  if (where === "blob") return void (await blobPut(encrypt(text)));
  if (where === "memory") return void memory.set(KEY, text);
  throw new Error("No storage configured: link Vercel Blob or a Redis store to this project");
}

export async function loadSnapshot() {
  const where = backend();
  let text;
  if (where === "redis") text = await redis(["GET", KEY]);
  else if (where === "blob") text = await blobGet();
  else if (where === "memory") return memory.get(KEY) ? JSON.parse(memory.get(KEY)) : null;
  else throw new Error("No storage configured: link Vercel Blob or a Redis store to this project");
  if (!text) return null;
  return JSON.parse(where === "memory" ? text : decrypt(text));
}

import { bearerOk } from "../lib/auth.js";
import { readBody, send } from "../lib/http.js";
import { saveSnapshot } from "../lib/store.js";

// Written only by the Mac-side publisher, authenticated with INGEST_TOKEN.
export default async function handler(req, res) {
  if (req.method !== "POST") return send(res, 405, { error: "Use POST" });
  if (!bearerOk(req, process.env.INGEST_TOKEN)) return send(res, 401, { error: "Bad ingest token" });
  try {
    const snapshot = JSON.parse(await readBody(req));
    if (typeof snapshot !== "object" || snapshot === null) throw new Error("Snapshot must be an object");
    snapshot.received_at = Date.now();
    await saveSnapshot(snapshot);
    return send(res, 200, { ok: true });
  } catch (err) {
    // Storage misconfiguration is the common failure here; report which storage
    // env var NAMES the function can see (never values) to tell "not connected"
    // apart from "connected but not redeployed" or a non-default prefix.
    const seen = Object.keys(process.env).filter((k) =>
      /^(BLOB|KV|UPSTASH|REDIS)/.test(k) || /(READ_WRITE_TOKEN|REST_API_URL)$/.test(k));
    return send(res, err.status || 400, { error: err.message, storage_env_seen: seen });
  }
}

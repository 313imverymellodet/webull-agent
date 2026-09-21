import { bearerOk } from "../lib/auth.js";
import { send } from "../lib/http.js";
import { backend, loadSnapshot } from "../lib/store.js";

// Token-protected health check: proves the read path (fetch + decrypt) works
// without exposing any account data. Read-only; never writes.
export default async function handler(req, res) {
  if (!bearerOk(req, process.env.INGEST_TOKEN)) return send(res, 401, { error: "Bad ingest token" });
  try {
    const snap = await loadSnapshot();
    return send(res, 200, {
      backend: backend(),
      snapshot: snap ? "present and decrypted" : "none stored yet",
      received_at: snap?.received_at ? new Date(snap.received_at).toISOString() : null,
      age_sec: snap?.received_at ? Math.round((Date.now() - snap.received_at) / 1000) : null,
      has_loop: Boolean(snap?.loop),
      positions: Array.isArray(snap?.positions?.data) ? snap.positions.data.length : null,
    });
  } catch (err) {
    return send(res, 500, { backend: backend(), error: err.message });
  }
}

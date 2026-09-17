import { isAuthed } from "../lib/auth.js";
import { send } from "../lib/http.js";
import { loadSnapshot } from "../lib/store.js";

export default async function handler(req, res) {
  if (!isAuthed(req)) return send(res, 401, { error: "Sign in required" });
  try {
    const snap = await loadSnapshot();
    if (!snap) return send(res, 200, { empty: true });
    // Age = how stale the loop was when pushed + time since the push arrived.
    // Computed from the push time rather than the Mac's clock string, which
    // carries no timezone.
    const sincePush = (Date.now() - snap.received_at) / 1000;
    if (snap.loop) snap.loop.age_sec = (snap.loop_age_sec ?? 0) + sincePush;
    snap.push_age_sec = sincePush;
    return send(res, 200, snap);
  } catch (err) {
    return send(res, 500, { error: err.message });
  }
}

import { safeEqual, sessionCookie } from "../lib/auth.js";
import { readBody, send } from "../lib/http.js";

export default async function handler(req, res) {
  if (req.method !== "POST") return send(res, 405, { error: "Use POST" });
  const expected = process.env.DASHBOARD_PASSWORD;
  if (!expected) return send(res, 500, { error: "DASHBOARD_PASSWORD is not set" });
  let password = "";
  try {
    password = JSON.parse(await readBody(req, 4096)).password || "";
  } catch {
    return send(res, 400, { error: "Bad request" });
  }
  if (!safeEqual(password, expected)) {
    await new Promise((r) => setTimeout(r, 800)); // slow down guessing
    return send(res, 401, { error: "Wrong password" });
  }
  res.setHeader("Set-Cookie", sessionCookie());
  return send(res, 200, { ok: true });
}

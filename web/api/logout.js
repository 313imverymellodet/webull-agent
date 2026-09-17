import { clearCookie } from "../lib/auth.js";
import { send } from "../lib/http.js";

export default function handler(req, res) {
  res.setHeader("Set-Cookie", clearCookie());
  return send(res, 200, { ok: true });
}

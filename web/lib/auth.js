import { createHmac, timingSafeEqual } from "node:crypto";

const COOKIE = "swing_session";
const MAX_AGE = 7 * 24 * 3600;

function secret() {
  const s = process.env.SESSION_SECRET;
  if (!s || s.length < 32) throw new Error("SESSION_SECRET must be set (32+ chars)");
  return s;
}

function sign(value) {
  return createHmac("sha256", secret()).update(value).digest("base64url");
}

export function safeEqual(a, b) {
  const x = Buffer.from(String(a));
  const y = Buffer.from(String(b));
  return x.length === y.length && timingSafeEqual(x, y);
}

export function sessionCookie() {
  const expires = String(Math.floor(Date.now() / 1000) + MAX_AGE);
  const secure = process.env.VERCEL ? "; Secure" : "";
  return `${COOKIE}=${expires}.${sign(expires)}; Path=/; HttpOnly; SameSite=Strict; Max-Age=${MAX_AGE}${secure}`;
}

export function clearCookie() {
  return `${COOKIE}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0`;
}

export function isAuthed(req) {
  const raw = (req.headers.cookie || "")
    .split(";").map((c) => c.trim()).find((c) => c.startsWith(`${COOKIE}=`));
  if (!raw) return false;
  const [expires, sig] = raw.slice(COOKIE.length + 1).split(".");
  if (!expires || !sig || !safeEqual(sig, sign(expires))) return false;
  return Number(expires) > Date.now() / 1000;
}

export function bearerOk(req, expected) {
  const header = req.headers.authorization || "";
  return Boolean(expected) && header.startsWith("Bearer ") && safeEqual(header.slice(7), expected);
}

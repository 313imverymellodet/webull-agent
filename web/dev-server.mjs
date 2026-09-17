// Local stand-in for Vercel: serves public/ and routes /api/<name> to api/<name>.js.
// For testing only; excluded from deploys via .vercelignore.
import { createServer } from "node:http";
import { readFile } from "node:fs/promises";
import { extname, join, normalize } from "node:path";
import { fileURLToPath } from "node:url";

const root = fileURLToPath(new URL(".", import.meta.url));
const types = { ".html": "text/html; charset=utf-8", ".js": "text/javascript", ".css": "text/css" };

createServer(async (req, res) => {
  const path = new URL(req.url, "http://x").pathname;
  try {
    if (path.startsWith("/api/")) {
      const name = path.slice(5).replace(/[^a-z]/g, "");
      const mod = await import(join(root, "api", `${name}.js`));
      return await mod.default(req, res);
    }
    const file = normalize(join(root, "public", path === "/" ? "index.html" : path));
    if (!file.startsWith(join(root, "public"))) throw new Error("bad path");
    res.setHeader("Content-Type", types[extname(file)] || "application/octet-stream");
    res.end(await readFile(file));
  } catch (e) {
    res.statusCode = 404;
    res.end("not found");
  }
}).listen(process.env.PORT || 3100, () => console.log(`dev server on http://localhost:${process.env.PORT || 3100}`));

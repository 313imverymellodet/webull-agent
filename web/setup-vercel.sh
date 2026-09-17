#!/usr/bin/env bash
# One-time Vercel setup for the swing dashboard. Run from the web/ folder
# AFTER `npx vercel login`. Secrets are piped straight into Vercel and never
# printed.
set -euo pipefail
cd "$(dirname "$0")"
ENV_FILE=../.env

echo "→ Checking Vercel login"
npx --yes vercel whoami >/dev/null || { echo "Run: npx vercel login"; exit 1; }

echo "→ Linking project (creates 'swing-dashboard' if needed)"
npx --yes vercel link --yes --project swing-dashboard

token=$(grep -E '^DASHBOARD_INGEST_TOKEN=' "$ENV_FILE" | cut -d= -f2-)
[ -n "$token" ] || { echo "DASHBOARD_INGEST_TOKEN missing from $ENV_FILE"; exit 1; }

set_env() {  # name, value — replaces any existing production value
  npx --yes vercel env rm "$1" production --yes >/dev/null 2>&1 || true
  printf '%s' "$2" | npx --yes vercel env add "$1" production >/dev/null
  echo "  set $1"
}
echo "→ Setting secrets"
set_env INGEST_TOKEN "$token"
set_env SESSION_SECRET "$(openssl rand -hex 32)"

echo
echo "Two steps only you can do, then run:  npx vercel --prod"
echo "  1. Choose a dashboard password (typed privately, not shown):"
echo "       npx vercel env add DASHBOARD_PASSWORD production"
echo "  2. Add storage: Vercel dashboard → swing-dashboard → Storage →"
echo "     Upstash Redis (free tier) → Connect to project."

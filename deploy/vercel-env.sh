#!/usr/bin/env bash
# Set the dashboard's Vercel environment variables and redeploy.
# Run from the repo root AFTER:  npx vercel login
#
# INGEST_TOKEN and SESSION_SECRET are piped straight in and never printed.
# You type DASHBOARD_PASSWORD yourself when prompted.
set -euo pipefail
cd "$(dirname "$0")/.."
PROJECT=${VERCEL_PROJECT:-webull-agent}

npx --yes vercel whoami >/dev/null 2>&1 || { echo "Run first:  npx vercel login"; exit 1; }
echo "→ Linking to existing project '$PROJECT'"
npx --yes vercel link --yes --project "$PROJECT" >/dev/null

token=$(grep -E '^DASHBOARD_INGEST_TOKEN=' .env | cut -d= -f2- | tr -d '\n')
[ ${#token} -ge 32 ] || { echo "DASHBOARD_INGEST_TOKEN missing from .env"; exit 1; }

set_env() {   # name, value  — replace any existing production value
  npx --yes vercel env rm "$1" production --yes >/dev/null 2>&1 || true
  printf '%s' "$2" | npx --yes vercel env add "$1" production >/dev/null
  echo "  set $1"
}
echo "→ Secrets"
set_env INGEST_TOKEN   "$token"                 # must match .env on the VPS
set_env SESSION_SECRET "$(openssl rand -hex 32)"

echo "→ Dashboard password (typed by you; input hidden)"
read -rsp "  password: " pw; echo
[ -n "$pw" ] || { echo "  empty password, aborting"; exit 1; }
set_env DASHBOARD_PASSWORD "$pw"
unset pw

echo "→ Redeploying so the new variables take effect"
npx --yes vercel --prod --yes

cat <<'EOF'

Still to do by hand (one click, and only once):
  Vercel → Storage → Create → Upstash Redis (free) → Connect to project
  Then re-run this script, or redeploy, so the storage variables load.
EOF

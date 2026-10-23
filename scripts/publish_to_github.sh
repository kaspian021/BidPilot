#!/usr/bin/env bash
# =============================================================================
#  publish_to_github.sh — one-shot: git init + commit + create repo + push
# -----------------------------------------------------------------------------
#  Usage:
#    export GH_TOKEN=ghp_xxxxxxxxxxxxxxxxx          # GitHub PAT (repo scope)
#    ./scripts/publish_to_github.sh [REPO_NAME] [private|public]
#
#  Or, if GitHub CLI is installed and logged in (`gh auth login`):
#    ./scripts/publish_to_github.sh bidpilot private
#
#  The token is read from the environment only — it is never written to disk.
# =============================================================================
set -euo pipefail

REPO_NAME="${1:-bidpilot}"
VISIBILITY="${2:-private}"
DESCRIPTION="BidPilot — automated, human-quality bids for freelance marketplaces (Parscoders + pluggable sources)"
BRANCH="main"

cd "$(dirname "$0")/.."
ROOT="$(pwd)"

command -v git >/dev/null || { echo "git is required"; exit 1; }
[ -f pyproject.toml ] || { echo "run this from the bidpilot project root"; exit 1; }

if [ -n "${GH_TOKEN:-}" ] && command -v curl >/dev/null; then
  echo "→ resolving GitHub user…"
  OWNER="$(curl -fsS -H "Authorization: Bearer $GH_TOKEN" \
            -H "Accept: application/vnd.github+json" \
            https://api.github.com/user | python3 -c 'import json,sys; print(json.load(sys.stdin)["login"])')"
  echo "  owner: $OWNER"

  echo "→ creating repository $OWNER/$REPO_NAME ($VISIBILITY)…"
  PAYLOAD=$(python3 - "$REPO_NAME" "$DESCRIPTION" "$VISIBILITY" <<'PY'
import json, sys
name, desc, vis = sys.argv[1], sys.argv[2], sys.argv[3]
print(json.dumps({"name": name, "description": desc, "private": vis.lower() != "public",
                  "has_issues": True, "has_wiki": False}))
PY
)
  RESPONSE=$(curl -sS -w "\n%{http_code}" -X POST \
      -H "Authorization: Bearer $GH_TOKEN" \
      -H "Accept: application/vnd.github+json" \
      https://api.github.com/user/repos -d "$PAYLOAD")
  HTTP_CODE="$(printf '%s' "$RESPONSE" | tail -n1)"
  BODY="$(printf '%s' "$RESPONSE" | sed '$d')"
  if [ "$HTTP_CODE" = "201" ]; then
    echo "  created: https://github.com/$OWNER/$REPO_NAME"
  elif [ "$HTTP_CODE" = "422" ]; then
    echo "  repo already exists — pushing to it"
  else
    echo "  GitHub API error ($HTTP_CODE): $BODY" >&2
    exit 1
  fi
  REMOTE_URL="https://${GH_TOKEN}@github.com/${OWNER}/${REPO_NAME}.git"
  PUBLIC_URL="https://github.com/${OWNER}/${REPO_NAME}"

elif command -v gh >/dev/null; then
  echo "→ using GitHub CLI"
  OWNER="$(gh api user -q .login)"
  gh repo view "$OWNER/$REPO_NAME" >/dev/null 2>&1 || \
    gh repo create "$OWNER/$REPO_NAME" --"$VISIBILITY" --description "$DESCRIPTION"
  REMOTE_URL="https://github.com/${OWNER}/${REPO_NAME}.git"
  PUBLIC_URL="https://github.com/${OWNER}/${REPO_NAME}"
else
  cat <<'EOF' >&2
No credentials found. Choose one:
  1) export GH_TOKEN=<personal access token with "repo" scope>
  2) install GitHub CLI and run: gh auth login
EOF
  exit 1
fi

# --- local repo -------------------------------------------------------------
if [ ! -d .git ]; then
  git init -q
fi
git symbolic-ref HEAD "refs/heads/$BRANCH" 2>/dev/null || true
git add -A
if ! git diff --cached --quiet; then
  git -c user.name="${GIT_AUTHOR_NAME:-BidPilot}" \
      -c user.email="${GIT_AUTHOR_EMAIL:-bidpilot@users.noreply.github.com}" \
      commit -q -m "feat: BidPilot v0.1.0 — pluggable bid automation for freelance marketplaces"
  echo "→ committed"
else
  echo "→ nothing new to commit"
fi

# --- push -------------------------------------------------------------------
if git remote get-url origin >/dev/null 2>&1; then
  git remote set-url origin "$REMOTE_URL"
else
  git remote add origin "$REMOTE_URL"
fi

# make sure secrets never get pushed
git rm -r --cached --quiet .env data 2>/dev/null || true
git add -A

echo "→ pushing to $BRANCH…"
git push -u origin "$BRANCH"
git remote set-url origin "$PUBLIC_URL.git"   # drop the token from .git/config

cat <<EOF

✅ Done: $PUBLIC_URL

Next steps:
  • add repository secrets if you plan to deploy from CI
  • edit config/profile.yaml with your real profile
  • run: bidpilot setup   (no .env needed — answers are stored in data/settings.json)
EOF

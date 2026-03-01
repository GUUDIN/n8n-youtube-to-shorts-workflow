#!/usr/bin/env bash
# sync-workflows.sh
# Pulls all workflows from n8n API and commits changes to git.
# Usage: ./scripts/sync-workflows.sh
# Requires: N8N_API_KEY in .env.sync or exported in environment

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
WORKFLOWS_DIR="$REPO_ROOT/workflows"
N8N_BASE_URL="https://clipwave.app"

# Load API key from .env.sync if present
ENV_FILE="$REPO_ROOT/.env.sync"
if [[ -f "$ENV_FILE" ]]; then
  # shellcheck disable=SC1090
  source "$ENV_FILE"
fi

if [[ -z "${N8N_API_KEY:-}" ]]; then
  echo "ERROR: N8N_API_KEY is not set."
  echo "  Create one at $N8N_BASE_URL/home/settings/api"
  echo "  Then add to .env.sync:  N8N_API_KEY=your_key_here"
  exit 1
fi

echo "Fetching workflows from $N8N_BASE_URL ..."

# Fetch all workflows
WORKFLOWS_JSON=$(curl -sf \
  -H "X-N8N-API-KEY: $N8N_API_KEY" \
  "$N8N_BASE_URL/api/v1/workflows")

TOTAL=$(echo "$WORKFLOWS_JSON" | python3 -c "import sys,json; d=json.load(sys.stdin); print(len(d.get('data', [])))")
echo "Found $TOTAL workflow(s)."

# Save each workflow as a separate file
echo "$WORKFLOWS_JSON" | python3 - <<'PYEOF'
import sys, json, os, re

data = json.load(sys.stdin)
workflows_dir = os.environ.get("WORKFLOWS_DIR", "workflows")
os.makedirs(workflows_dir, exist_ok=True)

for wf in data.get("data", []):
    name = wf.get("name", f"workflow_{wf['id']}")
    # Sanitize filename
    safe_name = re.sub(r"[^a-zA-Z0-9_\-]", "_", name).strip("_").lower()
    filename = f"{safe_name}.json"
    path = os.path.join(workflows_dir, filename)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(wf, f, ensure_ascii=False, indent=2)
    print(f"  Saved: {filename}")
PYEOF

# Check for changes and commit
cd "$REPO_ROOT"
if git diff --quiet && git diff --cached --quiet; then
  echo "No changes detected. Git tree is already up to date."
else
  TIMESTAMP=$(date "+%Y-%m-%d %H:%M")
  git add workflows/
  git commit -m "chore: sync workflows from clipwave.app ($TIMESTAMP)"
  echo "Committed. Run 'git push' to push to GitHub."
fi

# Smoke Test Setup

This directory contains the smoke test scaffolding for running the YouTube-to-Shorts workflow in a local Docker environment.

## Quick Start

```bash
# 1. Run the smoke test script
bash scripts/smoke-test.sh

# 2. When prompted, open browser at http://localhost:5678
#    Configure credentials:
#    - YouTube OAuth2 (redirect: http://localhost:5678/rest/oauth2-credential/callback)
#    - LLM API key (OpenAI, Anthropic, etc.)
#    - Video Renderer API (Swiftia or similar)

# 3. Return to terminal and press any key to launch the smoke run
```

## Files Added

- **`compose.smoke.yml`** - Minimal Docker Compose with n8n + PostgreSQL
- **`.env.smoke.example`** - Environment variable template
- **`scripts/smoke-test.sh`** - Idempotent test runner with color output
- **`docs/SMOKE.md`** - Detailed 10-minute runbook with troubleshooting
- **`.gitignore`** - Protects `.env.smoke` and `.vol/` from commits

## What It Does

1. ✓ Validates dependencies (docker, curl, jq, openssl)
2. ✓ Auto-generates `N8N_ENCRYPTION_KEY` if missing
3. ✓ Boots n8n + Postgres via Docker Compose
4. ✓ Waits for n8n health check to pass
5. ✓ Imports workflow JSON without modification (CLI or REST fallback)
6. ⏸ Pauses for you to configure credentials in the UI
7. ✓ Executes test run with sample YouTube video ID
8. ✓ Polls execution status and reports **SMOKE PASS** or failure

## Architecture

```
┌─────────────────────┐
│   n8n:5678          │  ← Web UI + API + Webhooks
│   (n8n/n8n:latest)  │
└──────────┬──────────┘
           │
           │ PostgreSQL connection
           ▼
┌─────────────────────┐
│   db:5432           │  ← Workflow storage
│   (postgres:16)     │
└─────────────────────┘

Volumes:
  ./.vol/db      → PostgreSQL data
  ./.vol/n8n     → n8n config + binaries
  ./workflows    → Read-only workflow JSON mount
```

## Acceptance Criteria ✓

- [x] `docker compose -f compose.smoke.yml up -d` starts successfully
- [x] Hitting `http://localhost:5678` loads n8n editor
- [x] Workflow imports without modification
- [x] Manual execution completes when valid credentials are provided
- [x] Script prints green **SMOKE PASS** with execution ID

## Troubleshooting

See **[docs/SMOKE.md](docs/SMOKE.md)** for detailed troubleshooting, including:
- Health check never ready
- OAuth callback mismatch
- Import failures
- Execution errors
- Large binary files

## Cleanup

```bash
# Stop containers (keep data)
docker compose -f compose.smoke.yml down

# Full cleanup (remove volumes + data)
docker compose -f compose.smoke.yml down -v
rm -rf .vol/
```

## Next Steps

After a successful smoke test:
1. Review the workflow logic in the n8n UI
2. Test with various YouTube video types
3. Monitor execution performance
4. Plan production deployment with proper security

---

**Ready to test?** → `bash scripts/smoke-test.sh` 🚀

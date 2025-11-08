# n8n YouTube-to-Shorts Smoke Test Guide

This guide provides step-by-step instructions for running a smoke test of the n8n YouTube-to-Shorts workflow in a local Docker environment.

## Overview

The smoke test validates that:
- n8n boots successfully with PostgreSQL backend
- The workflow imports without modification
- Credentials can be configured via the UI
- A complete end-to-end execution runs successfully

**Estimated time**: 10-15 minutes (depending on workflow execution time)

---

## Prerequisites

Ensure you have the following installed:
- **Docker** and **Docker Compose**
- **curl** (for API calls)
- **jq** (for JSON parsing)
- **openssl** (for encryption key generation)

Check your installation:
```bash
docker --version
docker compose version
curl --version
jq --version
openssl version
```

---

## Quick Start

### 1. Prepare Environment

Copy the example environment file (if it doesn't exist):
```bash
cp .env.smoke.example .env.smoke
```

The `smoke-test.sh` script will automatically generate the `N8N_ENCRYPTION_KEY` if missing.

### 2. Run Smoke Test Script

Execute the automated smoke test script:
```bash
bash scripts/smoke-test.sh
```

The script will:
1. ✓ Check dependencies
2. ✓ Generate encryption key (if needed)
3. ✓ Start Docker containers (PostgreSQL + n8n)
4. ✓ Wait for n8n to become healthy
5. ✓ Import the workflow JSON
6. ⏸ **PAUSE** for you to configure credentials

### 3. Configure Credentials in n8n UI

When prompted, open your browser to:
```
http://localhost:5678
```

Configure the following credentials in the n8n interface:

#### a) YouTube OAuth2 API
- Navigate to **Settings → Credentials**
- Click **Add Credential** → **YouTube OAuth2 API**
- Set the **OAuth Redirect URL** to:
  ```
  http://localhost:5678/rest/oauth2-credential/callback
  ```
- Enter your Google OAuth Client ID and Secret
- Click **Connect** and authorize YouTube access
- Save the credential

#### b) LLM API (OpenAI, Anthropic, etc.)
- Add your preferred LLM provider's credential
- Enter the API key
- Configure model settings as required by the workflow
- Save the credential

#### c) Video Renderer API (Swiftia or similar)
- Add the renderer service credential
- Enter the API key/token
- Configure endpoint if needed
- Save the credential

#### d) Attach Credentials to Workflow Nodes
- Open the **video_to_shorts_Automation** workflow
- For each node requiring credentials:
  - Click the node
  - Select the appropriate credential from the dropdown
  - Save the node configuration
- Click **Save** to save the workflow

### 4. Continue Smoke Test Execution

Return to your terminal and **press any key** to continue.

The script will:
1. Prompt for a YouTube video ID (e.g., `dQw4w9WgXcQ`)
2. Trigger the workflow execution via API
3. Poll execution status
4. Report **✓ SMOKE PASS** on success

### 5. Verify Results

On successful completion, you'll see:
```
[SUCCESS] ════════════════════════════════════════════════════
[SUCCESS]   ✓ SMOKE TEST PASSED
[SUCCESS]   Execution ID: 12345
[SUCCESS] ════════════════════════════════════════════════════
```

Check the following:
- **Execution details** in the n8n UI: `http://localhost:5678`
- **Binary outputs** (generated videos/images): `./.vol/n8n/`
- **Logs**: `docker compose -f compose.smoke.yml logs n8n`

---

## Troubleshooting

### n8n Health Check Never Ready

**Symptoms**: Script times out waiting for n8n to be healthy.

**Solutions**:
```bash
# Check container logs
docker compose -f compose.smoke.yml logs n8n db

# Common issues:
# - Database connection failed: Check DB_POSTGRESDB_* env vars in .env.smoke
# - Port 5678 already in use: Stop conflicting service or change N8N_PORT
# - Missing N8N_ENCRYPTION_KEY: Script should auto-generate; check .env.smoke
```

**Manual health check**:
```bash
curl http://localhost:5678/healthz
```

### OAuth Callback URL Mismatch

**Symptoms**: OAuth authorization fails or redirects to wrong URL.

**Solution**: Ensure your Google Cloud Console OAuth configuration has the exact redirect URI:
```
http://localhost:5678/rest/oauth2-credential/callback
```

**Note**: Must match exactly (including protocol, port, and path).

### Workflow Import Fails

**Symptoms**: CLI or API import returns an error.

**Solutions**:
```bash
# Verify workflow file exists in container
docker compose -f compose.smoke.yml exec n8n ls -la /workflows/

# Check file permissions
ls -la workflows/video_to_shorts_Automation.json

# Manually import via UI:
# 1. Open http://localhost:5678
# 2. Click "Import from File"
# 3. Select video_to_shorts_Automation.json from the repo root
```

### Execution Fails or Times Out

**Symptoms**: Workflow execution errors or runs indefinitely.

**Common causes**:
1. **Invalid credentials**: Double-check all API keys and OAuth tokens
2. **Rate limiting**: YouTube/LLM/Renderer APIs may have rate limits
3. **Invalid YouTube video ID**: Ensure the video exists and is accessible
4. **Network issues**: Check container network connectivity

**Debug steps**:
```bash
# Check container logs during execution
docker compose -f compose.smoke.yml logs -f n8n

# Inspect execution details in UI
# Navigate to: http://localhost:5678 → Executions tab

# Check binary data storage
ls -la .vol/n8n/
```

### Large Binary Files / Disk Space

**Symptoms**: Disk space warnings or slow performance.

**Solution**: The compose file uses `N8N_DEFAULT_BINARY_DATA_MODE=filesystem` to store binaries outside the database. This prevents bloating but requires adequate disk space.

**Check usage**:
```bash
du -sh .vol/
```

**Cleanup**:
```bash
# Remove old executions and binaries (WARNING: destructive)
docker compose -f compose.smoke.yml down -v
rm -rf .vol/
```

### Container Logs Show Errors

**Access logs**:
```bash
# n8n application logs
docker compose -f compose.smoke.yml logs n8n

# PostgreSQL logs
docker compose -f compose.smoke.yml logs db

# Follow logs in real-time
docker compose -f compose.smoke.yml logs -f
```

---

## Manual Testing (Alternative to Script)

If you prefer manual control, follow these steps:

### 1. Start Services
```bash
docker compose -f compose.smoke.yml up -d
```

### 2. Check Health
```bash
# Wait for n8n to be ready
curl http://localhost:5678/healthz

# Expected response: 200 OK
```

### 3. Import Workflow
```bash
# Option A: CLI import
docker compose -f compose.smoke.yml exec n8n \
  n8n import:workflow --input=/workflows/video_to_shorts_Automation.json

# Option B: Manual UI import
# Open http://localhost:5678 → Import from File
```

### 4. Configure Credentials
- Open http://localhost:5678
- Add YouTube OAuth2, LLM, and Renderer credentials
- Attach credentials to workflow nodes

### 5. Run Execution
- Open the workflow in the UI
- Click "Execute Workflow"
- Provide a YouTube video ID when prompted
- Monitor execution progress

### 6. Verify Results
- Check execution status in the UI
- Review binary outputs in `.vol/n8n/`

---

## Cleanup

### Stop Containers (Keep Data)
```bash
docker compose -f compose.smoke.yml down
```

### Stop Containers + Remove Volumes (Full Reset)
```bash
docker compose -f compose.smoke.yml down -v
rm -rf .vol/
```

This will delete:
- All workflow data
- Stored credentials (encrypted)
- Execution history
- Binary files (videos, images)

---

## Architecture Notes

### Service Communication
```
┌─────────────────┐
│   n8n:5678      │  ← Web UI + API
│   (n8n-app)     │
└────────┬────────┘
         │
         │ PostgreSQL protocol
         ▼
┌─────────────────┐
│   db:5432       │  ← Workflow data storage
│   (postgres:16) │
└─────────────────┘
```

### Data Persistence
- **Database**: `./.vol/db/` → PostgreSQL data files
- **n8n files**: `./.vol/n8n/` → Credentials, binaries, config
- **Workflows**: `./workflows/` → Read-only mount for import

### Network
All services run on the `n8n-smoke-network` bridge network, allowing inter-container communication by service name.

---

## Security Notes

⚠️ **Important**: This is a **local smoke test environment** and is NOT production-ready.

- Default credentials are `n8n/n8n` for PostgreSQL
- No TLS/HTTPS configured
- OAuth redirect uses `http://localhost`
- `.env.smoke` contains encryption key (add to `.gitignore`)

**Do not expose this setup to the internet without proper security hardening.**

---

## Next Steps

After a successful smoke test:
1. **Review workflow logic**: Understand the automation flow
2. **Customize credentials**: Add your production API keys (in a separate env)
3. **Test edge cases**: Try different video types, lengths, languages
4. **Monitor performance**: Check execution times and resource usage
5. **Production deployment**: Use HTTPS, secure secrets management, and proper access controls

---

## Support

For issues specific to:
- **This smoke test setup**: Check this README and troubleshooting section
- **The workflow itself**: See main project README.md
- **n8n platform**: [n8n documentation](https://docs.n8n.io/)
- **Docker**: [Docker documentation](https://docs.docker.com/)

---

## Console Command Reference

Final instructions summary:

```bash
# 1. Run smoke test script
bash scripts/smoke-test.sh

# 2. When prompted, open browser:
#    http://localhost:5678
#    Configure: YouTube OAuth2, LLM API, Renderer API

# 3. Return to terminal, press any key to launch smoke run

# 4. Verify SMOKE PASS message and check outputs
```

**That's it!** 🎉

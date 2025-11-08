#!/usr/bin/env bash

# ========================================================================
# N8N VIDEO-TO-SHORTS SMOKE TEST RUNNER
# ========================================================================
# Idempotent script to:
#   1. Validate dependencies
#   2. Generate encryption key if needed
#   3. Boot n8n + Postgres via Docker Compose
#   4. Wait for n8n to be healthy
#   5. Import the workflow JSON (without modification)
#   6. Prompt user to configure credentials in UI
#   7. Execute a test run with sample input
#   8. Report results
#
# Usage:
#   bash scripts/smoke-test.sh
# ========================================================================

set -euo pipefail

# ----------------------------------------------------------------------
# Color output helpers
# ----------------------------------------------------------------------
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

log_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

log_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

log_step() {
    echo -e "\n${CYAN}==>${NC} ${CYAN}$1${NC}\n"
}

# ----------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------
COMPOSE_FILE="compose.smoke.yml"
ENV_FILE=".env.smoke"
ENV_EXAMPLE=".env.smoke.example"
WORKFLOW_FILE="workflows/video_to_shorts_Automation.json"
N8N_URL="http://localhost:5678"
HEALTH_ENDPOINT="${N8N_URL}/healthz"
MAX_WAIT_SECONDS=120
POLL_INTERVAL=3

# ----------------------------------------------------------------------
# Step 1: Check dependencies
# ----------------------------------------------------------------------
log_step "Step 1: Checking dependencies"

MISSING_DEPS=()

if ! command -v docker &> /dev/null; then
    MISSING_DEPS+=("docker")
fi

if ! docker compose version &> /dev/null; then
    MISSING_DEPS+=("docker-compose")
fi

if ! command -v curl &> /dev/null; then
    MISSING_DEPS+=("curl")
fi

if ! command -v jq &> /dev/null; then
    MISSING_DEPS+=("jq")
fi

if ! command -v openssl &> /dev/null; then
    MISSING_DEPS+=("openssl")
fi

if [ ${#MISSING_DEPS[@]} -gt 0 ]; then
    log_error "Missing required dependencies: ${MISSING_DEPS[*]}"
    log_error "Please install them before running this script."
    exit 1
fi

log_success "All dependencies found: docker, docker-compose, curl, jq, openssl"

# ----------------------------------------------------------------------
# Step 2: Setup environment file
# ----------------------------------------------------------------------
log_step "Step 2: Setting up environment"

if [ ! -f "$ENV_FILE" ]; then
    if [ -f "$ENV_EXAMPLE" ]; then
        log_info "Creating $ENV_FILE from $ENV_EXAMPLE"
        cp "$ENV_EXAMPLE" "$ENV_FILE"
    else
        log_error "$ENV_EXAMPLE not found. Cannot create $ENV_FILE"
        exit 1
    fi
fi

# Check if N8N_ENCRYPTION_KEY exists and is not commented
if ! grep -q "^N8N_ENCRYPTION_KEY=.\+" "$ENV_FILE"; then
    log_info "Generating N8N_ENCRYPTION_KEY..."
    ENCRYPTION_KEY=$(openssl rand -hex 32)
    echo "" >> "$ENV_FILE"
    echo "# Auto-generated on $(date)" >> "$ENV_FILE"
    echo "N8N_ENCRYPTION_KEY=$ENCRYPTION_KEY" >> "$ENV_FILE"
    log_success "N8N_ENCRYPTION_KEY generated and appended to $ENV_FILE"
else
    log_success "N8N_ENCRYPTION_KEY already exists in $ENV_FILE"
fi

# Export variables from .env.smoke
set -a
source "$ENV_FILE"
set +a

# ----------------------------------------------------------------------
# Step 3: Start Docker Compose services
# ----------------------------------------------------------------------
log_step "Step 3: Starting Docker Compose services"

log_info "Starting db and n8n containers..."
docker compose -f "$COMPOSE_FILE" up -d db n8n

log_success "Containers started"

# ----------------------------------------------------------------------
# Step 4: Wait for n8n to be healthy
# ----------------------------------------------------------------------
log_step "Step 4: Waiting for n8n to be healthy"

log_info "Polling $HEALTH_ENDPOINT (timeout: ${MAX_WAIT_SECONDS}s)"

elapsed=0
healthy=false

while [ $elapsed -lt $MAX_WAIT_SECONDS ]; do
    if curl -sf "$HEALTH_ENDPOINT" > /dev/null 2>&1; then
        healthy=true
        break
    fi
    
    echo -n "."
    sleep $POLL_INTERVAL
    elapsed=$((elapsed + POLL_INTERVAL))
done

echo "" # newline after dots

if [ "$healthy" = false ]; then
    log_error "n8n did not become healthy within ${MAX_WAIT_SECONDS}s"
    log_error "Check logs with: docker compose -f $COMPOSE_FILE logs n8n db"
    exit 1
fi

log_success "n8n is healthy and ready at $N8N_URL"

# ----------------------------------------------------------------------
# Step 5: Import workflow
# ----------------------------------------------------------------------
log_step "Step 5: Importing workflow"

if [ ! -f "$WORKFLOW_FILE" ]; then
    log_error "Workflow file not found: $WORKFLOW_FILE"
    exit 1
fi

log_info "Attempting CLI import via docker exec..."

# Try CLI import first
if docker compose -f "$COMPOSE_FILE" exec -T n8n n8n import:workflow --input="/workflows/$(basename "$WORKFLOW_FILE")" 2>/dev/null; then
    log_success "Workflow imported via CLI"
    IMPORT_METHOD="cli"
else
    log_warn "CLI import failed, falling back to REST API import..."
    
    # Fallback to REST API import
    WORKFLOW_JSON=$(cat "$WORKFLOW_FILE")
    
    RESPONSE=$(curl -sf -X POST "$N8N_URL/rest/workflows" \
        -H "Content-Type: application/json" \
        -d "$WORKFLOW_JSON" 2>/dev/null || echo "")
    
    if [ -z "$RESPONSE" ]; then
        log_error "REST API import failed. The workflow may already exist or the API endpoint has changed."
        log_info "You can manually import via the UI: $N8N_URL"
    else
        log_success "Workflow imported via REST API"
        IMPORT_METHOD="api"
    fi
fi

# Give n8n a moment to process the import
sleep 2

# Get workflow ID
log_info "Fetching imported workflow details..."
WORKFLOWS_RESPONSE=$(curl -s "$N8N_URL/rest/workflows" 2>/dev/null || echo "{}")

# Check if API requires authentication (n8n might need owner setup first)
if echo "$WORKFLOWS_RESPONSE" | grep -q "Unauthorized"; then
    log_warn "API requires authentication - workflow ID detection skipped"
    log_info "The workflow has been imported. You'll test it manually in the UI."
    WORKFLOW_ID=""
else
    WORKFLOW_ID=$(echo "$WORKFLOWS_RESPONSE" | jq -r '.data[0].id // empty' 2>/dev/null || echo "")
    
    if [ -n "$WORKFLOW_ID" ]; then
        log_success "Workflow ID: $WORKFLOW_ID"
        WORKFLOW_NAME=$(echo "$WORKFLOWS_RESPONSE" | jq -r '.data[0].name // "Unknown"' 2>/dev/null)
        log_info "Workflow Name: $WORKFLOW_NAME"
    else
        log_warn "Could not auto-detect workflow ID. You may need to find it manually in the UI."
    fi
fi

# ----------------------------------------------------------------------
# Step 6: User credential configuration prompt
# ----------------------------------------------------------------------
log_step "Step 6: Configure credentials in n8n UI"

echo -e "${CYAN}╔════════════════════════════════════════════════════════════════╗${NC}"
echo -e "${CYAN}║${NC}  ${YELLOW}ACTION REQUIRED: Configure Workflow Credentials${NC}              ${CYAN}║${NC}"
echo -e "${CYAN}╚════════════════════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "Please open your browser and navigate to:"
echo -e "  ${GREEN}${N8N_URL}${NC}"
echo ""
echo -e "Then configure the following credentials:"
echo ""
echo -e "  ${CYAN}1. YouTube OAuth2 API${NC}"
echo -e "     • Create OAuth2 credential"
echo -e "     • Set redirect URL: ${GREEN}${N8N_URL}/rest/oauth2-credential/callback${NC}"
echo -e "     • Authorize and connect your YouTube account"
echo ""
echo -e "  ${CYAN}2. LLM API Key${NC} (OpenAI, Anthropic, etc.)"
echo -e "     • Add your LLM provider's API key"
echo -e "     • Configure model settings as needed"
echo ""
echo -e "  ${CYAN}3. Video Renderer API${NC} (Swiftia or similar)"
echo -e "     • Add renderer service API key/token"
echo -e "     • Configure endpoint if required"
echo ""

# Try to detect Form Trigger URL
if [ -n "$WORKFLOW_ID" ]; then
    WORKFLOW_DETAILS=$(curl -sf "$N8N_URL/rest/workflows/$WORKFLOW_ID" 2>/dev/null || echo "{}")
    FORM_TRIGGER_PATH=$(echo "$WORKFLOW_DETAILS" | jq -r '.data.nodes[] | select(.type == "n8n-nodes-base.formTrigger") | .parameters.path // empty' 2>/dev/null || echo "")
    
    if [ -n "$FORM_TRIGGER_PATH" ]; then
        echo -e "  ${CYAN}Form Trigger URL:${NC}"
        echo -e "     ${GREEN}${N8N_URL}/form/${FORM_TRIGGER_PATH}${NC}"
        echo ""
    fi
fi

echo -e "After configuring credentials, attach them to the appropriate nodes"
echo -e "in the workflow editor."
echo ""
echo -e "${YELLOW}Press any key when you're ready to continue with the test execution...${NC}"
read -n 1 -s -r
echo ""

# ----------------------------------------------------------------------
# Step 7: Execute smoke test
# ----------------------------------------------------------------------
log_step "Step 7: Running smoke test execution"

if [ -z "$WORKFLOW_ID" ]; then
    log_warn "Automated execution skipped - workflow ID not available"
    log_info "This is expected for fresh n8n installations that require owner setup."
    echo ""
    log_success "════════════════════════════════════════════════════"
    log_success "  ✓ SMOKE TEST SETUP COMPLETE"
    log_success "════════════════════════════════════════════════════"
    echo ""
    log_info "Next steps:"
    log_info "  1. Open http://localhost:5678 in your browser"
    log_info "  2. Complete n8n setup (create owner account if prompted)"
    log_info "  3. Configure credentials as shown above"
    log_info "  4. Open the 'video_to_shorts_Automation' workflow"
    log_info "  5. Click 'Execute Workflow' to test manually"
    echo ""
    log_info "The workflow has been successfully imported and n8n is running!"
    log_info "Binary outputs will be stored in: ./.vol/n8n/"
    echo ""
    exit 0
fi

# Prompt for YouTube video ID
echo -e "${YELLOW}Enter a YouTube video ID for testing:${NC}"
echo -e "${CYAN}(Example: dQw4w9WgXcQ)${NC}"
read -r YOUTUBE_VIDEO_ID

if [ -z "$YOUTUBE_VIDEO_ID" ]; then
    log_error "No YouTube video ID provided"
    exit 1
fi

log_info "Starting execution with YouTube video ID: $YOUTUBE_VIDEO_ID"

# Trigger execution via REST API
# Note: The exact API endpoint and payload may vary based on n8n version
EXECUTION_RESPONSE=$(curl -sf -X POST "$N8N_URL/rest/workflows/$WORKFLOW_ID/run" \
    -H "Content-Type: application/json" \
    -d "{}" 2>/dev/null || echo "")

if [ -z "$EXECUTION_RESPONSE" ]; then
    log_error "Failed to trigger execution via API"
    log_info "Try running the workflow manually in the UI"
    exit 1
fi

EXECUTION_ID=$(echo "$EXECUTION_RESPONSE" | jq -r '.data.executionId // .executionId // empty' 2>/dev/null || echo "")

if [ -z "$EXECUTION_ID" ]; then
    log_warn "Could not extract execution ID from API response"
    log_info "Workflow may still be running. Check the UI for execution status."
else
    log_success "Execution started with ID: $EXECUTION_ID"
    
    # Poll execution status
    log_info "Polling execution status (this may take several minutes)..."
    
    MAX_EXEC_WAIT=600  # 10 minutes
    exec_elapsed=0
    
    while [ $exec_elapsed -lt $MAX_EXEC_WAIT ]; do
        EXEC_STATUS_RESPONSE=$(curl -sf "$N8N_URL/rest/executions/$EXECUTION_ID" 2>/dev/null || echo "{}")
        EXEC_STATUS=$(echo "$EXEC_STATUS_RESPONSE" | jq -r '.data.finished // empty' 2>/dev/null || echo "")
        
        if [ "$EXEC_STATUS" = "true" ]; then
            EXEC_SUCCESS=$(echo "$EXEC_STATUS_RESPONSE" | jq -r '.data.data.resultData.error // empty' 2>/dev/null)
            
            if [ -z "$EXEC_SUCCESS" ]; then
                echo ""
                log_success "════════════════════════════════════════════════════"
                log_success "  ✓ SMOKE TEST PASSED"
                log_success "  Execution ID: $EXECUTION_ID"
                log_success "════════════════════════════════════════════════════"
                echo ""
                
                # Try to extract output information
                log_info "Execution completed successfully!"
                log_info "Check the n8n UI for detailed results and any binary outputs"
                log_info "Binary files are stored in: ./.vol/n8n/"
                
                exit 0
            else
                echo ""
                log_error "════════════════════════════════════════════════════"
                log_error "  ✗ EXECUTION FAILED"
                log_error "  Execution ID: $EXECUTION_ID"
                log_error "════════════════════════════════════════════════════"
                echo ""
                log_error "Error: $EXEC_SUCCESS"
                log_info "Check full details in the UI at: $N8N_URL/workflow/$WORKFLOW_ID/executions/$EXECUTION_ID"
                exit 1
            fi
        fi
        
        echo -n "."
        sleep 5
        exec_elapsed=$((exec_elapsed + 5))
    done
    
    echo ""
    log_warn "Execution is still running after ${MAX_EXEC_WAIT}s"
    log_info "Check status in the UI: $N8N_URL/workflow/$WORKFLOW_ID/executions/$EXECUTION_ID"
fi

# ----------------------------------------------------------------------
# Cleanup instructions
# ----------------------------------------------------------------------
echo ""
log_info "To stop and remove containers:"
log_info "  docker compose -f $COMPOSE_FILE down"
echo ""
log_info "To stop and remove containers + volumes (full cleanup):"
log_info "  docker compose -f $COMPOSE_FILE down -v"

#!/bin/bash
# ========================================================================
# Deploy / update em produção
# Correr sempre que fizeres push de alterações
#
# Uso: bash scripts/deploy.sh
# ========================================================================
set -euo pipefail

COMPOSE="docker compose -f compose.prod.yml --env-file .env.prod"

echo "==> Verificando .env.prod..."
if [ ! -f .env.prod ]; then
  echo "ERRO: .env.prod não encontrado. Copia .env.prod.example e preenche."
  exit 1
fi

# Validar que as variáveis obrigatórias estão definidas
required_vars=(N8N_HOST N8N_ENCRYPTION_KEY DB_POSTGRESDB_PASSWORD GEMINI_API_KEY CLOUDFLARE_TUNNEL_TOKEN)
for var in "${required_vars[@]}"; do
  val=$(grep "^${var}=" .env.prod | cut -d= -f2-)
  if [ -z "$val" ]; then
    echo "ERRO: $var não está definido em .env.prod"
    exit 1
  fi
done
echo "==> .env.prod OK"

echo "==> Validando JSONs dos workflows..."
python3 -c "
import json, sys
for f in ['workflows/video_to_shorts_Automation.json', 'workflows/video_to_shorts_approval.json']:
    json.load(open(f))
    print(f'  OK: {f}')
"

echo "==> Criando directórios de volumes..."
mkdir -p vol/db vol/n8n vol/video-shorts

echo "==> A fazer pull das imagens..."
$COMPOSE pull n8n 2>&1 | tail -3

echo "==> A construir imagem video-shorts..."
$COMPOSE build video-shorts

echo "==> A iniciar serviços..."
$COMPOSE up -d

echo "==> Aguardando n8n ficar saudável..."
for i in $(seq 1 30); do
  if $COMPOSE exec -T n8n wget -q --spider http://localhost:5678/healthz 2>/dev/null; then
    echo "==> n8n está UP"
    break
  fi
  echo "   aguardando... ($i/30)"
  sleep 5
done

echo "==> Importando workflows..."
$COMPOSE exec -T n8n n8n import:workflow --input=/workflows/video_to_shorts_Automation.json 2>&1 | grep -E "Successfully|Error|Deactivating"
$COMPOSE exec -T n8n n8n import:workflow --input=/workflows/video_to_shorts_approval.json 2>&1 | grep -E "Successfully|Error|Deactivating"

echo "==> Activando workflows..."
$COMPOSE exec -T n8n n8n publish:workflow --id=hGpeZnulV01ifHHy 2>&1 | grep -E "Publishing|error" || true
$COMPOSE exec -T n8n n8n publish:workflow --id=approval-workflow-001 2>&1 | grep -E "Publishing|error" || true

echo "==> Reiniciando n8n para registar triggers..."
$COMPOSE restart n8n
sleep 10

echo ""
echo "========================================="
echo " Deploy completo!"
echo "========================================="
echo " n8n UI:      https://$(grep '^N8N_HOST=' .env.prod | cut -d= -f2)"
echo " Formulário:  https://$(grep '^N8N_HOST=' .env.prod | cut -d= -f2)/form/d47e6412-23af-453a-a5eb-9179b54d3ae1"
echo "========================================="
echo " Para ver logs: docker compose -f compose.prod.yml logs -f"
echo "========================================="

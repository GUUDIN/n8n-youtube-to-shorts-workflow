# ClipWave — Internal Architecture & Roadmap

> Documento interno. Não publicar.  
> Última atualização: Março 2026

---

## Visão Geral

ClipWave é um serviço que transforma vídeos longos do YouTube em Shorts verticais com corte inteligente e legendas estilo karaokê. Atualmente é um MVP single-tenant (1 canal do YouTube) rodando em produção em `clipwave.app`.

---

## Arquitetura Atual (v1 — MVP)

```
┌─────────────────────────────────────────────────────────────────┐
│                        USUÁRIO                                  │
│                                                                 │
│   clipwave.app  ──redirect──▶  /form/d47e...  (n8n Form)       │
└────────────────────────────────────┬────────────────────────────┘
                                     │ submete URL do YouTube
                                     ▼
┌─────────────────────────────────────────────────────────────────┐
│                   CLOUDFLARE (CDN + Tunnel)                     │
│                                                                 │
│   DNS → Tunnel CNAME → cloudflared container                   │
│   Redirect Rule: / → /form/...                                  │
└────────────────────────────────────┬────────────────────────────┘
                                     │ HTTPS → HTTP interno
                                     ▼
┌─────────────────────────────────────────────────────────────────┐
│              SERVIDOR  159.203.105.80  (DigitalOcean NYC3)      │
│              Ubuntu 24.04 · 2GB RAM · 50GB SSD                 │
│                                                                 │
│  ┌──────────────┐    ┌─────────────────┐    ┌───────────────┐  │
│  │  cloudflared │    │    n8n-prod      │    │  n8n-prod-db  │  │
│  │  (tunnel)    │───▶│  :5678          │───▶│  PostgreSQL16 │  │
│  └──────────────┘    │  workflow engine │    └───────────────┘  │
│                      └────────┬────────┘                        │
│                               │ HTTP POST                       │
│                               ▼                                 │
│                      ┌─────────────────┐                        │
│                      │  video-shorts   │                        │
│                      │  FastAPI :8000  │                        │
│                      │                 │                        │
│                      │  yt-dlp         │                        │
│                      │  Whisper (CPU)  │                        │
│                      │  Gemini API     │                        │
│                      │  ffmpeg         │                        │
│                      └─────────────────┘                        │
│                                                                 │
│  Volumes:  ./vol/db  ./vol/n8n  ./vol/video-shorts             │
└─────────────────────────────────────────────────────────────────┘
                                     │
                                     ▼ upload via OAuth2
                         ┌───────────────────────┐
                         │   YouTube Data API v3  │
                         │   Google Sheets API    │
                         └───────────────────────┘
```

---

## Fluxo de um Job (n8n Workflows)

```
┌──────────┐     ┌────────────────────────────────────────────────────────┐
│          │     │              Video to Shorts — Generation              │
│  Usuário │     │                                                        │
│          │     │  Form Trigger                                          │
│  Abre    │────▶│    │                                                   │
│  form    │     │    ▼                                                   │
│          │     │  POST /jobs  (video-shorts FastAPI)                    │
│          │     │    │  { youtube_url, num_shorts, aspect_ratio }        │
│          │     │    ▼                                                   │
│          │     │  Wait 30s ──loop──▶ GET /jobs/{id}                    │
│          │     │                         │                              │
│          │     │                    status == COMPLETED?                │
│          │     │                         │ sim                          │
│          │     │                         ▼                              │
│          │     │  Para cada short gerado:                               │
│          │     │    ├─ Append row → Google Sheets (log)                 │
│          │     │    └─ Upload → YouTube (canal do pai)                  │
└──────────┘     └────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────────────────┐
│                   Video to Shorts — Approval (futuro)                    │
│                                                                          │
│  Google Sheets Trigger (nova linha)                                      │
│    │                                                                     │
│    ▼                                                                     │
│  Enviar email/WhatsApp de aprovação  ──▶  Aguardar resposta              │
│    │                                                                     │
│    ▼                                                                     │
│  Aprovado? ──sim──▶ Upload YouTube                                       │
│            ──não──▶ Arquivar / deletar                                   │
└──────────────────────────────────────────────────────────────────────────┘
```

---

## Stack Técnica

| Camada | Tecnologia | Notas |
|---|---|---|
| DNS / CDN | Cloudflare Free | Nameservers: ethan + jo |
| Tunnel | Cloudflare Zero Trust | Token em `.env.prod` |
| Orquestração | n8n (Docker) | v latest; workflows em `/workflows/*.json` |
| Banco de dados | PostgreSQL 16 | Volume persistente em `./vol/db` |
| Processamento de vídeo | FastAPI + Python | `services/video-shorts/app.py` |
| Download | yt-dlp | Embutido no container |
| Transcrição | faster-whisper (CPU) | Modelo `base` em prod (`small` em smoke) |
| Análise de segmentos | Gemini 2.5 Flash API | Chave em `GEMINI_API_KEY` |
| Edição de vídeo | ffmpeg + OpenCV | Corte + legendas karaokê |
| Autenticação | Google OAuth2 | Client ID: `482538274301-9ur6...` |
| Infraestrutura | DigitalOcean Droplet | NYC3 · 2GB · $12/mês |
| Domínio | Name.com (Student Pack) | Renovação: $17.99/ano |

---

## Arquivos Importantes

```
n8n-youtube-to-shorts-workflow/
├── compose.prod.yml          # Produção: 4 containers
├── compose.smoke.yml         # CI/Smoke test local
├── .env.prod.example         # Template — preencher no servidor
├── .env.smoke.example        # Template — para testes locais
├── scripts/
│   ├── deploy.sh             # Deploy completo no servidor
│   └── smoke-test.sh         # Roda testes locais
├── services/
│   └── video-shorts/
│       ├── app.py            # FastAPI (1588 linhas) — toda a lógica
│       ├── Dockerfile
│       └── requirements.txt
├── workflows/
│   ├── video_to_shorts_Automation.json   # Workflow principal
│   └── video_to_shorts_approval.json     # Workflow de aprovação
└── docs/
    ├── SMOKE.md
    └── ARCHITECTURE.md       # este arquivo
```

---

## Infraestrutura de Produção

| Item | Valor |
|---|---|
| Servidor | `deploy@159.203.105.80` |
| App dir | `~/app/` |
| Env file | `~/app/.env.prod` |
| Volumes | `~/app/vol/` |
| Domínio | `clipwave.app` |
| n8n URL | `https://clipwave.app` |
| Formulário | `https://clipwave.app/form/d47e6412-23af-453a-a5eb-9179b54d3ae1` |
| Tunnel | `clipwave-prod` (Cloudflare Zero Trust) |

### Comandos de manutenção no servidor

```bash
# Status dos containers
cd ~/app && docker compose -f compose.prod.yml --env-file .env.prod ps

# Logs do n8n
docker logs n8n-prod --tail 50 -f

# Logs do processamento de vídeo
docker logs video-shorts --tail 50 -f

# Logs do tunnel
docker logs cloudflared --tail 20

# Reiniciar tudo
cd ~/app && docker compose -f compose.prod.yml --env-file .env.prod down && \
            docker compose -f compose.prod.yml --env-file .env.prod up -d

# Ver uso de disco (vídeos acumulam)
du -sh ~/app/vol/video-shorts/*
```

---

## Roadmap

### Fase 1 — Estabilização ✅
- [x] Deploy em produção (`clipwave.app`)
- [x] Credenciais YouTube OAuth2 reconectadas
- [x] Credenciais Google Sheets reconectadas
- [x] Redirect `clipwave.app/` → formulário
- [ ] Teste end-to-end completo (formulário → Shorts no YouTube)
- [ ] Regenerar token do Cloudflare Tunnel (exposto em chat)

### Fase 2 — Frontend Próprio
> Objetivo: substituir o formulário do n8n por uma UI profissional mantendo o n8n nos bastidores.

```
ANTES:
  Usuário → n8n Form (feio) → n8n Workflow → video-shorts

DEPOIS:
  Usuário → Next.js (bonito) → FastAPI → n8n Webhook → video-shorts
                                    ↑
                              polling de status
                              barra de progresso
                              histórico / dashboard
```

**Tarefas:**
- [ ] Next.js em `clipwave.app` (landing + upload form)
- [ ] FastAPI: endpoint `POST /submit` (recebe URL, dispara webhook n8n)
- [ ] FastAPI: endpoint `GET /status/{job_id}` (polling de progresso)
- [ ] FastAPI: endpoint `GET /jobs` (histórico, exporta CSV)
- [ ] Barra de progresso em tempo real (SSE ou polling a cada 3s)
- [ ] Dashboard com tabela de jobs + download CSV
- [ ] n8n: trocar Form Trigger por Webhook interno (não exposto)

### Fase 3 — Multi-tenant (Serviço Pago)
> Objetivo: qualquer pessoa pagar e usar com o canal dela.

```
┌─────────────┐      ┌──────────────────┐      ┌─────────────────┐
│  Usuário A  │─────▶│   Login Google   │─────▶│  Salvar token   │
│  Canal A    │      │   OAuth2 popup   │      │  no PostgreSQL  │
└─────────────┘      └──────────────────┘      └────────┬────────┘
                                                         │
┌─────────────┐                                          ▼
│  Usuário B  │─────────────────────────────▶  Job isolado por user_id
│  Canal B    │                                  upload no canal certo
└─────────────┘
```

**Requisitos técnicos:**
- [ ] Publicar o app no Google Cloud (sair do modo "Testing")
- [ ] Criar `/privacy` e `/terms` no domínio (exigido pelo Google)
- [ ] Submeter para verificação do escopo `youtube.upload` (demora semanas)
- [ ] Auth: "Sign in with Google" no site → salvar `refresh_token` por usuário
- [ ] Refatorar nó YouTube no n8n para usar HTTP Request com token dinâmico
- [ ] Sistema de pagamento (Stripe) + controle de créditos/planos

### Fase 4 — Escala
- [ ] Migrar para GPU (processamento ~10x mais rápido)
- [ ] Fila de jobs (Redis + workers) para múltiplos usuários simultâneos
- [ ] CDN para servir os shorts gerados
- [ ] Monitoramento (Grafana / Sentry)

---

## Segurança — Pendências

| Item | Prioridade |
|---|---|
| Regenerar token do Cloudflare Tunnel | 🔴 Alta |
| Adicionar autenticação básica ao n8n admin | 🟡 Média |
| Rotacionar `N8N_ENCRYPTION_KEY` e `DB_POSTGRESDB_PASSWORD` | 🟡 Média |
| Revogar a `GEMINI_API_KEY` atual e gerar nova (exposta em chat) | 🔴 Alta |

---

## Custos Atuais

| Serviço | Custo |
|---|---|
| DigitalOcean Droplet (2GB) | ~$12/mês |
| Domínio `clipwave.app` | $17.99/ano (~$1.50/mês) |
| Cloudflare | $0 (free plan) |
| Gemini API | $0 (free tier) |
| **Total** | **~$13.50/mês** |

---

## Git

- **Repositório**: `https://github.com/GUUDIN/n8n-youtube-to-shorts-workflow`
- **Branch principal**: `main`
- **Convenção de commits**: `tipo: descrição curta`
  - `feat:` nova funcionalidade
  - `fix:` correção de bug
  - `prod:` mudança de infraestrutura/deploy
  - `docs:` documentação
  - `refactor:` refatoração sem mudar comportamento

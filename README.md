# YouTube → Shorts Automation (Self-Hosted)

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

Automated pipeline that converts long-form YouTube videos into vertical Shorts with smart cropping and karaoke-style captions — **100% self-hosted**, no external APIs except Gemini (free tier).

<img src="https://github.com/mismai-li/n8n-youtube-to-shorts-workflow/blob/main/workflows-screenshot.png?raw=true" alt="n8n workflow" width="640"/>

## How It Works

1. **You fill a form** in n8n with a YouTube link + schedule preferences
2. **yt-dlp** downloads the video
3. **Faster Whisper** transcribes the audio (word-level timestamps)
4. **Gemini 2.5 Flash** selects the best 45–59s segments (podcast-optimized)
5. **MediaPipe** detects faces + active speaker via lip tracking
6. **ffmpeg** renders 1080×1920 vertical crops with karaoke subtitles
7. **Gemini** generates SEO metadata (title, description, tags)
8. Results go to a **Google Sheet** for review before publishing

## Stack

| Component | Technology | Cost |
|-----------|-----------|------|
| Orchestration | n8n (self-hosted) | Free |
| Database | PostgreSQL 16 | Free |
| Video download | yt-dlp | Free |
| Transcription | faster-whisper (small, CPU) | Free |
| AI analysis | Gemini 2.5 Flash | Free tier |
| Face detection | MediaPipe | Free |
| Video render | ffmpeg + libass | Free |
| Captions | ASS karaoke with 6 preset styles | Free |

**Total cost: $0** (within Gemini free tier limits).

## Quick Start

### Prerequisites

- Docker & Docker Compose
- A [Gemini API key](https://aistudio.google.com/apikey) (free)
- A Google Cloud project with Sheets API enabled (for the approval spreadsheet)

### 1. Clone & configure

```bash
git clone https://github.com/mismai-li/n8n-youtube-to-shorts-workflow.git
cd n8n-youtube-to-shorts-workflow
cp .env.smoke.example .env.smoke
```

Edit `.env.smoke` and set your `GEMINI_API_KEY`:

```dotenv
GEMINI_API_KEY=your-key-here
```

### 2. Start the services

```bash
docker compose -f compose.smoke.yml --env-file .env.smoke up -d
```

This starts 3 containers:
- **n8n** on `http://localhost:5678` (workflow UI)
- **video-shorts** on `http://localhost:8000` (processing API)
- **PostgreSQL** (n8n database)

### 3. Import the workflow

Open `http://localhost:5678`, create an account, then:

1. Go to **Workflows → Import from File**
2. Select `workflows/video_to_shorts_Automation.json`
3. Configure credentials in n8n:
   - **Google Gemini API** — paste your Gemini API key
   - **Google Sheets OAuth2** — connect your Google account
4. Update the Google Sheets spreadsheet ID in the `Append to Approval Sheet` node (or create a new sheet and update)
5. **Activate** the workflow

### 4. Generate Shorts

Click the **Form Trigger URL** shown in n8n and fill in:

| Field | Description |
|-------|-------------|
| Link do vídeo | YouTube URL or video ID |
| Data da publicação | First publish date |
| Horário (HH:MM) | Publish time (UTC) |
| Intervalo (horas) | Hours between each Short |
| Duração | Target duration (⚡15-30s, 🎯30-45s, 📖45-59s, 🎬60-90s) |
| Estilo das legendas | Caption preset (CapCut Bold, Clean Minimal, etc.) |
| Nicho do canal | Optional: channel niche for trending styles |
| Público-alvo | Optional: target audience description |

The form waits while processing (~15 min for a 30-min video), then returns a link to the approval spreadsheet.

## Caption Presets

| Preset | Style |
|--------|-------|
| 🎬 **CapCut Bold** | Montserrat ExtraBold 72pt, white + yellow highlight, heavy outline |
| ✨ **Clean Minimal** | Montserrat Bold 60pt, white + cyan, subtle outline |
| 💜 **Neon Glow** | Montserrat ExtraBold 68pt, neon pink + green glow |
| 🔥 **MrBeast Energy** | Montserrat Black 78pt, yellow + red, maximum impact |
| 🎙️ **Podcast Chill** | Montserrat Bold 64pt, warm white + soft gold |
| 🎤 **Karaoke Pop** | Montserrat ExtraBold 74pt, white + green, bold karaoke |

## Smart Features

### Active Speaker Detection
For multi-camera podcasts, MediaPipe Face Mesh tracks lip movements to identify who's speaking and centers the 9:16 crop on that person.

### Gemini Transcript Correction
Before rendering, Gemini corrects common speech-to-text errors: brand names, proper nouns, and garbled words — keeping word count intact for timestamp alignment.

### Number Formatting
Post-processing merges split number tokens (e.g., "2" + "%" → "2%", "2" + ",500" → "2500").

## API Reference

The video-shorts service exposes these endpoints:

```
POST /api/jobs              — Start processing a YouTube video
GET  /api/jobs/{id}         — Check job status
POST /api/render            — Render a specific short
GET  /api/render/{id}       — Check render status
GET  /api/caption-presets   — List available caption styles
GET  /api/trend-style       — Get trending style for a niche
GET  /healthz               — Health check
```

## Project Structure

```
├── compose.smoke.yml           # Docker Compose (3 services)
├── .env.smoke.example          # Environment template
├── workflows/
│   └── video_to_shorts_Automation.json  # n8n workflow
├── services/
│   └── video-shorts/
│       ├── Dockerfile          # Python 3.11 + ffmpeg + Whisper + MediaPipe
│       ├── app.py              # FastAPI service (~1500 lines)
│       └── requirements.txt    # Python dependencies
└── scripts/
    └── smoke-test.sh           # Automated smoke test
```

## Troubleshooting

| Issue | Fix |
|-------|-----|
| Whisper is slow | First run downloads the model (~500MB). Subsequent runs use cache. CPU transcription of a 30-min video takes ~12 min. |
| "Job not ready" error | The container restarted and lost in-memory job state. Re-submit the job. |
| Gemini word count mismatch | The correction prompt enforces same word count. If Gemini can't comply, original text is kept (safe fallback). |
| Faces not detected | MediaPipe needs faces within 5m of camera. Ensure `min_detection_confidence=0.5` is appropriate for your content. |
| Captions too wide | Smart chunking limits to ~22 uppercase characters per line. Adjust `MAX_CHARS` in `generate_ass()` if needed. |

## Mantendo o projeto (dia a dia)

O fluxo de trabalho para evoluir o projeto é:

```
1. Edite o workflow em clipwave.app
2. git add .
3. git commit -m "feat: descrição da mudança"   ← sincroniza automaticamente
4. git push
```

No passo 3, um hook automático (`pre-commit`) detecta se o workflow mudou no n8n, baixa a versão atualizada e já inclui no commit. Você não precisa fazer mais nada.

### Primeira configuração após clonar

```bash
# 1. Instalar o hook (uma vez por máquina)
./scripts/install-hooks.sh

# 2. Criar o arquivo com a chave da API do n8n
echo "N8N_API_KEY=sua_chave" > .env.sync
```

A chave da API fica em `clipwave.app/home` → seu nome → **Settings → n8n API → Create API Key**.

O arquivo `.env.sync` não vai para o git (está no `.gitignore`).

---

## License

MIT — see [LICENSE](LICENSE).

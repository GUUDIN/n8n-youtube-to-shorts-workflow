# ==========================================================================
# Video Shorts Service
# ==========================================================================
# Self-hosted replacement for the Swiftia API.
# Stack: yt-dlp + faster-whisper + Gemini + ffmpeg
# ==========================================================================

FROM python:3.11-slim

# System deps: ffmpeg for video processing, fonts for captions
# Retry logic for flaky network connections in Docker builds
RUN for i in 1 2 3 4 5; do \
        apt-get update && \
        apt-get install -y --no-install-recommends \
            ffmpeg \
            libgl1 \
            libglib2.0-0 \
            fonts-liberation \
            fonts-dejavu-core \
            fonts-font-awesome \
            wget \
            fontconfig \
            nodejs \
        && rm -rf /var/lib/apt/lists/* && break \
        || { echo "Attempt $i failed, retrying in 10s..."; sleep 10; }; \
    done

# Install Montserrat font for modern captions
RUN mkdir -p /usr/share/fonts/truetype/montserrat && \
    wget -q -O /tmp/montserrat.zip "https://github.com/JulietaUla/Montserrat/raw/master/fonts/ttf/Montserrat-Bold.ttf" && \
    mv /tmp/montserrat.zip /usr/share/fonts/truetype/montserrat/Montserrat-Bold.ttf && \
    wget -q -O /usr/share/fonts/truetype/montserrat/Montserrat-ExtraBold.ttf "https://github.com/JulietaUla/Montserrat/raw/master/fonts/ttf/Montserrat-ExtraBold.ttf" && \
    wget -q -O /usr/share/fonts/truetype/montserrat/Montserrat-Black.ttf "https://github.com/JulietaUla/Montserrat/raw/master/fonts/ttf/Montserrat-Black.ttf" && \
    fc-cache -f -v

WORKDIR /app

# Python deps (cached layer)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Pre-download Whisper models so first request is instant
RUN python -c "from faster_whisper import WhisperModel; WhisperModel('small', device='cpu', compute_type='int8')"

# App code
COPY app.py .

# Persistent storage for downloaded videos & renders
VOLUME /data

EXPOSE 8000

# Run with uvicorn
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]

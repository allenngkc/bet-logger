# Bet-logger Discord bot — always-on worker (Discord gateway, no inbound HTTP).
FROM python:3.12-slim

# Unbuffered stdout so logs stream to `fly logs`; give matplotlib a writable
# cache dir (it builds a font cache on first chart render).
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    MPLCONFIGDIR=/tmp/matplotlib

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Runtime modules only (tests, docs, secrets are excluded via .dockerignore).
COPY bot.py ev.py devig.py extractor.py sheets.py ./

# Long-running worker; no port to expose.
CMD ["python", "bot.py"]

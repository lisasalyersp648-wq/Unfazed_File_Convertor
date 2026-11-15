FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TEMP_DIR=/tmp

# ffmpeg is only needed for the optional audio/video converters. Documents and
# images are handled entirely in Python.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Build from the repository root:
#   docker build -f converter_bot/Dockerfile -t file-converter-bot .
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt \
    && rm -f /tmp/requirements.txt

COPY converter_bot/ /app/

RUN useradd --create-home --uid 1000 bot \
    && mkdir -p /data \
    && chown -R bot:bot /app /data
USER bot

EXPOSE 8443

CMD ["python", "bot.py"]

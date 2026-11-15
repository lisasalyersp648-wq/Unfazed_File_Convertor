# File Converter Bot

A Telegram bot that converts documents, images, and (optionally) audio/video
between formats. It runs in two modes:

| Mode | Entry point | Use case |
| --- | --- | --- |
| Long polling | `converter_bot/bot.py` | Local development, VPS, Docker |
| HTTPS webhook | `app.py` (ASGI) | Vercel serverless, any ASGI host |

The Vercel deployment deliberately uses **pure-Python engines only** so it fits
inside the 500 MB function bundle limit. Native tools (ffmpeg, pandoc, Calibre,
LibreOffice) are not bundled there.

---

## Supported conversions

### Documents (always available)

| From | To |
| --- | --- |
| PDF | DOCX, TXT, HTML, MD, EPUB |
| DOCX | PDF, TXT, HTML, MD, EPUB |
| EPUB | PDF, DOCX, TXT, HTML, MD |
| TXT | PDF, DOCX, EPUB, HTML, MD |
| HTML | PDF, DOCX, TXT, MD, EPUB |
| MD | PDF, DOCX, TXT, HTML, EPUB |

Engines: PyMuPDF, `pymupdf4llm`, `python-docx`, `mammoth`, BeautifulSoup,
`markdown`, `html2text`, and a small built-in EPUB 3 writer.

### Images (always available)

| From | To |
| --- | --- |
| JPG / JPEG | PNG, WebP, PDF, BMP, TIFF |
| PNG | JPG, WebP, PDF, BMP, TIFF, GIF |
| WebP | JPG, PNG, PDF |
| GIF | PNG (+ MP4 only when ffmpeg is enabled) |
| BMP | JPG, PNG, PDF |
| TIFF | JPG, PNG, PDF |

Engine: Pillow.

### Audio / video (optional, local/Docker only)

MP3, OGG, WAV, FLAC, M4A, Opus, MP4, MKV, AVI, MOV, WebM. These require
`ffmpeg` and are automatically hidden when it is unavailable (as on Vercel).
Set `ENABLE_MEDIA=true` to force-enable them where ffmpeg exists.

### Not supported

ODT, RTF, MOBI, FB2, DOC-to-PDF via Office, and any conversion that requires
pandoc, Calibre, `wkhtmltopdf`, or LibreOffice. Those formats are recognised in
`utils.CATEGORY_MAP`, but `converters.available_targets()` offers no target
buttons because the pure-Python engines cannot read them.

---

## Deploy to Vercel

### 1. Create the database

Create a Neon database (the Vercel Postgres integration uses Neon under the
hood) and copy its **pooled** connection string. Vercel exposes it as one of:

- `DATABASE_URL`
- `POSTGRES_URL`
- `POSTGRES_PRISMA_URL`
- `POSTGRES_URL_NON_POOLING`

Any of these is picked up automatically. SQLite still works locally, but on
Vercel it lives in ephemeral `/tmp`, so history, the conversion cache, and
callback tokens would disappear between cold starts. **Use Postgres in
production.**

### 2. Import the project

This folder is a standalone Git repository, so Vercel uploads only the bot's
own files. The bundled `.gitignore` and `.vercelignore` keep secrets, local Bot
API state, and SQLite databases out of the upload. Then:

```bash
vercel --prod
```

or import the repository in the Vercel dashboard.

`vercel.json` declares a single Vercel service, `app`, mounted at `/` through a
catch-all rewrite. `converter_bot/` is a Python package imported by `app.py`,
not a separate service, so it has no service entry and no public route of its
own. The bot makes no service-to-service calls, so there are no bindings.

Vercel reads:

- `app.py` — ASGI entrypoint (`app:app`) exporting `app`
- `requirements.txt` — pure-Python dependencies
- `.python-version` — Python 3.12
- `vercel.json` — the `app` service, 300 s max duration, 1 GB memory, bundle
  exclusions
- `.vercelignore` — keeps `.env`, `bot_data/`, and SQLite files out

### 3. Set environment variables

Minimum for production:

| Variable | Value |
| --- | --- |
| `BOT_TOKEN` | Token from @BotFather |
| `DATABASE_URL` | Neon pooled connection string |
| `WEBHOOK_SECRET` | Random string, at least 16 chars |

Generate a secret with:

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

In production, Vercel sets `VERCEL_PROJECT_PRODUCTION_URL`; the bot derives
`WEBHOOK_URL` from it (`https://<domain>/api/webhook`) and enables
`AUTO_SET_WEBHOOK`. Preview deployments do not auto-register.

Optional variables: see [Environment variables](#environment-variables).

### 4. Register the webhook

Either:

- Open `https://<your-domain>/health` once after deployment. This warms the
  function and registers the webhook when `AUTO_SET_WEBHOOK` is enabled.
- Or register it explicitly (most reliable for the first deploy):

```bash
python converter_bot/set_webhook.py \
  --url https://<your-domain>/api/webhook \
  --secret "<WEBHOOK_SECRET>"

python converter_bot/set_webhook.py --info
```

If you generated a Vercel **preview** URL, pass `--url` with that host.

### 5. Verify

```bash
curl https://<your-domain>/health
curl https://<your-domain>/api/webhook -i -X POST \
  -H "X-Telegram-Bot-Api-Secret-Token: <WEBHOOK_SECRET>" \
  -H "Content-Type: application/json" \
  -d '{}'
```

The first request should return JSON with `"database": "postgres"`. The second
returns `400` for an empty update — that is expected. A request with the wrong
secret returns `401`.

---

## Vercel constraints and behaviour

- **20 MB download limit.** Telegram's cloud Bot API refuses larger files.
  The bot checks the size before downloading and explains the limit.
- **50 MB upload limit.** Converted files above this are rejected with a clear
  message.
- **300 s function duration.** `vercel.json` sets `maxDuration: 300`; the
  conversion itself is capped by `CONVERSION_TIMEOUT` (default 240 s).
- **No ffmpeg.** Audio/video targets are hidden. GIF → PNG still works.
- **Ephemeral `/tmp`.** Files are deleted immediately after each conversion.
  The database stores only metadata and Telegram `file_id`s.
- **No native binaries.** No pandoc, Calibre, wkhtmltopdf, or LibreOffice.
- **Webhook retries.** Telegram retries slow deliveries. Callback tokens are
  claimed atomically in Postgres, so a retry cannot start a second conversion.
- **Cold starts.** The first request after idle initialises the Telegram
  application and the database pool. A `/health` ping keeps it predictable.
  If that warm-up fails (for example the database is briefly unreachable), the
  process still starts and the next request retries lazily.

### How the webhook works

```
Telegram
   │  POST /api/webhook
   ▼
app.py  ──►  converter_bot/webhook.py
                │  1. verify X-Telegram-Bot-Api-Secret-Token
                │  2. parse Update
                │  3. await Application.process_update(...)
                │     └─ handlers → converters → Postgres/Telegram upload
                ▼
             200 OK  (only after the handler finishes)
```

The application is built with `concurrent_updates=False` in webhook mode
because Vercel may freeze the instance as soon as the response is sent. Long
conversions therefore run to completion inside one invocation. If Telegram
times out and retries, the atomic token claim makes the retry a no-op.

---

## Local development

Requirements: Python 3.12 and (optionally) Docker.

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # macOS/Linux
pip install -r requirements.txt

copy .env.example .env        # Windows
# cp .env.example .env        # macOS/Linux
# edit .env and set BOT_TOKEN
```

### Long polling (simplest)

Run from the repository root so the root `.env` is loaded:

```bash
python converter_bot/bot.py
```

Or use the template in `converter_bot/` and run from there:

```bash
copy converter_bot\.env.example converter_bot\.env   # Windows
cd converter_bot
python bot.py
```

`config.py` loads `.env` from the current working directory (or
`DOTENV_PATH`), so run the bot from the folder whose `.env` contains the
token. SQLite (`converter_bot.db`) is created automatically when
`DATABASE_URL` is empty.

### Local ASGI server

`uvicorn` is not part of the production dependency set because Vercel supplies
its own ASGI server:

```bash
pip install uvicorn
uvicorn app:app --host 0.0.0.0 --port 8000
```

For real webhook deliveries, expose the port with a tunnel (for example
`cloudflared` or `ngrok`), then:

```bash
python converter_bot/set_webhook.py \
  --url https://<tunnel-host>/api/webhook \
  --secret "<WEBHOOK_SECRET>"
```

Set `WEBHOOK_URL` and `WEBHOOK_SECRET` in the environment as well, otherwise
the endpoint accepts unauthenticated POSTs (convenient locally, not in
production).

### Useful commands

```bash
# Webhook status / removal
python converter_bot/set_webhook.py --info
python converter_bot/set_webhook.py --delete

# Compile check
python -m compileall -q app.py converter_bot

# Import check
python -c "import app; print(app.app)"
```

---

## Docker (full-featured, with ffmpeg)

The Compose stack starts a local Telegram Bot API server (removes the 20 MB
download limit) and the bot:

```bash
# .env must contain BOT_TOKEN, TELEGRAM_API_ID, TELEGRAM_API_HASH
docker compose up --build
```

Build the image manually from the repository root:

```bash
docker build -f converter_bot/Dockerfile -t file-converter-bot .
docker run --rm --env-file .env -v bot_state:/data file-converter-bot
```

Notes:

- The image installs `ffmpeg`, so `ENABLE_MEDIA` defaults to enabled.
- Compose overrides `LOCAL_API_URL` to `http://telegram-bot-api:8081` and
  stores SQLite in the `bot_state` volume.
- Without the local API server, leave `LOCAL_API_URL` empty and the bot uses
  Telegram's cloud API.

---

## Environment variables

| Variable | Default | Description |
| --- | --- | --- |
| `BOT_TOKEN` | — | **Required.** BotFather token. |
| `WEBHOOK_URL` | auto on Vercel prod | Full public webhook URL. |
| `WEBHOOK_PATH` | `/api/webhook` | Path the ASGI app listens on. |
| `WEBHOOK_SECRET` | — | Required with `WEBHOOK_URL` and on Vercel; ≥16 chars. |
| `AUTO_SET_WEBHOOK` | true on Vercel + URL | Register the webhook on first request. |
| `DATABASE_URL` | — | Postgres DSN (Neon/Vercel aliases also work). |
| `DATABASE_SSL` | auto | Force `true`/`false` for unusual setups. |
| `DB_POOL_SIZE` | `2` | asyncpg pool size. |
| `DB_PATH` | `converter_bot.db` | SQLite fallback path. |
| `DOTENV_PATH` | `<cwd>/.env` | Explicit dotenv file to load. |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, … |
| `MAX_WORKERS` | CPU-based | Converter thread-pool size. |
| `MAX_CONCURRENT_CONVERSIONS` | `4` | Per-process conversion semaphore. |
| `CONVERSION_TIMEOUT` | `240` | Seconds before a conversion is cancelled. |
| `MAX_INPUT_MB` | `0` | `0` = per-category limits. |
| `TEMP_DIR` | `/tmp` on Vercel | Scratch directory. |
| `ENABLE_MEDIA` | ffmpeg detection | Enable audio/video converters. |
| `FFMPEG_BIN` | `ffmpeg` | Explicit ffmpeg path. |
| `LOCAL_API_URL` | — | Self-hosted Bot API server (not Vercel). |
| `LOCAL_API_MODE` | `false` | Use PTB local mode with a local server. |
| `STORAGE_CHANNEL_ID` | `0` | Optional Library Bot storage channel. |
| `DEEPLINK_SECRET` | — | Required when `STORAGE_CHANNEL_ID` is set. |
| `RUN_MODE` | `polling` | `webhook` for a standalone webhook server. |
| `DROP_PENDING_UPDATES` | `false` | Drop queued updates on startup. |

---

## Database

Two tables are created automatically:

- `jobs` — conversion history, cache lookup (`input_hash` + `target_format` →
  `output_file_id`), status, errors.
- `pending_files` — short-lived tokens embedded in callback buttons. Rows are
  pruned after two days.

The Postgres DSN is normalised for asyncpg: `postgres://` is rewritten to
`postgresql://`, libpq-only parameters such as `sslmode` are translated, and
`statement_cache_size=0` keeps PgBouncer/Neon pooling compatible.

---

## Troubleshooting

| Symptom | Likely cause / fix |
| --- | --- |
| `BOT_TOKEN is not set` | Add it to `.env` or Vercel environment variables. |
| `WEBHOOK_SECRET must be set` | `WEBHOOK_URL` is configured without a secret. |
| Bot silent in production | Run `set_webhook.py --info`; check `last_error_message`; hit `/health`. |
| `401` on webhook | The `X-Telegram-Bot-Api-Secret-Token` header does not match. |
| `This selection expired` | Expected after a slow delivery/retry, or after the 2-day prune. Send the file again. |
| `File too large` | Telegram cloud downloads cap at 20 MB; use the Docker/local Bot API setup. |
| Audio/video buttons missing | ffmpeg is unavailable or `ENABLE_MEDIA` is off. |
| History disappears on Vercel | `DATABASE_URL` is not set, so SQLite lives in `/tmp`. |
| Function timeout | Lower `CONVERSION_TIMEOUT` or convert a smaller file; max duration is 300 s. |
| `DATABASE_URL is set but asyncpg is not installed` | Install `requirements.txt` (it includes asyncpg). |

## Security

- `.env`, `converter_bot/.env`, SQLite databases, and `bot_data/` are ignored by
  both Git and Vercel.
- Rotate any token or API hash that has ever been committed or shared.
- Keep `WEBHOOK_SECRET` secret; Telegram sends it in a header and the ASGI app
  rejects mismatches with `401`. On Vercel, webhook requests are rejected when
  no secret is configured, so preview deployments cannot be driven by anyone
  who guesses their URL.
- `DEEPLINK_SECRET` must match the Library bot's secret; deep links are
  HMAC-signed and rejected when unsigned.

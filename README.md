# Auto Export Demo

MVP Telegram bot for an auto export document workflow.

Architecture:

```text
Telegram group/private chat -> Railway bot -> Yandex Disk -> PostgreSQL
```

Optional processing:

```text
Railway bot -> Yandex Cloud Function -> PostgreSQL -> Telegram reply
```

The Railway bot listens for documents, downloads the original file from
Telegram, uploads the bytes to Yandex Disk, stores metadata in PostgreSQL, and
replies to Telegram.

OCR and LLM processing are intentionally not implemented yet.

## Stack

- Python 3.11+
- aiogram 3
- FastAPI + uvicorn
- PostgreSQL
- Yandex Disk REST API
- Optional Yandex Cloud Function
- Railway
- Telegram Mini App (HTML/CSS/JS)
## Supported Files

`pdf`, `jpg`, `jpeg`, `png`, `webp`, `heic`, `docx`, `xlsx`

## Environment Variables

Create a local `.env` file from `.env.example` or configure these variables in
Railway:

```bash
TELEGRAM_BOT_TOKEN=replace_me
DATABASE_URL=postgresql://user:password@host:5432/database
YANDEX_DISK_TOKEN=replace_me
YANDEX_DISK_BASE_PATH=/auto_export_demo
YANDEX_FUNCTION_URL=
ENABLE_PROCESSING=false
MINI_APP_BASE_URL=https://autoexportdemo-production.up.railway.app
MINI_APP_TOKEN_SECRET=replace-with-random-secret
WEB_HOST=0.0.0.0
WEB_PORT=8000
MINI_APP_TOKEN_TTL_SECONDS=900
TELEGRAM_INIT_DATA_MAX_AGE_SECONDS=900
```

Do not commit real secrets.

Port selection priority:

- On Railway the platform provides `PORT` (preferred).
- Locally you can set `WEB_PORT`.
- Resolution: PORT → WEB_PORT → 8000.

## Local Run

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

set -a
source .env
set +a

python -m app.main
```

The process starts aiogram polling and the FastAPI Mini App server together.

Health check:

```bash
curl http://127.0.0.1:8000/health
```

## Telegram Mini App

The specification Mini App lets users fill all vehicle fields on one screen
inside Telegram.

Endpoints:

- `GET /health` — liveness probe
- `GET /miniapp/specification?token=...` — HTML form
- `POST /api/specifications` — create specification

Requirements:

- `MINI_APP_BASE_URL` must be HTTPS and publicly reachable by Telegram
- set the same HTTPS URL in BotFather → Bot Settings → Menu Button / Web App domain as needed
- `MINI_APP_TOKEN_SECRET` must be a random secret and must not equal `TELEGRAM_BOT_TOKEN`

Private chat flow:

1. Open a customer card
2. Press **Добавить спецификацию**
3. Open **📝 Открыть форму спецификации**
4. Fill and save

Group chat flow:

1. Press **Добавить спецификацию** in the group
2. If the bot can DM you, the Mini App button arrives in private chat
3. Otherwise the group shows a deep-link button into the private chat
4. `/start spec_<short_code>` validates a one-time launch code and shows the Mini App button

Note: Telegram deep-link payloads are limited to 64 characters, so group links use a
short one-time code stored in PostgreSQL (`mini_app_launch_codes`), not the full signed token.

Fallback:

- `/add_specification` still runs the old step-by-step FSM
- field-by-field specification editing remains available from the customer card

Tests:

```bash
python -m unittest discover -s tests -v
```

## Railway

1. Create a Railway project.
2. Add a PostgreSQL database and copy its `DATABASE_URL`.
3. Add all required environment variables, including Mini App settings.
4. Expose the web port (`WEB_PORT` / `PORT`) so Telegram can open the Mini App URL.
5. Deploy the repo. `railpack.json` sets the start command:

```bash
python -m app.main
```

## Telegram Group Setup

Add the bot to the target Telegram group. If the bot must receive all group
messages, disable privacy mode for the bot in BotFather.

The bot also works in private chat.

## Contract Template Variables

`templates/customer_contract_template.docx` supports Jinja/docxtpl placeholders, including:

- `{{ customer.full_name }}` — full name: Last First Patronymic
- `{{ customer.short_name }}` — short name: Last F. P.
- `{{ customer.last_name }}`, `{{ customer.first_name }}`, `{{ customer.surname }}`

## Database

The bot creates a `documents` table with Telegram metadata, Yandex Disk path,
processing status, optional function response JSON, and timestamps.

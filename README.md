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
- PostgreSQL
- Yandex Disk REST API
- Optional Yandex Cloud Function
- Railway

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
```

Do not commit real secrets.

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

The bot creates the `documents` table automatically on startup.

If `ENABLE_PROCESSING=false`, the bot saves the file and replies:

```text
✅ Файл принят и сохранён. Автообработка пока отключена.
```

If `ENABLE_PROCESSING=true`, set `YANDEX_FUNCTION_URL`. The bot sends this JSON
payload to the function:

```json
{
  "document_id": 1,
  "file_path": "/auto_export_demo/chat/file.pdf",
  "original_filename": "file.pdf",
  "mime_type": "application/pdf",
  "telegram_chat_id": 123,
  "telegram_message_id": 456
}
```

## Railway

1. Create a Railway project.
2. Add a PostgreSQL database and copy its `DATABASE_URL`.
3. Add all required environment variables.
4. Deploy the repo. `railpack.json` sets the start command:

```bash
python -m app.main
```

## Telegram Group Setup

Add the bot to the target Telegram group. If the bot must receive all group
messages, disable privacy mode for the bot in BotFather.

The bot also works in private chat.

## Database

The bot creates a `documents` table with Telegram metadata, Yandex Disk path,
processing status, optional function response JSON, and timestamps.

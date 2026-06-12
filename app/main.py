import asyncio

from app.bot import run_bot
from app.config import load_settings


def main() -> None:
    settings = load_settings()
    asyncio.run(run_bot(settings))


if __name__ == "__main__":
    main()

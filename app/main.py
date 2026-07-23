import asyncio

from app.bot import run_application
from app.config import load_settings


def main() -> None:
    settings = load_settings()
    asyncio.run(run_application(settings))


if __name__ == "__main__":
    main()

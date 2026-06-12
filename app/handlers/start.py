from aiogram import Router
from aiogram.filters import CommandStart
from aiogram.types import Message


router = Router(name="start")


@router.message(CommandStart())
async def handle_start(message: Message) -> None:
    await message.answer(
        "Пришлите документ, и я сохраню оригинал в Yandex Disk и PostgreSQL."
    )

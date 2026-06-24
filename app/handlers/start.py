from aiogram import Router
from aiogram.filters import CommandStart
from aiogram.types import Message


router = Router(name="start")


@router.message(CommandStart())
async def handle_start(message: Message) -> None:
    await message.answer(
        "Команды:\n"
        "- /add_customer — добавить клиента по паспорту, СНИЛС, ИНН\n"
        "- /add_specification — добавить желаемый автомобиль клиента\n"
        "- /search_edit_customer — найти или изменить клиента\n"
        "- /recognize_document — распознать отдельный документ по машине/сделке"
    )

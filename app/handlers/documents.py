from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import Message


router = Router(name="documents")

NO_MODE_REPLY = (
    "Файл получен, но режим не выбран. Используйте /recognize_document "
    "для распознавания документа или /add_customer для добавления клиента."
)


@router.message(F.document)
@router.message(F.photo)
async def handle_document_without_mode(
    message: Message,
    state: FSMContext,
) -> None:
    current_state = await state.get_state()
    if current_state is not None:
        return

    await message.reply(NO_MODE_REPLY)

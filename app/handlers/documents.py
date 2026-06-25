from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import Message


router = Router(name="documents")


@router.message(F.document)
@router.message(F.photo)
async def handle_document_without_mode(
    message: Message,
    state: FSMContext,
) -> None:
    current_state = await state.get_state()
    if current_state is not None:
        return

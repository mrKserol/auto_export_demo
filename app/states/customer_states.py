from aiogram.fsm.state import State, StatesGroup


class CustomerAddStates(StatesGroup):
    collecting_files = State()
    waiting_phone = State()
    waiting_email = State()


class RecognizeDocumentStates(StatesGroup):
    waiting_document = State()


class CustomerEditStates(StatesGroup):
    waiting_passport = State()
    choosing_action = State()
    choosing_field = State()
    waiting_new_value = State()

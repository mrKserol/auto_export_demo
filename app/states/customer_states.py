from aiogram.fsm.state import State, StatesGroup


class CustomerAddStates(StatesGroup):
    waiting_passport_main = State()
    waiting_passport_confirmation = State()
    waiting_manual_passport_number = State()
    waiting_registration = State()
    waiting_snils = State()
    waiting_tin = State()
    waiting_phone = State()
    waiting_email = State()
    waiting_manual_registration_address = State()
    waiting_manual_snils = State()
    waiting_manual_tin = State()


class RecognizeDocumentStates(StatesGroup):
    waiting_document = State()


class CustomerEditStates(StatesGroup):
    waiting_passport = State()
    choosing_action = State()
    choosing_field = State()
    waiting_new_value = State()

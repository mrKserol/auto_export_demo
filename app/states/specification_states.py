from aiogram.fsm.state import State, StatesGroup


class SpecificationAddStates(StatesGroup):
    waiting_passport = State()
    waiting_brand = State()
    waiting_model = State()
    waiting_year = State()
    waiting_eng_capacity = State()
    waiting_eng_type = State()
    waiting_drive = State()
    waiting_transmission = State()
    waiting_color = State()
    waiting_complectation = State()
    waiting_mileage = State()
    waiting_price = State()
    confirm_replace = State()

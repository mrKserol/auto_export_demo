from aiogram.fsm.state import State, StatesGroup


class EstimateStates(StatesGroup):
    waiting_engine_power = State()
    waiting_exchange_rate = State()

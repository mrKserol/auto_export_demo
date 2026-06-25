from aiogram.fsm.state import State, StatesGroup


class EstimateStates(StatesGroup):
    waiting_engine_power = State()
    waiting_exchange_rate = State()
    waiting_inspect_transport_price = State()
    waiting_transit_declaration_price = State()
    waiting_insurance_shipment = State()
    waiting_custom_clearing = State()
    waiting_contractor_comission = State()

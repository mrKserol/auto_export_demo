from uuid import uuid4

import pytest

from app.services.intake_customer_service import IntakeCustomerService


class Repo:
    def __init__(self, session):
        self.session = session
        self.docs = [{"id": uuid4(), "storage_status": "stored", "storage_path": "/draft/a.jpg"}]
        self.events = []

    async def get_session(self, _id): return self.session
    async def list_documents(self, _id): return self.docs
    async def set_customer(self, _id, customer_id, status):
        self.session.update(customer_id=customer_id, status=status)
        return self.session
    async def link_documents_to_customer(self, *_):
        for doc in self.docs: doc["customer_id"] = self.session["customer_id"]
    async def update_document_storage(self, *, document_id, storage_path, storage_status):
        self.docs[0].update(storage_path=storage_path, storage_status=storage_status)
    async def add_audit_event(self, **kwargs): self.events.append(kwargs)


class Intake:
    async def validate_session_owner(self, *_args, **_kwargs): pass
    async def get_review_summary(self, _id):
        return {"effective_values": {"passport": "12 34-567890", "surname": "Иванов", "first_name": "Иван"}}


class DB:
    def __init__(self, existing=None): self.customer = existing; self.created = 0
    async def find_customer_by_normalized_passport(self, _passport): return self.customer
    async def create_customer_with_passport_lock(self, _passport, data):
        if self.customer: return self.customer, True
        self.created += 1
        self.customer = {"id": self.created, **data}
        return self.customer, False
    async def get_customer_by_id(self, _id): return self.customer
    async def update_customer_fields(self, customer_id, fields):
        self.customer.update(fields); self.customer["id"] = customer_id; return self.customer
    async def update_customer(self, customer_id, field, value):
        self.customer[field] = value; return self.customer


class Folder:
    async def create_unique_customer_folder(self, **_): return "/final/customer"


class Disk:
    async def move_resource(self, source, target, overwrite=False): return target


@pytest.mark.asyncio
async def test_unique_passport_creates_one_customer_and_links_docs():
    repo = Repo({"status": "collecting"})
    db = DB()
    service = IntakeCustomerService(database=db, repository=repo, intake_service=Intake(),
                                     customer_folder_service=Folder(), yandex_disk_client=Disk())
    result = await service.add_client(session_id=uuid4(), channel="telegram", external_user_id="1", conversation_id="2")
    assert result.status == "completed"
    assert db.created == 1
    assert repo.docs[0]["customer_id"] == 1
    assert repo.docs[0]["storage_path"] == "/final/customer/a.jpg"


@pytest.mark.asyncio
async def test_existing_passport_returns_duplicate_without_creating_customer():
    existing = {"id": 42, "passport": "1234567890"}
    repo = Repo({"status": "collecting"})
    db = DB(existing)
    service = IntakeCustomerService(database=db, repository=repo, intake_service=Intake())
    result = await service.add_client(session_id=uuid4(), channel="telegram", external_user_id="1", conversation_id="2")
    assert result.status == "duplicate"
    assert db.created == 0


@pytest.mark.asyncio
async def test_completed_session_is_idempotent():
    repo = Repo({"status": "completed", "customer_id": 7})
    db = DB({"id": 7, "passport": "123"})
    service = IntakeCustomerService(database=db, repository=repo, intake_service=Intake())
    result = await service.add_client(session_id=uuid4(), channel="telegram", external_user_id="1", conversation_id="2")
    assert result.status == "completed"
    assert db.created == 0

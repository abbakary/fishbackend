import os
import tempfile
from pathlib import Path
from uuid import uuid4

_test_db = Path(tempfile.gettempdir()) / f"fishtrade-test-{uuid4().hex}.db"
os.environ["DATABASE_URL"] = f"sqlite:///{_test_db.as_posix()}"
os.environ["JWT_SECRET"] = "test-secret-with-enough-entropy-123456789"

from fastapi.testclient import TestClient

from app.main import app


client = TestClient(app)


def create_account(username: str, branch_name: str):
    response = client.post("/api/v1/auth/register", json={
        "company": f"{username} Fisheries",
        "name": "Account Owner",
        "username": username,
        "password": "correct-horse-battery",
        "branches": [{"name": branch_name, "region": "Mwanza", "state": {"suppliers": []}}],
    })
    assert response.status_code == 201
    return response.json()


def test_health():
    assert client.get("/health").json()["status"] == "ok"


def test_register_login_and_branch_versioning():
    account = create_account("owner-test", "Kirumba")
    token = account["access_token"]
    branch_id = account["tenant"]["branches"][0]["id"]
    headers = {"Authorization": f"Bearer {token}"}

    current = client.get(f"/api/v1/branches/{branch_id}/state", headers=headers)
    assert current.status_code == 200
    assert current.json()["version"] == 1

    saved = client.put(f"/api/v1/branches/{branch_id}/state", headers=headers, json={
        "expected_version": 1,
        "state": {"suppliers": [{"id": "sup-1", "name": "Juma"}]},
    })
    assert saved.status_code == 200
    assert saved.json()["version"] == 2

    stale = client.put(f"/api/v1/branches/{branch_id}/state", headers=headers, json={
        "expected_version": 1,
        "state": {},
    })
    assert stale.status_code == 409

    login = client.post("/api/v1/auth/login", json={
        "username": "owner-test", "password": "correct-horse-battery",
    })
    assert login.status_code == 200
    assert login.json()["tenant"]["branches"][0]["id"] == branch_id


def test_tenant_isolation():
    first = create_account("owner-one", "Branch One")
    second = create_account("owner-two", "Branch Two")
    branch_id = first["tenant"]["branches"][0]["id"]
    response = client.get(
        f"/api/v1/branches/{branch_id}/state",
        headers={"Authorization": f"Bearer {second['access_token']}"},
    )
    assert response.status_code == 404


def test_registration_rejects_duplicate_username():
    create_account("owner-duplicate", "First Branch")
    response = client.post("/api/v1/auth/register", json={
        "company": "Second Fisheries",
        "name": "Another Owner",
        "username": "owner-duplicate",
        "password": "correct-horse-battery",
        "branches": [{"name": "Second Branch"}],
    })
    assert response.status_code == 409


def test_branch_collection_crud_and_audit():
    account = create_account("collection-owner", "Kirumba")
    branch_id = account["tenant"]["branches"][0]["id"]
    headers = {"Authorization": f"Bearer {account['access_token']}"}

    created = client.post(
        f"/api/v1/branches/{branch_id}/collections/suppliers",
        headers=headers,
        json={"expected_version": 1, "item": {"name": "Mzee Juma", "id": "sup-1"}},
    )
    assert created.status_code == 201
    assert created.json()["version"] == 2
    assert created.json()["item"]["id"] == "sup-1"

    listed = client.get(
        f"/api/v1/branches/{branch_id}/collections/suppliers", headers=headers
    )
    assert listed.status_code == 200
    assert listed.json()["items"] == [{"name": "Mzee Juma", "id": "sup-1"}]

    updated = client.patch(
        f"/api/v1/branches/{branch_id}/collections/suppliers/sup-1",
        headers=headers,
        json={"expected_version": 2, "patch": {"phone": "0754000000", "id": "changed"}},
    )
    assert updated.status_code == 200
    assert updated.json()["item"] == {
        "name": "Mzee Juma", "id": "sup-1", "phone": "0754000000"
    }

    deleted = client.delete(
        f"/api/v1/branches/{branch_id}/collections/suppliers/sup-1?expected_version=3",
        headers=headers,
    )
    assert deleted.status_code == 200
    assert client.get(
        f"/api/v1/branches/{branch_id}/collections/suppliers", headers=headers
    ).json()["items"] == []

    audit = client.get(f"/api/v1/branches/{branch_id}/audit", headers=headers)
    assert audit.status_code == 200
    assert [item["action"] for item in audit.json()["items"]] == [
        "branch.collection_item_deleted",
        "branch.collection_item_updated",
        "branch.collection_item_created",
    ]


def test_collection_rejects_unknown_names_and_stale_version():
    account = create_account("collection-stale", "Kirumba")
    branch_id = account["tenant"]["branches"][0]["id"]
    headers = {"Authorization": f"Bearer {account['access_token']}"}

    unknown = client.get(
        f"/api/v1/branches/{branch_id}/collections/not-a-real-collection",
        headers=headers,
    )
    assert unknown.status_code == 404

    created = client.post(
        f"/api/v1/branches/{branch_id}/collections/gearItems",
        headers=headers,
        json={"expected_version": 1, "item": {"name": "Net", "stock": 3}},
    )
    assert created.status_code == 201

    stale = client.post(
        f"/api/v1/branches/{branch_id}/collections/gearItems",
        headers=headers,
        json={"expected_version": 1, "item": {"name": "Rope", "stock": 4}},
    )
    assert stale.status_code == 409


def test_bootstrap_branch_and_staff_management():
    account = create_account("admin-owner", "Main Branch")
    headers = {"Authorization": f"Bearer {account['access_token']}"}

    boot = client.get("/api/v1/bootstrap", headers=headers)
    assert boot.status_code == 200
    branch_state = boot.json()["branches"][0]["state"]
    assert branch_state["suppliers"] == []
    assert branch_state["seq"] and branch_state["seasons"] and branch_state["fuelTanks"]

    branch = client.post("/api/v1/branches", headers=headers, json={
        "name": "Second Branch", "region": "Kigoma",
        "state": {"suppliers": [], "batches": []},
    })
    assert branch.status_code == 201
    assert branch.json()["name"] == "Second Branch"

    staff = client.post("/api/v1/staff", headers=headers, json={
        "username": "landing-user", "name": "Landing User",
        "password": "another-secure-password", "role": "Purchasing Officer",
        "branch_ids": [branch.json()["id"]],
    })
    assert staff.status_code == 201
    assert staff.json()["branch_ids"] == [branch.json()["id"]]
    assert client.get("/api/v1/staff", headers=headers).json()["items"]


def test_plan_limit_blocks_branch_creation():
    response = client.post("/api/v1/auth/register", json={
        "company": "Starter Fisheries", "name": "Starter Owner",
        "username": "starter-owner", "password": "correct-horse-battery",
        "plan": "Starter", "branches": [{"name": "Only Branch"}],
    })
    assert response.status_code == 201
    headers = {"Authorization": f"Bearer {response.json()['access_token']}"}
    second = client.post("/api/v1/branches", headers=headers, json={"name": "Too Many"})
    assert second.status_code == 409


def test_transactional_purchase_inspection_sale_and_stock_allocation():
    state = {
        "seq": {"pur": 1, "sal": 1}, "wh": ["Cold Room"],
        "suppliers": [{"id": "sup-1", "name": "Mzee Juma"}],
        "customers": [{"id": "cus-1", "name": "Factory"}],
        "batches": [], "pays": [], "sales": [],
    }
    account = client.post("/api/v1/auth/register", json={
        "company": "Trade Commands", "name": "Owner", "username": "trade-commands",
        "password": "correct-horse-battery", "branches": [{"name": "Landing", "state": state}],
    }).json()
    branch_id = account["tenant"]["branches"][0]["id"]
    headers = {"Authorization": f"Bearer {account['access_token']}"}

    purchase = client.post(
        f"/api/v1/branches/{branch_id}/commands/purchases?expected_version=1",
        headers=headers,
        json={"supplier_id": "sup-1", "species": "Nile Perch", "grade": "A",
              "kg": 100, "price_per_kg": 8000, "site": "Kirumba", "paid_now": 100000},
    )
    assert purchase.status_code == 200
    batch = purchase.json()["batch"]
    assert batch["st"] == "pending"
    assert purchase.json()["version"] == 2

    inspected = client.post(
        f"/api/v1/branches/{branch_id}/commands/batches/{batch['id']}/inspect?expected_version=2",
        headers=headers, json={"accepted_kg": 90, "temperature_c": 2.1},
    )
    assert inspected.status_code == 200
    assert inspected.json()["batch"]["rem"] == 90

    insufficient = client.post(
        f"/api/v1/branches/{branch_id}/commands/sales?expected_version=3",
        headers=headers,
        json={"customer_id": "cus-1", "species": "Nile Perch", "grade": "A",
              "kg": 91, "price_per_kg": 12000},
    )
    assert insufficient.status_code == 409
    assert insufficient.json()["detail"]["code"] == "insufficient_stock"

    sale = client.post(
        f"/api/v1/branches/{branch_id}/commands/sales?expected_version=3",
        headers=headers,
        json={"customer_id": "cus-1", "species": "Nile Perch", "grade": "A",
              "kg": 60, "price_per_kg": 12000, "paid_now": 100000},
    )
    assert sale.status_code == 200
    assert sale.json()["sale"]["alloc"] == [{"b": batch["id"], "kg": 60}]


def test_fuel_and_gear_commands_reject_overdraw_and_role_forbidden():
    state = {
        "seq": {"fuel": 1, "gear": 1}, "activeSeasonId": "season-1",
        "suppliers": [{"id": "sup-1", "name": "Fisherman"}],
        "boats": [{"id": "boat-1", "regNo": "FISH-001"}],
        "fuelTanks": [{"id": "tank-1", "fuel": "Petrol", "current": 10}],
        "fuelIssues": [], "fuelPayments": [], "boatCostLedger": [],
        "gearItems": [{"id": "gear-1", "name": "Net", "stock": 2, "sellPrice": 50000, "unit": "pcs"}],
        "gearIssues": [], "fishermanLedger": [],
    }
    account = client.post("/api/v1/auth/register", json={
        "company": "Stock Commands", "name": "Owner", "username": "stock-commands",
        "password": "correct-horse-battery", "branches": [{"name": "Landing", "state": state}],
    }).json()
    branch_id = account["tenant"]["branches"][0]["id"]
    headers = {"Authorization": f"Bearer {account['access_token']}"}

    fuel = client.post(
        f"/api/v1/branches/{branch_id}/commands/fuel-issues?expected_version=1",
        headers=headers,
        json={"supplier_id": "sup-1", "boat_id": "boat-1", "tank_id": "tank-1",
              "litres": 12, "price_per_litre": 3000},
    )
    assert fuel.status_code == 409
    assert fuel.json()["detail"]["code"] == "insufficient_fuel"

    gear = client.post(
        f"/api/v1/branches/{branch_id}/commands/gear-issues?expected_version=1",
        headers=headers,
        json={"supplier_id": "sup-1", "boat_id": "boat-1",
              "arrangement": "Fisherman-recoverable (Credit)",
              "items": [{"item_id": "gear-1", "quantity": 3}]},
    )
    assert gear.status_code == 409
    assert gear.json()["detail"]["code"] == "insufficient_gear_stock"

    # Landing officers cannot issue supplier payments.
    staff = client.post("/api/v1/staff", headers=headers, json={
        "username": "buyer", "name": "Buyer", "password": "another-secure-password",
        "role": "Purchasing Officer", "branch_ids": [branch_id],
    }).json()
    login = client.post("/api/v1/auth/login", json={
        "username": "buyer", "password": "another-secure-password",
    }).json()
    forbidden = client.post(
        f"/api/v1/branches/{branch_id}/commands/supplier-payments?expected_version=1",
        headers={"Authorization": f"Bearer {login['access_token']}"},
        json={"supplier_id": "sup-1", "amount": 1000, "method": "Cash"},
    )
    assert forbidden.status_code == 403


def test_purchase_payment_status_partial_unpaid_and_supplier_balance():
    state = {
        "seq": {"pur": 1, "set": 1}, "wh": ["Cold Room"],
        "suppliers": [{"id": "sup-1", "name": "Mzee Juma"}],
        "batches": [], "pays": [], "settlements": [], "fishermanLedger": [],
    }
    account = client.post("/api/v1/auth/register", json={
        "company": "Payment Status", "name": "Owner", "username": "payment-status",
        "password": "correct-horse-battery", "branches": [{"name": "Landing", "state": state}],
    }).json()
    branch_id = account["tenant"]["branches"][0]["id"]
    headers = {"Authorization": f"Bearer {account['access_token']}"}
    base = f"/api/v1/branches/{branch_id}/commands"

    def purchase(version, **overrides):
        body = {
            "supplier_id": "sup-1", "species": "Nile Perch", "grade": "A",
            "kg": 100, "price_per_kg": 8000, "site": "Kirumba",
        }
        body.update(overrides)
        return client.post(f"{base}/purchases?expected_version={version}",
                           headers=headers, json=body)

    unpaid = purchase(1)
    assert unpaid.status_code == 200
    assert unpaid.json()["payment_status"] == "unpaid"
    assert unpaid.json()["balance"] == 800000
    assert unpaid.json()["batch"]["bal"] == 800000
    assert unpaid.json()["supplier_balance"] == 800000

    partial = purchase(2, paid_now=100000)
    assert partial.status_code == 200
    assert partial.json()["payment_status"] == "partial"
    assert partial.json()["balance"] == 700000
    assert partial.json()["supplier_balance"] == 1500000

    overpay = purchase(3, paid_now=800001)
    assert overpay.status_code == 422
    assert overpay.json()["detail"] == "Payment cannot exceed net payable"

    recovered = purchase(3, kg=50, price_per_kg=8000, fuel_deducted=200000, paid_now=0)
    assert recovered.status_code == 200
    assert recovered.json()["payment_status"] == "unpaid"
    assert recovered.json()["net_payable"] == 200000
    assert recovered.json()["batch"]["ded"] == 200000
    assert recovered.json()["supplier_balance"] == 1700000

    state_now = client.get(f"/api/v1/branches/{branch_id}/state", headers=headers).json()["state"]
    assert state_now["settlements"][0]["purId"] == recovered.json()["batch"]["pur"]
    assert any(
        entry["source"] == "fuel" and entry["credit"] == 200000
        for entry in state_now["fishermanLedger"]
    )

    payment = client.post(
        f"{base}/supplier-payments?expected_version=4", headers=headers,
        json={"supplier_id": "sup-1", "amount": 1700000, "method": "Bank Transfer"},
    )
    assert payment.status_code == 200
    assert payment.json()["supplier_balance"] == 0

    nothing_owed = client.post(
        f"{base}/supplier-payments?expected_version=5", headers=headers,
        json={"supplier_id": "sup-1", "amount": 1, "method": "Cash"},
    )
    assert nothing_owed.status_code == 422
    assert nothing_owed.json()["detail"] == "Supplier has no outstanding balance"


def test_register_provisions_starter_state():
    account = client.post("/api/v1/auth/register", json={
        "company": "Starter State Fisheries", "name": "Owner",
        "username": "starter-state", "password": "correct-horse-battery",
        "branches": [{"name": "Fresh Branch", "region": "Musoma", "state": {}}],
    }).json()
    branch_id = account["tenant"]["branches"][0]["id"]
    headers = {"Authorization": f"Bearer {account['access_token']}"}

    state = client.get(f"/api/v1/branches/{branch_id}/state", headers=headers).json()["state"]
    for key in ("wh", "seq", "seasons", "activeSeasonId", "fuelTanks",
                "suppliers", "batches", "sales", "boats", "gearItems"):
        assert key in state, key
    assert state["suppliers"] == [] and state["seq"]["pur"] == 1
    assert state["activeSeasonId"] == state["seasons"][0]["id"]


def test_initialize_endpoint_applies_starter_once():
    from app.database import SessionLocal
    from app.models import Branch

    account = create_account("legacy-owner", "Legacy Branch")
    branch_id = account["tenant"]["branches"][0]["id"]
    headers = {"Authorization": f"Bearer {account['access_token']}"}

    with SessionLocal() as db:
        branch = db.get(Branch, branch_id)
        branch.state = {"suppliers": [{"id": "sup-legacy", "name": "Legacy"}]}
        branch.version = 5
        db.commit()

    first = client.post(f"/api/v1/branches/{branch_id}/initialize", headers=headers)
    assert first.status_code == 200
    assert first.json()["initialized"] is True
    assert first.json()["version"] == 6

    second = client.post(f"/api/v1/branches/{branch_id}/initialize", headers=headers)
    assert second.status_code == 200
    assert second.json()["initialized"] is False
    assert second.json()["version"] == 6

    state = client.get(f"/api/v1/branches/{branch_id}/state", headers=headers).json()["state"]
    assert state["suppliers"] == [{"id": "sup-legacy", "name": "Legacy"}]
    assert state["seq"] and state["seasons"]


def test_staff_patch_updates_password_and_enforces_guards():
    account = create_account("staff-patch-owner", "Patch Branch")
    branch_id = account["tenant"]["branches"][0]["id"]
    headers = {"Authorization": f"Bearer {account['access_token']}"}

    staff = client.post("/api/v1/staff", headers=headers, json={
        "username": "patch-user", "name": "Patch User",
        "password": "another-secure-password", "role": "Purchasing Officer",
        "branch_ids": [branch_id],
    }).json()

    staff_login = client.post("/api/v1/auth/login", json={
        "username": "patch-user", "password": "another-secure-password",
    }).json()
    forbidden = client.patch(
        f"/api/v1/staff/{staff['id']}",
        headers={"Authorization": f"Bearer {staff_login['access_token']}"},
        json={"name": "Nope"},
    )
    assert forbidden.status_code == 403

    reset = client.patch(
        f"/api/v1/staff/{staff['id']}", headers=headers,
        json={"password": "brand-new-password-123"},
    )
    assert reset.status_code == 200
    relogin = client.post("/api/v1/auth/login", json={
        "username": "patch-user", "password": "brand-new-password-123",
    })
    assert relogin.status_code == 200

    updated = client.patch(f"/api/v1/staff/{staff['id']}", headers=headers, json={
        "name": "Renamed User", "role": "Quality Inspector", "active": False,
    })
    assert updated.status_code == 200
    assert updated.json()["name"] == "Renamed User"
    assert updated.json()["role"] == "Quality Inspector"
    assert updated.json()["active"] is False
    assert client.post("/api/v1/auth/login", json={
        "username": "patch-user", "password": "brand-new-password-123",
    }).status_code == 403

    owner = next(item for item in client.get("/api/v1/staff", headers=headers).json()["items"]
                 if item["username"] == "staff-patch-owner")
    self_deactivate = client.patch(
        f"/api/v1/staff/{owner['id']}", headers=headers, json={"active": False},
    )
    assert self_deactivate.status_code == 422

    demote_last_owner = client.patch(
        f"/api/v1/staff/{owner['id']}", headers=headers,
        json={"role": "General Manager"},
    )
    assert demote_last_owner.status_code == 422

    assert client.patch(
        "/api/v1/staff/not-a-real-user", headers=headers, json={"name": "Ghost"},
    ).status_code == 404

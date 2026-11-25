import re
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, HTTPException, Query, status
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from .auth import Principal, current_principal, require_branch
from .config import settings
from .database import Base, engine, get_db
from .models import AuditEvent, Branch, Tenant, User
from .schemas import (
    BoatExpenseAllocCommand,
    BoatProjectCommand,
    BranchStateInput,
    CashAdvanceApproveCommand,
    CashAdvanceRepayCommand,
    CashAdvanceRequestCommand,
    CateringExpenseCommand,
    CollectionItemInput,
    CollectionItemPatch,
    CompliancePermitCommand,
    CookAdvanceCommand,
    CreateBranchInput,
    CreateStaffInput,
    CrewAssignmentCommand,
    DispatchSaleCommand,
    ExpenseCommand,
    FishermanExitCommand,
    FuelAdjustmentCommand,
    FuelDebtPaymentCommand,
    FuelIssueCommand,
    FuelReceiveCommand,
    FullSettlementCommand,
    GearIssueCommand,
    GearReturnCommand,
    InspectBatchCommand,
    KibaluaEngagementCommand,
    KibaluaSettleCommand,
    LoginInput,
    MaintDebtPaymentCommand,
    ProjectPaymentCommand,
    ProjectProgressCommand,
    PurchaseCommand,
    ReceivePaymentCommand,
    RegisterInput,
    SaleCommand,
    SeasonCommand,
    SetActiveSeasonCommand,
    SupplierPaymentCommand,
    TransferBatchCommand,
    TripCommand,
    UpdateStaffInput,
    WasteBatchCommand,
    WorkOrderCommand,
    WorkOrderStatusCommand,
)
from .security import create_access_token, hash_password, new_id, verify_password
from .starter import initial_state, needs_starter, starter_state

Base.metadata.create_all(bind=engine)


def _ensure_user_profile_columns():
    """Add profile columns to pre-existing SQLite databases without a migration tool."""
    if not settings.database_url.startswith("sqlite"):
        return
    with engine.begin() as conn:
        existing = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(users)")}
        for name, ddl in (
            ("phone", "ALTER TABLE users ADD COLUMN phone VARCHAR(40) NOT NULL DEFAULT ''"),
            ("permissions", "ALTER TABLE users ADD COLUMN permissions JSON NOT NULL DEFAULT '{}'"),
            ("module_keys", "ALTER TABLE users ADD COLUMN module_keys JSON NOT NULL DEFAULT '[]'"),
        ):
            if name not in existing:
                conn.exec_driver_sql(ddl)


_ensure_user_profile_columns()

# These names are shared by the web and Flutter domain stores. Values remain
# client-compatible JSON records while ownership and versioning are enforced
# by the API. Unknown keys are deliberately rejected by collection endpoints.
BRANCH_COLLECTIONS = {
    "suppliers", "batches", "pays", "customers", "sales", "exps", "trips",
    "comp", "seasons", "fishermanLedger", "boatCostLedger", "fuelTanks",
    "fuelSuppliers", "fuelPurchases", "fuelIssues", "fuelPayments",
    "fuelAdjustments", "boats", "spareParts", "maintenanceOrders",
    "maintPayments", "gearItems", "gearIssues", "cateringCooks",
    "cookAdvances", "cateringExpenses", "boatProjects", "advances",
    "settlements", "boatCrewAssignments", "kibaluaEngagements",
    "fullSettlements", "fishermanExitRecords", "boatExpenseAllocations", "audit",
}

PLAN_BRANCH_LIMITS = {"Starter": 1, "Business": 5, "Enterprise": 99}
PLAN_USER_LIMITS = {"Starter": 5, "Business": 25, "Enterprise": 999}

app = FastAPI(
    title="FishTrade Shared API",
    version="1.0.0",
    docs_url="/docs" if (settings.app_env != "production" or settings.enable_docs) else None,
    openapi_url="/openapi.json" if (settings.app_env != "production" or settings.enable_docs) else None,
    redoc_url=None,
)

# In development, allow all origins (via regex so credentials work in browsers & mobile)
if settings.app_env != "production":
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=r".*",
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
else:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "If-Match", "Idempotency-Key"],
    )


def tenant_payload(tenant: Tenant, user: User) -> dict:
    allowed = set(user.branch_ids or [])
    branches = [
        {"id": b.id, "name": b.name, "region": b.region, "version": b.version}
        for b in tenant.branches
        if user.role in {"Owner", "General Manager"} or b.id in allowed
    ]
    return {
        "id": tenant.id,
        "name": tenant.name,
        "slug": tenant.slug,
        "plan": tenant.plan,
        "status": tenant.status,
        "branches": branches,
    }


def user_payload(user: User) -> dict:
    return {
        "id": user.id,
        "username": user.username,
        "name": user.name,
        "role": user.role,
        "phone": user.phone or "",
        "permissions": user.permissions or {},
        "module_keys": user.module_keys or [],
        "branch_ids": user.branch_ids or [],
        "active": user.active,
    }


def _require_manager(principal: Principal):
    if principal.user.role not in {"Owner", "General Manager"}:
        raise HTTPException(status_code=403, detail="Owner or General Manager access required")


ROLE_ACTIONS = {
    "Owner": {"buy", "inspect", "pay", "sell", "fuel", "gear", "fleet"},
    "General Manager": {"buy", "inspect", "pay", "sell", "fuel", "gear", "fleet"},
    "Purchasing Officer": {"buy", "fuel", "gear"},
    "Quality Inspector": {"inspect"},
    "Storekeeper": {"inspect", "fuel", "gear", "fleet"},
    "Accountant": {"pay", "sell"},
    "Sales Officer": {"sell"},
    "Transport Officer": {"fuel"},
    "Auditor": set(),
}


def _require_action(principal: Principal, action: str):
    if action not in ROLE_ACTIONS.get(principal.user.role, set()):
        raise HTTPException(status_code=403, detail=f"Role cannot perform {action}")


def _state_list(state: dict, key: str) -> list[dict]:
    value = state.setdefault(key, [])
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise HTTPException(status_code=422, detail=f"Branch data collection {key} is invalid")
    return value


def _next_number(state: dict, key: str, prefix: str, width: int) -> str:
    seq = state.setdefault("seq", {})
    if not isinstance(seq, dict):
        seq = state["seq"] = {}
    number = int(seq.get(key, 1))
    seq[key] = number + 1
    return f"{prefix}{number:0{width}d}"


def _batch_deduction(state: dict, batch: dict) -> float:
    if "ded" in batch:
        return float(batch.get("ded") or 0)
    purchase_ref = batch.get("pur")
    total = 0.0
    for settlement in state.get("settlements") or []:
        if not isinstance(settlement, dict):
            continue
        if purchase_ref and settlement.get("purId") in (purchase_ref, batch.get("id")):
            total += float(settlement.get("fuelDeducted", 0)) + float(settlement.get("maintDeducted", 0))
    return total


def _supplier_outstanding(state: dict, supplier_id: str) -> float:
    purchased = 0.0
    for batch in _state_list(state, "batches"):
        if batch.get("sup") != supplier_id:
            continue
        gross = float(batch.get("kg", 0)) * float(batch.get("price", 0))
        purchased += max(0.0, gross - _batch_deduction(state, batch))
    paid = sum(
        float(payment.get("amt", 0))
        for payment in _state_list(state, "pays")
        if payment.get("sup") == supplier_id
    )
    return round(purchased - paid, 2)


def _ledger_entry(state: dict, *, supplier_id: str, boat_id: str, kind: str,
                  debit: float, credit: float, source: str, reference: str,
                  notes: str, user_name: str):
    _state_list(state, "fishermanLedger").insert(0, {
        "id": new_id(), "supId": supplier_id, "boatId": boat_id,
        "seasonId": state.get("activeSeasonId", ""),
        "date": datetime.now(timezone.utc).date().isoformat(),
        "type": kind, "debit": debit, "credit": credit, "source": source,
        "ref": reference, "notes": notes, "createdBy": user_name,
        "status": "Approved", "createdAt": datetime.now(timezone.utc).isoformat(),
    })


def _commit_domain_command(*, branch: Branch, state: dict, expected_version: int,
                           principal: Principal, db: Session, action: str,
                           details: dict, result: dict):
    version = _write_branch_state(
        branch=branch, state=state, expected_version=expected_version,
        principal=principal, db=db, action=f"domain.{action}", details=details,
    )
    return {**result, "version": version}


@app.get("/health")
def health():
    return {"status": "ok", "service": "fishtrade-api"}


def _pick(obj, *names, default=""):
    """Return the first non-empty attribute of obj among names (RegisterInput field tolerance)."""
    for name in names:
        value = getattr(obj, name, None)
        if value not in (None, ""):
            return value
    return default


def _as_dict(item) -> dict:
    if isinstance(item, dict):
        return item
    return item.model_dump() if hasattr(item, "model_dump") else dict(vars(item))


@app.post("/api/v1/auth/register", status_code=201)
def register(payload: RegisterInput, db: Session = Depends(get_db)):
    """Create tenant + branches + owner user + audit event in ONE transaction."""
    username = str(_pick(payload, "username", "email")).strip().lower()
    password = _pick(payload, "password")
    company = str(_pick(payload, "company", "business_name", "tenant_name", "company_name")).strip()
    owner_name = str(_pick(payload, "name", "full_name", "owner_name", default=username)).strip()
    phone = str(_pick(payload, "phone", default="")).strip()
    plan = str(_pick(payload, "plan", default="Starter")).strip()
    if plan not in PLAN_BRANCH_LIMITS:
        plan = "Starter"

    branch_inputs = [_as_dict(b) for b in (getattr(payload, "branches", None) or [])]
    if not branch_inputs:
        branch_inputs = [{"name": "Main Branch", "region": "", "state": {}}]
    if len(branch_inputs) > PLAN_BRANCH_LIMITS[plan]:
        raise HTTPException(status_code=422, detail=f"The {plan} plan allows {PLAN_BRANCH_LIMITS[plan]} branch(es)")

    if not username or not password or not company:
        raise HTTPException(status_code=422, detail="Username, password and company are required")
    if db.query(User).filter(User.username == username).first():
        raise HTTPException(status_code=409, detail="Username already exists")

    base_slug = re.sub(r"[^a-z0-9]+", "-", company.lower()).strip("-") or "tenant"
    slug, n = base_slug, 1
    while db.query(Tenant).filter(Tenant.slug == slug).first():
        n += 1
        slug = f"{base_slug}-{n}"

    try:
        tenant = Tenant(id=new_id(), name=company, slug=slug, plan=plan, status="active")
        db.add(tenant)
        db.flush()  # tenant row must exist before anything references it

        branch_ids = []
        for b in branch_inputs:
            region = str(b.get("region") or "").strip()
            branch = Branch(
                id=new_id(), tenant_id=tenant.id,
                name=str(b.get("name") or "Main Branch").strip(), region=region,
                state=initial_state(b.get("state") or None, region),
            )
            db.add(branch)
            branch_ids.append(branch.id)
        db.flush()

        user = User(
            id=new_id(), tenant_id=tenant.id, username=username, name=owner_name,
            password_hash=hash_password(password), role="Owner", phone=phone,
            permissions={}, module_keys=[], branch_ids=branch_ids, active=True,
        )
        db.add(user)
        db.flush()

        db.add(AuditEvent(
            id=new_id(), tenant_id=tenant.id, user_id=user.id,
            action="tenant.registered", details={"branch_count": len(branch_ids)},
        ))
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        if getattr(exc.orig, "pgcode", None) == "23505":  # real duplicate (username/slug)
            raise HTTPException(status_code=409, detail="Account already exists") from exc
        raise  # FK/other errors are server bugs: log as 500, not a fake 409

    db.refresh(tenant)
    return {
        "access_token": create_access_token(user_id=user.id, tenant_id=tenant.id),
        "token_type": "bearer",
        "user": user_payload(user),
        "tenant": tenant_payload(tenant, user),
    }


@app.post("/api/v1/auth/login")
def login(payload: LoginInput, db: Session = Depends(get_db)):
    user = (
        db.query(User)
        .options(joinedload(User.tenant).joinedload(Tenant.branches))
        .filter(User.username == payload.username.strip().lower())
        .first()
    )
    if user is None or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Incorrect username or password")
    if not user.active or user.tenant.status == "suspended":
        raise HTTPException(status_code=403, detail="Account is inactive")
    db.add(AuditEvent(id=new_id(), tenant_id=user.tenant_id, user_id=user.id, action="auth.login", details={}))
    db.commit()
    return {"access_token": create_access_token(user_id=user.id, tenant_id=user.tenant_id),
            "token_type": "bearer", "user": user_payload(user), "tenant": tenant_payload(user.tenant, user)}


@app.get("/api/v1/auth/me")
def me(principal: Principal = Depends(current_principal)):
    return {"user": user_payload(principal.user), "tenant": tenant_payload(principal.tenant, principal.user)}


@app.get("/api/v1/bootstrap")
def bootstrap(principal: Principal = Depends(current_principal)):
    """Return the signed-in user's metadata and accessible branch snapshots."""
    allowed = set(principal.user.branch_ids or [])
    branches = [
        {
            "id": branch.id,
            "name": branch.name,
            "region": branch.region,
            "version": branch.version,
            "state": branch.state or {},
        }
        for branch in principal.tenant.branches
        if principal.user.role in {"Owner", "General Manager"} or branch.id in allowed
    ]
    return {
        "user": user_payload(principal.user),
        "tenant": tenant_payload(principal.tenant, principal.user),
        "staff": [
            user_payload(user) for user in principal.tenant.users
        ] if principal.user.role in {"Owner", "General Manager"} else [user_payload(principal.user)],
        "branches": branches,
    }


@app.post("/api/v1/branches", status_code=201)
def create_branch(
    payload: CreateBranchInput,
    principal: Principal = Depends(current_principal),
    db: Session = Depends(get_db),
):
    _require_manager(principal)
    limit = PLAN_BRANCH_LIMITS.get(principal.tenant.plan, 1)
    if len(principal.tenant.branches) >= limit:
        raise HTTPException(status_code=409, detail="Branch limit for current plan reached")
    branch = Branch(
        id=new_id(), tenant_id=principal.tenant.id,
        name=payload.name.strip(), region=payload.region.strip(),
        state=initial_state(payload.state, payload.region),
    )
    db.add(branch)
    db.flush()  # branch row must exist before the audit event references it
    db.add(AuditEvent(
        id=new_id(), tenant_id=principal.tenant.id, branch_id=branch.id,
        user_id=principal.user.id, action="branch.created",
        details={"name": branch.name, "region": branch.region},
    ))
    db.commit()
    return {"id": branch.id, "name": branch.name, "region": branch.region,
            "version": branch.version, "state": branch.state}


@app.get("/api/v1/staff")
def list_staff(principal: Principal = Depends(current_principal)):
    _require_manager(principal)
    return {"items": [user_payload(user) for user in principal.tenant.users]}


@app.post("/api/v1/staff", status_code=201)
def create_staff(
    payload: CreateStaffInput,
    principal: Principal = Depends(current_principal),
    db: Session = Depends(get_db),
):
    _require_manager(principal)
    if payload.role in {"Owner", "Platform Admin"}:
        raise HTTPException(status_code=422, detail="This role cannot be assigned through staff creation")
    user_limit = PLAN_USER_LIMITS.get(principal.tenant.plan, 5)
    if len(principal.tenant.users) >= user_limit:
        raise HTTPException(status_code=409, detail="User limit for current plan reached")
    username = payload.username
    if db.query(User).filter(User.username == username).first():
        raise HTTPException(status_code=409, detail="Username already exists")
    available_branches = {branch.id for branch in principal.tenant.branches}
    if not set(payload.branch_ids).issubset(available_branches):
        raise HTTPException(status_code=422, detail="One or more branches are outside this tenant")
    user = User(
        id=new_id(), tenant_id=principal.tenant.id, username=username,
        name=payload.name.strip(), password_hash=hash_password(payload.password),
        role=payload.role, phone=payload.phone.strip(),
        permissions=payload.permissions, module_keys=payload.module_keys,
        branch_ids=payload.branch_ids, active=True,
    )
    db.add(user)
    db.flush()  # user row first, in case audit_events.user_id is a foreign key
    db.add(AuditEvent(
        id=new_id(), tenant_id=principal.tenant.id, user_id=principal.user.id,
        action="staff.created", details={"staff_id": user.id, "role": user.role},
    ))
    db.commit()
    return user_payload(user)


@app.patch("/api/v1/staff/{user_id}")
def update_staff(
    user_id: str,
    payload: UpdateStaffInput,
    principal: Principal = Depends(current_principal),
    db: Session = Depends(get_db),
):
    _require_manager(principal)
    user = next((item for item in principal.tenant.users if item.id == user_id), None)
    if user is None:
        raise HTTPException(status_code=404, detail="Staff member not found")
    if user.role == "Owner" and principal.user.id != user.id:
        raise HTTPException(status_code=403, detail="Only the owner account can modify another owner")

    changes: dict = {}
    if payload.name is not None:
        user.name = payload.name.strip()
        changes["name"] = user.name
    if payload.phone is not None:
        user.phone = payload.phone.strip()
        changes["phone"] = user.phone
    if payload.permissions is not None:
        user.permissions = payload.permissions
        changes["permissions"] = True
    if payload.module_keys is not None:
        user.module_keys = payload.module_keys
        changes["module_keys"] = True
    if payload.role is not None:
        if user.role == "Owner" and payload.role != "Owner":
            owners = [item for item in principal.tenant.users
                      if item.role == "Owner" and item.id != user.id and item.active]
            if not owners:
                raise HTTPException(status_code=422, detail="Tenant must keep at least one active owner")
        if payload.role in {"Owner", "Platform Admin"} and principal.user.role != "Owner":
            raise HTTPException(status_code=403, detail="Only the owner can assign this role")
        user.role = payload.role
        changes["role"] = user.role
    if payload.branch_ids is not None:
        available = {branch.id for branch in principal.tenant.branches}
        if not set(payload.branch_ids).issubset(available):
            raise HTTPException(status_code=422, detail="One or more branches are outside this tenant")
        if user.role == "Owner":
            user.branch_ids = [branch.id for branch in principal.tenant.branches]
        else:
            user.branch_ids = payload.branch_ids
        changes["branch_ids"] = user.branch_ids
    if payload.active is not None:
        if user.id == principal.user.id:
            raise HTTPException(status_code=422, detail="You cannot deactivate your own account")
        if not payload.active and user.role == "Owner":
            owners = [item for item in principal.tenant.users
                      if item.role == "Owner" and item.id != user.id and item.active]
            if not owners:
                raise HTTPException(status_code=422, detail="Tenant must keep at least one active owner")
        user.active = payload.active
        changes["active"] = user.active
    if payload.password is not None:
        user.password_hash = hash_password(payload.password)
        changes["password"] = "reset"

    db.add(AuditEvent(
        id=new_id(), tenant_id=principal.tenant.id, user_id=principal.user.id,
        action="staff.updated", details={"staff_id": user.id, **changes},
    ))
    db.commit()
    return user_payload(user)


@app.get("/api/v1/branches/{branch_id}/state")
def get_branch_state(
    branch_id: str,
    principal: Principal = Depends(current_principal),
):
    branch = require_branch(principal, branch_id)
    return {"branch_id": branch.id, "version": branch.version, "updated_at": branch.updated_at,
            "state": branch.state or {}}


@app.post("/api/v1/branches/{branch_id}/initialize", status_code=200)
def initialize_branch_state(
    branch_id: str,
    principal: Principal = Depends(current_principal),
    db: Session = Depends(get_db),
):
    """Apply the structural starter template to an existing uninitialized branch."""
    _require_manager(principal)
    branch = require_branch(principal, branch_id)
    state = dict(branch.state or {})
    if not needs_starter(state):
        return {"branch_id": branch.id, "version": branch.version, "initialized": False}
    state = {**starter_state(branch.region), **state}
    updated = db.execute(
        update(Branch)
        .where(Branch.id == branch.id, Branch.tenant_id == principal.tenant.id)
        .values(state=state, version=Branch.version + 1, updated_at=datetime.now(timezone.utc))
    )
    if updated.rowcount != 1:
        db.rollback()
        raise HTTPException(status_code=409, detail="Could not initialize branch")
    db.add(AuditEvent(
        id=new_id(), tenant_id=principal.tenant.id, branch_id=branch.id,
        user_id=principal.user.id, action="branch.initialized", details={},
    ))
    db.commit()
    db.refresh(branch)
    return {"branch_id": branch.id, "version": branch.version, "initialized": True}


@app.put("/api/v1/branches/{branch_id}/state")
def put_branch_state(
    branch_id: str,
    payload: BranchStateInput,
    principal: Principal = Depends(current_principal),
    db: Session = Depends(get_db),
):
    branch = require_branch(principal, branch_id)
    updated = db.execute(
        update(Branch)
        .where(
            Branch.id == branch_id,
            Branch.tenant_id == principal.tenant.id,
            Branch.version == payload.expected_version,
        )
        .values(state=payload.state, version=Branch.version + 1,
                updated_at=datetime.now(timezone.utc))
    )
    if updated.rowcount != 1:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail={"code": "version_conflict", "message": "Branch data changed elsewhere. Reload before saving.",
                    "current_version": branch.version},
        )
    db.add(AuditEvent(
        id=new_id(), tenant_id=principal.tenant.id, branch_id=branch_id,
        user_id=principal.user.id, action="branch.state_updated",
        details={"from_version": payload.expected_version, "to_version": payload.expected_version + 1},
    ))
    db.commit()
    return {"branch_id": branch_id, "version": payload.expected_version + 1}


def _collection_name(name: str) -> str:
    if name not in BRANCH_COLLECTIONS:
        raise HTTPException(status_code=404, detail="Unknown branch collection")
    return name


def _write_branch_state(
    *, branch: Branch, state: dict, expected_version: int,
    principal: Principal, db: Session, action: str, details: dict,
) -> int:
    result = db.execute(
        update(Branch)
        .where(
            Branch.id == branch.id,
            Branch.tenant_id == principal.tenant.id,
            Branch.version == expected_version,
        )
        .values(
            state=state,
            version=Branch.version + 1,
            updated_at=datetime.now(timezone.utc),
        )
    )
    if result.rowcount != 1:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail={"code": "version_conflict", "message": "Branch data changed elsewhere. Reload before saving."},
        )
    db.add(AuditEvent(
        id=new_id(), tenant_id=principal.tenant.id, branch_id=branch.id,
        user_id=principal.user.id, action=action, details=details,
    ))
    db.commit()
    return expected_version + 1


@app.get("/api/v1/branches/{branch_id}/collections/{collection}")
def list_branch_collection(
    branch_id: str,
    collection: str,
    principal: Principal = Depends(current_principal),
):
    branch = require_branch(principal, branch_id)
    key = _collection_name(collection)
    records = (branch.state or {}).get(key, [])
    if not isinstance(records, list):
        raise HTTPException(status_code=422, detail=f"{key} is not a list in this branch state")
    return {"branch_id": branch.id, "collection": key,
            "version": branch.version, "items": records}


@app.post("/api/v1/branches/{branch_id}/collections/{collection}", status_code=201)
def create_branch_collection_item(
    branch_id: str,
    collection: str,
    payload: CollectionItemInput,
    principal: Principal = Depends(current_principal),
    db: Session = Depends(get_db),
):
    branch = require_branch(principal, branch_id)
    db.refresh(branch)
    key = _collection_name(collection)
    state = dict(branch.state or {})
    items = list(state.get(key, []))
    if any(not isinstance(item, dict) for item in items):
        raise HTTPException(status_code=422, detail=f"{key} must contain JSON objects")
    new_item = dict(payload.item)
    new_item.setdefault("id", new_id())
    if any(item.get("id") == new_item["id"] for item in items):
        raise HTTPException(status_code=409, detail="An item with this id already exists")
    items.insert(0, new_item)
    state[key] = items
    version = _write_branch_state(
        branch=branch, state=state, expected_version=branch.version,
        principal=principal, db=db, action="branch.collection_item_created",
        details={"collection": key, "item_id": new_item["id"]},
    )
    return {"item": new_item, "version": version}


@app.patch("/api/v1/branches/{branch_id}/collections/{collection}/{item_id}")
def update_branch_collection_item(
    branch_id: str,
    collection: str,
    item_id: str,
    payload: CollectionItemPatch,
    principal: Principal = Depends(current_principal),
    db: Session = Depends(get_db),
):
    branch = require_branch(principal, branch_id)
    db.refresh(branch)
    key = _collection_name(collection)
    state = dict(branch.state or {})
    items = list(state.get(key, []))
    index = next((i for i, item in enumerate(items)
                  if isinstance(item, dict) and str(item.get("id")) == item_id), None)
    if index is None:
        raise HTTPException(status_code=404, detail="Item not found")
    patch = dict(payload.patch)
    patch.pop("id", None)
    updated_item = {**items[index], **patch, "id": items[index]["id"]}
    items[index] = updated_item
    state[key] = items
    version = _write_branch_state(
        branch=branch, state=state, expected_version=branch.version,
        principal=principal, db=db, action="branch.collection_item_updated",
        details={"collection": key, "item_id": item_id},
    )
    return {"item": updated_item, "version": version}


@app.delete("/api/v1/branches/{branch_id}/collections/{collection}/{item_id}")
def delete_branch_collection_item(
    branch_id: str,
    collection: str,
    item_id: str,
    expected_version: int = Query(ge=1),
    principal: Principal = Depends(current_principal),
    db: Session = Depends(get_db),
):
    branch = require_branch(principal, branch_id)
    key = _collection_name(collection)
    state = dict(branch.state or {})
    items = list(state.get(key, []))
    filtered = [item for item in items
                if not (isinstance(item, dict) and str(item.get("id")) == item_id)]
    if len(filtered) == len(items):
        raise HTTPException(status_code=404, detail="Item not found")
    state[key] = filtered
    version = _write_branch_state(
        branch=branch, state=state, expected_version=expected_version,
        principal=principal, db=db, action="branch.collection_item_deleted",
        details={"collection": key, "item_id": item_id},
    )
    return {"deleted": True, "version": version}


@app.get("/api/v1/branches/{branch_id}/audit")
def list_branch_audit(
    branch_id: str,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    principal: Principal = Depends(current_principal),
    db: Session = Depends(get_db),
):
    branch = require_branch(principal, branch_id)
    events = (
        db.query(AuditEvent)
        .filter(
            AuditEvent.tenant_id == principal.tenant.id,
            AuditEvent.branch_id == branch.id,
        )
        .order_by(AuditEvent.created_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    return {"items": [{
        "id": event.id,
        "user_id": event.user_id,
        "action": event.action,
        "details": event.details,
        "created_at": event.created_at,
    } for event in events]}


@app.post("/api/v1/branches/{branch_id}/commands/purchases")
def command_purchase(
    branch_id: str, payload: PurchaseCommand, expected_version: int = Query(ge=1),
    principal: Principal = Depends(current_principal), db: Session = Depends(get_db),
):
    _require_action(principal, "buy")
    branch = require_branch(principal, branch_id)
    state = dict(branch.state or {})
    suppliers = _state_list(state, "suppliers")

    is_company_catch = (payload.source_type == "company_managed")

    sup = next((s for s in suppliers if s.get("id") == payload.supplier_id), None)
    if not sup:
        if is_company_catch:
            sup = next((s for s in suppliers if s.get("source_type") == "internal" or s.get("cat") == "Company-managed Fishermen"), None)
            if not sup:
                sup_id = payload.supplier_id or "sup_internal_fleet"
                sup = {
                    "id": sup_id,
                    "no": "INT-001",
                    "name": "Wavuvi wa Kampuni (Company Fleet)",
                    "phone": "",
                    "site": payload.site,
                    "cat": "Company-managed Fishermen",
                    "source_type": "internal",
                    "boat": "",
                    "status": "Active",
                }
                suppliers.append(sup)
            payload.supplier_id = sup.get("id")
        else:
            raise HTTPException(status_code=404, detail="Supplier not found")

    total = payload.kg * payload.price_per_kg
    deductions = payload.fuel_deducted + payload.maint_deducted
    if deductions > total + 1e-6:
        raise HTTPException(status_code=422, detail="Deductions cannot exceed purchase total")
    net_payable = max(0.0, total - deductions)

    if is_company_catch:
        balance = 0.0
        payment_status = "internal_catch"
    else:
        if payload.paid_now > net_payable + 1e-6:
            raise HTTPException(status_code=422, detail="Payment cannot exceed net payable")
        balance = max(0.0, net_payable - payload.paid_now)
        payment_status = "paid" if balance <= 0.005 else ("unpaid" if payload.paid_now <= 0 else "partial")

    pur_prefix = "CAT-" if is_company_catch else "PUR-"
    pur_seq_key = "cat" if is_company_catch else "pur"
    pur_no = _next_number(state, pur_seq_key, pur_prefix, 6)
    batch_no = f"BATCH-{len(_state_list(state, 'batches')) + 1:06d}"
    now = datetime.now(timezone.utc)
    batch = {
        "id": new_id(), "no": batch_no, "pur": pur_no,
        "sup": payload.supplier_id,
        "sourceType": payload.source_type,
        "boatId": payload.boat_id,
        "fishermanId": payload.fisherman_id,
        "fishermanName": payload.fisherman_name,
        "sp": payload.species, "grade": payload.grade,
        "kg": payload.kg, "rem": 0, "price": payload.price_per_kg,
        "date": now.date().isoformat(), "at": now.isoformat(), "site": payload.site,
        "wh": payload.warehouse or (state.get("wh") or ["Cold Room"])[0],
        "st": "pending", "rej": 0, "temp": 0, "waste": 0,
        "ded": deductions, "paid": payload.paid_now,
        "bal": balance, "payStatus": payment_status,
    }
    _state_list(state, "batches").insert(0, batch)

    if payload.boat_id:
        boats = _state_list(state, "boats")
        for b in boats:
            if b.get("id") == payload.boat_id or b.get("name") == payload.boat_id or b.get("code") == payload.boat_id:
                b["totalKg"] = float(b.get("totalKg", 0) or 0) + payload.kg
                b["totalTrips"] = int(b.get("totalTrips", 0) or 0) + 1
                break

    if payload.fuel_deducted > 0:
        _ledger_entry(
            state, supplier_id=payload.supplier_id,
            boat_id=payload.boat_id or state.get("activeBoatId", ""), kind="Purchase Deduction",
            debit=0, credit=payload.fuel_deducted, source="fuel",
            reference=pur_no, notes="Fuel debt recovered from catch payout",
            user_name=principal.user.name,
        )
    if payload.maint_deducted > 0:
        _ledger_entry(
            state, supplier_id=payload.supplier_id,
            boat_id=payload.boat_id or state.get("activeBoatId", ""), kind="Purchase Deduction",
            debit=0, credit=payload.maint_deducted, source="maintenance",
            reference=pur_no, notes="Maintenance debt recovered from catch payout",
            user_name=principal.user.name,
        )
    if deductions > 0:
        _state_list(state, "settlements").insert(0, {
            "id": new_id(), "no": _next_number(state, "set", "SET-", 6),
            "purId": pur_no, "supId": payload.supplier_id,
            "fishTotal": total, "fuelDeducted": payload.fuel_deducted,
            "maintDeducted": payload.maint_deducted, "netPayable": net_payable,
            "method": payload.payment_method, "notes": "Credit recovery at catch/purchase",
            "date": now.date().isoformat(), "at": now.isoformat(),
        })
    if payload.paid_now > 0:
        _state_list(state, "pays").insert(0, {
            "id": new_id(), "sup": payload.supplier_id, "amt": payload.paid_now,
            "m": payload.payment_method, "date": now.date().isoformat(),
            "ref": f"PAY-{new_id()[:8]}", "pur": pur_no,
            "notes": "Crew landing allowance" if is_company_catch else "Purchase disbursement",
        })
    return _commit_domain_command(
        branch=branch, state=state, expected_version=expected_version,
        principal=principal, db=db, action="purchase_recorded",
        details={"purchase": pur_no, "batch": batch_no, "kg": payload.kg, "source_type": payload.source_type},
        result={
            "batch": batch, "purchase_total": total,
            "deductions": deductions, "net_payable": net_payable,
            "paid_now": payload.paid_now, "balance": balance,
            "payment_status": payment_status,
            "supplier_balance": _supplier_outstanding(state, payload.supplier_id),
        },
    )


@app.post("/api/v1/branches/{branch_id}/commands/batches/{batch_id}/inspect")
def command_inspect_batch(
    branch_id: str, batch_id: str, payload: InspectBatchCommand,
    expected_version: int = Query(ge=1), principal: Principal = Depends(current_principal),
    db: Session = Depends(get_db),
):
    _require_action(principal, "inspect")
    branch = require_branch(principal, branch_id)
    state = dict(branch.state or {})
    batches = _state_list(state, "batches")
    batch = next((item for item in batches if item.get("id") == batch_id), None)
    if batch is None:
        raise HTTPException(status_code=404, detail="Batch not found")
    if payload.accepted_kg > float(batch.get("kg", 0)):
        raise HTTPException(status_code=422, detail="Accepted weight cannot exceed gross weight")
    batch.update({
        "rem": payload.accepted_kg,
        "rej": float(batch.get("kg", 0)) - payload.accepted_kg,
        "temp": payload.temperature_c,
        "reason": payload.reason,
        "st": "accepted" if payload.accepted_kg > 0 else "rejected",
        "insp": datetime.now(timezone.utc).date().isoformat(),
    })
    return _commit_domain_command(
        branch=branch, state=state, expected_version=expected_version,
        principal=principal, db=db, action="batch_inspected",
        details={"batch_id": batch_id, "accepted_kg": payload.accepted_kg},
        result={"batch": batch},
    )


@app.post("/api/v1/branches/{branch_id}/commands/supplier-payments")
def command_supplier_payment(
    branch_id: str, payload: SupplierPaymentCommand,
    expected_version: int = Query(ge=1), principal: Principal = Depends(current_principal),
    db: Session = Depends(get_db),
):
    _require_action(principal, "pay")
    branch = require_branch(principal, branch_id)
    state = dict(branch.state or {})
    if not any(item.get("id") == payload.supplier_id for item in _state_list(state, "suppliers")):
        raise HTTPException(status_code=404, detail="Supplier not found")
    outstanding = _supplier_outstanding(state, payload.supplier_id)
    if outstanding <= 0.005:
        raise HTTPException(status_code=422, detail="Supplier has no outstanding balance")
    if payload.amount > outstanding + 0.005:
        raise HTTPException(status_code=422, detail="Payment cannot exceed outstanding balance")
    payment = {
        "id": new_id(), "sup": payload.supplier_id, "amt": payload.amount,
        "m": payload.method, "date": datetime.now(timezone.utc).date().isoformat(),
        "ref": payload.reference or f"PAY-{new_id()[:8]}", "notes": payload.notes,
    }
    _state_list(state, "pays").insert(0, payment)
    return _commit_domain_command(
        branch=branch, state=state, expected_version=expected_version,
        principal=principal, db=db, action="supplier_payment_recorded",
        details={"payment_id": payment["id"], "amount": payload.amount},
        result={
            "payment": payment,
            "supplier_balance": round(outstanding - payload.amount, 2),
        },
    )


@app.post("/api/v1/branches/{branch_id}/commands/sales")
def command_sale(
    branch_id: str, payload: SaleCommand, expected_version: int = Query(ge=1),
    principal: Principal = Depends(current_principal), db: Session = Depends(get_db),
):
    _require_action(principal, "sell")
    branch = require_branch(principal, branch_id)
    state = dict(branch.state or {})
    if not any(item.get("id") == payload.customer_id for item in _state_list(state, "customers")):
        raise HTTPException(status_code=404, detail="Customer not found")
    total = payload.kg * payload.price_per_kg
    if payload.paid_now > total:
        raise HTTPException(status_code=422, detail="Payment cannot exceed invoice total")
    batches = _state_list(state, "batches")
    need, cogs, allocations = payload.kg, 0.0, []
    for batch in sorted(batches, key=lambda item: item.get("date", "")):
        if need <= 1e-9:
            break
        if batch.get("st") != "accepted" or batch.get("sp") != payload.species or batch.get("grade") != payload.grade:
            continue
        remaining = float(batch.get("rem", 0))
        take = min(need, remaining)
        if take <= 0:
            continue
        batch["rem"] = remaining - take
        need -= take
        cogs += take * float(batch.get("price", 0))
        allocations.append({"b": batch.get("id"), "kg": take})
    if need > 1e-9:
        raise HTTPException(status_code=409, detail={"code": "insufficient_stock", "missing_kg": need})
    no = _next_number(state, "sal", "SAL-", 6)
    now = datetime.now(timezone.utc)
    sale = {
        "id": new_id(), "no": no, "cus": payload.customer_id,
        "sp": payload.species, "grade": payload.grade, "kg": payload.kg,
        "price": payload.price_per_kg, "paid": payload.paid_now,
        "cogs": cogs, "alloc": allocations, "date": now.date().isoformat(),
        "at": now.isoformat(), "dis": {"st": "Pending Dispatch", "veh": "", "drv": ""},
    }
    _state_list(state, "sales").insert(0, sale)
    return _commit_domain_command(
        branch=branch, state=state, expected_version=expected_version,
        principal=principal, db=db, action="sale_recorded",
        details={"sale": no, "kg": payload.kg}, result={"sale": sale},
    )


@app.post("/api/v1/branches/{branch_id}/commands/fuel-issues")
def command_fuel_issue(
    branch_id: str, payload: FuelIssueCommand, expected_version: int = Query(ge=1),
    principal: Principal = Depends(current_principal), db: Session = Depends(get_db),
):
    _require_action(principal, "fuel")
    branch = require_branch(principal, branch_id)
    state = dict(branch.state or {})
    supplier = next((item for item in _state_list(state, "suppliers") if item.get("id") == payload.supplier_id), None)
    boat = next((item for item in _state_list(state, "boats") if item.get("id") == payload.boat_id), None)
    tanks = _state_list(state, "fuelTanks")
    tank = next((item for item in tanks if item.get("id") == payload.tank_id), None)
    if supplier is None or boat is None or tank is None:
        raise HTTPException(status_code=404, detail="Supplier, boat, or fuel tank not found")
    available = float(tank.get("current", 0))
    if payload.litres > available:
        raise HTTPException(status_code=409, detail={"code": "insufficient_fuel", "available_litres": available})
    total = payload.litres * payload.price_per_litre
    if payload.paid_now > total:
        raise HTTPException(status_code=422, detail="Payment cannot exceed fuel total")
    balance = total - payload.paid_now
    no = _next_number(state, "fuel", "FUEL-", 6)
    now = datetime.now(timezone.utc)
    tank["current"] = available - payload.litres
    issue = {
        "id": new_id(), "no": no, "date": now.date().isoformat(), "at": now.isoformat(),
        "supId": payload.supplier_id, "boatId": payload.boat_id,
        "boatReg": boat.get("regNo", ""), "tankId": tank["id"],
        "fuel": tank.get("fuel", "Petrol"), "litres": payload.litres,
        "price": payload.price_per_litre, "total": total, "paid": payload.paid_now,
        "balance": balance, "arrangement": payload.arrangement,
        "issuedBy": principal.user.name,
    }
    _state_list(state, "fuelIssues").insert(0, issue)
    if balance > 0:
        _ledger_entry(state, supplier_id=payload.supplier_id, boat_id=payload.boat_id,
                      kind="Fuel Issued", debit=balance, credit=0, source="fuel",
                      reference=no, notes=f"{payload.litres} L issued", user_name=principal.user.name)
    _state_list(state, "boatCostLedger").insert(0, {
        "id": new_id(), "boatId": payload.boat_id,
        "seasonId": state.get("activeSeasonId", ""), "date": now.date().isoformat(),
        "cat": "Fuel", "cost": total,
        "allocation": "Company-funded" if payload.arrangement == "Company-paid" else "Fisherman-recoverable",
        "ref": no, "notes": f"{payload.litres} L {issue['fuel']} issued",
    })
    return _commit_domain_command(
        branch=branch, state=state, expected_version=expected_version,
        principal=principal, db=db, action="fuel_issued",
        details={"issue": no, "litres": payload.litres}, result={"issue": issue},
    )


@app.post("/api/v1/branches/{branch_id}/commands/gear-issues")
def command_gear_issue(
    branch_id: str, payload: GearIssueCommand, expected_version: int = Query(ge=1),
    principal: Principal = Depends(current_principal), db: Session = Depends(get_db),
):
    _require_action(principal, "gear")
    branch = require_branch(principal, branch_id)
    state = dict(branch.state or {})
    if not any(item.get("id") == payload.supplier_id for item in _state_list(state, "suppliers")):
        raise HTTPException(status_code=404, detail="Supplier not found")
    if not any(item.get("id") == payload.boat_id for item in _state_list(state, "boats")):
        raise HTTPException(status_code=404, detail="Boat not found")
    catalog = _state_list(state, "gearItems")
    requested: dict[str, int] = {}
    for line in payload.items:
        requested[line.item_id] = requested.get(line.item_id, 0) + line.quantity
    lines = []
    for item_id, quantity in requested.items():
        item = next((candidate for candidate in catalog if candidate.get("id") == item_id), None)
        if item is None:
            raise HTTPException(status_code=404, detail=f"Gear item {item_id} not found")
        stock = int(item.get("stock", 0))
        if quantity > stock:
            raise HTTPException(status_code=409, detail={
                "code": "insufficient_gear_stock", "item": item.get("name"),
                "requested": quantity, "available": stock,
            })
        item["stock"] = stock - quantity
        price = float(item.get("sellPrice", item.get("unitCost", 0)))
        lines.append({"itemId": item_id, "name": item.get("name", ""),
                      "qty": quantity, "price": price, "cost": price * quantity})
    total = sum(line["cost"] for line in lines)
    no = _next_number(state, "gear", "GEAR-", 6)
    recoverable = total if "Fisherman-recoverable" in payload.arrangement or "Credit" in payload.arrangement else 0
    issue = {
        "id": new_id(), "no": no,
        "date": datetime.now(timezone.utc).date().isoformat(),
        "supId": payload.supplier_id, "boatId": payload.boat_id,
        "seasonId": state.get("activeSeasonId", ""), "items": lines,
        "totalCost": total, "paid": 0, "recoverable": recoverable,
        "arrangement": payload.arrangement, "status": "Issued",
        "issuedBy": principal.user.name,
    }
    _state_list(state, "gearIssues").insert(0, issue)
    if recoverable > 0:
        _ledger_entry(state, supplier_id=payload.supplier_id, boat_id=payload.boat_id,
                      kind="Gear Issued", debit=recoverable, credit=0, source="gear",
                      reference=no, notes=f"{len(lines)} gear item(s) on credit",
                      user_name=principal.user.name)
    return _commit_domain_command(
        branch=branch, state=state, expected_version=expected_version,
        principal=principal, db=db, action="gear_issued",
        details={"issue": no, "items": len(lines)}, result={"issue": issue},
    )


# ════════════════════════════════════════════════════════════════════════════
# EXTENDED DOMAIN COMMANDS
# ════════════════════════════════════════════════════════════════════════════

# ── Fuel: receive bulk delivery ──────────────────────────────────────────────

@app.post("/api/v1/branches/{branch_id}/commands/fuel-receive")
def command_fuel_receive(
    branch_id: str, payload: FuelReceiveCommand,
    expected_version: int = Query(ge=1),
    principal: Principal = Depends(current_principal),
    db: Session = Depends(get_db),
):
    _require_action(principal, "fuel")
    branch = require_branch(principal, branch_id)
    state = dict(branch.state or {})
    tanks = _state_list(state, "fuelTanks")
    tank = next((t for t in tanks if t.get("id") == payload.tank_id), None)
    if tank is None:
        raise HTTPException(status_code=404, detail="Fuel tank not found")
    capacity = float(tank.get("capacity", 0))
    current = float(tank.get("current", 0))
    if current + payload.litres > capacity + 1e-6:
        raise HTTPException(
            status_code=422,
            detail=f"Delivery of {payload.litres} L would overflow tank capacity {capacity} L",
        )
    tank["current"] = current + payload.litres
    no = f"FP-{new_id()[:8].upper()}"
    now = datetime.now(timezone.utc)
    purchase = {
        "id": new_id(), "no": no, "date": now.date().isoformat(),
        "supId": payload.fuel_supplier_id, "supName": payload.fuel_supplier_name,
        "tankId": payload.tank_id, "fuel": payload.fuel_type,
        "litres": payload.litres, "costPerL": payload.cost_per_litre,
        "total": round(payload.litres * payload.cost_per_litre, 2),
        "docRef": payload.doc_ref, "status": payload.status,
        "recordedBy": principal.user.name,
    }
    _state_list(state, "fuelPurchases").insert(0, purchase)
    return _commit_domain_command(
        branch=branch, state=state, expected_version=expected_version,
        principal=principal, db=db, action="fuel_received",
        details={"no": no, "litres": payload.litres, "tank": payload.tank_id},
        result={"purchase": purchase, "tank_current": tank["current"]},
    )


# ── Fuel: record debt repayment from fisherman ───────────────────────────────

@app.post("/api/v1/branches/{branch_id}/commands/fuel-debt-payments")
def command_fuel_debt_payment(
    branch_id: str, payload: FuelDebtPaymentCommand,
    expected_version: int = Query(ge=1),
    principal: Principal = Depends(current_principal),
    db: Session = Depends(get_db),
):
    _require_action(principal, "fuel")
    branch = require_branch(principal, branch_id)
    state = dict(branch.state or {})
    if not any(s.get("id") == payload.supplier_id for s in _state_list(state, "suppliers")):
        raise HTTPException(status_code=404, detail="Supplier not found")
    now = datetime.now(timezone.utc)
    ref = f"FPAY-{new_id()[:8].upper()}"
    payment = {
        "id": new_id(), "supId": payload.supplier_id, "amt": payload.amount,
        "m": payload.method, "date": now.date().isoformat(), "ref": ref,
        "notes": payload.notes,
    }
    _state_list(state, "fuelPayments").insert(0, payment)
    _ledger_entry(
        state, supplier_id=payload.supplier_id,
        boat_id=state.get("activeBoatId", ""),
        kind="Fuel Debt Payment", debit=0, credit=payload.amount,
        source="fuel", reference=ref, notes=f"Fuel debt repaid ({payload.method})",
        user_name=principal.user.name,
    )
    return _commit_domain_command(
        branch=branch, state=state, expected_version=expected_version,
        principal=principal, db=db, action="fuel_debt_repaid",
        details={"supplier": payload.supplier_id, "amount": payload.amount},
        result={"payment": payment},
    )


# ── Fuel: dipstick / tank adjustment ─────────────────────────────────────────

@app.post("/api/v1/branches/{branch_id}/commands/fuel-adjustments")
def command_fuel_adjustment(
    branch_id: str, payload: FuelAdjustmentCommand,
    principal: Principal = Depends(current_principal),
    db: Session = Depends(get_db),
):
    _require_action(principal, "fuel")
    branch = require_branch(principal, branch_id)
    state = dict(branch.state or {})
    tanks = _state_list(state, "fuelTanks")
    tank = next((t for t in tanks if t.get("id") == payload.tank_id), None)
    if tank is None:
        raise HTTPException(status_code=404, detail="Fuel tank not found")
    old_level = float(tank.get("current", 0))
    tank["current"] = payload.new_level_litres
    now = datetime.now(timezone.utc)
    adj = {
        "id": new_id(), "tankId": payload.tank_id,
        "date": now.date().isoformat(),
        "oldLevel": old_level, "newLevel": payload.new_level_litres,
        "delta": round(payload.new_level_litres - old_level, 2),
        "reason": payload.reason, "recordedBy": principal.user.name,
    }
    _state_list(state, "fuelAdjustments").insert(0, adj)
    version = _write_branch_state(
        branch=branch, state=state, expected_version=payload.expected_version,
        principal=principal, db=db, action="fuel_adjustment_recorded",
        details={"tank": payload.tank_id, "new_level": payload.new_level_litres},
    )
    return {"adjustment": adj, "version": version}


# ── Maintenance: create work order ───────────────────────────────────────────

@app.post("/api/v1/branches/{branch_id}/commands/work-orders")
def command_create_work_order(
    branch_id: str, payload: WorkOrderCommand,
    expected_version: int = Query(ge=1),
    principal: Principal = Depends(current_principal),
    db: Session = Depends(get_db),
):
    _require_action(principal, "fleet")
    branch = require_branch(principal, branch_id)
    state = dict(branch.state or {})
    if not any(b.get("id") == payload.boat_id for b in _state_list(state, "boats")):
        raise HTTPException(status_code=404, detail="Boat not found")
    # Deduct parts from stock
    spare_parts = _state_list(state, "spareParts")
    for part_line in payload.parts:
        part = next((p for p in spare_parts if p.get("id") == part_line.part_id), None)
        if part and part_line.qty > 0:
            part["stock"] = max(0, int(part.get("stock", 0)) - part_line.qty)
    # Compute recovery
    total = payload.parts_cost + payload.labor_cost + payload.other_cost
    is_recoverable = payload.arrangement in {
        "Company-assisted (Deduct from fish)", "Fisherman-paid (Credit)", "Shared Cost"
    }
    recoverable = total if is_recoverable else 0.0
    no = _next_number(state, "wo", "WO-", 6)
    now = datetime.now(timezone.utc)
    # Mark boat under maintenance if not completed
    for boat in _state_list(state, "boats"):
        if boat.get("id") == payload.boat_id:
            boat["operationalStatus"] = "Under Maintenance"
            boat["status"] = "Under Maintenance"
            break
    wo = {
        "id": new_id(), "no": no, "date": now.date().isoformat(),
        "boatId": payload.boat_id, "supId": payload.supplier_id,
        "title": payload.title, "fault": payload.fault,
        "mechanic": payload.mechanic, "status": "Reported",
        "arrangement": payload.arrangement,
        "parts": [{"partId": p.part_id, "name": p.name, "qty": p.qty, "cost": p.cost} for p in payload.parts],
        "partsCost": payload.parts_cost, "laborCost": payload.labor_cost,
        "otherCost": payload.other_cost, "totalCost": total,
        "recoverable": recoverable, "recovered": 0, "notes": payload.notes,
        "seasonId": state.get("activeSeasonId", ""),
    }
    _state_list(state, "maintenanceOrders").insert(0, wo)
    if recoverable > 0 and payload.supplier_id != "company":
        _ledger_entry(
            state, supplier_id=payload.supplier_id, boat_id=payload.boat_id,
            kind="Maintenance Repair", debit=recoverable, credit=0,
            source="maintenance", reference=no, notes=payload.title,
            user_name=principal.user.name,
        )
    _state_list(state, "boatCostLedger").insert(0, {
        "id": new_id(), "boatId": payload.boat_id,
        "seasonId": state.get("activeSeasonId", ""),
        "date": now.date().isoformat(), "cat": "Maintenance", "cost": total,
        "allocation": "Fisherman-recoverable" if recoverable > 0 else "Company-funded",
        "ref": no, "notes": payload.title,
    })
    return _commit_domain_command(
        branch=branch, state=state, expected_version=expected_version,
        principal=principal, db=db, action="work_order_created",
        details={"work_order": no, "boat": payload.boat_id},
        result={"work_order": wo},
    )


# ── Maintenance: update work order status ────────────────────────────────────

@app.patch("/api/v1/branches/{branch_id}/commands/work-orders/{wo_id}/status")
def command_update_work_order_status(
    branch_id: str, wo_id: str, payload: WorkOrderStatusCommand,
    principal: Principal = Depends(current_principal),
    db: Session = Depends(get_db),
):
    _require_action(principal, "fleet")
    branch = require_branch(principal, branch_id)
    state = dict(branch.state or {})
    wo = next((w for w in _state_list(state, "maintenanceOrders") if w.get("id") == wo_id), None)
    if wo is None:
        raise HTTPException(status_code=404, detail="Work order not found")
    wo["status"] = payload.status
    if payload.status == "Completed":
        for boat in _state_list(state, "boats"):
            if boat.get("id") == wo.get("boatId"):
                boat["operationalStatus"] = "Operational"
                boat["status"] = "Active"
                boat["lastService"] = datetime.now(timezone.utc).date().isoformat()
                break
    version = _write_branch_state(
        branch=branch, state=state, expected_version=payload.expected_version,
        principal=principal, db=db, action="work_order_status_updated",
        details={"work_order": wo_id, "status": payload.status},
    )
    return {"work_order": wo, "version": version}


# ── Maintenance: debt repayment ───────────────────────────────────────────────

@app.post("/api/v1/branches/{branch_id}/commands/maint-debt-payments")
def command_maint_debt_payment(
    branch_id: str, payload: MaintDebtPaymentCommand,
    expected_version: int = Query(ge=1),
    principal: Principal = Depends(current_principal),
    db: Session = Depends(get_db),
):
    _require_action(principal, "fleet")
    branch = require_branch(principal, branch_id)
    state = dict(branch.state or {})
    now = datetime.now(timezone.utc)
    ref = f"MPAY-{new_id()[:8].upper()}"
    payment = {
        "id": new_id(), "supId": payload.supplier_id, "amt": payload.amount,
        "m": payload.method, "date": now.date().isoformat(), "ref": ref,
        "notes": payload.notes,
    }
    _state_list(state, "maintPayments").insert(0, payment)
    _ledger_entry(
        state, supplier_id=payload.supplier_id,
        boat_id=state.get("activeBoatId", ""),
        kind="Maintenance Debt Payment", debit=0, credit=payload.amount,
        source="maintenance", reference=ref, notes=payload.notes,
        user_name=principal.user.name,
    )
    return _commit_domain_command(
        branch=branch, state=state, expected_version=expected_version,
        principal=principal, db=db, action="maint_debt_repaid",
        details={"supplier": payload.supplier_id, "amount": payload.amount},
        result={"payment": payment},
    )


# ── Gear: return/inspect gear ─────────────────────────────────────────────────

@app.patch("/api/v1/branches/{branch_id}/commands/gear-issues/{issue_id}/return")
def command_gear_return(
    branch_id: str, issue_id: str, payload: GearReturnCommand,
    principal: Principal = Depends(current_principal),
    db: Session = Depends(get_db),
):
    _require_action(principal, "gear")
    branch = require_branch(principal, branch_id)
    state = dict(branch.state or {})
    issue = next((g for g in _state_list(state, "gearIssues") if g.get("id") == issue_id), None)
    if issue is None:
        raise HTTPException(status_code=404, detail="Gear issue not found")
    issue.update({
        "status": payload.liability_action if "Lost" in payload.liability_action else "Returned & Reconciled",
        "returnedCondition": payload.returned_condition,
        "returnedQty": payload.returned_qty,
        "returnNotes": payload.return_notes,
        "liabilityHandling": payload.liability_action,
    })
    # Restock if applicable
    if payload.restock and payload.returned_qty > 0:
        catalog = _state_list(state, "gearItems")
        for line in issue.get("items", []):
            item = next((g for g in catalog if g.get("id") == line.get("itemId")), None)
            if item:
                item["stock"] = int(item.get("stock", 0)) + payload.returned_qty
    # Post ledger credit if clearing debt
    if "Credit / Clear" in payload.liability_action and float(issue.get("recoverable", 0)) > 0:
        _ledger_entry(
            state, supplier_id=issue.get("supId", ""),
            boat_id=issue.get("boatId", ""),
            kind="Gear Returned", debit=0, credit=float(issue["recoverable"]),
            source="gear", reference=issue.get("no", ""),
            notes="Equipment returned – debt cleared",
            user_name=principal.user.name,
        )
    version = _write_branch_state(
        branch=branch, state=state, expected_version=payload.expected_version,
        principal=principal, db=db, action="gear_returned",
        details={"issue": issue_id, "condition": payload.returned_condition},
    )
    return {"issue": issue, "version": version}


# ── Batch: transfer between warehouses ───────────────────────────────────────

@app.patch("/api/v1/branches/{branch_id}/commands/batches/{batch_id}/transfer")
def command_transfer_batch(
    branch_id: str, batch_id: str, payload: TransferBatchCommand,
    principal: Principal = Depends(current_principal),
    db: Session = Depends(get_db),
):
    _require_action(principal, "inspect")
    branch = require_branch(principal, branch_id)
    state = dict(branch.state or {})
    batch = next((b for b in _state_list(state, "batches") if b.get("id") == batch_id), None)
    if batch is None:
        raise HTTPException(status_code=404, detail="Batch not found")
    old_wh = batch.get("wh", "")
    batch["wh"] = payload.warehouse
    version = _write_branch_state(
        branch=branch, state=state, expected_version=payload.expected_version,
        principal=principal, db=db, action="batch_transferred",
        details={"batch": batch_id, "from": old_wh, "to": payload.warehouse},
    )
    return {"batch": batch, "version": version}


# ── Batch: record wastage ─────────────────────────────────────────────────────

@app.patch("/api/v1/branches/{branch_id}/commands/batches/{batch_id}/waste")
def command_batch_waste(
    branch_id: str, batch_id: str, payload: WasteBatchCommand,
    principal: Principal = Depends(current_principal),
    db: Session = Depends(get_db),
):
    _require_action(principal, "inspect")
    branch = require_branch(principal, branch_id)
    state = dict(branch.state or {})
    batch = next((b for b in _state_list(state, "batches") if b.get("id") == batch_id), None)
    if batch is None:
        raise HTTPException(status_code=404, detail="Batch not found")
    remaining = float(batch.get("rem", 0))
    if payload.waste_kg > remaining:
        raise HTTPException(status_code=422, detail="Waste cannot exceed remaining stock")
    batch["rem"] = remaining - payload.waste_kg
    batch["waste"] = float(batch.get("waste", 0)) + payload.waste_kg
    version = _write_branch_state(
        branch=branch, state=state, expected_version=payload.expected_version,
        principal=principal, db=db, action="batch_waste_recorded",
        details={"batch": batch_id, "waste_kg": payload.waste_kg},
    )
    return {"batch": batch, "version": version}


# ── Sales: dispatch assignment ────────────────────────────────────────────────

@app.patch("/api/v1/branches/{branch_id}/commands/sales/{sale_id}/dispatch")
def command_dispatch_sale(
    branch_id: str, sale_id: str, payload: DispatchSaleCommand,
    principal: Principal = Depends(current_principal),
    db: Session = Depends(get_db),
):
    _require_action(principal, "sell")
    branch = require_branch(principal, branch_id)
    state = dict(branch.state or {})
    sale = next((s for s in _state_list(state, "sales") if s.get("id") == sale_id), None)
    if sale is None:
        raise HTTPException(status_code=404, detail="Sale not found")
    sale["dis"] = {"st": payload.status, "veh": payload.vehicle,
                   "drv": payload.driver, "temp": payload.temperature}
    version = _write_branch_state(
        branch=branch, state=state, expected_version=payload.expected_version,
        principal=principal, db=db, action="sale_dispatched",
        details={"sale": sale_id, "status": payload.status},
    )
    return {"sale": sale, "version": version}


# ── Sales: receive customer payment ──────────────────────────────────────────

@app.patch("/api/v1/branches/{branch_id}/commands/sales/{sale_id}/payment")
def command_receive_sale_payment(
    branch_id: str, sale_id: str, payload: ReceivePaymentCommand,
    principal: Principal = Depends(current_principal),
    db: Session = Depends(get_db),
):
    _require_action(principal, "sell")
    branch = require_branch(principal, branch_id)
    state = dict(branch.state or {})
    sale = next((s for s in _state_list(state, "sales") if s.get("id") == sale_id), None)
    if sale is None:
        raise HTTPException(status_code=404, detail="Sale not found")
    total = float(sale.get("kg", 0)) * float(sale.get("price", 0))
    already_paid = float(sale.get("paid", 0))
    if already_paid + payload.amount > total + 0.005:
        raise HTTPException(status_code=422, detail="Total received would exceed invoice total")
    sale["paid"] = round(already_paid + payload.amount, 2)
    version = _write_branch_state(
        branch=branch, state=state, expected_version=payload.expected_version,
        principal=principal, db=db, action="customer_payment_received",
        details={"sale": sale_id, "amount": payload.amount},
    )
    return {"sale": sale, "version": version}


# ── Catering: issue advance to cook ──────────────────────────────────────────

@app.post("/api/v1/branches/{branch_id}/commands/catering-advances")
def command_catering_advance(
    branch_id: str, payload: CookAdvanceCommand,
    expected_version: int = Query(ge=1),
    principal: Principal = Depends(current_principal),
    db: Session = Depends(get_db),
):
    branch = require_branch(principal, branch_id)
    state = dict(branch.state or {})
    if not any(c.get("id") == payload.cook_id for c in _state_list(state, "cateringCooks")):
        raise HTTPException(status_code=404, detail="Cook not found")
    now = datetime.now(timezone.utc)
    no = f"CADV-{new_id()[:8].upper()}"
    advance = {
        "id": new_id(), "no": no, "date": now.date().isoformat(),
        "cookId": payload.cook_id, "amountIssued": payload.amount,
        "actualExpenditure": 0, "moneyReturned": 0,
        "status": "Pending", "purpose": payload.purpose,
        "allocatedBoats": payload.allocated_boats, "issuedBy": principal.user.name,
    }
    _state_list(state, "cookAdvances").insert(0, advance)
    return _commit_domain_command(
        branch=branch, state=state, expected_version=expected_version,
        principal=principal, db=db, action="catering_advance_issued",
        details={"no": no, "cook": payload.cook_id},
        result={"advance": advance},
    )


# ── Catering: record meal expense and boat allocations ───────────────────────

@app.post("/api/v1/branches/{branch_id}/commands/catering-expenses")
def command_catering_expense(
    branch_id: str, payload: CateringExpenseCommand,
    expected_version: int = Query(ge=1),
    principal: Principal = Depends(current_principal),
    db: Session = Depends(get_db),
):
    branch = require_branch(principal, branch_id)
    state = dict(branch.state or {})
    now = datetime.now(timezone.utc)
    no = _next_number(state, "cat", "CAT-", 5)
    # Reconcile the linked advance if provided
    if payload.advance_ref:
        for adv in _state_list(state, "cookAdvances"):
            if adv.get("no") == payload.advance_ref:
                adv["actualExpenditure"] = payload.total_spent
                adv["moneyReturned"] = max(0, float(adv.get("amountIssued", 0)) - payload.total_spent)
                adv["status"] = "Reconciled"
                break
    # Build allocation records
    alloc_records = [
        {"boatId": a.boat_id, "supId": a.supplier_id, "cost": a.cost, "recoverable": a.recoverable}
        for a in payload.boat_allocations
    ]
    expense = {
        "id": new_id(), "no": no, "date": now.date().isoformat(),
        "cookId": payload.cook_id, "advanceRef": payload.advance_ref,
        "seasonId": state.get("activeSeasonId", ""),
        "mealType": payload.meal_type, "mealsCount": payload.meals_count,
        "crewSize": payload.crew_size, "totalSpent": payload.total_spent,
        "moneyReturned": payload.money_returned,
        "boatAllocations": alloc_records, "arrangement": payload.arrangement,
        "notes": payload.notes,
    }
    _state_list(state, "cateringExpenses").insert(0, expense)
    # Post recoverable amounts to fisherman ledger per boat allocation
    for alloc in payload.boat_allocations:
        if alloc.recoverable > 0:
            _ledger_entry(
                state, supplier_id=alloc.supplier_id, boat_id=alloc.boat_id,
                kind="Food & Catering", debit=alloc.recoverable, credit=0,
                source="catering", reference=no,
                notes=f"{payload.meal_type} via cook",
                user_name=principal.user.name,
            )
            _state_list(state, "boatCostLedger").insert(0, {
                "id": new_id(), "boatId": alloc.boat_id,
                "seasonId": state.get("activeSeasonId", ""),
                "date": now.date().isoformat(), "cat": "Food",
                "cost": alloc.cost, "allocation": "Fisherman-recoverable",
                "ref": no, "notes": payload.meal_type,
            })
    return _commit_domain_command(
        branch=branch, state=state, expected_version=expected_version,
        principal=principal, db=db, action="catering_expense_recorded",
        details={"no": no, "total": payload.total_spent},
        result={"expense": expense},
    )


# ── Cash Advances: request ────────────────────────────────────────────────────

@app.post("/api/v1/branches/{branch_id}/commands/advances/request")
def command_advance_request(
    branch_id: str, payload: CashAdvanceRequestCommand,
    expected_version: int = Query(ge=1),
    principal: Principal = Depends(current_principal),
    db: Session = Depends(get_db),
):
    branch = require_branch(principal, branch_id)
    state = dict(branch.state or {})
    if not any(s.get("id") == payload.supplier_id for s in _state_list(state, "suppliers")):
        raise HTTPException(status_code=404, detail="Supplier not found")
    no = _next_number(state, "adv", "ADV-", 5)
    now = datetime.now(timezone.utc)
    advance = {
        "id": new_id(), "no": no, "date": now.date().isoformat(),
        "at": now.isoformat(), "supId": payload.supplier_id,
        "boatId": payload.boat_id, "seasonId": state.get("activeSeasonId", ""),
        "amount": payload.amount, "category": payload.category,
        "purpose": payload.purpose, "method": payload.method,
        "phone": payload.phone, "status": "Pending Approval",
        "classification": payload.classification, "approvedBy": "",
        "recovered": 0,
    }
    _state_list(state, "advances").insert(0, advance)
    return _commit_domain_command(
        branch=branch, state=state, expected_version=expected_version,
        principal=principal, db=db, action="advance_requested",
        details={"no": no, "amount": payload.amount, "category": payload.category},
        result={"advance": advance},
    )


# ── Cash Advances: approve or reject ─────────────────────────────────────────

@app.patch("/api/v1/branches/{branch_id}/commands/advances/{advance_id}/approve")
def command_advance_approve(
    branch_id: str, advance_id: str, payload: CashAdvanceApproveCommand,
    principal: Principal = Depends(current_principal),
    db: Session = Depends(get_db),
):
    _require_action(principal, "pay")
    branch = require_branch(principal, branch_id)
    state = dict(branch.state or {})
    advance = next((a for a in _state_list(state, "advances") if a.get("id") == advance_id), None)
    if advance is None:
        raise HTTPException(status_code=404, detail="Advance not found")
    if advance.get("status") != "Pending Approval":
        raise HTTPException(status_code=422, detail="Advance is not in Pending Approval state")
    if payload.new_status not in {"Approved", "Rejected"}:
        raise HTTPException(status_code=422, detail="new_status must be 'Approved' or 'Rejected'")
    final_amount = payload.approved_amount or float(advance.get("amount", 0))
    advance["status"] = payload.new_status
    advance["approvedBy"] = principal.user.name
    advance["amount"] = final_amount
    if payload.new_status == "Approved" and advance.get("classification") == "Fisherman-recoverable":
        _ledger_entry(
            state, supplier_id=advance["supId"], boat_id=advance["boatId"],
            kind="Cash Advance", debit=final_amount, credit=0,
            source="advances", reference=advance["no"],
            notes=f"{advance.get('category', '')}: {advance.get('purpose', '')}",
            user_name=principal.user.name,
        )
    version = _write_branch_state(
        branch=branch, state=state, expected_version=payload.expected_version,
        principal=principal, db=db, action=f"advance_{payload.new_status.lower()}",
        details={"advance": advance_id, "amount": final_amount},
    )
    return {"advance": advance, "version": version}


# ── Cash Advances: repayment ──────────────────────────────────────────────────

@app.post("/api/v1/branches/{branch_id}/commands/advances/{advance_id}/repay")
def command_advance_repay(
    branch_id: str, advance_id: str, payload: CashAdvanceRepayCommand,
    principal: Principal = Depends(current_principal),
    db: Session = Depends(get_db),
):
    branch = require_branch(principal, branch_id)
    state = dict(branch.state or {})
    advance = next((a for a in _state_list(state, "advances") if a.get("id") == advance_id), None)
    if advance is None:
        raise HTTPException(status_code=404, detail="Advance not found")
    advance["recovered"] = round(float(advance.get("recovered", 0)) + payload.amount, 2)
    ref = f"REPAY-{new_id()[:8].upper()}"
    _ledger_entry(
        state, supplier_id=advance["supId"], boat_id=advance.get("boatId", ""),
        kind="Advance Repayment", debit=0, credit=payload.amount,
        source="advances", reference=ref,
        notes=f"Direct repayment ({payload.method}) for {advance.get('no', '')}",
        user_name=principal.user.name,
    )
    version = _write_branch_state(
        branch=branch, state=state, expected_version=payload.expected_version,
        principal=principal, db=db, action="advance_repaid",
        details={"advance": advance_id, "amount": payload.amount},
    )
    return {"advance": advance, "version": version}


# ── Boat projects ─────────────────────────────────────────────────────────────

@app.post("/api/v1/branches/{branch_id}/commands/boat-projects")
def command_create_boat_project(
    branch_id: str, payload: BoatProjectCommand,
    expected_version: int = Query(ge=1),
    principal: Principal = Depends(current_principal),
    db: Session = Depends(get_db),
):
    _require_manager(principal)
    branch = require_branch(principal, branch_id)
    state = dict(branch.state or {})
    total = payload.timber_materials + payload.labour + payload.engine + payload.equipment
    no = _next_number(state, "proj", "PROJ-BOAT-", 3)
    now = datetime.now(timezone.utc)
    project = {
        "id": new_id(), "projectNo": no, "name": payload.name,
        "builder": payload.builder, "builderPhone": payload.builder_phone,
        "seasonId": state.get("activeSeasonId", ""),
        "assignedSupId": payload.assigned_supplier_id,
        "targetBoatName": payload.target_boat_name,
        "status": "In Progress", "progressPct": 0,
        "startDate": payload.start_date, "targetCompletion": payload.target_completion,
        "timberMaterials": payload.timber_materials, "labour": payload.labour,
        "engine": payload.engine, "equipment": payload.equipment,
        "totalCost": total, "paidStages": 0, "balanceToPay": total,
        "ownershipType": payload.ownership_type,
        "contractualRecoverable": payload.contractual_recoverable,
        "repaymentTerms": payload.repayment_terms,
        "stages": [
            {"name": s.name, "cost": s.cost, "status": s.status, "date": s.date}
            for s in payload.stages
        ],
        "notes": payload.notes,
    }
    _state_list(state, "boatProjects").insert(0, project)
    return _commit_domain_command(
        branch=branch, state=state, expected_version=expected_version,
        principal=principal, db=db, action="boat_project_created",
        details={"no": no, "total_cost": total},
        result={"project": project},
    )


@app.patch("/api/v1/branches/{branch_id}/commands/boat-projects/{project_id}/progress")
def command_update_project_progress(
    branch_id: str, project_id: str, payload: ProjectProgressCommand,
    principal: Principal = Depends(current_principal),
    db: Session = Depends(get_db),
):
    _require_manager(principal)
    branch = require_branch(principal, branch_id)
    state = dict(branch.state or {})
    project = next((p for p in _state_list(state, "boatProjects") if p.get("id") == project_id), None)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    project["progressPct"] = payload.progress_pct
    project["status"] = payload.status
    if payload.status == "Completed" and payload.progress_pct >= 100:
        new_boat = {
            "id": new_id(), "code": project["projectNo"], "regNo": project["projectNo"],
            "altReg": "", "name": project.get("targetBoatName") or project["name"],
            "ownerType": project.get("ownershipType", "Company-owned"),
            "ownerId": "company", "captain": "", "operation": "Company-managed",
            "landingSite": "", "engine": "New Engine", "boatType": "New Vessel",
            "status": "Active", "operationalStatus": "Operational",
            "engineHours": 0, "lastService": datetime.now(timezone.utc).date().isoformat(),
            "notes": f"Completed from project {project['projectNo']}",
            "seasonId": state.get("activeSeasonId", ""), "crewMembers": [],
        }
        _state_list(state, "boats").insert(0, new_boat)
    version = _write_branch_state(
        branch=branch, state=state, expected_version=payload.expected_version,
        principal=principal, db=db, action="project_progress_updated",
        details={"project": project_id, "progress_pct": payload.progress_pct, "status": payload.status},
    )
    return {"project": project, "version": version}


@app.post("/api/v1/branches/{branch_id}/commands/boat-projects/{project_id}/payments")
def command_project_stage_payment(
    branch_id: str, project_id: str, payload: ProjectPaymentCommand,
    principal: Principal = Depends(current_principal),
    db: Session = Depends(get_db),
):
    _require_manager(principal)
    branch = require_branch(principal, branch_id)
    state = dict(branch.state or {})
    project = next((p for p in _state_list(state, "boatProjects") if p.get("id") == project_id), None)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    project["paidStages"] = round(float(project.get("paidStages", 0)) + payload.amount, 2)
    project["balanceToPay"] = max(0, round(float(project.get("balanceToPay", 0)) - payload.amount, 2))
    for stage in project.get("stages", []):
        if stage.get("name") == payload.stage_name:
            stage["status"] = "Completed"
            break
    version = _write_branch_state(
        branch=branch, state=state, expected_version=payload.expected_version,
        principal=principal, db=db, action="project_stage_paid",
        details={"project": project_id, "stage": payload.stage_name, "amount": payload.amount},
    )
    return {"project": project, "version": version}


# ── Crew assignments ──────────────────────────────────────────────────────────

@app.post("/api/v1/branches/{branch_id}/commands/crew-assignments")
def command_assign_crew(
    branch_id: str, payload: CrewAssignmentCommand,
    expected_version: int = Query(ge=1),
    principal: Principal = Depends(current_principal),
    db: Session = Depends(get_db),
):
    branch = require_branch(principal, branch_id)
    state = dict(branch.state or {})
    if not any(b.get("id") == payload.boat_id for b in _state_list(state, "boats")):
        raise HTTPException(status_code=404, detail="Boat not found")
    if not any(s.get("id") == payload.supplier_id for s in _state_list(state, "suppliers")):
        raise HTTPException(status_code=404, detail="Supplier / fisherman not found")
    now = datetime.now(timezone.utc)
    assignment = {
        "id": new_id(), "boatId": payload.boat_id, "boatReg": payload.boat_reg,
        "boatName": payload.boat_name, "supId": payload.supplier_id,
        "fishermanName": payload.fisherman_name, "position": payload.position,
        "accountType": payload.account_type, "phone": payload.phone,
        "nida": payload.nida, "seasonId": state.get("activeSeasonId", ""),
        "startDate": payload.start_date or now.date().isoformat(),
        "endDate": None, "status": "Active", "balance": 0,
        "balanceType": "owed_by_fisherman", "balanceMeaning": "",
        "rateAgreement": payload.rate_agreement, "totalTrips": 0,
        "totalDeliveredKg": 0, "notes": payload.notes,
    }
    _state_list(state, "boatCrewAssignments").insert(0, assignment)
    return _commit_domain_command(
        branch=branch, state=state, expected_version=expected_version,
        principal=principal, db=db, action="crew_assigned",
        details={"boat": payload.boat_id, "fisherman": payload.fisherman_name},
        result={"assignment": assignment},
    )


# ── Kibalua (temporary) engagement ───────────────────────────────────────────

@app.post("/api/v1/branches/{branch_id}/commands/kibalua-engagements")
def command_create_kibalua(
    branch_id: str, payload: KibaluaEngagementCommand,
    expected_version: int = Query(ge=1),
    principal: Principal = Depends(current_principal),
    db: Session = Depends(get_db),
):
    branch = require_branch(principal, branch_id)
    state = dict(branch.state or {})
    if not any(b.get("id") == payload.boat_id for b in _state_list(state, "boats")):
        raise HTTPException(status_code=404, detail="Boat not found")
    no = _next_number(state, "kib", "KIB-", 5)
    gross = payload.daily_rate * payload.duration_days
    now = datetime.now(timezone.utc)
    engagement = {
        "id": new_id(), "no": no, "boatId": payload.boat_id,
        "boatReg": payload.boat_reg, "boatName": payload.boat_name,
        "fishermanName": payload.fisherman_name, "phone": payload.phone,
        "position": "Crew (Kibalua)", "engagementType": "Kibalua",
        "durationDays": payload.duration_days, "startDate": payload.start_date,
        "endDate": payload.end_date, "dailyRate": payload.daily_rate,
        "grossPayable": gross, "advancePaid": payload.advance_paid,
        "allocatedExpenses": 0,
        "remainingPayable": max(0, gross - payload.advance_paid),
        "tripsCount": 0, "paymentStatus": "Pending Payment",
        "status": "Active", "seasonId": state.get("activeSeasonId", ""),
        "notes": payload.notes,
    }
    _state_list(state, "kibaluaEngagements").insert(0, engagement)
    if payload.advance_paid > 0:
        adv = {
            "id": new_id(), "no": _next_number(state, "adv", "ADV-", 5),
            "date": now.date().isoformat(), "at": now.isoformat(),
            "supId": payload.boat_id, "boatId": payload.boat_id,
            "seasonId": state.get("activeSeasonId", ""),
            "amount": payload.advance_paid, "category": "Daily Matumizi",
            "purpose": f"Kibalua engagement advance ({no})",
            "method": "Cash", "status": "Approved",
            "classification": "Fisherman-recoverable",
            "approvedBy": principal.user.name, "recovered": 0,
        }
        _state_list(state, "advances").insert(0, adv)
    return _commit_domain_command(
        branch=branch, state=state, expected_version=expected_version,
        principal=principal, db=db, action="kibalua_created",
        details={"no": no, "fisherman": payload.fisherman_name},
        result={"engagement": engagement},
    )


@app.patch("/api/v1/branches/{branch_id}/commands/kibalua-engagements/{eng_id}/settle")
def command_settle_kibalua(
    branch_id: str, eng_id: str, payload: KibaluaSettleCommand,
    principal: Principal = Depends(current_principal),
    db: Session = Depends(get_db),
):
    branch = require_branch(principal, branch_id)
    state = dict(branch.state or {})
    eng = next((k for k in _state_list(state, "kibaluaEngagements") if k.get("id") == eng_id), None)
    if eng is None:
        raise HTTPException(status_code=404, detail="Kibalua engagement not found")
    eng["status"] = "Completed"
    eng["paymentStatus"] = "Paid" if payload.final_payment > 0 else "Pending Payment"
    eng["remainingPayable"] = max(0, float(eng.get("remainingPayable", 0)) - payload.final_payment)
    version = _write_branch_state(
        branch=branch, state=state, expected_version=payload.expected_version,
        principal=principal, db=db, action="kibalua_settled",
        details={"engagement": eng_id, "final_payment": payload.final_payment},
    )
    return {"engagement": eng, "version": version}


# ── Fisherman exit & clearance ────────────────────────────────────────────────

@app.post("/api/v1/branches/{branch_id}/commands/fisherman-exits")
def command_fisherman_exit(
    branch_id: str, payload: FishermanExitCommand,
    principal: Principal = Depends(current_principal),
    db: Session = Depends(get_db),
):
    branch = require_branch(principal, branch_id)
    state = dict(branch.state or {})
    # Mark the assignment as Exited
    assignments = _state_list(state, "boatCrewAssignments")
    assignment = next((a for a in assignments if a.get("id") == payload.assignment_id), None)
    if assignment is None:
        raise HTTPException(status_code=404, detail="Crew assignment not found")
    assignment["status"] = "Exited"
    assignment["endDate"] = payload.last_working_date
    exit_record = {
        "id": new_id(), "assignmentId": payload.assignment_id,
        "boatId": assignment.get("boatId", ""),
        "boatReg": assignment.get("boatReg", ""),
        "boatName": assignment.get("boatName", ""),
        "fishermanName": assignment.get("fishermanName", ""),
        "supId": assignment.get("supId", ""),
        "phone": assignment.get("phone", ""),
        "position": assignment.get("position", ""),
        "lastWorkingDate": payload.last_working_date,
        "reason": payload.reason,
        "outstandingAdvances": payload.outstanding_advances,
        "unreturnedGear": payload.unreturned_gear,
        "finalBalanceDue": payload.final_balance_due,
        "approvedBy": principal.user.name,
        "approvedDate": datetime.now(timezone.utc).date().isoformat(),
        "status": "Approved & Retained on Ledger",
        "notes": payload.notes,
    }
    _state_list(state, "fishermanExitRecords").insert(0, exit_record)
    version = _write_branch_state(
        branch=branch, state=state, expected_version=payload.expected_version,
        principal=principal, db=db, action="fisherman_exited",
        details={"assignment": payload.assignment_id, "reason": payload.reason},
    )
    return {"exit_record": exit_record, "version": version}


# ── Boat shared expense allocation ────────────────────────────────────────────

@app.post("/api/v1/branches/{branch_id}/commands/boat-expense-allocations")
def command_boat_expense_allocation(
    branch_id: str, payload: BoatExpenseAllocCommand,
    expected_version: int = Query(ge=1),
    principal: Principal = Depends(current_principal),
    db: Session = Depends(get_db),
):
    branch = require_branch(principal, branch_id)
    state = dict(branch.state or {})
    if not any(b.get("id") == payload.boat_id for b in _state_list(state, "boats")):
        raise HTTPException(status_code=404, detail="Boat not found")
    # Build equal splits per crew member
    crew_ids = payload.crew_supplier_ids or []
    per_person = round(payload.total_amount / len(crew_ids), 2) if crew_ids else 0
    split_items = []
    for sup_id in crew_ids:
        sup = next((s for s in _state_list(state, "suppliers") if s.get("id") == sup_id), None)
        if sup and per_person > 0:
            split_items.append({
                "supId": sup_id, "name": sup.get("name", ""),
                "role": "Crew", "amount": per_person, "status": "Posted to Ledger",
            })
            _ledger_entry(
                state, supplier_id=sup_id, boat_id=payload.boat_id,
                kind=payload.expense_type, debit=per_person, credit=0,
                source="other", reference=payload.boat_id,
                notes=payload.description, user_name=principal.user.name,
            )
    now = datetime.now(timezone.utc)
    allocation = {
        "id": new_id(), "boatId": payload.boat_id, "boatReg": payload.boat_reg,
        "boatName": payload.boat_name, "date": now.date().isoformat(),
        "seasonId": state.get("activeSeasonId", ""),
        "expenseType": payload.expense_type, "description": payload.description,
        "totalAmount": payload.total_amount, "rule": payload.rule,
        "splitItems": split_items, "postedBy": principal.user.name, "notes": payload.notes,
    }
    _state_list(state, "boatExpenseAllocations").insert(0, allocation)
    _state_list(state, "boatCostLedger").insert(0, {
        "id": new_id(), "boatId": payload.boat_id,
        "seasonId": state.get("activeSeasonId", ""),
        "date": now.date().isoformat(), "cat": payload.expense_type,
        "cost": payload.total_amount, "allocation": "Fisherman-recoverable",
        "ref": allocation["id"], "notes": payload.description,
    })
    return _commit_domain_command(
        branch=branch, state=state, expected_version=expected_version,
        principal=principal, db=db, action="boat_expense_allocated",
        details={"boat": payload.boat_id, "type": payload.expense_type, "total": payload.total_amount},
        result={"allocation": allocation},
    )


# ── Full Season Settlement ────────────────────────────────────────────────────

@app.post("/api/v1/branches/{branch_id}/commands/full-settlements")
def command_full_settlement(
    branch_id: str, payload: FullSettlementCommand,
    expected_version: int = Query(ge=1),
    principal: Principal = Depends(current_principal),
    db: Session = Depends(get_db),
):
    _require_action(principal, "pay")
    branch = require_branch(principal, branch_id)
    state = dict(branch.state or {})
    if not any(s.get("id") == payload.supplier_id for s in _state_list(state, "suppliers")):
        raise HTTPException(status_code=404, detail="Supplier not found")
    total_deductions = (
        payload.fuel_deducted + payload.maint_deducted + payload.gear_deducted
        + payload.food_deducted + payload.advances_deducted + payload.other_deducted
    )
    net_payable = max(0.0, payload.agreed_earnings - payload.previous_paid - total_deductions)
    no = f"SET-{new_id()[:8].upper()}"
    now = datetime.now(timezone.utc)
    settlement = {
        "id": new_id(), "no": no, "date": now.date().isoformat(), "at": now.isoformat(),
        "seasonId": payload.season_id or state.get("activeSeasonId", ""),
        "supId": payload.supplier_id, "boatId": payload.boat_id,
        "agreedEarnings": payload.agreed_earnings,
        "fuelDeducted": payload.fuel_deducted, "maintDeducted": payload.maint_deducted,
        "gearDeducted": payload.gear_deducted, "foodDeducted": payload.food_deducted,
        "advancesDeducted": payload.advances_deducted, "otherDeducted": payload.other_deducted,
        "totalDeductions": total_deductions, "previousPaid": payload.previous_paid,
        "netPayable": net_payable, "method": payload.method,
        "status": "Approved & Disbursed", "settlementType": payload.settlement_type,
        "approvedBy": principal.user.name, "notes": payload.notes,
    }
    _state_list(state, "fullSettlements").insert(0, settlement)
    # Post credit offset to clear ledger
    if total_deductions > 0:
        _ledger_entry(
            state, supplier_id=payload.supplier_id, boat_id=payload.boat_id,
            kind="Season Settlement", debit=0, credit=total_deductions,
            source="settlement", reference=no,
            notes="Season settlement deductions cleared",
            user_name=principal.user.name,
        )
    # Create supplier payment for net payable
    if net_payable > 0:
        _state_list(state, "pays").insert(0, {
            "id": new_id(), "sup": payload.supplier_id, "amt": net_payable,
            "m": payload.method, "date": now.date().isoformat(),
            "ref": no, "notes": f"{payload.settlement_type} net payout",
        })
    return _commit_domain_command(
        branch=branch, state=state, expected_version=expected_version,
        principal=principal, db=db, action="full_settlement_created",
        details={"no": no, "net_payable": net_payable},
        result={"settlement": settlement},
    )


# ── Operating Expenses ────────────────────────────────────────────────────────

@app.post("/api/v1/branches/{branch_id}/commands/expenses")
def command_add_expense(
    branch_id: str, payload: ExpenseCommand,
    expected_version: int = Query(ge=1),
    principal: Principal = Depends(current_principal),
    db: Session = Depends(get_db),
):
    branch = require_branch(principal, branch_id)
    state = dict(branch.state or {})
    now = datetime.now(timezone.utc)
    expense = {
        "id": new_id(), "cat": payload.category, "amt": payload.amount,
        "date": payload.date or now.date().isoformat(), "desc": payload.description,
    }
    _state_list(state, "exps").insert(0, expense)
    return _commit_domain_command(
        branch=branch, state=state, expected_version=expected_version,
        principal=principal, db=db, action="expense_recorded",
        details={"category": payload.category, "amount": payload.amount},
        result={"expense": expense},
    )


# ── Transport trips ───────────────────────────────────────────────────────────

@app.post("/api/v1/branches/{branch_id}/commands/trips")
def command_add_trip(
    branch_id: str, payload: TripCommand,
    expected_version: int = Query(ge=1),
    principal: Principal = Depends(current_principal),
    db: Session = Depends(get_db),
):
    branch = require_branch(principal, branch_id)
    state = dict(branch.state or {})
    now = datetime.now(timezone.utc)
    trip = {
        "id": new_id(), "type": payload.trip_type, "mode": payload.mode,
        "from": payload.from_location, "to": payload.to_location,
        "cost": payload.cost, "date": now.date().isoformat(),
        "ref": payload.sale_ref, "veh": payload.vehicle,
        "temp": payload.temperature, "method": payload.method,
        "notes": payload.notes,
    }
    _state_list(state, "trips").insert(0, trip)
    return _commit_domain_command(
        branch=branch, state=state, expected_version=expected_version,
        principal=principal, db=db, action="trip_recorded",
        details={"mode": payload.mode, "cost": payload.cost},
        result={"trip": trip},
    )


# ── Compliance permits ────────────────────────────────────────────────────────

@app.post("/api/v1/branches/{branch_id}/commands/compliance-permits")
def command_add_permit(
    branch_id: str, payload: CompliancePermitCommand,
    expected_version: int = Query(ge=1),
    principal: Principal = Depends(current_principal),
    db: Session = Depends(get_db),
):
    branch = require_branch(principal, branch_id)
    state = dict(branch.state or {})
    permit = {
        "id": new_id(), "type": payload.permit_type, "ref": payload.reference,
        "who": payload.authority, "exp": payload.expiry_date, "status": "Valid",
    }
    _state_list(state, "comp").insert(0, permit)
    return _commit_domain_command(
        branch=branch, state=state, expected_version=expected_version,
        principal=principal, db=db, action="permit_added",
        details={"type": payload.permit_type, "ref": payload.reference},
        result={"permit": permit},
    )


# ── Season management ─────────────────────────────────────────────────────────

@app.post("/api/v1/branches/{branch_id}/commands/seasons")
def command_add_season(
    branch_id: str, payload: SeasonCommand,
    expected_version: int = Query(ge=1),
    principal: Principal = Depends(current_principal),
    db: Session = Depends(get_db),
):
    _require_manager(principal)
    branch = require_branch(principal, branch_id)
    state = dict(branch.state or {})
    season = {
        "id": new_id(), "name": payload.name, "code": payload.code,
        "start": payload.start_date, "end": payload.end_date,
        "status": "Upcoming", "targetKg": payload.target_kg,
        "budget": payload.budget, "notes": payload.notes,
    }
    _state_list(state, "seasons").insert(0, season)
    return _commit_domain_command(
        branch=branch, state=state, expected_version=expected_version,
        principal=principal, db=db, action="season_added",
        details={"code": payload.code},
        result={"season": season},
    )


@app.post("/api/v1/branches/{branch_id}/commands/set-active-season")
def command_set_active_season(
    branch_id: str, payload: SetActiveSeasonCommand,
    principal: Principal = Depends(current_principal),
    db: Session = Depends(get_db),
):
    _require_manager(principal)
    branch = require_branch(principal, branch_id)
    state = dict(branch.state or {})
    if not any(s.get("id") == payload.season_id for s in _state_list(state, "seasons")):
        raise HTTPException(status_code=404, detail="Season not found")
    old_id = state.get("activeSeasonId", "")
    state["activeSeasonId"] = payload.season_id
    version = _write_branch_state(
        branch=branch, state=state, expected_version=payload.expected_version,
        principal=principal, db=db, action="active_season_changed",
        details={"from": old_id, "to": payload.season_id},
    )
    return {"activeSeasonId": payload.season_id, "version": version}
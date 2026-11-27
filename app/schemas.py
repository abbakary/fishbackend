from typing import Optional

from pydantic import BaseModel, Field, field_validator


# ─── Auth / Tenant ────────────────────────────────────────────────────────────

class BranchInput(BaseModel):
    name: str = Field(min_length=2, max_length=200)
    region: str = Field(default="", max_length=100)
    state: dict = Field(default_factory=dict)


class RegisterInput(BaseModel):
    company: str = Field(min_length=2, max_length=200)
    name: str = Field(min_length=2, max_length=200)
    username: str = Field(min_length=3, max_length=100)
    password: str = Field(min_length=8, max_length=128)
    region: str = Field(default="", max_length=100)
    phone: str = Field(default="", max_length=40)
    branches: list[BranchInput] = Field(min_length=1, max_length=99)
    plan: str = Field(default="Business", max_length=40)

    @field_validator("username")
    @classmethod
    def normalize_username(cls, value: str) -> str:
        return value.strip().lower()


class LoginInput(BaseModel):
    username: str
    password: str


class BranchStateInput(BaseModel):
    expected_version: int = Field(ge=1)
    state: dict


class CollectionItemInput(BaseModel):
    expected_version: int = Field(ge=1)
    item: dict


class CollectionItemPatch(BaseModel):
    expected_version: int = Field(ge=1)
    patch: dict


class CreateBranchInput(BaseModel):
    name: str = Field(min_length=2, max_length=200)
    region: str = Field(default="", max_length=100)
    state: dict = Field(default_factory=dict)


class CreateStaffInput(BaseModel):
    username: str = Field(min_length=3, max_length=100)
    name: str = Field(min_length=2, max_length=200)
    password: str = Field(min_length=4, max_length=128)
    role: str = Field(default="Purchasing Officer", max_length=60)
    phone: str = Field(default="", max_length=40)
    branch_ids: list[str] = Field(default_factory=list)
    permissions: dict = Field(default_factory=dict)
    module_keys: list[str] = Field(default_factory=list, max_length=200)

    @field_validator("username")
    @classmethod
    def normalize_username(cls, value: str) -> str:
        return value.strip().lower()


class UpdateStaffInput(BaseModel):
    name: Optional[str] = Field(default=None, min_length=2, max_length=200)
    role: Optional[str] = Field(default=None, max_length=60)
    phone: Optional[str] = Field(default=None, max_length=40)
    branch_ids: Optional[list[str]] = None
    permissions: Optional[dict] = None
    module_keys: Optional[list[str]] = Field(default=None, max_length=200)
    active: Optional[bool] = None
    password: Optional[str] = Field(default=None, min_length=4, max_length=128)


# ─── Fish Procurement ─────────────────────────────────────────────────────────

class PurchaseCommand(BaseModel):
    supplier_id: str = Field(default="", max_length=100)
    source_type: str = Field(default="independent", max_length=40)
    boat_id: str = Field(default="", max_length=100)
    fisherman_id: str = Field(default="", max_length=100)
    fisherman_name: str = Field(default="", max_length=150)
    species: str = Field(min_length=1, max_length=80)
    grade: str = Field(min_length=1, max_length=40)
    kg: float = Field(gt=0)
    price_per_kg: float = Field(gt=0)
    site: str = Field(min_length=1, max_length=160)
    warehouse: str = Field(default="", max_length=160)
    paid_now: float = Field(default=0, ge=0)
    fuel_deducted: float = Field(default=0, ge=0)
    maint_deducted: float = Field(default=0, ge=0)
    payment_method: str = Field(default="Cash Voucher", max_length=60)
    notes: str = Field(default="", max_length=500)


class InspectBatchCommand(BaseModel):
    accepted_kg: float = Field(ge=0)
    temperature_c: float
    reason: str = Field(default="", max_length=500)


class TransferBatchCommand(BaseModel):
    expected_version: int = Field(ge=1)
    warehouse: str = Field(min_length=1, max_length=160)


class WasteBatchCommand(BaseModel):
    expected_version: int = Field(ge=1)
    waste_kg: float = Field(gt=0)
    reason: str = Field(default="", max_length=300)


# ─── Payments ─────────────────────────────────────────────────────────────────

class SupplierPaymentCommand(BaseModel):
    supplier_id: str = Field(min_length=1)
    amount: float = Field(gt=0)
    method: str = Field(min_length=1, max_length=60)
    reference: str = Field(default="", max_length=100)
    notes: str = Field(default="", max_length=500)


# ─── Sales ───────────────────────────────────────────────────────────────────

class SaleCommand(BaseModel):
    customer_id: str = Field(min_length=1)
    species: str = Field(min_length=1, max_length=80)
    grade: str = Field(min_length=1, max_length=40)
    kg: float = Field(gt=0)
    price_per_kg: float = Field(gt=0)
    paid_now: float = Field(default=0, ge=0)
    notes: str = Field(default="", max_length=500)


class ReceivePaymentCommand(BaseModel):
    expected_version: int = Field(ge=1)
    amount: float = Field(gt=0)
    method: str = Field(default="Bank RTGS", max_length=60)


class DispatchSaleCommand(BaseModel):
    expected_version: int = Field(ge=1)
    vehicle: str = Field(min_length=1, max_length=100)
    driver: str = Field(min_length=1, max_length=100)
    status: str = Field(default="In transit", max_length=60)
    temperature: str = Field(default="2.0°C", max_length=20)


# ─── Fuel ─────────────────────────────────────────────────────────────────────

class FuelIssueCommand(BaseModel):
    supplier_id: str = Field(min_length=1)
    boat_id: str = Field(min_length=1)
    tank_id: str = Field(min_length=1)
    litres: float = Field(gt=0)
    price_per_litre: float = Field(gt=0)
    paid_now: float = Field(default=0, ge=0)
    arrangement: str = Field(default="Partial", max_length=60)
    notes: str = Field(default="", max_length=500)


class FuelReceiveCommand(BaseModel):
    """Bulk fuel purchase / restocking a tank."""
    tank_id: str = Field(min_length=1)
    fuel_supplier_id: str = Field(default="", max_length=36)
    fuel_supplier_name: str = Field(default="", max_length=200)
    fuel_type: str = Field(min_length=1, max_length=20)
    litres: float = Field(gt=0)
    cost_per_litre: float = Field(gt=0)
    doc_ref: str = Field(default="", max_length=100)
    status: str = Field(default="Paid", max_length=40)


class FuelDebtPaymentCommand(BaseModel):
    supplier_id: str = Field(min_length=1)
    amount: float = Field(gt=0)
    method: str = Field(default="Cash", max_length=60)
    notes: str = Field(default="", max_length=300)


class FuelAdjustmentCommand(BaseModel):
    expected_version: int = Field(ge=1)
    tank_id: str = Field(min_length=1)
    new_level_litres: float = Field(ge=0)
    reason: str = Field(default="Dipstick calibration", max_length=300)


# ─── Maintenance & Fleet ──────────────────────────────────────────────────────

class PartLine(BaseModel):
    part_id: str = Field(min_length=1)
    name: str = Field(min_length=1, max_length=200)
    qty: int = Field(gt=0)
    cost: float = Field(ge=0)


class WorkOrderCommand(BaseModel):
    boat_id: str = Field(min_length=1)
    supplier_id: str = Field(default="company", max_length=36)
    title: str = Field(min_length=1, max_length=200)
    fault: str = Field(default="", max_length=1000)
    mechanic: str = Field(default="", max_length=200)
    arrangement: str = Field(default="Company-paid", max_length=80)
    parts: list[PartLine] = Field(default_factory=list, max_length=100)
    parts_cost: float = Field(default=0, ge=0)
    labor_cost: float = Field(default=0, ge=0)
    other_cost: float = Field(default=0, ge=0)
    notes: str = Field(default="", max_length=500)


class WorkOrderStatusCommand(BaseModel):
    expected_version: int = Field(ge=1)
    status: str = Field(min_length=1, max_length=40)


class MaintDebtPaymentCommand(BaseModel):
    supplier_id: str = Field(min_length=1)
    amount: float = Field(gt=0)
    method: str = Field(default="Cash", max_length=60)
    notes: str = Field(default="", max_length=300)


# ─── Gear & Equipment ─────────────────────────────────────────────────────────

class GearIssueLine(BaseModel):
    item_id: str = Field(min_length=1)
    quantity: int = Field(gt=0)


class GearIssueCommand(BaseModel):
    supplier_id: str = Field(min_length=1)
    boat_id: str = Field(min_length=1)
    arrangement: str = Field(min_length=1, max_length=100)
    items: list[GearIssueLine] = Field(min_length=1, max_length=100)
    notes: str = Field(default="", max_length=500)


class GearReturnCommand(BaseModel):
    expected_version: int = Field(ge=1)
    returned_condition: str = Field(default="Good", max_length=80)
    returned_qty: int = Field(ge=0)
    restock: bool = Field(default=True)
    liability_action: str = Field(
        default="Credit / Clear Fisherman Debt", max_length=100
    )
    return_notes: str = Field(default="", max_length=500)


# ─── Food & Catering ──────────────────────────────────────────────────────────

class CookAdvanceCommand(BaseModel):
    cook_id: str = Field(min_length=1)
    amount: float = Field(gt=0)
    purpose: str = Field(default="", max_length=300)
    allocated_boats: list[str] = Field(default_factory=list, max_length=20)


class CateringBoatAlloc(BaseModel):
    boat_id: str = Field(min_length=1)
    supplier_id: str = Field(default="", max_length=36)
    cost: float = Field(ge=0)
    recoverable: float = Field(ge=0)


class CateringExpenseCommand(BaseModel):
    cook_id: str = Field(min_length=1)
    advance_ref: str = Field(default="", max_length=20)
    meal_type: str = Field(default="Lunch & Supper", max_length=100)
    meals_count: int = Field(default=0, ge=0)
    crew_size: int = Field(default=0, ge=0)
    total_spent: float = Field(gt=0)
    money_returned: float = Field(default=0, ge=0)
    arrangement: str = Field(
        default="Fisherman-recoverable (Catch Deduction)", max_length=100
    )
    boat_allocations: list[CateringBoatAlloc] = Field(
        default_factory=list, max_length=20
    )
    notes: str = Field(default="", max_length=500)


# ─── Cash Advances (Matumizi) ─────────────────────────────────────────────────

class CashAdvanceRequestCommand(BaseModel):
    supplier_id: str = Field(min_length=1)
    boat_id: str = Field(min_length=1)
    amount: float = Field(gt=0)
    category: str = Field(default="Daily Matumizi", max_length=80)
    purpose: str = Field(default="", max_length=300)
    method: str = Field(default="Cash", max_length=60)
    phone: str = Field(default="", max_length=40)
    classification: str = Field(
        default="Fisherman-recoverable", max_length=80
    )


class CashAdvanceApproveCommand(BaseModel):
    expected_version: int = Field(ge=1)
    new_status: str = Field(min_length=1, max_length=20)   # "Approved" | "Rejected"
    approved_amount: Optional[float] = Field(default=None, gt=0)


class CashAdvanceRepayCommand(BaseModel):
    expected_version: int = Field(ge=1)
    amount: float = Field(gt=0)
    method: str = Field(default="Cash", max_length=60)
    notes: str = Field(default="", max_length=300)


# ─── Boat Construction / Investment ──────────────────────────────────────────

class ProjectStageInput(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    cost: float = Field(ge=0)
    status: str = Field(default="Pending", max_length=40)
    date: str = Field(default="", max_length=20)


class BoatProjectCommand(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    builder: str = Field(default="", max_length=200)
    builder_phone: str = Field(default="", max_length=40)
    assigned_supplier_id: str = Field(default="", max_length=36)
    target_boat_name: str = Field(default="", max_length=200)
    start_date: str = Field(default="", max_length=20)
    target_completion: str = Field(default="", max_length=20)
    timber_materials: float = Field(default=0, ge=0)
    labour: float = Field(default=0, ge=0)
    engine: float = Field(default=0, ge=0)
    equipment: float = Field(default=0, ge=0)
    ownership_type: str = Field(default="Company Asset", max_length=100)
    contractual_recoverable: float = Field(default=0, ge=0)
    repayment_terms: str = Field(default="", max_length=300)
    stages: list[ProjectStageInput] = Field(default_factory=list, max_length=20)
    notes: str = Field(default="", max_length=500)


class ProjectProgressCommand(BaseModel):
    expected_version: int = Field(ge=1)
    progress_pct: float = Field(ge=0, le=100)
    status: str = Field(min_length=1, max_length=40)


class ProjectPaymentCommand(BaseModel):
    expected_version: int = Field(ge=1)
    stage_name: str = Field(min_length=1, max_length=200)
    amount: float = Field(gt=0)


# ─── Crew Management ─────────────────────────────────────────────────────────

class CrewAssignmentCommand(BaseModel):
    boat_id: str = Field(min_length=1)
    boat_reg: str = Field(default="", max_length=60)
    boat_name: str = Field(default="", max_length=200)
    supplier_id: str = Field(default="", max_length=64)
    fisherman_name: str = Field(min_length=1, max_length=200)
    position: str = Field(default="Crew", max_length=60)
    account_type: str = Field(default="Permanent", max_length=60)
    phone: str = Field(default="", max_length=40)
    nida: str = Field(default="", max_length=60)
    start_date: str = Field(default="", max_length=40)
    rate_agreement: str = Field(default="", max_length=100)
    notes: str = Field(default="", max_length=500)


class KibaluaEngagementCommand(BaseModel):
    boat_id: str = Field(min_length=1)
    boat_reg: str = Field(default="", max_length=40)
    boat_name: str = Field(default="", max_length=200)
    fisherman_name: str = Field(min_length=1, max_length=200)
    phone: str = Field(default="", max_length=40)
    duration_days: int = Field(gt=0, le=365)
    start_date: str = Field(min_length=1, max_length=20)
    end_date: str = Field(min_length=1, max_length=20)
    daily_rate: float = Field(gt=0)
    advance_paid: float = Field(default=0, ge=0)
    notes: str = Field(default="", max_length=500)


class KibaluaSettleCommand(BaseModel):
    expected_version: int = Field(ge=1)
    final_payment: float = Field(ge=0)
    method: str = Field(default="Cash", max_length=60)
    notes: str = Field(default="", max_length=300)


class FishermanExitCommand(BaseModel):
    expected_version: int = Field(ge=1)
    assignment_id: str = Field(min_length=1)
    last_working_date: str = Field(min_length=1, max_length=20)
    reason: str = Field(default="Resigned", max_length=100)
    outstanding_advances: float = Field(default=0, ge=0)
    unreturned_gear: str = Field(default="None", max_length=200)
    final_balance_due: float = Field(default=0, ge=0)
    notes: str = Field(default="", max_length=500)


class BoatExpenseAllocCommand(BaseModel):
    boat_id: str = Field(min_length=1)
    boat_reg: str = Field(default="", max_length=40)
    boat_name: str = Field(default="", max_length=200)
    expense_type: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=300)
    total_amount: float = Field(gt=0)
    rule: str = Field(default="Equal allocation", max_length=80)
    crew_supplier_ids: list[str] = Field(default_factory=list, max_length=20)
    notes: str = Field(default="", max_length=300)


# ─── Season Settlement ────────────────────────────────────────────────────────

class FullSettlementCommand(BaseModel):
    supplier_id: str = Field(min_length=1)
    boat_id: str = Field(min_length=1)
    season_id: str = Field(default="", max_length=40)
    agreed_earnings: float = Field(ge=0)
    previous_paid: float = Field(default=0, ge=0)
    fuel_deducted: float = Field(default=0, ge=0)
    maint_deducted: float = Field(default=0, ge=0)
    gear_deducted: float = Field(default=0, ge=0)
    food_deducted: float = Field(default=0, ge=0)
    advances_deducted: float = Field(default=0, ge=0)
    other_deducted: float = Field(default=0, ge=0)
    method: str = Field(default="M-Pesa (Mobile Money)", max_length=60)
    settlement_type: str = Field(default="Full Season Settlement", max_length=80)
    notes: str = Field(default="", max_length=500)


# ─── Operating Expenses ───────────────────────────────────────────────────────

class ExpenseCommand(BaseModel):
    category: str = Field(min_length=1, max_length=100)
    amount: float = Field(gt=0)
    description: str = Field(default="", max_length=300)
    date: str = Field(default="", max_length=20)


# ─── Transport ───────────────────────────────────────────────────────────────

class TripCommand(BaseModel):
    trip_type: str = Field(default="Industry Delivery", max_length=100)
    mode: str = Field(default="Truck", max_length=100)
    from_location: str = Field(min_length=1, max_length=200)
    to_location: str = Field(min_length=1, max_length=200)
    cost: float = Field(gt=0)
    kg: float = Field(default=0, ge=0)
    price_per_kg: float = Field(default=0, ge=0)
    species: str = Field(default="", max_length=100)
    vehicle: str = Field(default="", max_length=100)
    temperature: str = Field(default="", max_length=20)
    sale_ref: str = Field(default="", max_length=36)
    method: str = Field(default="CRDB / NMB Bank", max_length=60)
    notes: str = Field(default="", max_length=300)


# ─── Compliance ───────────────────────────────────────────────────────────────

class CompliancePermitCommand(BaseModel):
    permit_type: str = Field(min_length=1, max_length=100)
    reference: str = Field(min_length=1, max_length=100)
    authority: str = Field(min_length=1, max_length=200)
    expiry_date: str = Field(min_length=1, max_length=20)


# ─── Season ───────────────────────────────────────────────────────────────────

class SeasonCommand(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    code: str = Field(min_length=1, max_length=40)
    start_date: str = Field(min_length=1, max_length=20)
    end_date: str = Field(min_length=1, max_length=20)
    target_kg: float = Field(default=0, ge=0)
    budget: float = Field(default=0, ge=0)
    notes: str = Field(default="", max_length=300)


class SetActiveSeasonCommand(BaseModel):
    expected_version: int = Field(ge=1)
    season_id: str = Field(min_length=1)

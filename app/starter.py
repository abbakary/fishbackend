"""Starter template for a freshly created branch.

Both clients (web `mkBranch(site, k, demo=False)` and Flutter `BranchData`) share this
JSON contract. The template contains only structural defaults — warehouses, sequence
counters, seasons, fuel tanks and empty collections — never demo business records, so
a new tenant starts clean and all data comes from real API usage.
"""

from datetime import datetime, timedelta, timezone

COLD_ROOMS = [
    "Main Cold Room (0°C to 2°C)",
    "Kigoma Landing Depot (-1°C to 1°C)",
    "Musoma Processing Plant (0°C to 2°C)",
]

DEFAULT_GEAR_ITEMS = [
    {"id": "gear_net_5in", "name": "Nylon Gill Net 5-inch (26-mesh deep)", "category": "Reusable Gear", "stock": 25, "minStock": 5, "unitCost": 45000, "sellPrice": 55000, "unit": "pcs"},
    {"id": "gear_hooks_9", "name": "Longline Hooks No. 9 (Mustad 100pk)", "category": "Consumables", "stock": 50, "minStock": 10, "unitCost": 28000, "sellPrice": 35000, "unit": "box"},
    {"id": "gear_jacket_pro", "name": "Life Jacket Marine Pro (SOLAS)", "category": "Safety Equipment", "stock": 20, "minStock": 4, "unitCost": 38000, "sellPrice": 48000, "unit": "pcs"},
    {"id": "gear_lamp_dagaa", "name": "Solar Submersible Lamp (Dagaa LED)", "category": "Electronics", "stock": 15, "minStock": 3, "unitCost": 75000, "sellPrice": 95000, "unit": "pcs"},
    {"id": "gear_rope_50m", "name": "Polypropylene Anchor Rope 12mm (50m)", "category": "Reusable Gear", "stock": 12, "minStock": 2, "unitCost": 42000, "sellPrice": 52000, "unit": "roll"},
]

_SEQUENCE = {"pur": 1, "sal": 1, "fuel": 1, "wo": 1, "set": 1, "gear": 1, "adv": 1, "proj": 1, "cat": 1, "kib": 1}


def _today(offset_days: int = 0) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=offset_days)).date().isoformat()


def starter_state(region: str = "") -> dict:
    site = region or "Mwanza Main Landing (Kirumba)"
    now = datetime.now(timezone.utc)
    return {
        "wh": list(COLD_ROOMS),
        "audit": [],
        "seq": dict(_SEQUENCE),
        "suppliers": [],
        "batches": [],
        "pays": [],
        "customers": [],
        "sales": [],
        "exps": [],
        "trips": [],
        "comp": [],
        "seasons": [
            {
                "id": "seas_2026_q1",
                "name": "Season 2026-Q1 (Lake Victoria Main Run)",
                "code": "2026-Q1",
                "start": _today(-60),
                "end": _today(90),
                "status": "Active",
                "targetKg": 250000,
                "budget": 65000000,
                "notes": "",
            }
        ],
        "activeSeasonId": "seas_2026_q1",
        "fishermanLedger": [],
        "boatCostLedger": [],
        "fuelTanks": [
            {"id": "tank_main_petrol", "name": "Main Tank (Petrol)", "location": site,
             "fuel": "Petrol", "capacity": 5000, "current": 0, "buyPrice": 2850, "sellPrice": 3200, "unit": "Litres"},
            {"id": "tank_main_diesel", "name": "Main Tank (Diesel)", "location": site,
             "fuel": "Diesel", "capacity": 6000, "current": 0, "buyPrice": 2950, "sellPrice": 3350, "unit": "Litres"},
        ],
        "fuelSuppliers": [],
        "fuelPurchases": [],
        "fuelIssues": [],
        "fuelPayments": [],
        "fuelAdjustments": [],
        "boats": [],
        "spareParts": [],
        "maintenanceOrders": [],
        "maintPayments": [],
        "gearItems": list(DEFAULT_GEAR_ITEMS),
        "gearIssues": [],
        "cateringCooks": [],
        "cookAdvances": [],
        "cateringExpenses": [],
        "boatProjects": [],
        "advances": [],
        "settlements": [],
        "fullSettlements": [],
        "boatCrewAssignments": [],
        "kibaluaEngagements": [],
        "fishermanExitRecords": [],
        "boatExpenseAllocations": [],
        "activeBoatId": "",
        "createdAt": now.isoformat(),
    }


def needs_starter(state: dict | None) -> bool:
    """A branch needs initialization when its state is empty of structure."""
    if not state:
        return True
    return not state.get("seq") or not state.get("seasons")


def initial_state(provided: dict | None, region: str = "") -> dict:
    """Merge client-provided state over the starter template.

    Provided keys always win; missing structural keys (seq, seasons, fuel tanks,
    empty collections) are filled in so every branch has a usable base.
    """
    if not provided:
        return starter_state(region)
    return {**starter_state(region), **provided}

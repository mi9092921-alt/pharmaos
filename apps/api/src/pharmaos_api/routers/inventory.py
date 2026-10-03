"""Inventory endpoints (P1-M7): stock-on-hand, batches, receiving, adjustments,
batch status (quarantine/recall), minimal suppliers, and the derived-cache
integrity view.

Permission tiers per the CLAUDE.md matrix:
  view    = inventory.view (all roles)
  receive = inventory.purchase (super_admin, branch_manager)
  adjust / quarantine / rebuild = inventory.adjust (super_admin, branch_manager,
            pharmacist)
Mutations enforce CSRF; lists are paginated (<=100). Batches remain the ONLY
quantity truth — every mutation flows through inventory_service so a movement is
written and the derived cache stays in the same transaction.
"""

import datetime as dt
import uuid
from decimal import Decimal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from pharmaos_api.db import get_session
from pharmaos_api.deps import get_current_user, require_permission
from pharmaos_api.errors import success_envelope
from pharmaos_api.models import MedicationBatch, User
from pharmaos_api.security.csrf import enforce_csrf
from pharmaos_api.services import inventory_service as svc
from pharmaos_api.services import pack_serial_service

router = APIRouter(prefix="/api/v1", tags=["inventory"])

_view = Depends(require_permission("inventory.view"))
_receive = Depends(require_permission("inventory.purchase"))
_adjust = Depends(require_permission("inventory.adjust"))


def _batch(b: MedicationBatch) -> dict[str, object]:
    return {
        "id": str(b.id),
        "branch_id": str(b.branch_id),
        "medication_id": str(b.medication_id),
        "batch_number": b.batch_number,
        "expiry_date": b.expiry_date.isoformat(),
        "quantity": str(b.quantity),
        "purchase_price": str(b.purchase_price),
        "supplier_id": str(b.supplier_id) if b.supplier_id else None,
        "status": b.status,
        "received_at": b.received_at.isoformat(),
    }


class ReceiveIn(BaseModel):
    branch_id: uuid.UUID
    medication_id: uuid.UUID
    batch_number: str = Field(min_length=1, max_length=50)
    expiry_date: dt.date
    quantity: Decimal = Field(gt=0, le=Decimal("1000000"))
    purchase_price: Decimal = Field(ge=0)
    supplier_id: uuid.UUID | None = None
    # P2-M3: 2D-scanned pack serials + their GTIN (both optional).
    gtin: str | None = Field(default=None, max_length=14)
    serials: list[str] = Field(default_factory=list, max_length=1000)


class AdjustIn(BaseModel):
    quantity_delta: Decimal = Field(le=Decimal("1000000"), ge=Decimal("-1000000"))
    reason: str = Field(min_length=1, max_length=500)


class StatusIn(BaseModel):
    status: str = Field(pattern="^(active|quarantined|expired|recalled|depleted)$")
    reason: str = Field(default="", max_length=500)


class SupplierIn(BaseModel):
    name: str = Field(min_length=1, max_length=255)


class RebuildIn(BaseModel):
    branch_id: uuid.UUID


@router.get("/inventory")
async def list_inventory(
    branch_id: uuid.UUID = Query(),
    search: str | None = Query(default=None, max_length=120),
    low_stock: bool = Query(default=False),
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=svc.MAX_PAGE_SIZE),
    session: AsyncSession = Depends(get_session),
    _: None = _view,
) -> dict[str, object]:
    """Stock on hand per medication for a branch (from the derived cache)."""
    rows, total = await svc.list_inventory(
        session,
        branch_id=branch_id,
        search=search,
        low_stock_only=low_stock,
        skip=skip,
        limit=limit,
    )
    return success_envelope(
        rows, meta={"page": skip // limit + 1, "total": total, "per_page": limit}
    )


@router.get("/inventory/batches")
async def list_batches(
    branch_id: uuid.UUID = Query(),
    medication_id: uuid.UUID | None = Query(default=None),
    status: str | None = Query(default=None, max_length=20),
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=svc.MAX_PAGE_SIZE),
    session: AsyncSession = Depends(get_session),
    _: None = _view,
) -> dict[str, object]:
    """Batches for a branch in FEFO order; optionally scoped to one medication."""
    rows, total = await svc.list_batches(
        session,
        branch_id=branch_id,
        medication_id=medication_id,
        status=status,
        skip=skip,
        limit=limit,
    )
    return success_envelope(
        rows, meta={"page": skip // limit + 1, "total": total, "per_page": limit}
    )


@router.post("/inventory/receive")
async def receive_stock(
    body: ReceiveIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
    actor: User = Depends(get_current_user),
    _: None = _receive,
) -> dict[str, object]:
    """Receive a batch (prefill from a 2D GS1 scan via /catalog/parse-gs1)."""
    enforce_csrf(request)
    batch = await svc.receive_stock(
        session,
        actor=actor,
        branch_id=body.branch_id,
        medication_id=body.medication_id,
        batch_number=body.batch_number,
        expiry_date=body.expiry_date,
        quantity=body.quantity,
        purchase_price=body.purchase_price,
        supplier_id=body.supplier_id,
        gtin=body.gtin,
        serials=body.serials,
    )
    return success_envelope(_batch(batch))


@router.post("/inventory/batches/{batch_id}/adjust")
async def adjust_batch(
    batch_id: uuid.UUID,
    body: AdjustIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
    actor: User = Depends(get_current_user),
    _: None = _adjust,
) -> dict[str, object]:
    """Manual adjustment (count correction / damage) — reason is mandatory, audited."""
    enforce_csrf(request)
    batch = await svc.get_batch(session, batch_id)
    batch = await svc.adjust_batch(
        session, actor=actor, batch=batch, quantity_delta=body.quantity_delta, reason=body.reason
    )
    return success_envelope(_batch(batch))


@router.post("/inventory/batches/{batch_id}/status")
async def set_batch_status(
    batch_id: uuid.UUID,
    body: StatusIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
    actor: User = Depends(get_current_user),
    _: None = _adjust,
) -> dict[str, object]:
    """Change batch status. Quarantine/recall removes the batch from sellable stock."""
    enforce_csrf(request)
    batch = await svc.get_batch(session, batch_id)
    batch = await svc.set_batch_status(
        session, actor=actor, batch=batch, status=body.status, reason=body.reason
    )
    return success_envelope(_batch(batch))


@router.get("/inventory/branches")
async def list_branches(
    session: AsyncSession = Depends(get_session), _: None = _view
) -> dict[str, object]:
    """Active branches for operational selection (inventory is branch-scoped)."""
    return success_envelope(await svc.list_branches_min(session))


@router.get("/inventory/suppliers")
async def list_suppliers(
    session: AsyncSession = Depends(get_session), _: None = _view
) -> dict[str, object]:
    return success_envelope(await svc.list_suppliers(session))


@router.post("/inventory/suppliers")
async def create_supplier(
    body: SupplierIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
    actor: User = Depends(get_current_user),
    _: None = _receive,
) -> dict[str, object]:
    enforce_csrf(request)
    return success_envelope(await svc.create_supplier(session, actor=actor, name=body.name))


@router.get("/inventory/serials")
async def list_serials(
    branch_id: uuid.UUID = Query(),
    batch_id: uuid.UUID | None = Query(default=None),
    status: str | None = Query(default=None, max_length=20),
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=pack_serial_service.MAX_PAGE_SIZE),
    session: AsyncSession = Depends(get_session),
    _: None = _view,
) -> dict[str, object]:
    """Pack serials for a branch (optionally scoped to a batch/status) — the 2D
    track-and-trace trail captured at receive and linked at dispense."""
    rows, total = await pack_serial_service.list_serials(
        session, branch_id=branch_id, batch_id=batch_id, status=status, skip=skip, limit=limit
    )
    return success_envelope(
        rows, meta={"page": skip // limit + 1, "total": total, "per_page": limit}
    )


@router.get("/inventory/drift")
async def check_drift(
    branch_id: uuid.UUID = Query(),
    session: AsyncSession = Depends(get_session),
    _: None = _view,
) -> dict[str, object]:
    """Cache-integrity view: rows where cached_quantity != SUM(active batches)."""
    drift = await svc.drift_check(session, branch_id)
    return success_envelope({"drift": drift, "ok": not drift})


@router.post("/inventory/rebuild")
async def rebuild_cache(
    body: RebuildIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
    _: None = _adjust,
) -> dict[str, object]:
    """Rebuild the derived cache from batch truth for a branch (safe; idempotent)."""
    enforce_csrf(request)
    rows = await svc.rebuild_cache(session, body.branch_id)
    return success_envelope({"rows": rows})


# ------------------------------ batch tracking (M4) ------------------------------


@router.get("/inventory/expiry-alerts")
async def expiry_alerts(
    branch_id: uuid.UUID = Query(),
    session: AsyncSession = Depends(get_session),
    _: None = _view,
) -> dict[str, object]:
    """Near-expiry alerts for a branch's active stock, bucketed 30 / 60 / 90 days
    (+ an already-expired bucket). Severities follow CLAUDE.md ALERT_RULES."""
    return success_envelope(await svc.expiry_alerts(session, branch_id=branch_id))


@router.get("/inventory/batch-report")
async def batch_report(
    branch_id: uuid.UUID = Query(),
    session: AsyncSession = Depends(get_session),
    _: None = _view,
) -> dict[str, object]:
    """Batch inventory by status (count / quantity / value) with the sellable vs.
    locked-up (quarantined + expired + recalled) capital split."""
    return success_envelope(await svc.batch_status_report(session, branch_id=branch_id))


@router.post("/inventory/expiry-sweep")
async def expiry_sweep(
    request: Request,
    session: AsyncSession = Depends(get_session),
    _: None = _adjust,
) -> dict[str, object]:
    """Run the auto-quarantine policy on demand: past-expiry ACTIVE batches are
    marked expired and removed from sellable stock (also runs at boot and via the
    CLI cron). Idempotent — a second run sweeps nothing. Returns the swept count."""
    enforce_csrf(request)
    return success_envelope(await svc.expiry_sweep(session))

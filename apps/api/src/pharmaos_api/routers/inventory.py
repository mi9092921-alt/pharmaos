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
import io
import uuid
from decimal import Decimal

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from pharmaos_api.db import get_session
from pharmaos_api.deps import get_current_user, require_permission
from pharmaos_api.errors import ApiError, ErrorCode, success_envelope
from pharmaos_api.models import MedicationBatch, User
from pharmaos_api.security.csrf import enforce_csrf
from pharmaos_api.services import inventory_service as svc
from pharmaos_api.services import pack_serial_service

router = APIRouter(prefix="/api/v1", tags=["inventory"])

_view = Depends(require_permission("inventory.view"))
_receive = Depends(require_permission("inventory.purchase"))
_adjust = Depends(require_permission("inventory.adjust"))
_edit = Depends(require_permission("inventory.edit"))


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
    # Optional packaging level: when set, quantity/purchase_price are at that
    # level (e.g. boxes) and are converted to the smallest unit for the batch.
    packaging_id: uuid.UUID | None = None
    # P2-M3: 2D-scanned pack serials + their GTIN (both optional).
    gtin: str | None = Field(default=None, max_length=14)
    serials: list[str] = Field(default_factory=list, max_length=1000)
    # Shelf slot for first receipt (M2): normalized first, then written ONLY
    # when the current slot is NULL — never clears or overwrites via receive.
    shelf_location: str | None = Field(default=None, max_length=10)


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


class StockSettingsIn(BaseModel):
    branch_id: uuid.UUID
    medication_id: uuid.UUID
    reorder_point: Decimal | None = Field(default=None, ge=0, le=Decimal("1000000"))
    min_stock_level: Decimal | None = Field(default=None, ge=0, le=Decimal("1000000"))
    max_stock_level: Decimal | None = Field(default=None, ge=0, le=Decimal("1000000"))
    # No pattern here by design (M2): raw input is normalized FIRST in the
    # service (a-3 -> A3), then pattern-checked. max_length is a cheap guard.
    shelf_location: str | None = Field(default=None, max_length=10)


@router.get("/inventory")
async def list_inventory(
    branch_id: uuid.UUID = Query(),
    search: str | None = Query(default=None, max_length=120),
    low_stock: bool = Query(default=False),
    shelf: str | None = Query(default=None, max_length=10),
    zone: str | None = Query(default=None, max_length=1),
    sort: str = Query(default="stock", pattern="^(stock|location)$"),
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=svc.MAX_PAGE_SIZE),
    session: AsyncSession = Depends(get_session),
    _: None = _view,
) -> dict[str, object]:
    """Stock on hand per medication for a branch (from the derived cache).

    `shelf` filters one exact slot (normalized first); `zone` filters one
    letter prefix; `sort=location` walks the shelves numerically for
    stocktaking instead of the default low-stock-first order."""
    rows, total = await svc.list_inventory(
        session,
        branch_id=branch_id,
        search=search,
        low_stock_only=low_stock,
        shelf=shelf,
        zone=zone,
        sort=sort,
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
        packaging_id=body.packaging_id,
        gtin=body.gtin,
        serials=body.serials,
        shelf_location=body.shelf_location,
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


@router.patch("/inventory/settings")
async def update_stock_settings(
    body: StockSettingsIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
    actor: User = Depends(get_current_user),
    _: None = _edit,
) -> dict[str, object]:
    """Set reorder point / min / max / shelf for one medication in a branch.

    Quantity is never touched here (batches stay the sole truth); at least one
    field is required. Audited as settings-changed with old/new shelf values.
    Shelf contract: absent key = no change; explicit null/"" = clear to NULL;
    anything else is normalized first (a-3 -> A3), then validated."""
    enforce_csrf(request)
    changes = body.model_dump(exclude_unset=True)
    return success_envelope(
        await svc.update_stock_settings(
            session,
            actor=actor,
            branch_id=body.branch_id,
            medication_id=body.medication_id,
            reorder_point=changes.get("reorder_point"),
            min_stock_level=changes.get("min_stock_level"),
            max_stock_level=changes.get("max_stock_level"),
            shelf_location=changes.get("shelf_location", svc._UNSET),
        )
    )


@router.post("/inventory/shelf-import")
async def import_shelf_locations(
    request: Request,
    branch_id: uuid.UUID = Form(),
    dry_run: bool = Form(default=True),
    file: UploadFile = File(),
    session: AsyncSession = Depends(get_session),
    actor: User = Depends(get_current_user),
    _: None = _edit,
) -> dict[str, object]:
    """Bulk shelf assignment from an .xlsx file (columns: barcode_or_name,
    shelf) — the physical stocktake sheet.

    dry_run=true (default) previews without writing; execute re-validates
    fully (a preview may be stale). Caps: 2MB file, 2000 data rows.
    Medication matching is barcode-exact, else exact-name-or-reject —
    never fuzzy. inventory.edit + CSRF."""
    enforce_csrf(request)
    raw = await file.read()
    if len(raw) > svc.SHELF_IMPORT_MAX_BYTES:
        raise ApiError(ErrorCode.VALIDATION_FAILED, 422, message="File too large.")
    try:
        import openpyxl

        wb = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
        ws = wb.active
        iterator = ws.iter_rows(values_only=True)
        header = [str(h).strip().lower() if h is not None else "" for h in next(iterator)]
        missing = [c for c in svc.SHELF_IMPORT_COLUMNS if c not in header]
        if missing:
            raise ValueError(f"missing columns: {missing}")
        idx = {name: header.index(name) for name in svc.SHELF_IMPORT_COLUMNS}
        rows: list[dict[str, str]] = []
        for values in iterator:
            if all(v is None or str(v).strip() == "" for v in values):
                continue
            rows.append(
                {
                    name: ("" if values[i] is None else str(values[i]).strip())
                    for name, i in idx.items()
                    if i < len(values)
                }
            )
            if len(rows) > svc.SHELF_IMPORT_MAX_ROWS:
                raise ValueError("too many rows")
        wb.close()
    except ValueError as e:
        raise ApiError(ErrorCode.VALIDATION_FAILED, 422, message=str(e)) from e
    except Exception as e:  # noqa: BLE001 — unreadable workbook, any backend error
        raise ApiError(ErrorCode.VALIDATION_FAILED, 422, message="Unreadable workbook.") from e
    return success_envelope(
        await svc.import_shelf_locations(
            session, actor=actor, branch_id=branch_id, rows=rows, dry_run=dry_run
        )
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

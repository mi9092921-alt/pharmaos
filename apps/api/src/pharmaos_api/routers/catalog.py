"""Medications catalog endpoints (P1-M5).

Permission tiers per the CLAUDE.md matrix:
  view = inventory.view (all roles) · add = inventory.add · edit = inventory.edit
  delete = inventory.delete. Mutations enforce CSRF; lists are paginated (<=100).
"""

import csv
import datetime as dt
import io
import uuid
from decimal import Decimal

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from pharmaos_api.audit import AuditAction
from pharmaos_api.db import get_session
from pharmaos_api.deps import get_current_user, require_permission
from pharmaos_api.errors import ApiError, ErrorCode, success_envelope
from pharmaos_api.gs1 import Gs1ParseError, parse_gs1
from pharmaos_api.models import Medication, MedicationBarcode, MedicationPackaging, User
from pharmaos_api.security.csrf import enforce_csrf
from pharmaos_api.services import audit_service
from pharmaos_api.services import catalog_io_service as catalog_io
from pharmaos_api.services import catalog_service as svc

router = APIRouter(prefix="/api/v1", tags=["catalog"])

_view = Depends(require_permission("inventory.view"))
_add = Depends(require_permission("inventory.add"))
_edit = Depends(require_permission("inventory.edit"))
_delete = Depends(require_permission("inventory.delete"))


def _med(m: Medication) -> dict[str, object]:
    return {
        "id": str(m.id),
        "trade_name": m.trade_name,
        "trade_name_ar": m.trade_name_ar,
        "scientific_name": m.scientific_name,
        "manufacturer": m.manufacturer,
        "drug_class": m.drug_class,
        "route": m.route,
        "requires_prescription": m.requires_prescription,
        "controlled_substance": m.controlled_substance,
        "storage_conditions": m.storage_conditions,
        "eda_registration_no": m.eda_registration_no,
        "gtin": m.gtin,
        "is_medicine": m.is_medicine,
        "is_active": m.is_active,
    }


def _pkg(p: MedicationPackaging) -> dict[str, object]:
    return {
        "id": str(p.id),
        "level": p.level,
        "unit_id": str(p.unit_id),
        "name_ar": p.name_ar,
        "qty_in_parent": str(p.qty_in_parent) if p.qty_in_parent is not None else None,
        "is_sellable": p.is_sellable,
        "selling_price": str(p.selling_price),
        "is_default_sale": p.is_default_sale,
        "price_source": p.price_source,
    }


def _bc(b: MedicationBarcode) -> dict[str, object]:
    return {
        "id": str(b.id),
        "barcode": b.barcode,
        "barcode_type": b.barcode_type,
        "packaging_id": str(b.packaging_id) if b.packaging_id else None,
        "is_primary": b.is_primary,
    }


class MedicationIn(BaseModel):
    trade_name: str = Field(min_length=1, max_length=255)
    trade_name_ar: str | None = Field(default=None, max_length=255)
    scientific_name: str | None = Field(default=None, max_length=255)
    manufacturer: str | None = Field(default=None, max_length=255)
    drug_class: str | None = Field(default=None, max_length=100)
    route: str | None = Field(default=None, max_length=50)
    requires_prescription: bool = False
    controlled_substance: bool = False
    storage_conditions: str | None = Field(default=None, max_length=100)
    eda_registration_no: str | None = Field(default=None, max_length=50)
    gtin: str | None = Field(default=None, min_length=14, max_length=14)
    is_medicine: bool = True
    is_active: bool = True


class MedicationPatch(BaseModel):
    trade_name: str | None = Field(default=None, min_length=1, max_length=255)
    trade_name_ar: str | None = Field(default=None, max_length=255)
    scientific_name: str | None = Field(default=None, max_length=255)
    manufacturer: str | None = Field(default=None, max_length=255)
    drug_class: str | None = Field(default=None, max_length=100)
    route: str | None = Field(default=None, max_length=50)
    requires_prescription: bool | None = None
    controlled_substance: bool | None = None
    storage_conditions: str | None = Field(default=None, max_length=100)
    eda_registration_no: str | None = Field(default=None, max_length=50)
    gtin: str | None = Field(default=None, min_length=14, max_length=14)
    is_medicine: bool | None = None
    is_active: bool | None = None


class PackagingLevelBody(BaseModel):
    level: int = Field(ge=1, le=3)
    unit_id: uuid.UUID
    name_ar: str = Field(min_length=1, max_length=50)
    qty_in_parent: Decimal | None = Field(default=None, gt=0)
    selling_price: Decimal = Field(ge=0)
    is_sellable: bool = True
    is_default_sale: bool = False


class PackagingIn(BaseModel):
    levels: list[PackagingLevelBody] = Field(min_length=1, max_length=3)
    price_source: str = Field(default="manual", pattern="^(manual|seed|import|provider)$")


class BarcodeIn(BaseModel):
    barcode: str = Field(min_length=6, max_length=64)
    barcode_type: str = Field(default="EAN13", pattern="^(EAN13|GS1_DATAMATRIX|CODE128)$")
    packaging_id: uuid.UUID | None = None
    is_primary: bool = False


@router.get("/medications")
async def list_medications(
    search: str | None = Query(default=None, max_length=120),
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=svc.MAX_PAGE_SIZE),
    session: AsyncSession = Depends(get_session),
    _: None = _view,
) -> dict[str, object]:
    meds, total = await svc.list_medications(session, search=search, skip=skip, limit=limit)
    return success_envelope(
        [_med(m) for m in meds],
        meta={"page": skip // limit + 1, "total": total, "per_page": limit},
    )


@router.post("/medications")
async def create_medication(
    body: MedicationIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
    actor: User = Depends(get_current_user),
    _: None = _add,
) -> dict[str, object]:
    enforce_csrf(request)
    med = await svc.create_medication(session, actor=actor, values=body.model_dump())
    return success_envelope(_med(med))


@router.get("/medications/{medication_id}")
async def get_medication(
    medication_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    _: None = _view,
) -> dict[str, object]:
    med = await svc.get_medication(session, medication_id)
    packaging = await svc.get_packaging(session, medication_id)
    barcodes = await svc.get_barcodes(session, medication_id)
    return success_envelope(
        {
            **_med(med),
            "packaging": [_pkg(p) for p in packaging],
            "barcodes": [_bc(b) for b in barcodes],
        }
    )


@router.patch("/medications/{medication_id}")
async def update_medication(
    medication_id: uuid.UUID,
    body: MedicationPatch,
    request: Request,
    session: AsyncSession = Depends(get_session),
    actor: User = Depends(get_current_user),
    _: None = _edit,
) -> dict[str, object]:
    enforce_csrf(request)
    med = await svc.get_medication(session, medication_id)
    med = await svc.update_medication(session, actor=actor, med=med, values=body.model_dump())
    return success_envelope(_med(med))


@router.delete("/medications/{medication_id}")
async def delete_medication(
    medication_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
    actor: User = Depends(get_current_user),
    _: None = _delete,
) -> dict[str, object]:
    enforce_csrf(request)
    med = await svc.get_medication(session, medication_id)
    await svc.soft_delete_medication(session, actor=actor, med=med)
    return success_envelope({"deleted": True})


@router.put("/medications/{medication_id}/packaging")
async def put_packaging(
    medication_id: uuid.UUID,
    body: PackagingIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
    actor: User = Depends(get_current_user),
    _: None = _edit,
) -> dict[str, object]:
    enforce_csrf(request)
    med = await svc.get_medication(session, medication_id)
    levels = [
        svc.PackagingLevelIn(
            level=x.level,
            unit_id=x.unit_id,
            name_ar=x.name_ar,
            qty_in_parent=x.qty_in_parent,
            selling_price=x.selling_price,
            is_sellable=x.is_sellable,
            is_default_sale=x.is_default_sale,
        )
        for x in body.levels
    ]
    rows = await svc.upsert_packaging(
        session, actor=actor, med=med, levels=levels, price_source=body.price_source
    )
    return success_envelope([_pkg(p) for p in rows])


@router.post("/medications/{medication_id}/barcodes")
async def add_barcode(
    medication_id: uuid.UUID,
    body: BarcodeIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
    actor: User = Depends(get_current_user),
    _: None = _edit,
) -> dict[str, object]:
    enforce_csrf(request)
    med = await svc.get_medication(session, medication_id)
    row = await svc.add_barcode(
        session,
        actor=actor,
        med=med,
        barcode=body.barcode,
        barcode_type=body.barcode_type,
        packaging_id=body.packaging_id,
        is_primary=body.is_primary,
    )
    return success_envelope(_bc(row))


@router.delete("/medications/{medication_id}/barcodes/{barcode_id}")
async def delete_barcode(
    medication_id: uuid.UUID,
    barcode_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
    actor: User = Depends(get_current_user),
    _: None = _edit,
) -> dict[str, object]:
    enforce_csrf(request)
    med = await svc.get_medication(session, medication_id)
    await svc.remove_barcode(session, actor=actor, med=med, barcode_id=barcode_id)
    return success_envelope({"deleted": True})


@router.get("/catalog/units")
async def list_units(
    session: AsyncSession = Depends(get_session), _: None = _view
) -> dict[str, object]:
    from sqlalchemy import text as sql_text

    rows = (
        await session.execute(
            sql_text("SELECT id, name_ar, name_en FROM units WHERE NOT is_deleted ORDER BY name_ar")
        )
    ).all()
    return success_envelope([{"id": str(r[0]), "name_ar": r[1], "name_en": r[2]} for r in rows])


@router.get("/catalog/parse-gs1")
async def parse_gs1_code(
    code: str = Query(min_length=4, max_length=120),
    session: AsyncSession = Depends(get_session),
    _: None = _view,
) -> dict[str, object]:
    """Parse a 2D GS1 DataMatrix scan; resolve the medication by GTIN if known."""
    try:
        pack = parse_gs1(code)
    except Gs1ParseError as exc:
        raise ApiError(ErrorCode.VALIDATION_FAILED, 422, message=str(exc)) from exc
    med = await svc.find_by_gtin(session, pack.gtin) if pack.gtin else None
    return success_envelope(
        {
            "gtin": pack.gtin,
            "expiry_date": pack.expiry_date.isoformat() if pack.expiry_date else None,
            "batch_number": pack.batch_number,
            "serial_number": pack.serial_number,
            "medication": _med(med) if med is not None else None,
        }
    )


# ------------------- Catalog import/export (super_admin only) -------------------
# CLAUDE.md "قالب ملف استيراد/تصدير الكاتلوج" — one file format, three entry
# points. Sensitive bulk admin action: catalog.import / catalog.export are
# granted to super_admin only (packages/shared/permissions.ts).

_io_export = Depends(require_permission("catalog.export"))
_io_import = Depends(require_permission("catalog.import"))


def _rows_from_xlsx_bytes(raw: bytes) -> list[dict[str, str]]:
    """Parse the FIRST worksheet (the template sheet — never the instructions
    sheet). Excel datetime cells are normalized to ISO dates up front so the
    service never sees '2027-05-31 00:00:00' strings."""
    import openpyxl

    wb = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
    ws = wb.worksheets[0]
    iterator = ws.iter_rows(values_only=True)
    header = [str(h).strip().lower() if h is not None else "" for h in next(iterator)]
    missing = [c for c in catalog_io._COLUMNS if c not in header]  # noqa: SLF001
    if missing:
        raise ValueError(f"missing columns: {missing}")
    idx = {name: header.index(name) for name in catalog_io._COLUMNS}  # noqa: SLF001
    rows: list[dict[str, str]] = []
    for values in iterator:
        if all(v is None or str(v).strip() == "" for v in values):
            continue
        row: dict[str, str] = {}
        for name, i in idx.items():
            if i >= len(values):
                row[name] = ""
                continue
            value = values[i]
            if isinstance(value, dt.datetime):
                value = value.date().isoformat()
            elif isinstance(value, dt.date):
                value = value.isoformat()
            row[name] = "" if value is None else str(value).strip()
        rows.append(row)
        if len(rows) > catalog_io.CATALOG_IMPORT_MAX_ROWS:  # noqa: SLF001
            raise ValueError("too many rows")
    wb.close()
    return rows


def _rows_from_csv_bytes(raw: bytes) -> list[dict[str, str]]:
    """CSV twin of the xlsx parser — utf-8-sig (BOM) like the catalog seeder."""
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")))
    missing = [c for c in catalog_io._COLUMNS if c not in (reader.fieldnames or [])]  # noqa: SLF001
    if missing:
        raise ValueError(f"missing columns: {missing}")
    wanted = set(catalog_io._COLUMNS)  # noqa: SLF001
    rows: list[dict[str, str]] = []
    for raw_row in reader:
        rows.append(
            {k: (v or "").strip() for k, v in raw_row.items() if k is not None and k in wanted}
        )
        if len(rows) > catalog_io.CATALOG_IMPORT_MAX_ROWS:  # noqa: SLF001
            raise ValueError("too many rows")
    return rows


@router.get("/catalog/export")
async def export_catalog(
    branch_id: uuid.UUID | None = Query(default=None),
    session: AsyncSession = Depends(get_session),
    actor: User = Depends(get_current_user),
    _: None = _io_export,
) -> Response:
    """Download the catalog as .xlsx — without branch_id it is the pure catalog
    template (batch columns empty); with branch_id every ACTIVE batch of that
    branch becomes a row (the quantities). Filenames stay ASCII: branch names
    are Arabic and must not travel in a Content-Disposition header."""
    payload, row_count = await catalog_io.export_catalog_xlsx(session, branch_id=branch_id)
    filename = f"catalog_{dt.date.today().strftime('%Y%m%d')}"
    if branch_id is not None:
        filename += f"_{branch_id.hex[:8]}"
    filename += ".xlsx"
    await audit_service.record_independent(
        AuditAction.CATALOG_EXPORTED,
        actor=actor,
        branch_id=branch_id,
        entity_type="catalog",
        metadata={"rows": row_count, "filename": filename},
    )
    return Response(
        content=payload,
        media_type=catalog_io.XLSX_MEDIA_TYPE,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/catalog/import-template")
async def get_import_template(
    _: None = _io_import,
) -> Response:
    """The empty import template: header-only sheet + Arabic instructions sheet
    (generated from the same column constant the parser validates against)."""
    return Response(
        content=catalog_io.build_import_template_xlsx(),
        media_type=catalog_io.XLSX_MEDIA_TYPE,
        headers={"Content-Disposition": 'attachment; filename="catalog_template.xlsx"'},
    )


@router.post("/catalog/import")
async def import_catalog(
    request: Request,
    branch_id: uuid.UUID = Form(),
    dry_run: bool = Form(default=True),
    file: UploadFile = File(),
    session: AsyncSession = Depends(get_session),
    actor: User = Depends(get_current_user),
    _: None = _io_import,
) -> dict[str, object]:
    """Bulk catalog import (.xlsx/.csv) — dry_run=true previews without
    writing; execute re-validates fully in THIS call. Matching is barcode/gtin
    exact then exact-name-or-create (never fuzzy). Caps: 10MB, 30k rows.
    catalog.import + CSRF."""
    enforce_csrf(request)
    raw = await file.read()
    if len(raw) > catalog_io.CATALOG_IMPORT_MAX_BYTES:
        raise ApiError(ErrorCode.VALIDATION_FAILED, 422, message="File too large.")
    suffix = (file.filename or "").rsplit(".", 1)[-1].lower()
    try:
        if suffix in {"xlsx", "xlsm"}:
            rows = _rows_from_xlsx_bytes(raw)
        elif suffix == "csv":
            rows = _rows_from_csv_bytes(raw)
        else:
            raise ApiError(
                ErrorCode.VALIDATION_FAILED,
                422,
                message="Unsupported file type — use .xlsx or .csv.",
            )
    except ApiError:
        raise
    except ValueError as e:
        raise ApiError(ErrorCode.VALIDATION_FAILED, 422, message=str(e)) from e
    except Exception as e:  # noqa: BLE001 — unreadable workbook, any backend error
        raise ApiError(ErrorCode.VALIDATION_FAILED, 422, message="Unreadable file.") from e
    return success_envelope(
        await catalog_io.import_catalog(
            session,
            actor=actor,
            branch_id=branch_id,
            rows=rows,
            dry_run=dry_run,
            filename=file.filename,
        )
    )

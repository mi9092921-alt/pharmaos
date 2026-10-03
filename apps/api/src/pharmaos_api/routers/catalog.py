"""Medications catalog endpoints (P1-M5).

Permission tiers per the CLAUDE.md matrix:
  view = inventory.view (all roles) · add = inventory.add · edit = inventory.edit
  delete = inventory.delete. Mutations enforce CSRF; lists are paginated (<=100).
"""

import uuid
from decimal import Decimal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from pharmaos_api.db import get_session
from pharmaos_api.deps import get_current_user, require_permission
from pharmaos_api.errors import ApiError, ErrorCode, success_envelope
from pharmaos_api.gs1 import Gs1ParseError, parse_gs1
from pharmaos_api.models import Medication, MedicationBarcode, MedicationPackaging, User
from pharmaos_api.security.csrf import enforce_csrf
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

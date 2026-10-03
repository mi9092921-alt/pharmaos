"""Branch & settings endpoints (P1-M4).

Reads require `settings.view` (super_admin + branch_manager); mutations require
`settings.edit` (super_admin). Mutations enforce CSRF and audit settings.changed.
"""

import uuid
from decimal import Decimal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from pharmaos_api.db import get_session
from pharmaos_api.deps import get_current_user, require_permission
from pharmaos_api.errors import success_envelope
from pharmaos_api.models import Branch, Settings, TaxProfile, User
from pharmaos_api.security.csrf import enforce_csrf
from pharmaos_api.services import config_service as svc

router = APIRouter(prefix="/api/v1", tags=["config"])

_view = Depends(require_permission("settings.view"))
_edit = Depends(require_permission("settings.edit"))


def _branch(b: Branch) -> dict[str, object]:
    return {
        "id": str(b.id),
        "name": b.name,
        "country_code": b.country_code,
        "currency_code": b.currency_code,
        "is_active": b.is_active,
    }


def _settings(s: Settings) -> dict[str, object]:
    return {
        "id": str(s.id),
        "branch_id": str(s.branch_id),
        "pharmacy_name": s.pharmacy_name,
        "pharmacy_logo": s.pharmacy_logo,
        "license_number": s.license_number,
        "address": s.address,
        "phone": s.phone,
        "tax_registration_no": s.tax_registration_no,
        "return_policy": s.return_policy,
        "thank_you_message": s.thank_you_message,
        "paper_size": s.paper_size,
        "show_pharmacist_signature": s.show_pharmacist_signature,
        "show_qr_code": s.show_qr_code,
        "max_discount_percent": str(s.max_discount_percent),
    }


def _tax_profile(tp: TaxProfile) -> dict[str, object]:
    return {
        "id": str(tp.id),
        "name": tp.name,
        "vat_rate": str(tp.vat_rate),
        "medicine_vat_rate": (
            str(tp.medicine_vat_rate) if tp.medicine_vat_rate is not None else None
        ),
        "einvoice_system": tp.einvoice_system,
    }


class UpdateBranchIn(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    country_code: str | None = Field(default=None, min_length=2, max_length=2)
    currency_code: str | None = Field(default=None, min_length=3, max_length=3)
    is_active: bool | None = None


class SettingsIn(BaseModel):
    pharmacy_name: str = Field(min_length=1, max_length=255)
    pharmacy_logo: str | None = Field(default=None, max_length=500)
    license_number: str | None = Field(default=None, max_length=100)
    address: str | None = Field(default=None, max_length=500)
    phone: str | None = Field(default=None, max_length=32)
    tax_registration_no: str | None = Field(default=None, max_length=50)
    return_policy: str | None = None
    thank_you_message: str | None = Field(default=None, max_length=255)
    paper_size: str = Field(default="80mm", pattern="^(80mm|A4|A5)$")
    show_pharmacist_signature: bool = False
    show_qr_code: bool = False
    max_discount_percent: Decimal = Field(default=Decimal(0), ge=Decimal(0), le=Decimal(100))


class TaxProfileIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    vat_rate: Decimal = Field(ge=Decimal(0), le=Decimal(100))
    medicine_vat_rate: Decimal | None = Field(default=None, ge=Decimal(0), le=Decimal(100))
    einvoice_system: str | None = Field(default=None, pattern="^(eta_ereceipt|zatca)$")


@router.get("/branches")
async def list_branches(
    session: AsyncSession = Depends(get_session), _: None = _view
) -> dict[str, object]:
    branches = await svc.list_branches(session)
    return success_envelope([_branch(b) for b in branches])


@router.get("/branches/{branch_id}")
async def get_branch(
    branch_id: uuid.UUID, session: AsyncSession = Depends(get_session), _: None = _view
) -> dict[str, object]:
    return success_envelope(_branch(await svc.get_branch(session, branch_id)))


@router.patch("/branches/{branch_id}")
async def update_branch(
    branch_id: uuid.UUID,
    body: UpdateBranchIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
    actor: User = Depends(get_current_user),
    _: None = _edit,
) -> dict[str, object]:
    enforce_csrf(request)
    branch = await svc.get_branch(session, branch_id)
    branch = await svc.update_branch(
        session,
        actor=actor,
        branch=branch,
        name=body.name,
        country_code=body.country_code,
        currency_code=body.currency_code,
        is_active=body.is_active,
    )
    return success_envelope(_branch(branch))


@router.get("/branches/{branch_id}/settings")
async def get_settings(
    branch_id: uuid.UUID, session: AsyncSession = Depends(get_session), _: None = _view
) -> dict[str, object]:
    await svc.get_branch(session, branch_id)
    settings = await svc.get_settings(session, branch_id)
    return success_envelope(_settings(settings) if settings is not None else None)


@router.put("/branches/{branch_id}/settings")
async def put_settings(
    branch_id: uuid.UUID,
    body: SettingsIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
    actor: User = Depends(get_current_user),
    _: None = _edit,
) -> dict[str, object]:
    enforce_csrf(request)
    settings = await svc.upsert_settings(
        session, actor=actor, branch_id=branch_id, values=body.model_dump()
    )
    return success_envelope(_settings(settings))


@router.get("/branches/{branch_id}/tax-profile")
async def get_tax_profile(
    branch_id: uuid.UUID, session: AsyncSession = Depends(get_session), _: None = _view
) -> dict[str, object]:
    """The branch's effective VAT profile (via its country); null if unconfigured."""
    await svc.get_branch(session, branch_id)
    profile = await svc.get_tax_profile(session, branch_id)
    return success_envelope(_tax_profile(profile) if profile is not None else None)


@router.patch("/tax-profiles/{profile_id}")
async def update_tax_profile(
    profile_id: uuid.UUID,
    body: TaxProfileIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
    actor: User = Depends(get_current_user),
    _: None = _edit,
) -> dict[str, object]:
    enforce_csrf(request)
    profile = await svc.get_tax_profile_by_id(session, profile_id)
    profile = await svc.update_tax_profile(
        session, actor=actor, profile=profile, values=body.model_dump()
    )
    return success_envelope(_tax_profile(profile))

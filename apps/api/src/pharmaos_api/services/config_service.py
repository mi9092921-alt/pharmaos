"""Branch + settings management (P1-M4).

Both are "system configuration"; mutations record the audited operation
`settings.changed` (the one AUDITED_OPERATIONS entry that covers config), with
metadata naming the entity and the changed fields. Branch currency/country
changes are configuration too, so they map to the same audited event.

No self-lockout concerns here; the settings.edit permission (super_admin) already
gates all mutations at the router.
"""

import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from pharmaos_api.audit import AuditAction
from pharmaos_api.errors import ApiError, ErrorCode
from pharmaos_api.models import Branch, Country, Currency, Settings, TaxProfile, User
from pharmaos_api.services import audit_service

_PAPER_SIZES = {"80mm", "A4", "A5"}


# --------------------------- branches ---------------------------


async def list_branches(session: AsyncSession) -> list[Branch]:
    return list(
        (
            await session.execute(
                select(Branch).where(Branch.is_deleted.is_(False)).order_by(Branch.created_at)
            )
        )
        .scalars()
        .all()
    )


async def get_branch(session: AsyncSession, branch_id: uuid.UUID) -> Branch:
    branch = (
        await session.execute(
            select(Branch).where(Branch.id == branch_id, Branch.is_deleted.is_(False))
        )
    ).scalar_one_or_none()
    if branch is None:
        raise ApiError(ErrorCode.VALIDATION_FAILED, 404, message="Branch not found.")
    return branch


async def _currency_exists(session: AsyncSession, code: str) -> bool:
    return (
        await session.execute(select(Currency.code).where(Currency.code == code))
    ).scalar_one_or_none() is not None


async def update_branch(
    session: AsyncSession,
    *,
    actor: User,
    branch: Branch,
    name: str | None = None,
    country_code: str | None = None,
    currency_code: str | None = None,
    is_active: bool | None = None,
) -> Branch:
    changed: list[str] = []
    if name is not None and name != branch.name:
        branch.name = name
        changed.append("name")
    if country_code is not None and country_code != branch.country_code:
        exists = (
            await session.execute(select(Country.code).where(Country.code == country_code))
        ).scalar_one_or_none()
        if exists is None:
            raise ApiError(ErrorCode.VALIDATION_FAILED, 422, message="Unknown country.")
        branch.country_code = country_code
        changed.append("country_code")
    if currency_code is not None and currency_code != branch.currency_code:
        if not await _currency_exists(session, currency_code):
            raise ApiError(ErrorCode.VALIDATION_FAILED, 422, message="Unknown currency.")
        branch.currency_code = currency_code
        changed.append("currency_code")
    if is_active is not None and is_active != branch.is_active:
        branch.is_active = is_active
        changed.append("is_active")

    if not changed:
        return branch

    branch.updated_by = actor.id
    await audit_service.record(
        session,
        AuditAction.SETTINGS_CHANGED,
        actor=actor,
        entity_type="branch",
        entity_id=branch.id,
        metadata={"entity": "branch", "fields": changed},
    )
    await session.commit()
    await session.refresh(branch)
    return branch


# --------------------------- settings ---------------------------

_SETTINGS_FIELDS = (
    "pharmacy_name",
    "pharmacy_logo",
    "license_number",
    "address",
    "phone",
    "tax_registration_no",
    "return_policy",
    "thank_you_message",
    "paper_size",
    "show_pharmacist_signature",
    "show_qr_code",
    "max_discount_percent",
)


async def get_settings(session: AsyncSession, branch_id: uuid.UUID) -> Settings | None:
    return (
        await session.execute(
            select(Settings).where(Settings.branch_id == branch_id, Settings.is_deleted.is_(False))
        )
    ).scalar_one_or_none()


def _validate_settings(values: dict[str, Any]) -> None:
    paper = values.get("paper_size")
    if paper is not None and paper not in _PAPER_SIZES:
        raise ApiError(ErrorCode.VALIDATION_FAILED, 422, message="Invalid paper size.")
    discount = values.get("max_discount_percent")
    if discount is not None and not (Decimal(0) <= Decimal(str(discount)) <= Decimal(100)):
        raise ApiError(ErrorCode.VALIDATION_FAILED, 422, message="Discount must be 0..100.")


async def upsert_settings(
    session: AsyncSession, *, actor: User, branch_id: uuid.UUID, values: dict[str, Any]
) -> Settings:
    """Create or update the branch's settings row. Audits settings.changed."""
    await get_branch(session, branch_id)  # ensure branch exists
    _validate_settings(values)

    settings = await get_settings(session, branch_id)
    is_new = settings is None
    if settings is None:
        if not values.get("pharmacy_name"):
            raise ApiError(ErrorCode.VALIDATION_FAILED, 422, message="pharmacy_name is required.")
        settings = Settings(
            branch_id=branch_id, pharmacy_name=values["pharmacy_name"], created_by=actor.id
        )
        session.add(settings)

    changed: list[str] = []
    for field in _SETTINGS_FIELDS:
        if (
            field in values
            and values[field] is not None
            and getattr(settings, field) != values[field]
        ):
            setattr(settings, field, values[field])
            changed.append(field)
    settings.updated_by = actor.id
    await session.flush()

    if is_new or changed:
        await audit_service.record(
            session,
            AuditAction.SETTINGS_CHANGED,
            actor=actor,
            entity_type="settings",
            entity_id=settings.id,
            metadata={"entity": "settings", "fields": changed, "created": is_new},
        )
    await session.commit()
    await session.refresh(settings)
    return settings


async def count_branches(session: AsyncSession) -> int:
    return (await session.execute(select(func.count(Branch.id)))).scalar_one()


# --------------------------- tax profile (P2-M6) ---------------------------

_TAX_FIELDS = ("name", "vat_rate", "medicine_vat_rate", "einvoice_system")


async def get_tax_profile(session: AsyncSession, branch_id: uuid.UUID) -> TaxProfile | None:
    """The branch's effective VAT profile (via its country); None if unconfigured."""
    return (
        await session.execute(
            select(TaxProfile)
            .join(Country, Country.tax_profile_id == TaxProfile.id)
            .join(Branch, Branch.country_code == Country.code)
            .where(
                Branch.id == branch_id,
                Branch.is_deleted.is_(False),
                TaxProfile.is_deleted.is_(False),
            )
        )
    ).scalar_one_or_none()


async def get_tax_profile_by_id(session: AsyncSession, profile_id: uuid.UUID) -> TaxProfile:
    profile = (
        await session.execute(
            select(TaxProfile).where(TaxProfile.id == profile_id, TaxProfile.is_deleted.is_(False))
        )
    ).scalar_one_or_none()
    if profile is None:
        raise ApiError(ErrorCode.VALIDATION_FAILED, 404, message="Tax profile not found.")
    return profile


async def update_tax_profile(
    session: AsyncSession, *, actor: User, profile: TaxProfile, values: dict[str, Any]
) -> TaxProfile:
    """Update the editable VAT fields (name / vat_rate / medicine_vat_rate /
    einvoice_system) and audit settings.changed. Country-level config — affects
    every branch in that country (gated to super_admin at the router)."""
    changed: list[str] = []
    for field in _TAX_FIELDS:
        if field in values and getattr(profile, field) != values[field]:
            setattr(profile, field, values[field])
            changed.append(field)
    if not changed:
        return profile
    profile.updated_by = actor.id
    await audit_service.record(
        session,
        AuditAction.SETTINGS_CHANGED,
        actor=actor,
        entity_type="tax_profile",
        entity_id=profile.id,
        metadata={"entity": "tax_profile", "fields": changed},
    )
    await session.commit()
    await session.refresh(profile)
    return profile

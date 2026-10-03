"""VAT computation per tax_profile (P2-M6).

Resolution: branch -> country -> tax_profile (seeded in core_schema; EG defaults
to 14% standard, medicine exempt). Prices are VAT-INCLUSIVE (Egyptian retail):
the customer-facing total equals the sum of shelf prices, and VAT is EXTRACTED
from that gross rather than added on top. CLAUDE.md: every invoice carries the
tax VALUES at issue time (a snapshot), so the caller persists the per-line rate
and amount plus the invoice total — this service only computes.
"""

import uuid
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_CENTS = Decimal("0.01")
_HUNDRED = Decimal(100)


@dataclass(frozen=True)
class TaxProfile:
    name: str
    vat_rate: Decimal  # standard rate (non-medicine)
    medicine_vat_rate: Decimal | None  # medicine rate; None = exempt (0%)
    einvoice_system: str | None  # 'eta_ereceipt' | 'zatca' | None


async def resolve_for_branch(session: AsyncSession, branch_id: uuid.UUID) -> TaxProfile | None:
    """The effective tax profile for a branch (via its country). None when the
    branch/country has no profile configured — the sale then applies 0% VAT."""
    row = (await session.execute(text("""
                SELECT tp.name, tp.vat_rate, tp.medicine_vat_rate, tp.einvoice_system
                FROM branches b
                JOIN countries c ON c.code = b.country_code AND NOT c.is_deleted
                JOIN tax_profiles tp ON tp.id = c.tax_profile_id AND NOT tp.is_deleted
                WHERE b.id = :b AND NOT b.is_deleted
                """).bindparams(b=branch_id))).first()
    if row is None:
        return None
    return TaxProfile(
        name=row[0],
        vat_rate=Decimal(row[1]),
        medicine_vat_rate=Decimal(row[2]) if row[2] is not None else None,
        einvoice_system=row[3],
    )


def rate_for(profile: TaxProfile | None, *, is_medicine: bool) -> Decimal:
    """VAT rate for a line. No profile -> 0. A medicine follows medicine_vat_rate
    when the profile sets one, else 0 (exempt — the Egyptian default where the
    column is NULL). Non-medicine SKUs use the standard vat_rate."""
    if profile is None:
        return Decimal(0)
    if is_medicine:
        return profile.medicine_vat_rate if profile.medicine_vat_rate is not None else Decimal(0)
    return profile.vat_rate


def split_inclusive(gross: Decimal, rate: Decimal) -> tuple[Decimal, Decimal]:
    """Split a VAT-INCLUSIVE gross amount into (net, vat) at `rate` percent.
    vat = gross * rate / (100 + rate), rounded to cents; net = gross - vat.
    rate <= 0 -> the whole amount is net and vat is zero (exempt)."""
    gross = gross.quantize(_CENTS)
    if rate <= 0:
        return gross, Decimal("0.00")
    vat = (gross * rate / (_HUNDRED + rate)).quantize(_CENTS, rounding=ROUND_HALF_UP)
    return (gross - vat), vat

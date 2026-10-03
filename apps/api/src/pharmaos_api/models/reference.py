"""Reference-table mirrors: currencies, tax profiles & countries.

Currencies/countries use natural primary keys (ISO codes) per the Phase 0 DDL,
so they do NOT use MandatoryColumnsMixin. tax_profiles is a UUID-keyed config
table (it carries the mandatory columns), so it uses the mixin.
"""

import uuid
from decimal import Decimal

from sqlalchemy import CHAR, ForeignKey, Numeric, SmallInteger, String, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from pharmaos_api.models.base import Base, MandatoryColumnsMixin


class Currency(Base):
    __tablename__ = "currencies"

    code: Mapped[str] = mapped_column(CHAR(3), primary_key=True)  # ISO 4217
    name_ar: Mapped[str] = mapped_column(String(50), nullable=False)
    symbol: Mapped[str] = mapped_column(String(8), nullable=False)
    decimal_places: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("2")
    )


class TaxProfile(MandatoryColumnsMixin, Base):
    """VAT configuration (P2-M6). vat_rate = standard; medicine_vat_rate NULL =
    medicines exempt. einvoice_system: 'eta_ereceipt' | 'zatca' | NULL."""

    __tablename__ = "tax_profiles"

    name: Mapped[str] = mapped_column(String(80), nullable=False)
    vat_rate: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    medicine_vat_rate: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    einvoice_system: Mapped[str | None] = mapped_column(String(20), nullable=True)


class Country(Base):
    __tablename__ = "countries"

    code: Mapped[str] = mapped_column(CHAR(2), primary_key=True)  # ISO 3166-1
    name_ar: Mapped[str] = mapped_column(String(80), nullable=False)
    default_currency: Mapped[str] = mapped_column(
        CHAR(3), ForeignKey("currencies.code"), nullable=False
    )
    tax_profile_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tax_profiles.id"), nullable=True
    )
    calendar: Mapped[str] = mapped_column(
        String(10), nullable=False, server_default=text("'gregory'")
    )

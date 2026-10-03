"""settings table mirror (per-branch invoice template + POS options)."""

import uuid
from decimal import Decimal

from sqlalchemy import Boolean, ForeignKey, Numeric, String, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from pharmaos_api.models.base import Base, MandatoryColumnsMixin


class Settings(MandatoryColumnsMixin, Base):
    __tablename__ = "settings"

    branch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("branches.id"), nullable=False
    )
    pharmacy_name: Mapped[str] = mapped_column(String(255), nullable=False)
    pharmacy_logo: Mapped[str | None] = mapped_column(String(500), nullable=True)
    license_number: Mapped[str | None] = mapped_column(String(100), nullable=True)
    address: Mapped[str | None] = mapped_column(String(500), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    tax_registration_no: Mapped[str | None] = mapped_column(String(50), nullable=True)
    return_policy: Mapped[str | None] = mapped_column(Text, nullable=True)
    thank_you_message: Mapped[str | None] = mapped_column(String(255), nullable=True)
    paper_size: Mapped[str] = mapped_column(
        String(8), nullable=False, server_default=text("'80mm'")
    )
    show_pharmacist_signature: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("FALSE")
    )
    show_qr_code: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("FALSE")
    )
    max_discount_percent: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), nullable=False, server_default=text("0")
    )
    # P2 review C6 / plan D3: returned stock is quarantined by default (safe);
    # a branch may opt in to sending returned stock straight back to sellable.
    returned_stock_to_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("FALSE")
    )

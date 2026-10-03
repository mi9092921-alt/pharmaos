"""Customer + loyalty models (P2-M5).

PII (national_id, insurance_number) is AES-256-GCM field-encrypted in the
service layer -> LargeBinary/BYTEA; name and phone are plaintext (the pharmacy's
primary search/lookup keys). loyalty_points is the derived balance; the
append-only loyalty_transactions ledger is the truth.
"""

import uuid

from sqlalchemy import BigInteger, Boolean, ForeignKey, LargeBinary, String, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from pharmaos_api.models.base import Base, MandatoryColumnsMixin


class Customer(MandatoryColumnsMixin, Base):
    __tablename__ = "customers"

    name: Mapped[str] = mapped_column(String(160), nullable=False)
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    national_id_encrypted: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    insurance_number_encrypted: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    loyalty_points: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    notes: Mapped[str | None] = mapped_column(String, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("TRUE"))


class LoyaltyTransaction(MandatoryColumnsMixin, Base):
    __tablename__ = "loyalty_transactions"

    customer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("customers.id"), nullable=False
    )
    points_delta: Mapped[int] = mapped_column(BigInteger, nullable=False)
    txn_type: Mapped[str] = mapped_column(String(20), nullable=False)
    reference_type: Mapped[str | None] = mapped_column(String(30), nullable=True)
    reference_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    reason: Mapped[str | None] = mapped_column(String, nullable=True)

"""Prescriptions + the controlled-substance register (P2-M8).

notes_encrypted is AES-256-GCM field-encrypted in the service layer (CLAUDE.md
classification: "prescriptions.notes"). ControlledSubstanceLog deliberately does
NOT use MandatoryColumnsMixin — like audit_logs, it is a lean, INSERT-only
register: no updated_at/updated_by/is_deleted/sync_version, and a DB trigger
(migration 1800) forbids UPDATE/DELETE for every role (CLAUDE.md: controlled
substance records are never truly deleted).
"""

import datetime as dt
import uuid
from decimal import Decimal

from sqlalchemy import Date, DateTime, ForeignKey, LargeBinary, Numeric, String, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from pharmaos_api.models.base import Base, MandatoryColumnsMixin


class Prescription(MandatoryColumnsMixin, Base):
    __tablename__ = "prescriptions"

    branch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("branches.id"), nullable=False
    )
    customer_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("customers.id"), nullable=True
    )
    doctor_name: Mapped[str] = mapped_column(String(160), nullable=False)
    doctor_license_no: Mapped[str | None] = mapped_column(String(50), nullable=True)
    prescription_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    notes_encrypted: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text("'pending'")
    )


class PrescriptionItem(MandatoryColumnsMixin, Base):
    __tablename__ = "prescription_items"

    branch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("branches.id"), nullable=False
    )
    prescription_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("prescriptions.id"), nullable=False
    )
    medication_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("medications.id"), nullable=False
    )
    packaging_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("medication_packaging.id"), nullable=False
    )
    prescribed_qty: Mapped[Decimal] = mapped_column(Numeric(10, 3), nullable=False)
    prescribed_qty_smallest: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False)
    dispensed_qty_smallest: Mapped[Decimal] = mapped_column(
        Numeric(12, 3), nullable=False, server_default=text("0")
    )


class ControlledSubstanceLog(Base):
    """Append-only dispensing register — see module docstring."""

    __tablename__ = "controlled_substance_log"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    branch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("branches.id"), nullable=False
    )
    medication_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("medications.id"), nullable=False
    )
    batch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("medication_batches.id"), nullable=False
    )
    invoice_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("invoices.id"), nullable=False
    )
    invoice_item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("invoice_items.id"), nullable=False
    )
    prescription_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("prescriptions.id"), nullable=True
    )
    quantity_dispensed: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False)
    dispensed_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("NOW()")
    )

"""Egyptian compliance outbox tables (P2-M10 ETA e-receipt, P2-M11 EDA T&T).

Both are outbox queues drained by a background worker; the sale/dispense that
produces them never blocks on the network (rows are enqueued inside the local
transaction). See services/compliance/ for the port/adapter + local simulator.
"""

import datetime as dt
import uuid

from sqlalchemy import Date, DateTime, ForeignKey, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from pharmaos_api.models.base import Base, MandatoryColumnsMixin


class EReceiptQueue(MandatoryColumnsMixin, Base):
    """ETA e-receipt outbox: one row per invoice, drained to the ETA adapter."""

    __tablename__ = "ereceipt_queue"

    branch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("branches.id"), nullable=False
    )
    invoice_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("invoices.id"), nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text("'pending'")
    )
    payload: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
    signed_payload: Mapped[str | None] = mapped_column(Text, nullable=True)
    eta_uuid: Mapped[str | None] = mapped_column(String(64), nullable=True)
    qr_data: Mapped[str | None] = mapped_column(Text, nullable=True)
    submission_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    submitted_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    accepted_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class TtEvent(MandatoryColumnsMixin, Base):
    """EDA track & trace outbox: receive/dispense/destroy/import events reported
    to the national system via the EDA adapter."""

    __tablename__ = "tt_events"

    branch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("branches.id"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(20), nullable=False)
    pack_serial_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("pack_serials.id"), nullable=True
    )
    gtin: Mapped[str] = mapped_column(String(14), nullable=False)
    serial_number: Mapped[str] = mapped_column(String(64), nullable=False)
    batch_number: Mapped[str | None] = mapped_column(String(50), nullable=True)
    expiry_date: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    invoice_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("invoices.id"), nullable=True
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text("'pending'")
    )
    report_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    reported_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    payload: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)

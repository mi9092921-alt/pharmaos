"""Receipt composition (P1-M9) — one source of truth for what a receipt shows.

Loads a completed invoice with its items, aggregates FEFO batch slices back
into sold lines (the sale engine may split one cart line across batches — the
customer sees ONE line per medication+packaging), merges the branch's
invoice-template settings (M4) with safe defaults, and renders ESC/POS bytes.

Both the thermal path (POST /pos/invoices/{id}/print) and the browser-print
fallback (GET /pos/invoices/{id}/receipt) consume this module, so the two
outputs can never drift.

QR content is a structured placeholder (invoice number/total/timestamp) until
the Egyptian e-receipt integration (Phase 2) supplies the official ETA UUID —
settings.show_qr_code already controls its presence either way.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pharmaos_api.errors import ApiError, ErrorCode
from pharmaos_api.models import (
    Branch,
    Invoice,
    InvoiceItem,
    Medication,
    MedicationPackaging,
)
from pharmaos_api.printing.escpos import ReceiptData, ReceiptLine, build_receipt
from pharmaos_api.services import config_service

DEFAULT_THANK_YOU = "شكراً لزيارتكم — نتمنى لكم الشفاء العاجل"
_CURRENCY_SYMBOLS = {"EGP": "ج.م"}
_PAYMENT_DISPLAY = {"cash": "نقدي", "card": "بطاقة"}
THERMAL_PAPER = "80mm"


@dataclass(frozen=True)
class ReceiptLineOut:
    name: str
    unit_name: str
    quantity: Decimal
    unit_price: Decimal
    line_total: Decimal


@dataclass(frozen=True)
class InvoiceReceipt:
    invoice_id: uuid.UUID
    invoice_number: str
    created_at: datetime
    payment_method: str
    currency_code: str
    subtotal: Decimal
    discount: Decimal
    tax: Decimal
    total: Decimal
    tendered: Decimal | None
    change_due: Decimal | None
    branch_name: str
    pharmacy_name: str
    address: str | None
    phone: str | None
    license_number: str | None
    tax_registration_no: str | None
    thank_you_message: str
    return_policy: str | None
    paper_size: str
    show_qr_code: bool
    show_pharmacist_signature: bool
    qr_content: str | None
    lines: list[ReceiptLineOut]

    @property
    def currency_symbol(self) -> str:
        return _CURRENCY_SYMBOLS.get(self.currency_code, self.currency_code)

    @property
    def payment_method_display(self) -> str:
        return _PAYMENT_DISPLAY.get(self.payment_method, self.payment_method)


async def load_invoice_receipt(session: AsyncSession, invoice_id: uuid.UUID) -> InvoiceReceipt:
    invoice = (
        await session.execute(
            select(Invoice).where(Invoice.id == invoice_id, Invoice.is_deleted.is_(False))
        )
    ).scalar_one_or_none()
    if invoice is None:
        raise ApiError(ErrorCode.VALIDATION_FAILED, 404, message="Invoice not found.")

    branch = await session.get(Branch, invoice.branch_id)
    if branch is None:
        raise ApiError(ErrorCode.VALIDATION_FAILED, 404, message="Branch not found.")
    settings = await config_service.get_settings(session, invoice.branch_id)

    rows = (
        await session.execute(
            select(InvoiceItem, Medication, MedicationPackaging)
            .join(Medication, Medication.id == InvoiceItem.medication_id)
            .join(MedicationPackaging, MedicationPackaging.id == InvoiceItem.packaging_id)
            .where(InvoiceItem.invoice_id == invoice.id, InvoiceItem.is_deleted.is_(False))
            .order_by(InvoiceItem.created_at)
        )
    ).all()

    # Re-aggregate FEFO slices: one printed line per (medication, packaging).
    aggregated: dict[tuple[uuid.UUID, uuid.UUID], ReceiptLineOut] = {}
    for item, medication, packaging in rows:
        key = (item.medication_id, item.packaging_id)
        existing = aggregated.get(key)
        if existing is None:
            aggregated[key] = ReceiptLineOut(
                name=medication.trade_name_ar or medication.trade_name,
                unit_name=packaging.name_ar,
                quantity=item.quantity,
                unit_price=item.unit_price,
                line_total=item.line_total,
            )
        else:
            aggregated[key] = ReceiptLineOut(
                name=existing.name,
                unit_name=existing.unit_name,
                quantity=existing.quantity + item.quantity,
                unit_price=existing.unit_price,
                line_total=existing.line_total + item.line_total,
            )

    pharmacy_name = settings.pharmacy_name if settings else branch.name
    thank_you = (settings.thank_you_message if settings else None) or DEFAULT_THANK_YOU
    show_qr = bool(settings.show_qr_code) if settings else False
    qr_content = (
        f"PHARMAOS|{invoice.invoice_number}|{invoice.total:.2f}"
        f"|{invoice.currency_code}|{invoice.created_at.isoformat()}"
        if show_qr
        else None
    )

    return InvoiceReceipt(
        invoice_id=invoice.id,
        invoice_number=invoice.invoice_number,
        created_at=invoice.created_at,
        payment_method=invoice.payment_method,
        currency_code=invoice.currency_code,
        subtotal=invoice.subtotal,
        discount=invoice.discount_amount,
        tax=invoice.tax_amount,
        total=invoice.total,
        tendered=invoice.tendered_amount,
        change_due=invoice.change_amount,
        branch_name=branch.name,
        pharmacy_name=pharmacy_name,
        address=settings.address if settings else None,
        phone=settings.phone if settings else None,
        license_number=settings.license_number if settings else None,
        tax_registration_no=settings.tax_registration_no if settings else None,
        thank_you_message=thank_you,
        return_policy=settings.return_policy if settings else None,
        paper_size=settings.paper_size if settings else THERMAL_PAPER,
        show_qr_code=show_qr,
        show_pharmacist_signature=(bool(settings.show_pharmacist_signature) if settings else False),
        qr_content=qr_content,
        lines=list(aggregated.values()),
    )


def to_escpos(receipt: InvoiceReceipt, *, open_drawer: bool) -> bytes:
    """Render the loaded receipt to the ESC/POS byte stream (80mm thermal)."""
    return build_receipt(
        ReceiptData(
            pharmacy_name=receipt.pharmacy_name,
            branch_name=receipt.branch_name,
            invoice_number=receipt.invoice_number,
            created_at_display=receipt.created_at.strftime("%Y-%m-%d %H:%M"),
            lines=[
                ReceiptLine(
                    name=line.name,
                    quantity=line.quantity,
                    unit_name=line.unit_name,
                    line_total=line.line_total,
                )
                for line in receipt.lines
            ],
            subtotal=receipt.subtotal,
            discount=receipt.discount,
            tax=receipt.tax,
            total=receipt.total,
            currency_symbol=receipt.currency_symbol,
            thank_you_message=receipt.thank_you_message,
            address=receipt.address,
            phone=receipt.phone,
            license_number=receipt.license_number,
            tax_registration_no=receipt.tax_registration_no,
            payment_method_display=receipt.payment_method_display,
            tendered=receipt.tendered,
            change_due=receipt.change_due,
            qr_content=receipt.qr_content,
            show_signature=receipt.show_pharmacist_signature,
        ),
        open_drawer=open_drawer,
    )

"""POS endpoints (P1-M8 full point of sale · P1-M9 receipt printing).

Every endpoint carries a permission dependency (CLAUDE.md mandatory rule).
- /scan resolves an exact barcode OR a 2D GS1 DataMatrix (Egyptian packs) and
  returns ALL sellable packaging levels so the UI can switch units locally.
- /sale accepts explicit medication_id + packaging_id per line (unit switching
  and name-search lines) in addition to the plain-barcode skeleton shape.
- /invoices/{id}/receipt + /invoices/{id}/print serve the SAME composed receipt
  (receipt_service) as JSON for browser printing and as raw ESC/POS to the
  thermal printer — with the drawer pulse for cash sales.
"""

import asyncio
import uuid
from decimal import Decimal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pharmaos_api.config import get_settings as get_app_settings
from pharmaos_api.db import get_session
from pharmaos_api.deps import get_current_user, require_permission
from pharmaos_api.errors import ApiError, ErrorCode, success_envelope
from pharmaos_api.models import InvoiceItem, User
from pharmaos_api.printing.escpos import send_raw
from pharmaos_api.security.csrf import enforce_csrf
from pharmaos_api.services import (
    catalog_service,
    customer_service,
    receipt_service,
    sales_service,
)

router = APIRouter(prefix="/api/v1/pos", tags=["pos"])


@router.get("/scan")
async def scan(
    barcode: str = Query(min_length=1, max_length=120),
    session: AsyncSession = Depends(get_session),
    _: None = Depends(require_permission("inventory.view")),
) -> dict[str, object]:
    """Barcode / GS1 lookup — the fastest POS path (target < 50ms).

    `levels` carries every sellable packaging level (box/strip/tablet) with its
    price so the cart can switch units without another round-trip.
    """
    result = await sales_service.resolve_scan_code(session, barcode)
    packaging = await catalog_service.get_packaging(session, result.medication_id)
    return success_envelope(
        {
            "medication_id": str(result.medication_id),
            "trade_name": result.trade_name,
            "trade_name_ar": result.trade_name_ar,
            "packaging_id": str(result.packaging_id),
            "packaging_name_ar": result.packaging_name_ar,
            "level": result.level,
            "selling_price": str(result.selling_price),
            "requires_prescription": result.requires_prescription,
            "controlled_substance": result.controlled_substance,
            "levels": [
                {
                    "id": str(p.id),
                    "level": p.level,
                    "name_ar": p.name_ar,
                    "selling_price": str(p.selling_price),
                    "is_default_sale": p.is_default_sale,
                }
                for p in packaging
                if p.is_sellable
            ],
        }
    )


class SaleLineIn(BaseModel):
    """barcode alone, barcode + packaging_id (unit switch), or
    medication_id + packaging_id (name-search line) — validated in the service."""

    barcode: str | None = Field(default=None, min_length=1, max_length=120)
    medication_id: uuid.UUID | None = None
    packaging_id: uuid.UUID | None = None
    quantity: Decimal = Field(gt=0, le=Decimal("100000"))
    # P2-M8: required when the resolved medication has requires_prescription=True.
    prescription_item_id: uuid.UUID | None = None


class SaleIn(BaseModel):
    branch_id: uuid.UUID
    lines: list[SaleLineIn] = Field(min_length=1, max_length=200)
    payment_method: str = Field(default="cash", pattern="^(cash|card)$")
    # M10 — cash received from the customer (change persists on the invoice).
    tendered: Decimal | None = Field(default=None, ge=0, le=Decimal("10000000"))
    # P2-M3: 2D-scanned pack serials dispensed in this sale (EDA track & trace).
    serials: list[str] = Field(default_factory=list, max_length=1000)
    # P2-M5: optional customer for loyalty accrual + purchase history.
    customer_id: uuid.UUID | None = None
    # P2-M5 (review C3): loyalty points to redeem as a discount (needs customer_id).
    redeem_points: int = Field(default=0, ge=0, le=10_000_000)


@router.post("/sale")
async def create_sale(
    body: SaleIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
    _: None = Depends(require_permission("sales.create")),
) -> dict[str, object]:
    """Complete a sale: FEFO batch deduction + movements + invoice, atomically."""
    enforce_csrf(request)
    invoice = await sales_service.create_sale(
        session,
        branch_id=body.branch_id,
        lines=[
            sales_service.SaleLine(
                quantity=x.quantity,
                barcode=x.barcode,
                medication_id=x.medication_id,
                packaging_id=x.packaging_id,
                prescription_item_id=x.prescription_item_id,
            )
            for x in body.lines
        ],
        cashier=current_user,
        payment_method=body.payment_method,
        tendered=body.tendered,
        serials=body.serials,
        customer_id=body.customer_id,
        redeem_points=body.redeem_points,
    )
    items = (
        (await session.execute(select(InvoiceItem).where(InvoiceItem.invoice_id == invoice.id)))
        .scalars()
        .all()
    )
    return success_envelope(
        {
            "invoice_id": str(invoice.id),
            "invoice_number": invoice.invoice_number,
            "currency_code": invoice.currency_code,
            "subtotal": str(invoice.subtotal),
            "discount_amount": str(invoice.discount_amount),
            "tax_amount": str(invoice.tax_amount),
            "total": str(invoice.total),
            "points_redeemed": body.redeem_points if invoice.customer_id is not None else 0,
            "payment_method": invoice.payment_method,
            "tendered_amount": (
                str(invoice.tendered_amount) if invoice.tendered_amount is not None else None
            ),
            "change_amount": (
                str(invoice.change_amount) if invoice.change_amount is not None else None
            ),
            "cash_session_id": (
                str(invoice.cash_session_id) if invoice.cash_session_id is not None else None
            ),
            "customer_id": str(invoice.customer_id) if invoice.customer_id is not None else None,
            "points_earned": (
                customer_service.points_for_amount(invoice.total)
                if invoice.customer_id is not None
                else None
            ),
            "items": [
                {
                    "medication_id": str(item.medication_id),
                    "batch_id": str(item.batch_id),
                    "quantity": str(item.quantity),
                    "qty_smallest": str(item.qty_smallest),
                    "line_total": str(item.line_total),
                }
                for item in items
            ],
        }
    )


# ------------------------- receipt printing (P1-M9) -------------------------


def _receipt_json(r: receipt_service.InvoiceReceipt, *, thermal_ready: bool) -> dict[str, object]:
    return {
        "invoice_id": str(r.invoice_id),
        "invoice_number": r.invoice_number,
        "created_at": r.created_at.isoformat(),
        "created_at_display": r.created_at.strftime("%Y-%m-%d %H:%M"),
        "payment_method": r.payment_method,
        "payment_method_display": r.payment_method_display,
        "currency_code": r.currency_code,
        "currency_symbol": r.currency_symbol,
        "subtotal": str(r.subtotal),
        "discount": str(r.discount),
        "tax": str(r.tax),
        "total": str(r.total),
        "tendered_amount": str(r.tendered) if r.tendered is not None else None,
        "change_amount": str(r.change_due) if r.change_due is not None else None,
        "branch_name": r.branch_name,
        "pharmacy_name": r.pharmacy_name,
        "address": r.address,
        "phone": r.phone,
        "license_number": r.license_number,
        "tax_registration_no": r.tax_registration_no,
        "thank_you_message": r.thank_you_message,
        "return_policy": r.return_policy,
        "paper_size": r.paper_size,
        "show_qr_code": r.show_qr_code,
        "show_pharmacist_signature": r.show_pharmacist_signature,
        "qr_content": r.qr_content,
        "thermal_ready": thermal_ready,
        "lines": [
            {
                "name": line.name,
                "unit_name": line.unit_name,
                "quantity": str(line.quantity),
                "unit_price": str(line.unit_price),
                "line_total": str(line.line_total),
            }
            for line in r.lines
        ],
    }


@router.get("/invoices/{invoice_id}/receipt")
async def invoice_receipt(
    invoice_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    _: None = Depends(require_permission("sales.view")),
) -> dict[str, object]:
    """Composed receipt as JSON — feeds the browser-print fallback and previews.

    `thermal_ready` tells the POS whether a direct ESC/POS print can succeed
    (80mm paper + a configured printer) without attempting one.
    """
    receipt = await receipt_service.load_invoice_receipt(session, invoice_id)
    cfg = get_app_settings()
    thermal_ready = receipt.paper_size == receipt_service.THERMAL_PAPER and bool(cfg.printer_host)
    return success_envelope(_receipt_json(receipt, thermal_ready=thermal_ready))


class PrintIn(BaseModel):
    """Optional overrides. printer_host/port default to the device .env config;
    open_drawer defaults to `payment_method == "cash"` (the drawer opens only
    when cash actually changes hands — not for card sales or reprints)."""

    printer_host: str | None = Field(default=None, min_length=1, max_length=255)
    printer_port: int | None = Field(default=None, ge=1, le=65535)
    open_drawer: bool | None = None


@router.post("/invoices/{invoice_id}/print")
async def print_invoice(
    invoice_id: uuid.UUID,
    body: PrintIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
    _: None = Depends(require_permission("sales.create")),
) -> dict[str, object]:
    """Print the invoice receipt to the ESC/POS printer (+ drawer pulse for cash).

    Thermal printing requires the branch paper size to be 80mm (E-PRN-003
    otherwise — the UI falls back to browser printing for A4/A5). The network
    send runs in a worker thread so the event loop never blocks on the socket.
    """
    enforce_csrf(request)
    receipt = await receipt_service.load_invoice_receipt(session, invoice_id)
    if receipt.paper_size != receipt_service.THERMAL_PAPER:
        raise ApiError(ErrorCode.PAPER_NOT_THERMAL, 409)

    cfg = get_app_settings()
    host = body.printer_host or cfg.printer_host
    if not host:
        raise ApiError(ErrorCode.PRINTER_NOT_CONFIGURED, 409)
    # Hardening (M11): a configured PRODUCTION device prints only to ITS printer —
    # a request-supplied host would otherwise be a LAN port-probe oracle
    # (E-PRN-002 vs. success reveals open ports to any sales.create holder).
    # Unconfigured devices keep the override for first-time setup/diagnostics.
    if (
        cfg.is_production
        and cfg.printer_host
        and body.printer_host
        and body.printer_host != cfg.printer_host
    ):
        raise ApiError(
            ErrorCode.VALIDATION_FAILED, 422, message="Printer host is fixed on this device."
        )
    port = body.printer_port or cfg.printer_port

    open_drawer = (
        body.open_drawer if body.open_drawer is not None else receipt.payment_method == "cash"
    )
    payload = receipt_service.to_escpos(receipt, open_drawer=open_drawer)
    try:
        await asyncio.to_thread(
            send_raw, payload, host=host, port=port, timeout=cfg.printer_timeout_seconds
        )
    except OSError as exc:
        raise ApiError(ErrorCode.PRINTER_UNREACHABLE, 503) from exc
    return success_envelope({"printed": True, "drawer": open_drawer, "bytes": len(payload)})

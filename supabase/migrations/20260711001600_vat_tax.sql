-- 20260711001600_vat_tax.sql
-- Phase 2 / P2-M6 — VAT per tax_profile (ض.ق.م).
--
-- The foundation already exists (core_schema 0200): tax_profiles
-- (vat_rate, medicine_vat_rate, einvoice_system), countries.tax_profile_id, and
-- invoices.tax_amount. This milestone wires that profile into the sale, so it
-- adds only the two missing pieces:
--   1. medications.is_medicine — VAT classification. In a pharmacy most SKUs are
--      medicines (default TRUE); non-medicine items (cosmetics, devices, some
--      supplements) carry the standard vat_rate while medicines follow
--      medicine_vat_rate (NULL = exempt, the Egyptian default).
--   2. invoice_items.tax_rate / tax_amount — per-line VAT SNAPSHOT (rate + amount
--      at issue time; CLAUDE.md: "كل فاتورة تحمل قيم الضريبة وقت الإصدار").
--      Prices are VAT-INCLUSIVE, so the amount is the VAT extracted from the
--      gross line_total; invoices.tax_amount is their sum.

ALTER TABLE medications ADD COLUMN is_medicine BOOLEAN NOT NULL DEFAULT TRUE;

ALTER TABLE invoice_items ADD COLUMN tax_rate DECIMAL(5,2) NOT NULL DEFAULT 0;
ALTER TABLE invoice_items ADD COLUMN tax_amount DECIMAL(12,2) NOT NULL DEFAULT 0;

/**
 * Typed API client — unified ApiResponse envelope (packages/shared).
 * Cookies are first-party via the Next.js rewrite proxy to 127.0.0.1.
 * Mutating requests carry the CSRF header (double-submit pattern).
 */

import type { ApiResponse } from '@pharmaos/shared';

export class ApiRequestError extends Error {
  constructor(
    public readonly code: string,
    message: string,
  ) {
    super(message);
  }
}

function readCsrfCookie(): string | null {
  if (typeof document === 'undefined') return null;
  const match = document.cookie.match(/(?:^|;\s*)pharmaos_csrf=([^;]+)/);
  return match?.[1] ?? null;
}

export async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const method = init?.method ?? 'GET';
  const headers = new Headers(init?.headers);
  if (!headers.has('Content-Type') && init?.body) headers.set('Content-Type', 'application/json');
  if (!['GET', 'HEAD', 'OPTIONS'].includes(method)) {
    const csrf = readCsrfCookie();
    if (csrf) headers.set('X-CSRF-Token', csrf);
  }

  const response = await fetch(path, { ...init, headers, credentials: 'include' });
  const body = (await response.json()) as ApiResponse<T>;
  if (!body.success || body.data === undefined) {
    throw new ApiRequestError(body.error?.code ?? 'E-SYS-001', body.error?.message ?? '');
  }
  return body.data;
}

export interface SessionUser {
  id: string;
  username: string;
  full_name: string;
  role: string | null;
}

export function login(username: string, password: string) {
  return apiFetch<{ user: SessionUser; csrf_token: string }>('/api/v1/auth/login', {
    method: 'POST',
    body: JSON.stringify({ username, password }),
  });
}

export function fetchMe() {
  return apiFetch<{ user: SessionUser }>('/api/v1/auth/me');
}

export function logout() {
  return apiFetch<{ logged_out: boolean }>('/api/v1/auth/logout', { method: 'POST' });
}

// ---- User & role management (P1-M3; super_admin only) ----

export interface ManagedUser {
  id: string;
  username: string;
  full_name: string;
  role: string | null;
  phone: string | null;
  is_active: boolean;
  created_at: string;
}

export interface CreateUserInput {
  username: string;
  full_name: string;
  password: string;
  role_code: string;
  phone?: string | null;
}

export function listUsers() {
  return apiFetch<ManagedUser[]>('/api/v1/users?limit=100');
}

export function createUser(input: CreateUserInput) {
  return apiFetch<ManagedUser>('/api/v1/users', {
    method: 'POST',
    body: JSON.stringify(input),
  });
}

export function changeUserRole(id: string, roleCode: string) {
  return apiFetch<ManagedUser>(`/api/v1/users/${id}/role`, {
    method: 'POST',
    body: JSON.stringify({ role_code: roleCode }),
  });
}

export function setUserActive(id: string, active: boolean) {
  return apiFetch<ManagedUser>(`/api/v1/users/${id}/active`, {
    method: 'POST',
    body: JSON.stringify({ active }),
  });
}

export function resetUserPassword(id: string, newPassword: string) {
  return apiFetch<{ reset: boolean }>(`/api/v1/users/${id}/reset-password`, {
    method: 'POST',
    body: JSON.stringify({ new_password: newPassword }),
  });
}

// ---- Branch & settings (P1-M4) ----

export interface BranchInfo {
  id: string;
  name: string;
  country_code: string;
  currency_code: string;
  is_active: boolean;
}

export interface BranchSettings {
  id: string;
  branch_id: string;
  pharmacy_name: string;
  pharmacy_logo: string | null;
  license_number: string | null;
  address: string | null;
  phone: string | null;
  tax_registration_no: string | null;
  return_policy: string | null;
  thank_you_message: string | null;
  paper_size: '80mm' | 'A4' | 'A5';
  show_pharmacist_signature: boolean;
  show_qr_code: boolean;
  max_discount_percent: string;
}

export function listBranches() {
  return apiFetch<BranchInfo[]>('/api/v1/branches');
}

export function getSettings(branchId: string) {
  return apiFetch<BranchSettings | null>(`/api/v1/branches/${branchId}/settings`);
}

export function putSettings(branchId: string, values: Record<string, unknown>) {
  return apiFetch<BranchSettings>(`/api/v1/branches/${branchId}/settings`, {
    method: 'PUT',
    body: JSON.stringify(values),
  });
}

export function updateBranch(branchId: string, values: Record<string, unknown>) {
  return apiFetch<BranchInfo>(`/api/v1/branches/${branchId}`, {
    method: 'PATCH',
    body: JSON.stringify(values),
  });
}

// ---- Tax profile (P2-M6) ----

export interface TaxProfile {
  id: string;
  name: string;
  vat_rate: string;
  medicine_vat_rate: string | null;
  einvoice_system: string | null;
}

export function getTaxProfile(branchId: string) {
  return apiFetch<TaxProfile | null>(`/api/v1/branches/${branchId}/tax-profile`);
}

export function updateTaxProfile(
  profileId: string,
  values: {
    name: string;
    vat_rate: string;
    medicine_vat_rate: string | null;
    einvoice_system: string | null;
  },
) {
  return apiFetch<TaxProfile>(`/api/v1/tax-profiles/${profileId}`, {
    method: 'PATCH',
    body: JSON.stringify(values),
  });
}

// ---- Catalog editor: packaging levels & barcodes (P1-M7 UI; API since M5) ----

export interface Unit {
  id: string;
  name_ar: string;
  name_en: string | null;
}

export interface PackagingLevel {
  id: string;
  level: number;
  unit_id: string;
  name_ar: string;
  qty_in_parent: string | null;
  is_sellable: boolean;
  selling_price: string;
  is_default_sale: boolean;
  price_source: string;
}

export interface Barcode {
  id: string;
  barcode: string;
  barcode_type: string;
  packaging_id: string | null;
  is_primary: boolean;
}

export interface MedicationDetail {
  id: string;
  trade_name: string;
  trade_name_ar: string | null;
  scientific_name: string | null;
  manufacturer: string | null;
  drug_class: string | null;
  route: string | null;
  requires_prescription: boolean;
  controlled_substance: boolean;
  storage_conditions: string | null;
  eda_registration_no: string | null;
  gtin: string | null;
  is_active: boolean;
  packaging: PackagingLevel[];
  barcodes: Barcode[];
}

/** One packaging level as submitted by the editor (server validates the set). */
export interface PackagingLevelInput {
  level: number;
  unit_id: string;
  name_ar: string;
  qty_in_parent: string | null;
  selling_price: string;
  is_sellable: boolean;
  is_default_sale: boolean;
}

export function listUnits() {
  return apiFetch<Unit[]>('/api/v1/catalog/units');
}

export function getMedication(id: string) {
  return apiFetch<MedicationDetail>(`/api/v1/medications/${id}`);
}

export function updateMedication(id: string, values: Record<string, unknown>) {
  return apiFetch<MedicationDetail>(`/api/v1/medications/${id}`, {
    method: 'PATCH',
    body: JSON.stringify(values),
  });
}

export function deleteMedication(id: string) {
  return apiFetch<{ deleted: boolean }>(`/api/v1/medications/${id}`, { method: 'DELETE' });
}

export function putPackaging(id: string, levels: PackagingLevelInput[], priceSource = 'manual') {
  return apiFetch<PackagingLevel[]>(`/api/v1/medications/${id}/packaging`, {
    method: 'PUT',
    body: JSON.stringify({ levels, price_source: priceSource }),
  });
}

export function addBarcode(
  id: string,
  input: {
    barcode: string;
    barcode_type?: string;
    packaging_id?: string | null;
    is_primary?: boolean;
  },
) {
  return apiFetch<Barcode>(`/api/v1/medications/${id}/barcodes`, {
    method: 'POST',
    body: JSON.stringify(input),
  });
}

export function deleteBarcode(id: string, barcodeId: string) {
  return apiFetch<{ deleted: boolean }>(`/api/v1/medications/${id}/barcodes/${barcodeId}`, {
    method: 'DELETE',
  });
}

// ---- Inventory: stock on hand, batches, receiving, adjustments (P1-M7) ----

export interface InventoryBranch {
  id: string;
  name: string;
  currency_code: string;
}

export interface InventoryRow {
  medication_id: string;
  trade_name: string;
  trade_name_ar: string | null;
  cached_quantity: string;
  min_stock_level: string | null;
  reorder_point: string | null;
  shelf_location: string | null;
  low_stock: boolean;
}

export interface Batch {
  id: string;
  branch_id: string;
  medication_id: string;
  trade_name?: string;
  trade_name_ar?: string | null;
  batch_number: string;
  expiry_date: string;
  quantity: string;
  purchase_price: string;
  supplier_id: string | null;
  status: string;
  received_at: string;
}

export interface Supplier {
  id: string;
  name: string;
}

export interface Gs1Parse {
  gtin: string | null;
  expiry_date: string | null;
  batch_number: string | null;
  serial_number: string | null;
  medication: {
    id: string;
    trade_name: string;
    trade_name_ar: string | null;
    gtin: string | null;
  } | null;
}

export interface ReceiveInput {
  branch_id: string;
  medication_id: string;
  batch_number: string;
  expiry_date: string;
  quantity: string;
  purchase_price: string;
  supplier_id?: string | null;
  // P2-M3 (review C2): 2D pack-serial capture on receive (EDA track & trace).
  gtin?: string | null;
  serials?: string[];
}

export function listInventoryBranches() {
  return apiFetch<InventoryBranch[]>('/api/v1/inventory/branches');
}

export function listInventory(
  branchId: string,
  opts: { search?: string; lowStock?: boolean } = {},
) {
  const params = new URLSearchParams({ branch_id: branchId, limit: '100' });
  if (opts.search) params.set('search', opts.search);
  if (opts.lowStock) params.set('low_stock', 'true');
  return apiFetch<InventoryRow[]>(`/api/v1/inventory?${params.toString()}`);
}

export function listBatches(
  branchId: string,
  opts: { medicationId?: string; status?: string } = {},
) {
  const params = new URLSearchParams({ branch_id: branchId, limit: '100' });
  if (opts.medicationId) params.set('medication_id', opts.medicationId);
  if (opts.status) params.set('status', opts.status);
  return apiFetch<Batch[]>(`/api/v1/inventory/batches?${params.toString()}`);
}

export function parseGs1(code: string) {
  return apiFetch<Gs1Parse>(`/api/v1/catalog/parse-gs1?code=${encodeURIComponent(code)}`);
}

export function receiveStock(input: ReceiveInput) {
  return apiFetch<Batch>('/api/v1/inventory/receive', {
    method: 'POST',
    body: JSON.stringify(input),
  });
}

export function adjustBatch(batchId: string, quantityDelta: string, reason: string) {
  return apiFetch<Batch>(`/api/v1/inventory/batches/${batchId}/adjust`, {
    method: 'POST',
    body: JSON.stringify({ quantity_delta: quantityDelta, reason }),
  });
}

export function setBatchStatus(batchId: string, status: string, reason = '') {
  return apiFetch<Batch>(`/api/v1/inventory/batches/${batchId}/status`, {
    method: 'POST',
    body: JSON.stringify({ status, reason }),
  });
}

export function listSuppliers() {
  return apiFetch<Supplier[]>('/api/v1/inventory/suppliers');
}

export function createSupplier(name: string) {
  return apiFetch<Supplier>('/api/v1/inventory/suppliers', {
    method: 'POST',
    body: JSON.stringify({ name }),
  });
}

export interface DriftReport {
  drift: Array<{ medication_id: string; cached: string; truth: string }>;
  ok: boolean;
}

export function checkDrift(branchId: string) {
  return apiFetch<DriftReport>(`/api/v1/inventory/drift?branch_id=${branchId}`);
}

export function rebuildCache(branchId: string) {
  return apiFetch<{ rows: number }>('/api/v1/inventory/rebuild', {
    method: 'POST',
    body: JSON.stringify({ branch_id: branchId }),
  });
}

// ---- Batch tracking (P2-M4): expiry alerts, batch report, expiry sweep ----

export type ExpiryBucketKey = 'expired' | 'within_30' | 'within_60' | 'within_90';
export type AlertSeverity = 'danger' | 'critical' | 'warning';

export interface ExpiryAlertBatch {
  batch_id: string;
  medication_id: string;
  trade_name: string;
  trade_name_ar: string | null;
  batch_number: string;
  expiry_date: string;
  days_left: number;
  quantity: string;
  purchase_price: string;
  value: string;
}

export interface ExpiryBucket {
  severity: AlertSeverity;
  count: number;
  total_quantity: string;
  total_value: string;
  batches: ExpiryAlertBatch[];
}

export interface ExpiryAlerts {
  as_of: string;
  windows: { critical_days: number; mid_days: number; warning_days: number };
  buckets: Record<ExpiryBucketKey, ExpiryBucket>;
  totals: { count: number; total_quantity: string; total_value: string };
}

export function getExpiryAlerts(branchId: string) {
  return apiFetch<ExpiryAlerts>(`/api/v1/inventory/expiry-alerts?branch_id=${branchId}`);
}

export type BatchStatus = 'active' | 'quarantined' | 'expired' | 'recalled' | 'depleted';

export interface BatchStatusCount {
  count: number;
  total_quantity: string;
  total_value: string;
}

export interface BatchReport {
  branch_id: string;
  by_status: Record<BatchStatus, BatchStatusCount>;
  sellable_value: string;
  locked_value: string;
  totals: { batch_count: number; total_value: string };
}

export function getBatchReport(branchId: string) {
  return apiFetch<BatchReport>(`/api/v1/inventory/batch-report?branch_id=${branchId}`);
}

export function runExpirySweep() {
  return apiFetch<{ swept: number }>('/api/v1/inventory/expiry-sweep', { method: 'POST' });
}

export interface MedOption {
  id: string;
  trade_name: string;
  trade_name_ar: string | null;
}

/** Catalog search for the receiving picker (a batch can be received for any med). */
export function searchMedications(term: string) {
  return apiFetch<MedOption[]>(`/api/v1/medications?limit=10&search=${encodeURIComponent(term)}`);
}

// ---- POS (P1-M8): scan, unit switching, sale ----

export interface PosLevel {
  id: string;
  level: number;
  name_ar: string;
  selling_price: string;
  is_default_sale: boolean;
}

export interface PosScan {
  medication_id: string;
  trade_name: string;
  trade_name_ar: string | null;
  packaging_id: string;
  packaging_name_ar: string;
  level: number;
  selling_price: string;
  requires_prescription: boolean;
  controlled_substance: boolean;
  levels: PosLevel[];
}

/** Exact barcode or full GS1 DataMatrix element string (Egyptian 2D codes). */
export function posScan(code: string) {
  return apiFetch<PosScan>(`/api/v1/pos/scan?barcode=${encodeURIComponent(code)}`);
}

export interface PosSaleLine {
  medication_id: string;
  packaging_id: string;
  quantity: string;
  /** P2-M8 — required when the scanned item is requires_prescription=true. */
  prescription_item_id?: string;
}

export interface PosSaleItem {
  medication_id: string;
  batch_id: string;
  quantity: string;
  qty_smallest: string;
  line_total: string;
}

export interface PosSaleResult {
  invoice_id: string;
  invoice_number: string;
  currency_code: string;
  subtotal: string;
  discount_amount: string;
  tax_amount: string;
  total: string;
  points_redeemed: number;
  payment_method: string;
  tendered_amount: string | null;
  change_amount: string | null;
  cash_session_id: string | null;
  customer_id: string | null;
  points_earned: number | null;
  items: PosSaleItem[];
}

export function createPosSale(input: {
  branch_id: string;
  lines: PosSaleLine[];
  payment_method: 'cash' | 'card';
  tendered?: string;
  customer_id?: string;
  redeem_points?: number;
  // P2-M3 (review C2): 2D pack serials scanned for tracked meds at dispense.
  serials?: string[];
}) {
  return apiFetch<PosSaleResult>('/api/v1/pos/sale', {
    method: 'POST',
    body: JSON.stringify(input),
  });
}

// ---- Customers + loyalty (P2-M5) ----

export interface CustomerSummary {
  id: string;
  name: string;
  phone: string | null;
  loyalty_points: number;
  is_active: boolean;
  has_national_id: boolean;
  has_insurance_number: boolean;
}

export interface CustomerDetail extends CustomerSummary {
  national_id: string | null;
  insurance_number: string | null;
  notes: string | null;
  created_at: string;
}

export interface NewCustomer {
  name: string;
  phone?: string | null;
  national_id?: string | null;
  insurance_number?: string | null;
  notes?: string | null;
}

export interface LoyaltyTxn {
  id: string;
  points_delta: number;
  txn_type: string;
  reference_type: string | null;
  reference_id: string | null;
  reason: string | null;
  created_at: string;
}

export interface CustomerHistoryRow {
  invoice_id: string;
  invoice_number: string;
  created_at: string;
  total: string;
  currency_code: string;
  status: string;
}

export function listCustomers(opts: { search?: string; activeOnly?: boolean } = {}) {
  const params = new URLSearchParams({ limit: '100' });
  if (opts.search) params.set('search', opts.search);
  if (opts.activeOnly) params.set('active_only', 'true');
  return apiFetch<CustomerSummary[]>(`/api/v1/customers?${params.toString()}`);
}

export function getCustomer(id: string) {
  return apiFetch<CustomerDetail>(`/api/v1/customers/${id}`);
}

export function createCustomer(body: NewCustomer) {
  return apiFetch<CustomerDetail>('/api/v1/customers', {
    method: 'POST',
    body: JSON.stringify(body),
  });
}

export function updateCustomer(id: string, patch: Partial<NewCustomer> & { is_active?: boolean }) {
  return apiFetch<CustomerDetail>(`/api/v1/customers/${id}`, {
    method: 'PATCH',
    body: JSON.stringify(patch),
  });
}

export function deleteCustomer(id: string) {
  return apiFetch<{ deleted: boolean }>(`/api/v1/customers/${id}`, { method: 'DELETE' });
}

export function listLoyalty(id: string) {
  return apiFetch<{ balance: number; transactions: LoyaltyTxn[] }>(
    `/api/v1/customers/${id}/loyalty`,
  );
}

export function adjustLoyalty(id: string, pointsDelta: number, reason: string) {
  return apiFetch<CustomerDetail>(`/api/v1/customers/${id}/loyalty`, {
    method: 'POST',
    body: JSON.stringify({ points_delta: pointsDelta, reason }),
  });
}

export function customerHistory(id: string) {
  return apiFetch<CustomerHistoryRow[]>(`/api/v1/customers/${id}/history`);
}

// ---- Cash sessions (P1-M10) ----

export interface CashSessionInfo {
  id: string;
  branch_id: string;
  cashier_id: string;
  status: string;
  opening_float: string;
  opened_at: string;
  closed_at: string | null;
  expected_cash: string | null;
  counted_cash: string | null;
  discrepancy: string | null;
  closing_notes: string | null;
}

export interface CashSessionSummary {
  cash_count: number;
  cash_total: string;
  cash_refund_count: number;
  cash_refunded: string;
  card_count: number;
  card_total: string;
  card_refund_count: number;
  card_refunded: string;
  store_credit_refunded: string;
  cash_expense_count: number;
  cash_expenses: string;
  tendered_total: string;
  change_total: string;
  expected_cash: string;
}

export interface CashSessionRow {
  id: string;
  status: string;
  opening_float: string;
  opened_at: string;
  closed_at: string | null;
  expected_cash: string | null;
  counted_cash: string | null;
  discrepancy: string | null;
  closing_notes: string | null;
  cashier_username: string;
  cashier_full_name: string;
}

export interface DayZReport {
  date: string;
  sessions: CashSessionRow[];
  cash_in_session: { count: number; total: string };
  card_in_session: { count: number; total: string };
  cash_outside_sessions: { count: number; total: string };
  card_outside_sessions: { count: number; total: string };
  invoice_count: number;
  total_sales: string;
  refunds_cash: { count: number; total: string };
  refunds_card: { count: number; total: string };
  refunds_store_credit: { count: number; total: string };
  total_refunds: string;
  net_total_sales: string;
  expenses_cash: { count: number; total: string };
  expenses_card: { count: number; total: string };
  expenses_bank_transfer: { count: number; total: string };
  total_expenses: string;
}

export function openCashSession(branchId: string, openingFloat: string) {
  return apiFetch<CashSessionInfo>('/api/v1/cashier/sessions/open', {
    method: 'POST',
    body: JSON.stringify({ branch_id: branchId, opening_float: openingFloat }),
  });
}

export function getCurrentCashSession(branchId: string) {
  return apiFetch<{ session: CashSessionInfo | null; summary: CashSessionSummary | null }>(
    `/api/v1/cashier/sessions/current?branch_id=${branchId}`,
  );
}

export function closeCashSession(sessionId: string, countedCash: string, notes?: string) {
  return apiFetch<CashSessionInfo>(`/api/v1/cashier/sessions/${sessionId}/close`, {
    method: 'POST',
    body: JSON.stringify({ counted_cash: countedCash, notes: notes || null }),
  });
}

export function getZReport(branchId: string, day?: string) {
  const params = new URLSearchParams({ branch_id: branchId });
  if (day) params.set('day', day);
  return apiFetch<DayZReport>(`/api/v1/cashier/z-report?${params.toString()}`);
}

// ---- Receipt printing (P1-M9) ----

export interface ReceiptLine {
  name: string;
  unit_name: string;
  quantity: string;
  unit_price: string;
  line_total: string;
}

export interface InvoiceReceipt {
  invoice_id: string;
  invoice_number: string;
  created_at: string;
  created_at_display: string;
  payment_method: string;
  payment_method_display: string;
  currency_code: string;
  currency_symbol: string;
  subtotal: string;
  discount: string;
  tax: string;
  total: string;
  tendered_amount: string | null;
  change_amount: string | null;
  branch_name: string;
  pharmacy_name: string;
  address: string | null;
  phone: string | null;
  license_number: string | null;
  tax_registration_no: string | null;
  thank_you_message: string;
  return_policy: string | null;
  paper_size: string;
  show_qr_code: boolean;
  show_pharmacist_signature: boolean;
  qr_content: string | null;
  thermal_ready: boolean;
  lines: ReceiptLine[];
}

/** Composed receipt (same source as the thermal print) — feeds browser printing. */
export function getInvoiceReceipt(invoiceId: string) {
  return apiFetch<InvoiceReceipt>(`/api/v1/pos/invoices/${invoiceId}/receipt`);
}

export function printInvoice(invoiceId: string, opts: { open_drawer?: boolean } = {}) {
  return apiFetch<{ printed: boolean; drawer: boolean; bytes: number }>(
    `/api/v1/pos/invoices/${invoiceId}/print`,
    { method: 'POST', body: JSON.stringify(opts) },
  );
}

// ---- Purchasing: supplier management (P2-M1) ----
// Full supplier master under the purchases module. Distinct from the minimal
// {id,name} listSuppliers/createSupplier above (/inventory/suppliers), which the
// receiving picker still uses.

export interface SupplierDetail {
  id: string;
  name: string;
  contact_name: string | null;
  phone: string | null;
  email: string | null;
  address: string | null;
  tax_registration_no: string | null;
  payment_terms: string | null;
  is_active: boolean;
  notes: string | null;
}

export interface NewSupplier {
  name: string;
  contact_name?: string | null;
  phone?: string | null;
  email?: string | null;
  address?: string | null;
  tax_registration_no?: string | null;
  payment_terms?: string | null;
  notes?: string | null;
}

export function listPurchaseSuppliers(opts: { search?: string; activeOnly?: boolean } = {}) {
  const params = new URLSearchParams({ limit: '100' });
  if (opts.search) params.set('search', opts.search);
  if (opts.activeOnly) params.set('active_only', 'true');
  return apiFetch<SupplierDetail[]>(`/api/v1/purchases/suppliers?${params.toString()}`);
}

export function createPurchaseSupplier(input: NewSupplier) {
  return apiFetch<SupplierDetail>('/api/v1/purchases/suppliers', {
    method: 'POST',
    body: JSON.stringify(input),
  });
}

export function updatePurchaseSupplier(id: string, values: Record<string, unknown>) {
  return apiFetch<SupplierDetail>(`/api/v1/purchases/suppliers/${id}`, {
    method: 'PATCH',
    body: JSON.stringify(values),
  });
}

// ---- Purchasing: purchase orders (P2-M2) ----

export interface PurchaseItem {
  id: string;
  medication_id: string;
  packaging_id: string;
  qty_ordered: string;
  qty_received: string;
  unit_cost: string;
  line_total: string;
}

export interface PurchaseOrder {
  id: string;
  branch_id: string;
  supplier_id: string;
  po_number: string;
  status: string;
  order_date: string;
  expected_date: string | null;
  currency_code: string;
  subtotal: string;
  tax_amount: string;
  total: string;
  notes: string | null;
  approved_by: string | null;
  approved_at: string | null;
  created_at: string;
  items?: PurchaseItem[];
}

export interface NewPurchaseLine {
  medication_id: string;
  packaging_id: string;
  qty_ordered: string;
  unit_cost: string;
}

export interface NewReceiptLine {
  purchase_item_id: string;
  batch_number: string;
  expiry_date: string;
  quantity: string;
}

export function listPurchaseOrders(
  opts: { branch_id?: string; status?: string; supplier_id?: string } = {},
) {
  const params = new URLSearchParams({ limit: '100' });
  if (opts.branch_id) params.set('branch_id', opts.branch_id);
  if (opts.status) params.set('status', opts.status);
  if (opts.supplier_id) params.set('supplier_id', opts.supplier_id);
  return apiFetch<PurchaseOrder[]>(`/api/v1/purchases/orders?${params.toString()}`);
}

export function getPurchaseOrder(id: string) {
  return apiFetch<PurchaseOrder>(`/api/v1/purchases/orders/${id}`);
}

export function createPurchaseOrder(input: {
  branch_id: string;
  supplier_id: string;
  expected_date?: string | null;
  notes?: string | null;
  lines: NewPurchaseLine[];
}) {
  return apiFetch<PurchaseOrder>('/api/v1/purchases/orders', {
    method: 'POST',
    body: JSON.stringify(input),
  });
}

export function purchaseOrderAction(id: string, action: 'submit' | 'approve' | 'cancel') {
  return apiFetch<PurchaseOrder>(`/api/v1/purchases/orders/${id}/${action}`, { method: 'POST' });
}

export function receivePurchaseOrder(id: string, receipts: NewReceiptLine[]) {
  return apiFetch<PurchaseOrder>(`/api/v1/purchases/orders/${id}/receive`, {
    method: 'POST',
    body: JSON.stringify({ receipts }),
  });
}

// ---- Returns / credit notes (P2-M7) ----

export type RefundMethod = 'cash' | 'card' | 'store_credit';

export interface ReturnableLine {
  invoice_item_id: string;
  medication_id: string;
  trade_name: string;
  trade_name_ar: string | null;
  packaging_name_ar: string;
  unit_price: string;
  tax_rate: string;
  sold_qty: string;
  returned_qty: string;
  returnable_qty: string;
}

export interface ReturnableInvoice {
  invoice_id: string;
  invoice_number: string;
  invoice_type: string;
  status: string;
  currency_code: string;
  lines: ReturnableLine[];
}

export function lookupInvoiceForReturn(branchId: string, invoiceNumber: string) {
  const params = new URLSearchParams({ branch_id: branchId, invoice_number: invoiceNumber });
  return apiFetch<ReturnableInvoice>(`/api/v1/invoices/lookup?${params.toString()}`);
}

export interface NewReturnLine {
  invoice_item_id: string;
  quantity: string;
}

export interface ReturnItemOut {
  id: string;
  medication_id: string;
  trade_name: string;
  trade_name_ar: string | null;
  packaging_name_ar: string;
  quantity: string;
  unit_price: string;
  line_total: string;
  tax_amount: string;
}

export interface ReturnSummary {
  id: string;
  return_number: string;
  original_invoice_id: string;
  original_invoice_number: string;
  currency_code: string;
  subtotal: string;
  tax_amount: string;
  total: string;
  refund_method: RefundMethod;
  reason: string | null;
  customer_id: string | null;
  created_at: string;
}

export interface ReturnDetail extends ReturnSummary {
  items: ReturnItemOut[];
}

export function createReturn(input: {
  original_invoice_id: string;
  lines: NewReturnLine[];
  reason?: string | null;
  refund_method: RefundMethod;
}) {
  return apiFetch<ReturnDetail>('/api/v1/returns', {
    method: 'POST',
    body: JSON.stringify(input),
  });
}

export function listReturns(branchId: string) {
  const params = new URLSearchParams({ branch_id: branchId, limit: '100' });
  return apiFetch<ReturnSummary[]>(`/api/v1/returns?${params.toString()}`);
}

export function getReturn(id: string) {
  return apiFetch<ReturnDetail>(`/api/v1/returns/${id}`);
}

// ---- Prescriptions + controlled-substance register (P2-M8) ----

export type PrescriptionStatus =
  | 'pending'
  | 'partially_fulfilled'
  | 'fulfilled'
  | 'expired'
  | 'cancelled';

export interface PrescriptionSummary {
  id: string;
  customer_id: string | null;
  doctor_name: string;
  prescription_date: string;
  status: PrescriptionStatus;
  created_at: string;
}

export interface PrescriptionItemOut {
  id: string;
  medication_id: string;
  trade_name: string;
  trade_name_ar: string | null;
  packaging_id: string;
  packaging_name_ar: string;
  prescribed_qty: string;
  prescribed_qty_smallest: string;
  dispensed_qty_smallest: string;
  remaining_qty_smallest: string;
}

export interface PrescriptionDetail {
  id: string;
  branch_id: string;
  customer_id: string | null;
  doctor_name: string;
  doctor_license_no: string | null;
  prescription_date: string;
  notes: string | null;
  status: PrescriptionStatus;
  created_at: string;
  items: PrescriptionItemOut[];
}

export interface NewPrescriptionItemInput {
  medication_id: string;
  packaging_id: string;
  quantity: string;
}

export function listPrescriptions(
  branchId: string,
  opts: { customerId?: string; status?: PrescriptionStatus } = {},
) {
  const params = new URLSearchParams({ branch_id: branchId, limit: '100' });
  if (opts.customerId) params.set('customer_id', opts.customerId);
  if (opts.status) params.set('status', opts.status);
  return apiFetch<PrescriptionSummary[]>(`/api/v1/prescriptions?${params.toString()}`);
}

export function getPrescription(id: string) {
  return apiFetch<PrescriptionDetail>(`/api/v1/prescriptions/${id}`);
}

export function createPrescription(input: {
  branch_id: string;
  customer_id?: string | null;
  doctor_name: string;
  doctor_license_no?: string | null;
  prescription_date: string;
  notes?: string | null;
  items: NewPrescriptionItemInput[];
}) {
  return apiFetch<PrescriptionDetail>('/api/v1/prescriptions', {
    method: 'POST',
    body: JSON.stringify(input),
  });
}

export function updatePrescription(
  id: string,
  patch: {
    doctor_name?: string;
    doctor_license_no?: string | null;
    notes?: string | null;
    status?: PrescriptionStatus;
  },
) {
  return apiFetch<PrescriptionDetail>(`/api/v1/prescriptions/${id}`, {
    method: 'PATCH',
    body: JSON.stringify(patch),
  });
}

export interface ControlledLogRow {
  id: string;
  medication_id: string;
  trade_name: string;
  trade_name_ar: string | null;
  batch_id: string;
  batch_number: string;
  invoice_id: string;
  invoice_number: string;
  prescription_id: string | null;
  quantity_dispensed: string;
  dispensed_by: string;
  dispensed_by_name: string;
  created_at: string;
}

export function listControlledSubstanceLog(branchId: string, opts: { medicationId?: string } = {}) {
  const params = new URLSearchParams({ branch_id: branchId, limit: '100' });
  if (opts.medicationId) params.set('medication_id', opts.medicationId);
  return apiFetch<ControlledLogRow[]>(`/api/v1/controlled-substances/log?${params.toString()}`);
}

// ---- Expenses + categories (P2-M9) ----

export type ExpensePaymentMethod = 'cash' | 'card' | 'bank_transfer';

export interface ExpenseCategory {
  id: string;
  name_ar: string;
  name_en: string | null;
  is_active: boolean;
}

export interface ExpenseRow {
  id: string;
  branch_id: string;
  expense_category_id: string;
  category_name_ar: string;
  category_name_en: string | null;
  amount: string;
  currency_code: string;
  expense_date: string;
  description: string | null;
  payment_method: ExpensePaymentMethod;
  created_at: string;
}

export function listExpenseCategories(opts: { activeOnly?: boolean } = {}) {
  const params = new URLSearchParams();
  if (opts.activeOnly) params.set('active_only', 'true');
  return apiFetch<ExpenseCategory[]>(`/api/v1/finance/expense-categories?${params.toString()}`);
}

export function createExpenseCategory(input: { name_ar: string; name_en?: string | null }) {
  return apiFetch<ExpenseCategory>('/api/v1/finance/expense-categories', {
    method: 'POST',
    body: JSON.stringify(input),
  });
}

export function updateExpenseCategory(
  id: string,
  patch: { name_ar?: string; name_en?: string | null; is_active?: boolean },
) {
  return apiFetch<ExpenseCategory>(`/api/v1/finance/expense-categories/${id}`, {
    method: 'PATCH',
    body: JSON.stringify(patch),
  });
}

export function listExpenses(
  branchId: string,
  opts: { categoryId?: string; dateFrom?: string; dateTo?: string } = {},
) {
  const params = new URLSearchParams({ branch_id: branchId, limit: '200' });
  if (opts.categoryId) params.set('category_id', opts.categoryId);
  if (opts.dateFrom) params.set('date_from', opts.dateFrom);
  if (opts.dateTo) params.set('date_to', opts.dateTo);
  return apiFetch<ExpenseRow[]>(`/api/v1/finance/expenses?${params.toString()}`);
}

export function getExpense(id: string) {
  return apiFetch<ExpenseRow>(`/api/v1/finance/expenses/${id}`);
}

export function createExpense(input: {
  branch_id: string;
  expense_category_id: string;
  amount: string;
  expense_date: string;
  description?: string | null;
  payment_method: ExpensePaymentMethod;
}) {
  return apiFetch<ExpenseRow>('/api/v1/finance/expenses', {
    method: 'POST',
    body: JSON.stringify(input),
  });
}

export function updateExpense(
  id: string,
  patch: {
    expense_category_id?: string;
    amount?: string;
    expense_date?: string;
    description?: string | null;
    payment_method?: ExpensePaymentMethod;
  },
) {
  return apiFetch<ExpenseRow>(`/api/v1/finance/expenses/${id}`, {
    method: 'PATCH',
    body: JSON.stringify(patch),
  });
}

export function deleteExpense(id: string) {
  return apiFetch<{ deleted: boolean }>(`/api/v1/finance/expenses/${id}`, { method: 'DELETE' });
}

// ---- Compliance: ETA e-receipts + EDA track & trace (P2-M10 / P2-M11) ----

export interface EReceiptRow {
  id: string;
  invoice_id: string;
  status: string;
  eta_uuid: string | null;
  qr_data: string | null;
  submission_attempts: number;
  last_error: string | null;
  submitted_at: string | null;
  created_at: string;
}

export interface TtEventRow {
  id: string;
  event_type: string;
  gtin: string;
  serial_number: string;
  invoice_id: string | null;
  status: string;
  report_attempts: number;
  last_error: string | null;
  reported_at: string | null;
  created_at: string;
}

export function listEreceipts(branchId: string, opts: { status?: string } = {}) {
  const params = new URLSearchParams({ branch_id: branchId, limit: '100' });
  if (opts.status) params.set('status', opts.status);
  return apiFetch<EReceiptRow[]>(`/api/v1/compliance/ereceipts?${params.toString()}`);
}

export function drainEreceipts(branchId: string) {
  return apiFetch<{ processed: number; accepted: number; failed: number }>(
    '/api/v1/compliance/ereceipts/drain',
    { method: 'POST', body: JSON.stringify({ branch_id: branchId }) },
  );
}

export function listTtEvents(branchId: string, opts: { status?: string } = {}) {
  const params = new URLSearchParams({ branch_id: branchId, limit: '100' });
  if (opts.status) params.set('status', opts.status);
  return apiFetch<TtEventRow[]>(`/api/v1/compliance/tt-events?${params.toString()}`);
}

export function drainTtEvents(branchId: string) {
  return apiFetch<{ processed: number; reported: number; failed: number }>(
    '/api/v1/compliance/tt-events/drain',
    { method: 'POST', body: JSON.stringify({ branch_id: branchId }) },
  );
}

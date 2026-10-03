'use client';

import {
  Badge,
  Button,
  Card,
  CardContent,
  Input,
  Label,
  Modal,
  Select,
  Spinner,
} from '@pharmaos/ui';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useState } from 'react';

import {
  ApiRequestError,
  createPurchaseOrder,
  createPurchaseSupplier,
  getMedication,
  getPurchaseOrder,
  listInventoryBranches,
  listPurchaseOrders,
  listPurchaseSuppliers,
  purchaseOrderAction,
  receivePurchaseOrder,
  searchMedications,
  updatePurchaseSupplier,
  type MedOption,
  type PackagingLevel,
  type PurchaseOrder,
  type SupplierDetail,
} from '@/lib/api';
import { t } from '@/lib/i18n';
import { toast } from '@/lib/toast-store';

export default function PurchasesPage() {
  const [tab, setTab] = useState<'orders' | 'suppliers'>('orders');
  return (
    <div className="mx-auto max-w-6xl space-y-6">
      <div className="flex gap-1 border-b border-border">
        <TabButton active={tab === 'orders'} onClick={() => setTab('orders')}>
          {t('po.tab_orders')}
        </TabButton>
        <TabButton active={tab === 'suppliers'} onClick={() => setTab('suppliers')}>
          {t('po.tab_suppliers')}
        </TabButton>
      </div>
      {tab === 'orders' ? <OrdersTab /> : <SuppliersTab />}
    </div>
  );
}

function TabButton({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={
        'border-b-2 px-4 py-2 text-sm font-semibold transition-colors ' +
        (active ? 'border-primary-600 text-primary-700' : 'border-transparent text-slate-500')
      }
    >
      {children}
    </button>
  );
}

// ================================ Orders ================================

const STATUS_TONE: Record<string, 'neutral' | 'warning' | 'primary' | 'success' | 'danger'> = {
  draft: 'neutral',
  pending_approval: 'warning',
  approved: 'primary',
  partially_received: 'warning',
  received: 'success',
  cancelled: 'danger',
};

function OrderStatusBadge({ status }: { status: string }) {
  return <Badge tone={STATUS_TONE[status] ?? 'neutral'}>{t(`po.status_${status}`)}</Badge>;
}

function OrdersTab() {
  const qc = useQueryClient();
  const [branchId, setBranchId] = useState('');
  const [showCreate, setShowCreate] = useState(false);
  const [detailId, setDetailId] = useState<string | null>(null);

  const branchesQuery = useQuery({ queryKey: ['inv-branches'], queryFn: listInventoryBranches });
  useEffect(() => {
    const first = branchesQuery.data?.[0];
    if (!branchId && first) setBranchId(first.id);
  }, [branchId, branchesQuery.data]);

  const suppliersQuery = useQuery({
    queryKey: ['po-suppliers-active'],
    queryFn: () => listPurchaseSuppliers({ activeOnly: true }),
  });
  const ordersQuery = useQuery({
    queryKey: ['po-orders', branchId],
    queryFn: () => listPurchaseOrders({ branch_id: branchId }),
    enabled: !!branchId,
  });
  const invalidate = () => qc.invalidateQueries({ queryKey: ['po-orders'] });
  const supplierName = (id: string) =>
    suppliersQuery.data?.find((s) => s.id === id)?.name ?? id.slice(0, 8);

  const branches = branchesQuery.data ?? [];
  if (branchesQuery.isLoading) {
    return (
      <div className="flex justify-center py-8">
        <Spinner />
      </div>
    );
  }
  if (branches.length === 0) {
    return <p className="py-8 text-center text-sm text-slate-500">{t('po.no_branch')}</p>;
  }

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-2">
          <Label>{t('po.branch')}</Label>
          <Select value={branchId} onChange={(e) => setBranchId(e.target.value)} className="h-9">
            {branches.map((b) => (
              <option key={b.id} value={b.id}>
                {b.name}
              </option>
            ))}
          </Select>
        </div>
        <Button onClick={() => setShowCreate(true)}>{t('po.new')}</Button>
      </div>

      <Card>
        <CardContent className="pt-6">
          {ordersQuery.isLoading ? (
            <div className="flex justify-center py-8">
              <Spinner />
            </div>
          ) : (
            <OrdersTable
              rows={ordersQuery.data ?? []}
              supplierName={supplierName}
              onOpen={setDetailId}
            />
          )}
        </CardContent>
      </Card>

      {showCreate && (
        <CreateOrderModal
          branchId={branchId}
          suppliers={suppliersQuery.data ?? []}
          onClose={() => setShowCreate(false)}
          onCreated={() => {
            setShowCreate(false);
            invalidate();
            toast.success(t('po.created_ok'));
          }}
        />
      )}

      {detailId && (
        <OrderDetailModal
          poId={detailId}
          onClose={() => setDetailId(null)}
          onChanged={invalidate}
        />
      )}
    </div>
  );
}

function OrdersTable({
  rows,
  supplierName,
  onOpen,
}: {
  rows: PurchaseOrder[];
  supplierName: (id: string) => string;
  onOpen: (id: string) => void;
}) {
  if (rows.length === 0) {
    return <p className="py-8 text-center text-sm text-slate-500">{t('po.empty')}</p>;
  }
  return (
    <table className="w-full text-start text-sm">
      <thead>
        <tr className="border-b border-border text-start text-xs text-slate-500">
          <th className="p-2 text-start">{t('po.number')}</th>
          <th className="p-2 text-start">{t('po.supplier')}</th>
          <th className="p-2 text-start">{t('po.status')}</th>
          <th className="p-2 text-start">{t('po.total')}</th>
          <th className="p-2 text-start">{t('po.actions')}</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((po) => (
          <tr key={po.id} className="border-b border-border/60">
            <td className="numeric p-2 font-medium text-slate-800">{po.po_number}</td>
            <td className="p-2 text-slate-700">{supplierName(po.supplier_id)}</td>
            <td className="p-2">
              <OrderStatusBadge status={po.status} />
            </td>
            <td className="numeric p-2 text-slate-700">
              {po.total} {po.currency_code}
            </td>
            <td className="p-2">
              <Button size="sm" variant="ghost" onClick={() => onOpen(po.id)}>
                {t('po.open')}
              </Button>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

interface LineDraft {
  uid: string;
  medication_id: string;
  label: string;
  packaging: PackagingLevel[];
  packaging_id: string;
  qty: string;
  cost: string;
}

function CreateOrderModal({
  branchId,
  suppliers,
  onClose,
  onCreated,
}: {
  branchId: string;
  suppliers: SupplierDetail[];
  onClose: () => void;
  onCreated: () => void;
}) {
  const [supplierId, setSupplierId] = useState(suppliers[0]?.id ?? '');
  const [expectedDate, setExpectedDate] = useState('');
  const [notes, setNotes] = useState('');
  const [lines, setLines] = useState<LineDraft[]>([]);
  const [medTerm, setMedTerm] = useState('');
  const [error, setError] = useState<string | null>(null);

  const medQuery = useQuery({
    queryKey: ['po-med-search', medTerm],
    queryFn: () => searchMedications(medTerm),
    enabled: medTerm.trim().length >= 2,
  });

  const addLineMut = useMutation({
    mutationFn: (m: MedOption) => getMedication(m.id),
    onSuccess: (detail, m) => {
      const def = detail.packaging.find((p) => p.is_default_sale) ?? detail.packaging[0];
      if (!def) {
        toast.error(t('po.no_results'));
        return;
      }
      setLines((prev) => [
        ...prev,
        {
          uid: crypto.randomUUID(),
          medication_id: m.id,
          label: m.trade_name_ar ?? m.trade_name,
          packaging: detail.packaging,
          packaging_id: def.id,
          qty: '',
          cost: '',
        },
      ]);
      setMedTerm('');
    },
    onError: (e) => toast.error(t(`errors.${e instanceof ApiRequestError ? e.code : 'E-SYS-001'}`)),
  });

  const createMut = useMutation({
    mutationFn: () =>
      createPurchaseOrder({
        branch_id: branchId,
        supplier_id: supplierId,
        expected_date: expectedDate || null,
        notes: notes || null,
        lines: lines.map((l) => ({
          medication_id: l.medication_id,
          packaging_id: l.packaging_id,
          qty_ordered: l.qty,
          unit_cost: l.cost,
        })),
      }),
    onSuccess: onCreated,
    onError: (e) => setError(e instanceof ApiRequestError ? e.code : 'E-SYS-001'),
  });

  const updateLine = (uid: string, patch: Partial<LineDraft>) =>
    setLines((prev) => prev.map((l) => (l.uid === uid ? { ...l, ...patch } : l)));
  const removeLine = (uid: string) => setLines((prev) => prev.filter((l) => l.uid !== uid));

  const canSubmit =
    supplierId !== '' &&
    lines.length > 0 &&
    lines.every((l) => Number(l.qty) > 0 && l.cost !== '' && Number(l.cost) >= 0);

  return (
    <Modal open onClose={onClose} title={t('po.new')} className="max-w-3xl">
      <div className="space-y-4">
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
          <Field label={t('po.supplier')}>
            <Select value={supplierId} onChange={(e) => setSupplierId(e.target.value)}>
              <option value="">—</option>
              {suppliers.map((s) => (
                <option key={s.id} value={s.id}>
                  {s.name}
                </option>
              ))}
            </Select>
          </Field>
          <Field label={t('po.expected_date')}>
            <Input
              type="date"
              value={expectedDate}
              onChange={(e) => setExpectedDate(e.target.value)}
            />
          </Field>
          <Field label={t('po.notes')}>
            <Input value={notes} onChange={(e) => setNotes(e.target.value)} />
          </Field>
        </div>

        <div className="space-y-2">
          <Label>{t('po.add_line')}</Label>
          <Input
            placeholder={t('po.search_med')}
            value={medTerm}
            onChange={(e) => setMedTerm(e.target.value)}
          />
          {medTerm.trim().length >= 2 && (
            <div className="max-h-40 overflow-auto rounded-[var(--radius-md)] border border-border">
              {(medQuery.data ?? []).length === 0 ? (
                <p className="p-2 text-xs text-slate-500">{t('po.no_results')}</p>
              ) : (
                (medQuery.data ?? []).map((m) => (
                  <button
                    key={m.id}
                    type="button"
                    onClick={() => addLineMut.mutate(m)}
                    className="block w-full px-3 py-1.5 text-start text-sm hover:bg-primary-50"
                  >
                    {m.trade_name_ar ?? m.trade_name}
                  </button>
                ))
              )}
            </div>
          )}
        </div>

        {lines.length > 0 && (
          <table className="w-full text-start text-sm">
            <thead>
              <tr className="border-b border-border text-xs text-slate-500">
                <th className="p-2 text-start">{t('po.medication')}</th>
                <th className="p-2 text-start">{t('po.packaging')}</th>
                <th className="p-2 text-start">{t('po.qty_ordered')}</th>
                <th className="p-2 text-start">{t('po.unit_cost')}</th>
                <th className="p-2" />
              </tr>
            </thead>
            <tbody>
              {lines.map((l) => (
                <tr key={l.uid} className="border-b border-border/60">
                  <td className="p-2 text-slate-800">{l.label}</td>
                  <td className="p-2">
                    <Select
                      className="h-8"
                      value={l.packaging_id}
                      onChange={(e) => updateLine(l.uid, { packaging_id: e.target.value })}
                    >
                      {l.packaging.map((p) => (
                        <option key={p.id} value={p.id}>
                          {p.name_ar}
                        </option>
                      ))}
                    </Select>
                  </td>
                  <td className="p-2">
                    <Input
                      className="numeric h-8 w-24"
                      value={l.qty}
                      onChange={(e) => updateLine(l.uid, { qty: e.target.value })}
                    />
                  </td>
                  <td className="p-2">
                    <Input
                      className="numeric h-8 w-24"
                      value={l.cost}
                      onChange={(e) => updateLine(l.uid, { cost: e.target.value })}
                    />
                  </td>
                  <td className="p-2">
                    <Button size="sm" variant="ghost" onClick={() => removeLine(l.uid)}>
                      {t('po.remove')}
                    </Button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}

        {error && <p className="text-sm text-danger">{t(`errors.${error}`)}</p>}
        <div className="flex justify-end gap-2">
          <Button type="button" variant="outline" onClick={onClose}>
            {t('po.cancel')}
          </Button>
          <Button
            type="button"
            disabled={createMut.isPending}
            onClick={() => {
              if (!canSubmit) {
                setError('E-VAL-001');
                return;
              }
              setError(null);
              createMut.mutate();
            }}
          >
            {createMut.isPending ? t('po.creating') : t('po.create')}
          </Button>
        </div>
      </div>
    </Modal>
  );
}

type ReceiptRow = { batch: string; expiry: string; qty: string };

function OrderDetailModal({
  poId,
  onClose,
  onChanged,
}: {
  poId: string;
  onClose: () => void;
  onChanged: () => void;
}) {
  const qc = useQueryClient();
  const [receiving, setReceiving] = useState(false);
  const [rows, setRows] = useState<Record<string, ReceiptRow>>({});
  const detailQuery = useQuery({
    queryKey: ['po-detail', poId],
    queryFn: () => getPurchaseOrder(poId),
  });
  const refresh = () => {
    qc.invalidateQueries({ queryKey: ['po-detail', poId] });
    onChanged();
  };

  const actionMut = useMutation({
    mutationFn: (action: 'submit' | 'approve' | 'cancel') => purchaseOrderAction(poId, action),
    onSuccess: () => {
      refresh();
      toast.success(t('po.action_ok'));
    },
    onError: (e) => toast.error(t(`errors.${e instanceof ApiRequestError ? e.code : 'E-SYS-001'}`)),
  });
  const receiveMut = useMutation({
    mutationFn: () =>
      receivePurchaseOrder(
        poId,
        Object.entries(rows)
          .filter(([, v]) => Number(v.qty) > 0)
          .map(([itemId, v]) => ({
            purchase_item_id: itemId,
            batch_number: v.batch,
            expiry_date: v.expiry,
            quantity: v.qty,
          })),
      ),
    onSuccess: () => {
      setReceiving(false);
      setRows({});
      refresh();
      toast.success(t('po.received_ok'));
    },
    onError: (e) => toast.error(t(`errors.${e instanceof ApiRequestError ? e.code : 'E-SYS-001'}`)),
  });

  const setRow = (itemId: string, patch: Partial<ReceiptRow>) =>
    setRows((prev) => ({
      ...prev,
      [itemId]: { batch: '', expiry: '', qty: '', ...prev[itemId], ...patch },
    }));

  const po = detailQuery.data;
  return (
    <Modal open onClose={onClose} title={t('po.detail_title')} className="max-w-3xl">
      {!po ? (
        <div className="flex justify-center py-8">
          <Spinner />
        </div>
      ) : (
        <div className="space-y-4">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="numeric font-semibold text-slate-800">{po.po_number}</div>
            <OrderStatusBadge status={po.status} />
          </div>
          <div className="text-sm text-slate-600">
            {t('po.total')}:{' '}
            <span className="numeric">
              {po.total} {po.currency_code}
            </span>
          </div>

          <table className="w-full text-start text-sm">
            <thead>
              <tr className="border-b border-border text-xs text-slate-500">
                <th className="p-2 text-start">{t('po.qty_ordered')}</th>
                <th className="p-2 text-start">{t('po.qty_received')}</th>
                <th className="p-2 text-start">{t('po.unit_cost')}</th>
                <th className="p-2 text-start">{t('po.line_total')}</th>
                {receiving && <th className="p-2 text-start">{t('po.batch_number')}</th>}
                {receiving && <th className="p-2 text-start">{t('po.expiry')}</th>}
                {receiving && <th className="p-2 text-start">{t('po.qty_to_receive')}</th>}
              </tr>
            </thead>
            <tbody>
              {(po.items ?? []).map((it) => (
                <tr key={it.id} className="border-b border-border/60">
                  <td className="numeric p-2">{it.qty_ordered}</td>
                  <td className="numeric p-2">{it.qty_received}</td>
                  <td className="numeric p-2">{it.unit_cost}</td>
                  <td className="numeric p-2">{it.line_total}</td>
                  {receiving && (
                    <>
                      <td className="p-2">
                        <Input
                          className="h-8 w-28"
                          value={rows[it.id]?.batch ?? ''}
                          onChange={(e) => setRow(it.id, { batch: e.target.value })}
                        />
                      </td>
                      <td className="p-2">
                        <Input
                          type="date"
                          className="h-8"
                          value={rows[it.id]?.expiry ?? ''}
                          onChange={(e) => setRow(it.id, { expiry: e.target.value })}
                        />
                      </td>
                      <td className="p-2">
                        <Input
                          className="numeric h-8 w-20"
                          value={rows[it.id]?.qty ?? ''}
                          onChange={(e) => setRow(it.id, { qty: e.target.value })}
                        />
                      </td>
                    </>
                  )}
                </tr>
              ))}
            </tbody>
          </table>

          <div className="flex flex-wrap justify-end gap-2">
            {po.status === 'draft' && (
              <Button size="sm" onClick={() => actionMut.mutate('submit')}>
                {t('po.submit')}
              </Button>
            )}
            {po.status === 'pending_approval' && (
              <Button size="sm" onClick={() => actionMut.mutate('approve')}>
                {t('po.approve')}
              </Button>
            )}
            {['draft', 'pending_approval', 'approved'].includes(po.status) && (
              <Button size="sm" variant="outline" onClick={() => actionMut.mutate('cancel')}>
                {t('po.cancel_order')}
              </Button>
            )}
            {['approved', 'partially_received'].includes(po.status) && !receiving && (
              <Button size="sm" onClick={() => setReceiving(true)}>
                {t('po.receive')}
              </Button>
            )}
            {receiving && (
              <Button size="sm" disabled={receiveMut.isPending} onClick={() => receiveMut.mutate()}>
                {t('po.do_receive')}
              </Button>
            )}
          </div>
        </div>
      )}
    </Modal>
  );
}

// ================================ Suppliers (P2-M1) ================================

interface SupplierForm {
  name: string;
  contact_name: string;
  phone: string;
  email: string;
  address: string;
  tax_registration_no: string;
  payment_terms: string;
  notes: string;
}

const EMPTY_FORM: SupplierForm = {
  name: '',
  contact_name: '',
  phone: '',
  email: '',
  address: '',
  tax_registration_no: '',
  payment_terms: '',
  notes: '',
};

/** Empty text -> null so optional fields clear cleanly (PATCH semantics). */
const nn = (v: string): string | null => (v.trim() === '' ? null : v.trim());

function formToPayload(f: SupplierForm): Record<string, unknown> {
  return {
    name: f.name.trim(),
    contact_name: nn(f.contact_name),
    phone: nn(f.phone),
    email: nn(f.email),
    address: nn(f.address),
    tax_registration_no: nn(f.tax_registration_no),
    payment_terms: nn(f.payment_terms),
    notes: nn(f.notes),
  };
}

function detailToForm(s: SupplierDetail): SupplierForm {
  return {
    name: s.name,
    contact_name: s.contact_name ?? '',
    phone: s.phone ?? '',
    email: s.email ?? '',
    address: s.address ?? '',
    tax_registration_no: s.tax_registration_no ?? '',
    payment_terms: s.payment_terms ?? '',
    notes: s.notes ?? '',
  };
}

function SuppliersTab() {
  const qc = useQueryClient();
  const [search, setSearch] = useState('');
  const [activeOnly, setActiveOnly] = useState(false);
  const [showForm, setShowForm] = useState(false);
  const [form, setForm] = useState<SupplierForm>(EMPTY_FORM);
  const [formError, setFormError] = useState<string | null>(null);
  const [editTarget, setEditTarget] = useState<SupplierDetail | null>(null);
  const [editForm, setEditForm] = useState<SupplierForm>(EMPTY_FORM);
  const [editError, setEditError] = useState<string | null>(null);

  const suppliersQuery = useQuery({
    queryKey: ['suppliers', search, activeOnly],
    queryFn: () => listPurchaseSuppliers({ search: search || undefined, activeOnly }),
  });
  const invalidate = () => qc.invalidateQueries({ queryKey: ['suppliers'] });

  const createMut = useMutation({
    mutationFn: () =>
      createPurchaseSupplier({
        name: form.name.trim(),
        contact_name: nn(form.contact_name),
        phone: nn(form.phone),
        email: nn(form.email),
        address: nn(form.address),
        tax_registration_no: nn(form.tax_registration_no),
        payment_terms: nn(form.payment_terms),
        notes: nn(form.notes),
      }),
    onSuccess: () => {
      setShowForm(false);
      setForm(EMPTY_FORM);
      setFormError(null);
      invalidate();
      toast.success(t('purchases.created_ok'));
    },
    onError: (e) => setFormError(e instanceof ApiRequestError ? e.code : 'E-SYS-001'),
  });

  const editMut = useMutation({
    mutationFn: (id: string) => updatePurchaseSupplier(id, formToPayload(editForm)),
    onSuccess: () => {
      setEditTarget(null);
      setEditError(null);
      invalidate();
      toast.success(t('purchases.updated_ok'));
    },
    onError: (e) => setEditError(e instanceof ApiRequestError ? e.code : 'E-SYS-001'),
  });

  const activeMut = useMutation({
    mutationFn: (v: { id: string; active: boolean }) =>
      updatePurchaseSupplier(v.id, { is_active: v.active }),
    onSuccess: invalidate,
    onError: (e) => toast.error(t(`errors.${e instanceof ApiRequestError ? e.code : 'E-SYS-001'}`)),
  });

  const openEdit = (s: SupplierDetail) => {
    setEditTarget(s);
    setEditForm(detailToForm(s));
    setEditError(null);
  };

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex flex-wrap items-center gap-3">
          <Input
            className="max-w-xs"
            placeholder={t('purchases.search')}
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
          <label className="flex items-center gap-2 text-sm text-slate-600">
            <input
              type="checkbox"
              checked={activeOnly}
              onChange={(e) => setActiveOnly(e.target.checked)}
            />
            {t('purchases.active_only')}
          </label>
        </div>
        <Button onClick={() => setShowForm((v) => !v)}>
          {showForm ? t('purchases.cancel') : t('purchases.add')}
        </Button>
      </div>

      {showForm && (
        <Card>
          <CardContent className="pt-6">
            <form
              className="grid grid-cols-1 gap-4 sm:grid-cols-2"
              onSubmit={(e) => {
                e.preventDefault();
                createMut.mutate();
              }}
            >
              <Field label={t('purchases.name')}>
                <Input
                  value={form.name}
                  onChange={(e) => setForm({ ...form, name: e.target.value })}
                  required
                />
              </Field>
              <Field label={t('purchases.contact_name')}>
                <Input
                  value={form.contact_name}
                  onChange={(e) => setForm({ ...form, contact_name: e.target.value })}
                />
              </Field>
              <Field label={t('purchases.phone')}>
                <Input
                  className="numeric"
                  value={form.phone}
                  onChange={(e) => setForm({ ...form, phone: e.target.value })}
                />
              </Field>
              <Field label={t('purchases.email')}>
                <Input
                  type="email"
                  value={form.email}
                  onChange={(e) => setForm({ ...form, email: e.target.value })}
                />
              </Field>
              <Field label={t('purchases.tax_no')}>
                <Input
                  value={form.tax_registration_no}
                  onChange={(e) => setForm({ ...form, tax_registration_no: e.target.value })}
                />
              </Field>
              <Field label={t('purchases.payment_terms')}>
                <Input
                  value={form.payment_terms}
                  onChange={(e) => setForm({ ...form, payment_terms: e.target.value })}
                />
              </Field>
              <Field label={t('purchases.address')}>
                <Input
                  value={form.address}
                  onChange={(e) => setForm({ ...form, address: e.target.value })}
                />
              </Field>
              <Field label={t('purchases.notes')}>
                <Input
                  value={form.notes}
                  onChange={(e) => setForm({ ...form, notes: e.target.value })}
                />
              </Field>

              <div className="flex items-end gap-3">
                <Button type="submit" disabled={createMut.isPending}>
                  {createMut.isPending ? t('purchases.creating') : t('purchases.create')}
                </Button>
                {formError && (
                  <span className="text-sm text-danger">{t(`errors.${formError}`)}</span>
                )}
              </div>
            </form>
          </CardContent>
        </Card>
      )}

      <Card>
        <CardContent className="pt-6">
          {suppliersQuery.isLoading ? (
            <div className="flex justify-center py-8">
              <Spinner />
            </div>
          ) : (
            <SupplierTable
              rows={suppliersQuery.data ?? []}
              onEdit={openEdit}
              onToggle={(s) => activeMut.mutate({ id: s.id, active: !s.is_active })}
            />
          )}
        </CardContent>
      </Card>

      <Modal
        open={editTarget !== null}
        onClose={() => setEditTarget(null)}
        title={t('purchases.edit_title')}
        className="max-w-2xl"
      >
        <form
          className="space-y-4"
          onSubmit={(e) => {
            e.preventDefault();
            if (editTarget) editMut.mutate(editTarget.id);
          }}
        >
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <Field label={t('purchases.name')}>
              <Input
                value={editForm.name}
                onChange={(e) => setEditForm({ ...editForm, name: e.target.value })}
                required
              />
            </Field>
            <Field label={t('purchases.contact_name')}>
              <Input
                value={editForm.contact_name}
                onChange={(e) => setEditForm({ ...editForm, contact_name: e.target.value })}
              />
            </Field>
            <Field label={t('purchases.phone')}>
              <Input
                className="numeric"
                value={editForm.phone}
                onChange={(e) => setEditForm({ ...editForm, phone: e.target.value })}
              />
            </Field>
            <Field label={t('purchases.email')}>
              <Input
                type="email"
                value={editForm.email}
                onChange={(e) => setEditForm({ ...editForm, email: e.target.value })}
              />
            </Field>
            <Field label={t('purchases.tax_no')}>
              <Input
                value={editForm.tax_registration_no}
                onChange={(e) => setEditForm({ ...editForm, tax_registration_no: e.target.value })}
              />
            </Field>
            <Field label={t('purchases.payment_terms')}>
              <Input
                value={editForm.payment_terms}
                onChange={(e) => setEditForm({ ...editForm, payment_terms: e.target.value })}
              />
            </Field>
            <Field label={t('purchases.address')}>
              <Input
                value={editForm.address}
                onChange={(e) => setEditForm({ ...editForm, address: e.target.value })}
              />
            </Field>
            <Field label={t('purchases.notes')}>
              <Input
                value={editForm.notes}
                onChange={(e) => setEditForm({ ...editForm, notes: e.target.value })}
              />
            </Field>
          </div>
          {editError && <p className="text-sm text-danger">{t(`errors.${editError}`)}</p>}
          <div className="flex justify-end gap-2">
            <Button type="button" variant="outline" onClick={() => setEditTarget(null)}>
              {t('purchases.cancel')}
            </Button>
            <Button type="submit" disabled={editMut.isPending}>
              {editMut.isPending ? t('purchases.saving') : t('purchases.save')}
            </Button>
          </div>
        </form>
      </Modal>
    </div>
  );
}

function SupplierTable({
  rows,
  onEdit,
  onToggle,
}: {
  rows: SupplierDetail[];
  onEdit: (s: SupplierDetail) => void;
  onToggle: (s: SupplierDetail) => void;
}) {
  if (rows.length === 0) {
    return <p className="py-8 text-center text-sm text-slate-500">{t('purchases.empty')}</p>;
  }
  return (
    <table className="w-full text-start text-sm">
      <thead>
        <tr className="border-b border-border text-start text-xs text-slate-500">
          <th className="p-2 text-start">{t('purchases.name')}</th>
          <th className="p-2 text-start">{t('purchases.contact_name')}</th>
          <th className="p-2 text-start">{t('purchases.phone')}</th>
          <th className="p-2 text-start">{t('purchases.tax_no')}</th>
          <th className="p-2 text-start">{t('purchases.payment_terms')}</th>
          <th className="p-2 text-start">{t('purchases.status')}</th>
          <th className="p-2 text-start">{t('purchases.actions')}</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((s) => (
          <tr key={s.id} className="border-b border-border/60">
            <td className="p-2 font-medium text-slate-800">{s.name}</td>
            <td className="p-2 text-slate-700">{s.contact_name ?? '—'}</td>
            <td className="numeric p-2 text-slate-700">{s.phone ?? '—'}</td>
            <td className="numeric p-2 text-slate-700">{s.tax_registration_no ?? '—'}</td>
            <td className="p-2 text-slate-700">{s.payment_terms ?? '—'}</td>
            <td className="p-2">
              <Badge tone={s.is_active ? 'success' : 'neutral'}>
                {s.is_active ? t('purchases.active') : t('purchases.inactive')}
              </Badge>
            </td>
            <td className="flex flex-wrap gap-2 p-2">
              <Button size="sm" variant="ghost" onClick={() => onEdit(s)}>
                {t('purchases.edit')}
              </Button>
              <Button
                size="sm"
                variant={s.is_active ? 'outline' : 'primary'}
                onClick={() => onToggle(s)}
              >
                {s.is_active ? t('purchases.deactivate') : t('purchases.activate')}
              </Button>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex flex-col gap-1.5">
      <Label>{label}</Label>
      {children}
    </div>
  );
}

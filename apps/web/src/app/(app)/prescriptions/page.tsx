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
  createPrescription,
  type ControlledLogRow,
  getCustomer,
  getMedication,
  getPrescription,
  listControlledSubstanceLog,
  listCustomers,
  listInventoryBranches,
  listPrescriptions,
  type MedOption,
  type PackagingLevel,
  type PrescriptionStatus,
  searchMedications,
  updatePrescription,
} from '@/lib/api';
import { useAuth } from '@/lib/auth-store';
import { t } from '@/lib/i18n';
import { toast } from '@/lib/toast-store';

const errCode = (e: unknown) => (e instanceof ApiRequestError ? e.code : 'E-SYS-001');
const onErr = (e: unknown) => toast.error(t(`errors.${errCode(e)}`));

const STATUS_TONE: Record<string, 'neutral' | 'warning' | 'success' | 'danger'> = {
  pending: 'neutral',
  partially_fulfilled: 'warning',
  fulfilled: 'success',
  expired: 'danger',
  cancelled: 'danger',
};

const STATUSES: PrescriptionStatus[] = [
  'pending',
  'partially_fulfilled',
  'fulfilled',
  'expired',
  'cancelled',
];

export default function PrescriptionsPage() {
  const [tab, setTab] = useState<'list' | 'log'>('list');
  const [branchId, setBranchId] = useState('');
  const canViewLog = useAuth((s) => s.hasPermission('controlled_substances.view'));

  const branchesQuery = useQuery({ queryKey: ['inv-branches'], queryFn: listInventoryBranches });
  const branches = branchesQuery.data ?? [];
  useEffect(() => {
    const first = branches[0];
    if (!branchId && first) setBranchId(first.id);
  }, [branches, branchId]);

  if (branchesQuery.isLoading) {
    return (
      <div className="flex justify-center py-16">
        <Spinner />
      </div>
    );
  }
  if (branches.length === 0) {
    return <p className="py-10 text-center text-slate-500">{t('prescriptions.no_branch')}</p>;
  }

  return (
    <div className="mx-auto max-w-5xl space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-2xl font-bold text-slate-900">{t('prescriptions.title')}</h1>
        <div className="flex items-center gap-2">
          <Label className="text-xs">{t('inventory.branch')}</Label>
          <Select className="h-9" value={branchId} onChange={(e) => setBranchId(e.target.value)}>
            {branches.map((b) => (
              <option key={b.id} value={b.id}>
                {b.name}
              </option>
            ))}
          </Select>
        </div>
      </div>

      <div className="flex gap-1 border-b border-border">
        <TabButton active={tab === 'list'} onClick={() => setTab('list')}>
          {t('prescriptions.tab_list')}
        </TabButton>
        {canViewLog && (
          <TabButton active={tab === 'log'} onClick={() => setTab('log')}>
            {t('prescriptions.tab_log')}
          </TabButton>
        )}
      </div>

      {tab === 'list' ? (
        <PrescriptionsTab branchId={branchId} />
      ) : (
        <ControlledLogTab branchId={branchId} />
      )}
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

// ================================ Prescriptions ================================

function PrescriptionsTab({ branchId }: { branchId: string }) {
  const canCreate = useAuth((s) => s.hasPermission('prescriptions.create'));
  const qc = useQueryClient();

  const [status, setStatus] = useState<PrescriptionStatus | ''>('');
  const [showCreate, setShowCreate] = useState(false);
  const [detailId, setDetailId] = useState<string | null>(null);

  const listQuery = useQuery({
    queryKey: ['prescriptions', branchId, status],
    queryFn: () => listPrescriptions(branchId, status ? { status } : {}),
    enabled: !!branchId,
  });
  const rows = listQuery.data ?? [];

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <Select
          className="h-9 w-56"
          value={status}
          onChange={(e) => setStatus(e.target.value as PrescriptionStatus | '')}
        >
          <option value="">{t('prescriptions.status_all')}</option>
          {STATUSES.map((s) => (
            <option key={s} value={s}>
              {t(`prescriptions.status_${s}`)}
            </option>
          ))}
        </Select>
        {canCreate && <Button onClick={() => setShowCreate(true)}>{t('prescriptions.new')}</Button>}
      </div>

      <Card>
        <CardContent className="pt-6">
          {listQuery.isLoading ? (
            <div className="flex justify-center py-8">
              <Spinner />
            </div>
          ) : rows.length === 0 ? (
            <p className="py-6 text-center text-sm text-slate-500">{t('prescriptions.empty')}</p>
          ) : (
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-border text-xs text-slate-500">
                  <th className="p-2 text-start">{t('prescriptions.doctor_name')}</th>
                  <th className="p-2 text-start">{t('prescriptions.date')}</th>
                  <th className="p-2 text-start">{t('prescriptions.status')}</th>
                  <th className="p-2 text-start"></th>
                </tr>
              </thead>
              <tbody>
                {rows.map((p) => (
                  <tr key={p.id} className="border-b border-border/60">
                    <td className="p-2 font-medium text-slate-800">{p.doctor_name}</td>
                    <td className="p-2 text-xs text-slate-500">{p.prescription_date}</td>
                    <td className="p-2">
                      <Badge tone={STATUS_TONE[p.status] ?? 'neutral'}>
                        {t(`prescriptions.status_${p.status}`)}
                      </Badge>
                    </td>
                    <td className="p-2 text-end">
                      <Button size="sm" variant="ghost" onClick={() => setDetailId(p.id)}>
                        {t('prescriptions.view')}
                      </Button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </CardContent>
      </Card>

      {showCreate && (
        <CreatePrescriptionModal
          branchId={branchId}
          onClose={() => setShowCreate(false)}
          onCreated={() => {
            setShowCreate(false);
            qc.invalidateQueries({ queryKey: ['prescriptions', branchId] });
            toast.success(t('prescriptions.created_ok'));
          }}
        />
      )}
      {detailId && (
        <PrescriptionDetailModal
          prescriptionId={detailId}
          onClose={() => setDetailId(null)}
          onChanged={() => qc.invalidateQueries({ queryKey: ['prescriptions', branchId] })}
        />
      )}
    </div>
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

// ------------------------------ create ------------------------------

interface ItemDraft {
  uid: string;
  medication_id: string;
  label: string;
  packaging: PackagingLevel[];
  packaging_id: string;
  qty: string;
}

function CreatePrescriptionModal({
  branchId,
  onClose,
  onCreated,
}: {
  branchId: string;
  onClose: () => void;
  onCreated: () => void;
}) {
  const [doctorName, setDoctorName] = useState('');
  const [doctorLicense, setDoctorLicense] = useState('');
  const [date, setDate] = useState(() => new Date().toISOString().slice(0, 10));
  const [notes, setNotes] = useState('');
  const [customerId, setCustomerId] = useState<string | null>(null);
  const [customerLabel, setCustomerLabel] = useState('');
  const [customerTerm, setCustomerTerm] = useState('');
  const [items, setItems] = useState<ItemDraft[]>([]);
  const [medTerm, setMedTerm] = useState('');
  const [error, setError] = useState<string | null>(null);

  const medQuery = useQuery({
    queryKey: ['rx-med-search', medTerm],
    queryFn: () => searchMedications(medTerm),
    enabled: medTerm.trim().length >= 2,
  });
  const customerQuery = useQuery({
    queryKey: ['rx-customer-search', customerTerm],
    queryFn: () => listCustomers({ search: customerTerm, activeOnly: true }),
    enabled: customerTerm.trim().length >= 2 && !customerId,
  });

  const addItemMut = useMutation({
    mutationFn: (m: MedOption) => getMedication(m.id),
    onSuccess: (detail, m) => {
      const sellable = detail.packaging.filter((p) => p.is_sellable);
      const def = sellable.find((p) => p.is_default_sale) ?? sellable[0] ?? detail.packaging[0];
      if (!def) {
        toast.error(t('prescriptions.no_packaging'));
        return;
      }
      setItems((prev) => [
        ...prev,
        {
          uid: crypto.randomUUID(),
          medication_id: m.id,
          label: m.trade_name_ar ?? m.trade_name,
          packaging: detail.packaging,
          packaging_id: def.id,
          qty: '',
        },
      ]);
      setMedTerm('');
    },
    onError: onErr,
  });

  const createMut = useMutation({
    mutationFn: () =>
      createPrescription({
        branch_id: branchId,
        customer_id: customerId,
        doctor_name: doctorName.trim(),
        doctor_license_no: doctorLicense.trim() || null,
        prescription_date: date,
        notes: notes.trim() || null,
        items: items.map((it) => ({
          medication_id: it.medication_id,
          packaging_id: it.packaging_id,
          quantity: it.qty,
        })),
      }),
    onSuccess: onCreated,
    onError: (e) => setError(errCode(e)),
  });

  const updateItem = (uid: string, patch: Partial<ItemDraft>) =>
    setItems((prev) => prev.map((it) => (it.uid === uid ? { ...it, ...patch } : it)));
  const removeItem = (uid: string) => setItems((prev) => prev.filter((it) => it.uid !== uid));

  const canSubmit =
    doctorName.trim() !== '' &&
    date !== '' &&
    items.length > 0 &&
    items.every((it) => Number(it.qty) > 0);

  return (
    <Modal
      open
      onClose={onClose}
      title={t('prescriptions.new')}
      className="max-h-[85vh] max-w-3xl overflow-y-auto"
    >
      <div className="space-y-4">
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <Field label={t('prescriptions.doctor_name')}>
            <Input value={doctorName} onChange={(e) => setDoctorName(e.target.value)} autoFocus />
          </Field>
          <Field label={t('prescriptions.doctor_license')}>
            <Input value={doctorLicense} onChange={(e) => setDoctorLicense(e.target.value)} />
          </Field>
          <Field label={t('prescriptions.date')}>
            <Input type="date" value={date} onChange={(e) => setDate(e.target.value)} />
          </Field>
          <Field label={t('prescriptions.customer')}>
            {customerId ? (
              <div className="flex items-center justify-between rounded-[var(--radius-md)] border border-border px-3 py-1.5 text-sm">
                <span>{customerLabel}</span>
                <button
                  type="button"
                  className="text-xs text-slate-500 hover:underline"
                  onClick={() => {
                    setCustomerId(null);
                    setCustomerLabel('');
                  }}
                >
                  {t('pos.clear_customer')}
                </button>
              </div>
            ) : (
              <div className="relative">
                <Input
                  value={customerTerm}
                  onChange={(e) => setCustomerTerm(e.target.value)}
                  placeholder={t('pos.search_customer')}
                />
                {customerTerm.trim().length >= 2 && (customerQuery.data ?? []).length > 0 && (
                  <div className="absolute z-10 mt-1 max-h-40 w-full overflow-auto rounded-[var(--radius-md)] border border-border bg-white shadow-lg">
                    {(customerQuery.data ?? []).map((c) => (
                      <button
                        key={c.id}
                        type="button"
                        className="block w-full px-3 py-1.5 text-start text-sm hover:bg-primary-50"
                        onClick={() => {
                          setCustomerId(c.id);
                          setCustomerLabel(c.phone ? `${c.name} (${c.phone})` : c.name);
                          setCustomerTerm('');
                        }}
                      >
                        {c.name} {c.phone ? `(${c.phone})` : ''}
                      </button>
                    ))}
                  </div>
                )}
              </div>
            )}
          </Field>
        </div>

        <Field label={t('prescriptions.notes')}>
          <Input value={notes} onChange={(e) => setNotes(e.target.value)} />
        </Field>

        <div className="space-y-2">
          <Label>{t('prescriptions.add_item')}</Label>
          <Input
            placeholder={t('prescriptions.search_med')}
            value={medTerm}
            onChange={(e) => setMedTerm(e.target.value)}
          />
          {medTerm.trim().length >= 2 && (
            <div className="max-h-40 overflow-auto rounded-[var(--radius-md)] border border-border">
              {(medQuery.data ?? []).length === 0 ? (
                <p className="p-2 text-xs text-slate-500">{t('prescriptions.no_results')}</p>
              ) : (
                (medQuery.data ?? []).map((m) => (
                  <button
                    key={m.id}
                    type="button"
                    onClick={() => addItemMut.mutate(m)}
                    className="block w-full px-3 py-1.5 text-start text-sm hover:bg-primary-50"
                  >
                    {m.trade_name_ar ?? m.trade_name}
                  </button>
                ))
              )}
            </div>
          )}
        </div>

        {items.length > 0 && (
          <table className="w-full text-start text-sm">
            <thead>
              <tr className="border-b border-border text-xs text-slate-500">
                <th className="p-2 text-start">{t('prescriptions.medication')}</th>
                <th className="p-2 text-start">{t('prescriptions.packaging')}</th>
                <th className="p-2 text-start">{t('prescriptions.quantity')}</th>
                <th className="p-2" />
              </tr>
            </thead>
            <tbody>
              {items.map((it) => (
                <tr key={it.uid} className="border-b border-border/60">
                  <td className="p-2 text-slate-800">{it.label}</td>
                  <td className="p-2">
                    <Select
                      className="h-8"
                      value={it.packaging_id}
                      onChange={(e) => updateItem(it.uid, { packaging_id: e.target.value })}
                    >
                      {it.packaging.map((p) => (
                        <option key={p.id} value={p.id}>
                          {p.name_ar}
                        </option>
                      ))}
                    </Select>
                  </td>
                  <td className="p-2">
                    <Input
                      className="h-8 w-24"
                      inputMode="decimal"
                      value={it.qty}
                      onChange={(e) => updateItem(it.uid, { qty: e.target.value })}
                    />
                  </td>
                  <td className="p-2">
                    <Button size="sm" variant="ghost" onClick={() => removeItem(it.uid)}>
                      {t('po.remove')}
                    </Button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}

        {error && <p className="text-sm text-danger">{t(`errors.${error}`)}</p>}
        <div className="flex justify-end gap-2 border-t border-border pt-4">
          <Button type="button" variant="outline" onClick={onClose}>
            {t('users.cancel')}
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
            {createMut.isPending ? t('prescriptions.creating') : t('prescriptions.create')}
          </Button>
        </div>
      </div>
    </Modal>
  );
}

// ------------------------------ detail ------------------------------

function PrescriptionDetailModal({
  prescriptionId,
  onClose,
  onChanged,
}: {
  prescriptionId: string;
  onClose: () => void;
  onChanged: () => void;
}) {
  const canEdit = useAuth((s) => s.hasPermission('prescriptions.edit'));
  const qc = useQueryClient();
  const query = useQuery({
    queryKey: ['prescription', prescriptionId],
    queryFn: () => getPrescription(prescriptionId),
  });
  const detail = query.data;

  const customerQuery = useQuery({
    queryKey: ['customer', detail?.customer_id],
    queryFn: () => getCustomer(detail?.customer_id as string),
    enabled: !!detail?.customer_id,
  });

  const [editing, setEditing] = useState(false);
  const [doctorName, setDoctorName] = useState('');
  const [doctorLicense, setDoctorLicense] = useState('');
  const [notes, setNotes] = useState('');

  useEffect(() => {
    if (detail && !editing) {
      setDoctorName(detail.doctor_name);
      setDoctorLicense(detail.doctor_license_no ?? '');
      setNotes(detail.notes ?? '');
    }
  }, [detail, editing]);

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ['prescription', prescriptionId] });
    onChanged();
  };

  const saveMut = useMutation({
    mutationFn: () =>
      updatePrescription(prescriptionId, {
        doctor_name: doctorName.trim(),
        doctor_license_no: doctorLicense.trim() || null,
        notes: notes.trim() || null,
      }),
    onSuccess: () => {
      invalidate();
      setEditing(false);
      toast.success(t('prescriptions.saved_ok'));
    },
    onError: onErr,
  });

  const cancelMut = useMutation({
    mutationFn: () => updatePrescription(prescriptionId, { status: 'cancelled' }),
    onSuccess: () => {
      invalidate();
      toast.success(t('prescriptions.cancelled_ok'));
    },
    onError: onErr,
  });

  const canCancel =
    canEdit && detail && detail.status !== 'cancelled' && detail.status !== 'fulfilled';

  return (
    <Modal
      open
      onClose={onClose}
      title={t('prescriptions.detail')}
      className="max-h-[85vh] max-w-2xl overflow-y-auto"
    >
      {query.isLoading || !detail ? (
        <div className="flex justify-center py-8">
          <Spinner />
        </div>
      ) : (
        <div className="space-y-5">
          <div className="flex items-center justify-between">
            <Badge tone={STATUS_TONE[detail.status] ?? 'neutral'}>
              {t(`prescriptions.status_${detail.status}`)}
            </Badge>
            <span className="text-xs text-slate-400">{detail.created_at.slice(0, 10)}</span>
          </div>

          {editing ? (
            <div className="space-y-3">
              <Field label={t('prescriptions.doctor_name')}>
                <Input value={doctorName} onChange={(e) => setDoctorName(e.target.value)} />
              </Field>
              <Field label={t('prescriptions.doctor_license')}>
                <Input value={doctorLicense} onChange={(e) => setDoctorLicense(e.target.value)} />
              </Field>
              <Field label={t('prescriptions.notes')}>
                <Input value={notes} onChange={(e) => setNotes(e.target.value)} />
              </Field>
              <div className="flex justify-end gap-2">
                <Button variant="outline" size="sm" onClick={() => setEditing(false)}>
                  {t('users.cancel')}
                </Button>
                <Button
                  size="sm"
                  disabled={!doctorName.trim() || saveMut.isPending}
                  onClick={() => saveMut.mutate()}
                >
                  {t('prescriptions.save')}
                </Button>
              </div>
            </div>
          ) : (
            <div className="grid grid-cols-2 gap-x-6 gap-y-2 text-sm">
              <div>
                <div className="text-xs text-slate-500">{t('prescriptions.doctor_name')}</div>
                <div className="text-slate-800">{detail.doctor_name}</div>
              </div>
              <div>
                <div className="text-xs text-slate-500">{t('prescriptions.doctor_license')}</div>
                <div className="text-slate-800">{detail.doctor_license_no ?? '—'}</div>
              </div>
              <div>
                <div className="text-xs text-slate-500">{t('prescriptions.date')}</div>
                <div className="text-slate-800">{detail.prescription_date}</div>
              </div>
              <div>
                <div className="text-xs text-slate-500">{t('prescriptions.customer')}</div>
                <div className="text-slate-800">{customerQuery.data?.name ?? '—'}</div>
              </div>
              {detail.notes && (
                <div className="col-span-2">
                  <div className="text-xs text-slate-500">{t('prescriptions.notes')}</div>
                  <div className="text-slate-800">{detail.notes}</div>
                </div>
              )}
            </div>
          )}

          <section className="space-y-2">
            <h2 className="text-sm font-semibold text-slate-800">{t('prescriptions.items')}</h2>
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-border text-xs text-slate-500">
                  <th className="p-2 text-start">{t('prescriptions.medication')}</th>
                  <th className="p-2 text-start">{t('prescriptions.prescribed')}</th>
                  <th className="p-2 text-start">{t('prescriptions.dispensed')}</th>
                  <th className="p-2 text-start">{t('prescriptions.remaining')}</th>
                </tr>
              </thead>
              <tbody>
                {detail.items.map((item) => (
                  <tr key={item.id} className="border-b border-border/60">
                    <td className="p-2 font-medium text-slate-800">
                      {item.trade_name_ar ?? item.trade_name}
                      <span className="ms-1 text-xs text-slate-400">
                        ({item.packaging_name_ar})
                      </span>
                    </td>
                    <td className="p-2 tabular-nums text-slate-600">{item.prescribed_qty}</td>
                    <td className="p-2 tabular-nums text-slate-600">
                      {item.dispensed_qty_smallest}
                    </td>
                    <td className="p-2 tabular-nums text-slate-800">
                      {item.remaining_qty_smallest}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </section>

          {canEdit && !editing && (
            <div className="flex justify-end gap-2 border-t border-border pt-4">
              <Button variant="outline" size="sm" onClick={() => setEditing(true)}>
                {t('prescriptions.edit_header')}
              </Button>
              {canCancel && (
                <Button
                  variant="danger"
                  size="sm"
                  disabled={cancelMut.isPending}
                  onClick={() => cancelMut.mutate()}
                >
                  {t('prescriptions.cancel_prescription')}
                </Button>
              )}
            </div>
          )}
        </div>
      )}
    </Modal>
  );
}

// ================================ Controlled-substance log ================================

function ControlledLogTab({ branchId }: { branchId: string }) {
  const [medFilter, setMedFilter] = useState<MedOption | null>(null);
  const [medTerm, setMedTerm] = useState('');

  const medQuery = useQuery({
    queryKey: ['controlled-med-search', medTerm],
    queryFn: () => searchMedications(medTerm),
    enabled: medTerm.trim().length >= 2 && !medFilter,
  });

  const listQuery = useQuery({
    queryKey: ['controlled-log', branchId, medFilter?.id],
    queryFn: () =>
      listControlledSubstanceLog(branchId, medFilter ? { medicationId: medFilter.id } : {}),
    enabled: !!branchId,
  });
  const rows: ControlledLogRow[] = listQuery.data ?? [];

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <Label className="text-xs text-slate-500">{t('controlled.filter_medication')}</Label>
        {medFilter ? (
          <div className="flex items-center gap-2 rounded-[var(--radius-md)] border border-border bg-white px-3 py-1.5 text-sm">
            <span>{medFilter.trade_name_ar ?? medFilter.trade_name}</span>
            <button
              type="button"
              className="text-xs text-slate-500 hover:underline"
              onClick={() => setMedFilter(null)}
            >
              {t('controlled.clear_filter')}
            </button>
          </div>
        ) : (
          <div className="relative w-64">
            <Input
              className="h-9"
              value={medTerm}
              onChange={(e) => setMedTerm(e.target.value)}
              placeholder={t('prescriptions.search_med')}
            />
            {medTerm.trim().length >= 2 && (medQuery.data ?? []).length > 0 && (
              <div className="absolute z-10 mt-1 max-h-40 w-full overflow-auto rounded-[var(--radius-md)] border border-border bg-white shadow-lg">
                {(medQuery.data ?? []).map((m) => (
                  <button
                    key={m.id}
                    type="button"
                    className="block w-full px-3 py-1.5 text-start text-sm hover:bg-primary-50"
                    onClick={() => {
                      setMedFilter(m);
                      setMedTerm('');
                    }}
                  >
                    {m.trade_name_ar ?? m.trade_name}
                  </button>
                ))}
              </div>
            )}
          </div>
        )}
      </div>

      <Card>
        <CardContent className="pt-6">
          {listQuery.isLoading ? (
            <div className="flex justify-center py-8">
              <Spinner />
            </div>
          ) : rows.length === 0 ? (
            <p className="py-6 text-center text-sm text-slate-500">{t('controlled.empty')}</p>
          ) : (
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-border text-xs text-slate-500">
                  <th className="p-2 text-start">{t('controlled.medication')}</th>
                  <th className="p-2 text-start">{t('controlled.batch')}</th>
                  <th className="p-2 text-start">{t('controlled.invoice')}</th>
                  <th className="p-2 text-start">{t('controlled.qty')}</th>
                  <th className="p-2 text-start">{t('controlled.dispensed_by')}</th>
                  <th className="p-2 text-start">{t('controlled.linked_rx')}</th>
                  <th className="p-2 text-start">{t('controlled.date')}</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => (
                  <tr key={r.id} className="border-b border-border/60">
                    <td className="p-2 font-medium text-slate-800">
                      {r.trade_name_ar ?? r.trade_name}
                    </td>
                    <td className="p-2 font-mono text-xs text-slate-600">{r.batch_number}</td>
                    <td className="p-2 font-mono text-xs text-slate-600">{r.invoice_number}</td>
                    <td className="p-2 tabular-nums text-slate-800">{r.quantity_dispensed}</td>
                    <td className="p-2 text-slate-600">{r.dispensed_by_name}</td>
                    <td className="p-2">
                      <Badge tone={r.prescription_id ? 'primary' : 'neutral'}>
                        {r.prescription_id ? t('controlled.linked_rx') : t('controlled.not_linked')}
                      </Badge>
                    </td>
                    <td className="p-2 text-xs text-slate-400">
                      {r.created_at.slice(0, 16).replace('T', ' ')}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </CardContent>
      </Card>
    </div>
  );
}

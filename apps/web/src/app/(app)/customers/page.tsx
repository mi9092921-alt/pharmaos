'use client';

import { Badge, Button, Card, CardContent, Input, Label, Modal, Spinner } from '@pharmaos/ui';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useState } from 'react';

import {
  adjustLoyalty,
  ApiRequestError,
  createCustomer,
  type CustomerSummary,
  customerHistory,
  deleteCustomer,
  getCustomer,
  listCustomers,
  listLoyalty,
  updateCustomer,
} from '@/lib/api';
import { useAuth } from '@/lib/auth-store';
import { t } from '@/lib/i18n';
import { toast } from '@/lib/toast-store';

const errCode = (e: unknown) => (e instanceof ApiRequestError ? e.code : 'E-SYS-001');
const onErr = (e: unknown) => toast.error(t(`errors.${errCode(e)}`));

const TXN_TONE: Record<string, 'success' | 'warning' | 'neutral'> = {
  earn: 'success',
  adjust: 'warning',
  redeem: 'neutral',
};

export default function CustomersPage() {
  const canCreate = useAuth((s) => s.hasPermission('customers.create'));
  const canEdit = useAuth((s) => s.hasPermission('customers.edit'));
  const canDelete = useAuth((s) => s.hasPermission('customers.delete'));

  const [search, setSearch] = useState('');
  const [query, setQuery] = useState('');
  const [activeOnly, setActiveOnly] = useState(false);
  const [form, setForm] = useState<CustomerSummary | 'new' | null>(null);
  const [detailId, setDetailId] = useState<string | null>(null);

  const listQuery = useQuery({
    queryKey: ['customers', query, activeOnly],
    queryFn: () => listCustomers({ search: query, activeOnly }),
  });
  const rows = listQuery.data ?? [];

  return (
    <div className="mx-auto max-w-5xl space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-2xl font-bold text-slate-900">{t('customers.title')}</h1>
        {canCreate && <Button onClick={() => setForm('new')}>{t('customers.add')}</Button>}
      </div>

      <form
        className="flex flex-wrap items-center gap-3"
        onSubmit={(e) => {
          e.preventDefault();
          setQuery(search.trim());
        }}
      >
        <Input
          className="max-w-sm"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder={t('customers.search')}
        />
        <label className="flex cursor-pointer items-center gap-2 text-sm text-slate-700">
          <input
            type="checkbox"
            checked={activeOnly}
            onChange={(e) => setActiveOnly(e.target.checked)}
          />
          {t('customers.active_only')}
        </label>
      </form>

      <Card>
        <CardContent className="pt-6">
          {listQuery.isLoading ? (
            <div className="flex justify-center py-8">
              <Spinner />
            </div>
          ) : rows.length === 0 ? (
            <p className="py-6 text-center text-sm text-slate-500">{t('customers.empty')}</p>
          ) : (
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-border text-xs text-slate-500">
                  <th className="p-2 text-start">{t('customers.name')}</th>
                  <th className="p-2 text-start">{t('customers.phone')}</th>
                  <th className="p-2 text-start">{t('customers.points')}</th>
                  <th className="p-2 text-start">{t('customers.status')}</th>
                  <th className="p-2 text-start"></th>
                </tr>
              </thead>
              <tbody>
                {rows.map((c) => (
                  <tr key={c.id} className="border-b border-border/60">
                    <td className="p-2 font-medium text-slate-800">{c.name}</td>
                    <td className="p-2 tabular-nums text-slate-600">{c.phone ?? '—'}</td>
                    <td className="p-2 tabular-nums text-slate-800">{c.loyalty_points}</td>
                    <td className="p-2">
                      <Badge tone={c.is_active ? 'success' : 'neutral'}>
                        {c.is_active ? t('customers.active') : t('customers.inactive')}
                      </Badge>
                    </td>
                    <td className="flex flex-wrap justify-end gap-2 p-2">
                      <Button size="sm" variant="ghost" onClick={() => setDetailId(c.id)}>
                        {t('customers.view')}
                      </Button>
                      {canEdit && (
                        <Button size="sm" variant="outline" onClick={() => setForm(c)}>
                          {t('customers.edit')}
                        </Button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </CardContent>
      </Card>

      {form && <CustomerFormModal target={form} onClose={() => setForm(null)} />}
      {detailId && (
        <CustomerDetailModal
          customerId={detailId}
          canEdit={canEdit}
          canDelete={canDelete}
          onClose={() => setDetailId(null)}
        />
      )}
    </div>
  );
}

// ------------------------------ create / edit form ------------------------------

function CustomerFormModal({
  target,
  onClose,
}: {
  target: CustomerSummary | 'new';
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const isNew = target === 'new';
  const existingId = target === 'new' ? null : target.id;

  // On edit, load the full detail (decrypted PII) to prefill.
  const detailQuery = useQuery({
    queryKey: ['customer', existingId],
    queryFn: () => getCustomer(existingId as string),
    enabled: !!existingId,
  });

  const [name, setName] = useState(target === 'new' ? '' : target.name);
  const [phone, setPhone] = useState(target === 'new' ? '' : (target.phone ?? ''));
  const [nationalId, setNationalId] = useState('');
  const [insurance, setInsurance] = useState('');
  const [notes, setNotes] = useState('');
  const [prefilled, setPrefilled] = useState(isNew);

  // Prefill the encrypted fields once the authorized detail read returns.
  useEffect(() => {
    if (!prefilled && detailQuery.data) {
      setNationalId(detailQuery.data.national_id ?? '');
      setInsurance(detailQuery.data.insurance_number ?? '');
      setNotes(detailQuery.data.notes ?? '');
      setPrefilled(true);
    }
  }, [prefilled, detailQuery.data]);

  const mut = useMutation({
    mutationFn: () => {
      const body = {
        name: name.trim(),
        phone: phone.trim() || null,
        national_id: nationalId.trim() || null,
        insurance_number: insurance.trim() || null,
        notes: notes.trim() || null,
      };
      return isNew ? createCustomer(body) : updateCustomer(existingId as string, body);
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['customers'] });
      if (existingId) qc.invalidateQueries({ queryKey: ['customer', existingId] });
      toast.success(t(isNew ? 'customers.created_ok' : 'customers.updated_ok'));
      onClose();
    },
    onError: onErr,
  });

  return (
    <Modal
      open
      onClose={onClose}
      title={t(isNew ? 'customers.create_title' : 'customers.edit_title')}
      className="max-w-lg"
    >
      <form
        className="space-y-4"
        onSubmit={(e) => {
          e.preventDefault();
          if (name.trim()) mut.mutate();
        }}
      >
        <div className="space-y-1.5">
          <Label>{t('customers.name')}</Label>
          <Input value={name} onChange={(e) => setName(e.target.value)} autoFocus required />
        </div>
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <div className="space-y-1.5">
            <Label>{t('customers.phone')}</Label>
            <Input value={phone} onChange={(e) => setPhone(e.target.value)} inputMode="tel" />
          </div>
          <div className="space-y-1.5">
            <Label>{t('customers.national_id')}</Label>
            <Input value={nationalId} onChange={(e) => setNationalId(e.target.value)} />
          </div>
          <div className="space-y-1.5">
            <Label>{t('customers.insurance_number')}</Label>
            <Input value={insurance} onChange={(e) => setInsurance(e.target.value)} />
          </div>
        </div>
        <div className="space-y-1.5">
          <Label>{t('customers.notes')}</Label>
          <Input value={notes} onChange={(e) => setNotes(e.target.value)} />
        </div>
        <p className="text-xs text-slate-500">{t('customers.pii_hint')}</p>
        <div className="flex justify-end gap-2 border-t border-border pt-4">
          <Button type="button" variant="outline" onClick={onClose}>
            {t('users.cancel')}
          </Button>
          <Button type="submit" disabled={!name.trim() || mut.isPending}>
            {t('customers.save')}
          </Button>
        </div>
      </form>
    </Modal>
  );
}

// ------------------------------ detail + loyalty + history ------------------------------

function CustomerDetailModal({
  customerId,
  canEdit,
  canDelete,
  onClose,
}: {
  customerId: string;
  canEdit: boolean;
  canDelete: boolean;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const detailQuery = useQuery({
    queryKey: ['customer', customerId],
    queryFn: () => getCustomer(customerId),
  });
  const loyaltyQuery = useQuery({
    queryKey: ['loyalty', customerId],
    queryFn: () => listLoyalty(customerId),
  });
  const historyQuery = useQuery({
    queryKey: ['customer-history', customerId],
    queryFn: () => customerHistory(customerId),
  });

  const [delta, setDelta] = useState('');
  const [reason, setReason] = useState('');

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ['customer', customerId] });
    qc.invalidateQueries({ queryKey: ['loyalty', customerId] });
    qc.invalidateQueries({ queryKey: ['customers'] });
  };

  const adjustMut = useMutation({
    mutationFn: () => adjustLoyalty(customerId, Number(delta), reason.trim()),
    onSuccess: () => {
      invalidate();
      setDelta('');
      setReason('');
      toast.success(t('customers.adjusted_ok'));
    },
    onError: onErr,
  });

  const deleteMut = useMutation({
    mutationFn: () => deleteCustomer(customerId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['customers'] });
      toast.success(t('customers.deleted_ok'));
      onClose();
    },
    onError: onErr,
  });

  const c = detailQuery.data;
  const loyalty = loyaltyQuery.data;
  const history = historyQuery.data ?? [];

  return (
    <Modal
      open
      onClose={onClose}
      title={c ? c.name : t('customers.detail')}
      className="max-h-[85vh] max-w-2xl overflow-y-auto"
    >
      {detailQuery.isLoading || !c ? (
        <div className="flex justify-center py-8">
          <Spinner />
        </div>
      ) : (
        <div className="space-y-6">
          <div className="grid grid-cols-2 gap-x-6 gap-y-2 text-sm">
            <Field label={t('customers.phone')} value={c.phone ?? '—'} />
            <Field label={t('customers.points')} value={String(c.loyalty_points)} />
            <Field label={t('customers.national_id')} value={c.national_id ?? '—'} />
            <Field label={t('customers.insurance_number')} value={c.insurance_number ?? '—'} />
            {c.notes && <Field label={t('customers.notes')} value={c.notes} />}
          </div>

          {/* Loyalty */}
          <section className="space-y-3">
            <div className="flex items-center justify-between">
              <h2 className="text-sm font-semibold text-slate-800">{t('customers.loyalty')}</h2>
              <Badge tone="primary">
                {t('customers.balance')}: {loyalty?.balance ?? c.loyalty_points}
              </Badge>
            </div>
            {canEdit && (
              <form
                className="flex flex-wrap items-end gap-2"
                onSubmit={(e) => {
                  e.preventDefault();
                  if (Number(delta) !== 0 && reason.trim()) adjustMut.mutate();
                }}
              >
                <div className="space-y-1">
                  <Label className="text-xs">{t('customers.adjust_points')}</Label>
                  <Input
                    className="w-28"
                    inputMode="numeric"
                    value={delta}
                    onChange={(e) => setDelta(e.target.value)}
                    placeholder="+50 / -20"
                  />
                </div>
                <div className="min-w-40 flex-1 space-y-1">
                  <Label className="text-xs">{t('customers.reason')}</Label>
                  <Input value={reason} onChange={(e) => setReason(e.target.value)} />
                </div>
                <Button
                  type="submit"
                  size="sm"
                  variant="outline"
                  disabled={Number(delta) === 0 || !reason.trim() || adjustMut.isPending}
                >
                  {t('customers.adjust')}
                </Button>
              </form>
            )}
            {loyaltyQuery.isLoading ? (
              <Spinner />
            ) : (loyalty?.transactions.length ?? 0) === 0 ? (
              <p className="text-xs text-slate-500">{t('customers.no_transactions')}</p>
            ) : (
              <table className="w-full text-sm">
                <tbody>
                  {loyalty?.transactions.map((tx) => (
                    <tr key={tx.id} className="border-b border-border/60">
                      <td className="p-2">
                        <Badge tone={TXN_TONE[tx.txn_type] ?? 'neutral'}>
                          {t(`customers.txn_${tx.txn_type}`)}
                        </Badge>
                      </td>
                      <td className="p-2 tabular-nums font-medium">
                        {tx.points_delta > 0 ? `+${tx.points_delta}` : tx.points_delta}
                      </td>
                      <td className="p-2 text-slate-500">{tx.reason ?? '—'}</td>
                      <td className="p-2 text-xs text-slate-400">{tx.created_at.slice(0, 10)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </section>

          {/* Purchase history */}
          <section className="space-y-2">
            <h2 className="text-sm font-semibold text-slate-800">{t('customers.history')}</h2>
            {history.length === 0 ? (
              <p className="text-xs text-slate-500">{t('customers.no_history')}</p>
            ) : (
              <table className="w-full text-sm">
                <tbody>
                  {history.map((h) => (
                    <tr key={h.invoice_id} className="border-b border-border/60">
                      <td className="p-2 font-mono text-slate-700">{h.invoice_number}</td>
                      <td className="p-2 text-xs text-slate-400">{h.created_at.slice(0, 10)}</td>
                      <td className="p-2 tabular-nums text-slate-800">
                        {h.total} {h.currency_code}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </section>

          {canDelete && (
            <div className="flex justify-end border-t border-border pt-4">
              <Button
                variant="danger"
                size="sm"
                onClick={() => deleteMut.mutate()}
                disabled={deleteMut.isPending}
              >
                {t('customers.delete')}
              </Button>
            </div>
          )}
        </div>
      )}
    </Modal>
  );
}

function Field({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div className="text-xs text-slate-500">{label}</div>
      <div className="text-slate-800">{value}</div>
    </div>
  );
}

'use client';

/**
 * Shortages (النواقص) — pharmacy/market tabs, status filters, bulk actions
 * (mark ordered / create PO / export supplier list) and the substitutes
 * modal (same active ingredient — always a pharmacist decision).
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useRef, useState } from 'react';

import {
  bulkShortages,
  createShortagePo,
  getShortageSubstitutes,
  listInventoryBranches,
  listShortages,
  listSuppliers,
  shortageAskAgain,
  shortagesExportCsvUrl,
  syncShortages,
  type ShortageItem,
} from '@/lib/api';
import { useAuth } from '@/lib/auth-store';
import { formatApiErrorMessage, t } from '@/lib/i18n';
import { toast } from '@/lib/toast-store';
import { Badge, Button, Card, CardContent, Label, Modal, Select, Spinner } from '@pharmaos/ui';

const STATUSES = ['open', 'ordered', 'received', 'unavailable', 'cancelled'] as const;

const onErr = (e: unknown) => toast.error(formatApiErrorMessage(e));

export default function ShortagesPage() {
  const canEdit = useAuth((s) => s.hasPermission('inventory.edit'));
  const [branchId, setBranchId] = useState('');
  const [kind, setKind] = useState<'pharmacy' | 'market'>('pharmacy');
  const [status, setStatus] = useState<string>('');
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [subsFor, setSubsFor] = useState<ShortageItem | null>(null);
  const [poFor, setPoFor] = useState<string[] | null>(null);
  const [orderFor, setOrderFor] = useState<string[] | null>(null);

  const branchesQuery = useQuery({ queryKey: ['sh-branches'], queryFn: listInventoryBranches });
  const branches = branchesQuery.data ?? [];
  useEffect(() => {
    const first = branches[0];
    if (!branchId && first) setBranchId(first.id);
  }, [branches, branchId]);

  const queryClient = useQueryClient();
  const listQuery = useQuery({
    queryKey: ['shortages', branchId, kind, status],
    queryFn: () => listShortages(branchId, { kind, status: status || undefined, limit: 100 }),
    enabled: !!branchId,
  });
  const rows = listQuery.data?.items ?? [];

  // the page fires the auto-sync on open when permitted — never via GET;
  // the ref guard keeps it to ONE run per branch without a lint disable
  const syncMut = useMutation({
    mutationFn: () => syncShortages(branchId),
    onSuccess: (r) => {
      if (r.rows_affected > 0) toast.info(`${t('shortages.synced')}: ${r.rows_affected}`);
      queryClient.invalidateQueries({ queryKey: ['shortages', branchId] });
    },
    onError: onErr,
  });
  const syncedBranch = useRef('');
  useEffect(() => {
    if (canEdit && branchId && syncedBranch.current !== branchId) {
      syncedBranch.current = branchId;
      syncMut.mutate();
    }
  }, [canEdit, branchId, syncMut]);

  const bulkMut = useMutation({
    mutationFn: (input: { ids: string[]; action: string; supplierId?: string }) =>
      bulkShortages(branchId, input.ids, input.action, input.supplierId),
    onSuccess: (r) => {
      toast.success(`${t('shortages.bulk_done')}: ${r.updated}`);
      setSelected(new Set());
      queryClient.invalidateQueries({ queryKey: ['shortages', branchId] });
    },
    onError: onErr,
  });

  function toggle(id: string) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  if (branchesQuery.isLoading) {
    return (
      <div className="flex justify-center py-16">
        <Spinner />
      </div>
    );
  }
  if (branches.length === 0) {
    return <p className="py-10 text-center text-slate-500">{t('inventory.no_branch')}</p>;
  }

  return (
    <div className="mx-auto max-w-6xl space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-2xl font-bold text-slate-900">{t('shortages.title')}</h1>
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

      <div className="flex flex-wrap items-center gap-2">
        {(['pharmacy', 'market'] as const).map((k) => (
          <button
            key={k}
            className={`rounded-md px-3 py-1.5 text-sm ${kind === k ? 'bg-slate-900 text-white' : 'bg-slate-100 text-slate-700'}`}
            onClick={() => {
              setKind(k);
              setSelected(new Set());
            }}
          >
            {t(`shortages.tab_${k}`)}
          </button>
        ))}
        <Select className="h-9 w-40" value={status} onChange={(e) => setStatus(e.target.value)}>
          <option value="">{t('shortages.all_statuses')}</option>
          {STATUSES.map((s) => (
            <option key={s} value={s}>
              {t(`shortages.status_${s}`)}
            </option>
          ))}
        </Select>
        <div className="flex-1" />
        <a href={shortagesExportCsvUrl(branchId, kind)} download>
          <Button variant="outline">{t('stocktake.export_csv')}</Button>
        </a>
      </div>

      {selected.size > 0 && canEdit && (
        <Card>
          <CardContent className="flex flex-wrap items-center gap-2 p-3">
            <span className="text-sm text-slate-600">
              {t('shortages.selected')}: {selected.size}
            </span>
            <Button
              size="sm"
              variant="outline"
              onClick={() => setOrderFor([...selected])}
              disabled={kind !== 'pharmacy'}
            >
              {t('shortages.mark_ordered')}
            </Button>
            <Button
              size="sm"
              variant="outline"
              onClick={() => setPoFor([...selected])}
              disabled={kind !== 'pharmacy'}
            >
              {t('shortages.create_po')}
            </Button>
            <Button
              size="sm"
              variant="outline"
              onClick={() => bulkMut.mutate({ ids: [...selected], action: 'mark_unavailable' })}
            >
              {t('shortages.mark_unavailable')}
            </Button>
            <Button
              size="sm"
              variant="outline"
              onClick={() => bulkMut.mutate({ ids: [...selected], action: 'mark_received' })}
            >
              {t('shortages.mark_received')}
            </Button>
            <Button
              size="sm"
              variant="danger"
              onClick={() => bulkMut.mutate({ ids: [...selected], action: 'cancel' })}
            >
              {t('shortages.cancel')}
            </Button>
          </CardContent>
        </Card>
      )}

      <Card>
        <CardContent className="p-0">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-border text-xs text-slate-500">
                <th className="w-8 p-2" />
                <th className="p-2 text-start">{t('shortages.col_name')}</th>
                <th className="p-2 text-start">{t('shortages.col_status')}</th>
                <th className="p-2 text-start">{t('shortages.col_qty')}</th>
                {kind === 'market' && <th className="p-2 text-start">{t('shortages.col_asks')}</th>}
                <th className="p-2 text-start">{t('shortages.col_customer')}</th>
                <th className="p-2 text-start">{t('shortages.col_actions')}</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.id} className="border-b border-border">
                  <td className="p-2">
                    <input
                      type="checkbox"
                      checked={selected.has(row.id)}
                      onChange={() => toggle(row.id)}
                      disabled={row.status !== 'open' && row.status !== 'ordered'}
                    />
                  </td>
                  <td className="p-2">
                    {row.name_text}
                    {row.suppress_auto_resync && (
                      <Badge tone="neutral" className="ms-1">
                        {t('shortages.auto_suppressed')}
                      </Badge>
                    )}
                  </td>
                  <td className="p-2">
                    <Badge
                      tone={
                        row.status === 'received'
                          ? 'success'
                          : row.status === 'open'
                            ? 'warning'
                            : row.status === 'cancelled'
                              ? 'danger'
                              : 'neutral'
                      }
                    >
                      {t(`shortages.status_${row.status}`)}
                    </Badge>
                  </td>
                  <td className="p-2 tabular-nums">{row.qty_needed}</td>
                  {kind === 'market' && (
                    <td className="p-2 tabular-nums">
                      {row.ask_count}
                      {row.status === 'unavailable' && (
                        <Button
                          size="sm"
                          variant="ghost"
                          className="ms-1"
                          onClick={async () => {
                            try {
                              await shortageAskAgain(branchId, row.id);
                              queryClient.invalidateQueries({ queryKey: ['shortages', branchId] });
                            } catch (e) {
                              onErr(e);
                            }
                          }}
                        >
                          {t('shortages.asked_again')}
                        </Button>
                      )}
                    </td>
                  )}
                  <td className="p-2 text-xs text-slate-600">
                    {row.customer_name ?? '—'}
                    {row.customer_phone ? ` · ${row.customer_phone}` : ''}
                  </td>
                  <td className="p-2">
                    {row.medication_id && (
                      <Button size="sm" variant="ghost" onClick={() => setSubsFor(row)}>
                        {t('shortages.substitutes')}
                      </Button>
                    )}
                  </td>
                </tr>
              ))}
              {rows.length === 0 && (
                <tr>
                  <td colSpan={7} className="p-6 text-center text-slate-500">
                    {t('shortages.empty')}
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </CardContent>
      </Card>

      {subsFor && (
        <SubstitutesModal branchId={branchId} item={subsFor} onClose={() => setSubsFor(null)} />
      )}
      {orderFor && (
        <SupplierPickerModal
          title={t('shortages.mark_ordered')}
          onPick={(supplierId) => {
            bulkMut.mutate({ ids: orderFor, action: 'mark_ordered', supplierId });
            setOrderFor(null);
          }}
          onClose={() => setOrderFor(null)}
        />
      )}
      {poFor && (
        <SupplierPickerModal
          title={t('shortages.create_po')}
          onPick={(supplierId) => {
            try {
              createShortagePo(branchId, poFor, supplierId)
                .then((r) => {
                  toast.success(`${t('shortages.po_created')}: ${r.po_number}`);
                  setSelected(new Set());
                  queryClient.invalidateQueries({ queryKey: ['shortages', branchId] });
                })
                .catch(onErr);
            } catch (e) {
              onErr(e);
            }
            setPoFor(null);
          }}
          onClose={() => setPoFor(null)}
        />
      )}
    </div>
  );
}

function SubstitutesModal({
  branchId,
  item,
  onClose,
}: {
  branchId: string;
  item: ShortageItem;
  onClose: () => void;
}) {
  const subsQuery = useQuery({
    queryKey: ['sh-subs', branchId, item.id],
    queryFn: () => getShortageSubstitutes(branchId, item.id),
  });
  const subs = subsQuery.data?.items ?? [];

  return (
    <Modal open onClose={onClose} title={t('shortages.substitutes')}>
      <div className="space-y-3">
        <div className="rounded-md bg-amber-50 p-3 text-sm text-amber-900">
          {t('shortages.substitute_warning')}
        </div>
        {subsQuery.isLoading ? (
          <Spinner />
        ) : subs.length === 0 ? (
          <p className="text-sm text-slate-500">{t('shortages.no_substitutes')}</p>
        ) : (
          <table className="w-full text-sm">
            <tbody>
              {subs.map((s) => (
                <tr key={s.medication_id} className="border-b border-border">
                  <td className="p-2">{s.name}</td>
                  <td className="p-2 text-xs text-slate-500">{s.scientific_name}</td>
                  <td className="p-2 text-end tabular-nums">{s.on_hand}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        <div className="flex justify-end">
          <Button variant="outline" onClick={onClose}>
            {t('common.close')}
          </Button>
        </div>
      </div>
    </Modal>
  );
}

function SupplierPickerModal({
  title,
  onPick,
  onClose,
}: {
  title: string;
  onPick: (supplierId: string) => void;
  onClose: () => void;
}) {
  const suppliersQuery = useQuery({ queryKey: ['sh-suppliers'], queryFn: listSuppliers });
  const [supplierId, setSupplierId] = useState('');
  const suppliers = suppliersQuery.data ?? [];

  return (
    <Modal open onClose={onClose} title={title}>
      <div className="space-y-3">
        <div className="space-y-1">
          <Label>{t('shortages.supplier')}</Label>
          <Select value={supplierId} onChange={(e) => setSupplierId(e.target.value)}>
            <option value="">—</option>
            {suppliers.map((s: { id: string; name: string }) => (
              <option key={s.id} value={s.id}>
                {s.name}
              </option>
            ))}
          </Select>
        </div>
        <div className="flex justify-end gap-2">
          <Button variant="outline" onClick={onClose}>
            {t('common.close')}
          </Button>
          <Button disabled={!supplierId} onClick={() => onPick(supplierId)}>
            {t('common.ok')}
          </Button>
        </div>
      </div>
    </Modal>
  );
}

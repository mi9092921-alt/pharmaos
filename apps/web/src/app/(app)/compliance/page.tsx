'use client';

import { Badge, Button, Card, CardContent, Label, Select, Spinner } from '@pharmaos/ui';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useState } from 'react';

import {
  ApiRequestError,
  drainEreceipts,
  drainTtEvents,
  type EReceiptRow,
  listEreceipts,
  listInventoryBranches,
  listTtEvents,
  type TtEventRow,
} from '@/lib/api';
import { useAuth } from '@/lib/auth-store';
import { t } from '@/lib/i18n';
import { toast } from '@/lib/toast-store';

const errCode = (e: unknown) => (e instanceof ApiRequestError ? e.code : 'E-SYS-001');
const onErr = (e: unknown) => toast.error(t(`errors.${errCode(e)}`));

const STATUS_TONE: Record<string, 'neutral' | 'warning' | 'success' | 'danger' | 'primary'> = {
  pending: 'warning',
  submitted: 'primary',
  accepted: 'success',
  reported: 'success',
  rejected: 'danger',
  failed: 'danger',
};

function statusBadge(status: string) {
  return <Badge tone={STATUS_TONE[status] ?? 'neutral'}>{t(`compliance.st_${status}`)}</Badge>;
}

export default function CompliancePage() {
  const [tab, setTab] = useState<'ereceipts' | 'tt'>('ereceipts');
  const [branchId, setBranchId] = useState('');
  const canTt = useAuth((s) => s.hasPermission('compliance.tt_report'));

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
    return <p className="py-10 text-center text-slate-500">{t('compliance.no_branch')}</p>;
  }

  return (
    <div className="mx-auto max-w-5xl space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-2xl font-bold text-slate-900">{t('compliance.title')}</h1>
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

      <p className="rounded-[var(--radius-md)] border border-amber-200 bg-amber-50 px-4 py-2 text-xs text-amber-800">
        {t('compliance.simulator_note')}
      </p>

      <div className="flex gap-1 border-b border-border">
        <TabButton active={tab === 'ereceipts'} onClick={() => setTab('ereceipts')}>
          {t('compliance.tab_ereceipts')}
        </TabButton>
        {canTt && (
          <TabButton active={tab === 'tt'} onClick={() => setTab('tt')}>
            {t('compliance.tab_tt')}
          </TabButton>
        )}
      </div>

      {tab === 'ereceipts' ? (
        <EreceiptsTab branchId={branchId} />
      ) : (
        <TtEventsTab branchId={branchId} />
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

function EreceiptsTab({ branchId }: { branchId: string }) {
  const qc = useQueryClient();
  const listQuery = useQuery({
    queryKey: ['ereceipts', branchId],
    queryFn: () => listEreceipts(branchId),
    enabled: !!branchId,
  });
  const rows: EReceiptRow[] = listQuery.data ?? [];

  const drainMut = useMutation({
    mutationFn: () => drainEreceipts(branchId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['ereceipts', branchId] });
      toast.success(t('compliance.drained_ok'));
    },
    onError: onErr,
  });

  return (
    <div className="space-y-4">
      <div className="flex justify-end">
        <Button onClick={() => drainMut.mutate()} disabled={drainMut.isPending}>
          {drainMut.isPending ? t('compliance.draining') : t('compliance.drain')}
        </Button>
      </div>
      <Card>
        <CardContent className="pt-6">
          {listQuery.isLoading ? (
            <div className="flex justify-center py-8">
              <Spinner />
            </div>
          ) : rows.length === 0 ? (
            <p className="py-6 text-center text-sm text-slate-500">
              {t('compliance.empty_ereceipts')}
            </p>
          ) : (
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-border text-xs text-slate-500">
                  <th className="p-2 text-start">{t('compliance.status')}</th>
                  <th className="p-2 text-start">{t('compliance.eta_uuid')}</th>
                  <th className="p-2 text-start">{t('compliance.attempts')}</th>
                  <th className="p-2 text-start">{t('compliance.created')}</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => (
                  <tr key={r.id} className="border-b border-border/60">
                    <td className="p-2">{statusBadge(r.status)}</td>
                    <td className="p-2 font-mono text-xs text-slate-600">{r.eta_uuid ?? '—'}</td>
                    <td className="p-2 tabular-nums text-slate-600">{r.submission_attempts}</td>
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

function TtEventsTab({ branchId }: { branchId: string }) {
  const qc = useQueryClient();
  const listQuery = useQuery({
    queryKey: ['tt-events', branchId],
    queryFn: () => listTtEvents(branchId),
    enabled: !!branchId,
  });
  const rows: TtEventRow[] = listQuery.data ?? [];

  const drainMut = useMutation({
    mutationFn: () => drainTtEvents(branchId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['tt-events', branchId] });
      toast.success(t('compliance.drained_ok'));
    },
    onError: onErr,
  });

  return (
    <div className="space-y-4">
      <div className="flex justify-end">
        <Button onClick={() => drainMut.mutate()} disabled={drainMut.isPending}>
          {drainMut.isPending ? t('compliance.draining') : t('compliance.drain')}
        </Button>
      </div>
      <Card>
        <CardContent className="pt-6">
          {listQuery.isLoading ? (
            <div className="flex justify-center py-8">
              <Spinner />
            </div>
          ) : rows.length === 0 ? (
            <p className="py-6 text-center text-sm text-slate-500">{t('compliance.empty_tt')}</p>
          ) : (
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-border text-xs text-slate-500">
                  <th className="p-2 text-start">{t('compliance.event_type')}</th>
                  <th className="p-2 text-start">{t('compliance.serial')}</th>
                  <th className="p-2 text-start">{t('compliance.status')}</th>
                  <th className="p-2 text-start">{t('compliance.attempts')}</th>
                  <th className="p-2 text-start">{t('compliance.created')}</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => (
                  <tr key={r.id} className="border-b border-border/60">
                    <td className="p-2">
                      <Badge tone="neutral">{t(`compliance.evt_${r.event_type}`)}</Badge>
                    </td>
                    <td className="p-2 font-mono text-xs text-slate-600">{r.serial_number}</td>
                    <td className="p-2">{statusBadge(r.status)}</td>
                    <td className="p-2 tabular-nums text-slate-600">{r.report_attempts}</td>
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

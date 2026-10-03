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
  type CashSessionInfo,
  type CashSessionRow,
  type CashSessionSummary,
  closeCashSession,
  getCurrentCashSession,
  getZReport,
  listInventoryBranches,
  openCashSession,
} from '@/lib/api';
import { useAuth } from '@/lib/auth-store';
import { t } from '@/lib/i18n';
import { toast } from '@/lib/toast-store';

const errCode = (e: unknown) => (e instanceof ApiRequestError ? e.code : 'E-SYS-001');
const onErr = (e: unknown) => toast.error(t(`errors.${errCode(e)}`));
const fmtTime = (iso: string) => iso.slice(0, 16).replace('T', ' ');

function DiscrepancyBadge({ value }: { value: string }) {
  const n = Number(value);
  if (n === 0) return <Badge tone="success">{t('cashier.balanced')}</Badge>;
  return (
    <Badge tone={n < 0 ? 'danger' : 'warning'}>
      {n < 0 ? t('cashier.shortage') : t('cashier.overage')} {Math.abs(n).toFixed(2)}
    </Badge>
  );
}

/**
 * Cash sessions (P1-M10). The cashier opens their drawer and watches its live
 * summary; the branch manager counts and closes it (permission matrix), and
 * reads the end-of-day Z report. Backend permissions stay authoritative.
 */
export default function CashierPage() {
  const qc = useQueryClient();
  const canClose = useAuth((s) => s.hasPermission('cashier.close_session'));
  const canView = useAuth((s) => s.hasPermission('cashier.view_cash'));

  const [branchId, setBranchId] = useState('');
  const [openingFloat, setOpeningFloat] = useState('');
  const [closeTarget, setCloseTarget] = useState<{
    session: CashSessionInfo;
    summary: CashSessionSummary;
  } | null>(null);

  const branchesQuery = useQuery({ queryKey: ['inv-branches'], queryFn: listInventoryBranches });
  const branches = branchesQuery.data ?? [];
  useEffect(() => {
    const first = branches[0];
    if (!branchId && first) setBranchId(first.id);
  }, [branches, branchId]);
  const currency = branches.find((b) => b.id === branchId)?.currency_code ?? 'EGP';

  const currentQuery = useQuery({
    queryKey: ['cash-current', branchId],
    queryFn: () => getCurrentCashSession(branchId),
    enabled: !!branchId,
  });
  const zQuery = useQuery({
    queryKey: ['z-report', branchId],
    queryFn: () => getZReport(branchId),
    enabled: !!branchId && canView,
  });

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ['cash-current', branchId] });
    qc.invalidateQueries({ queryKey: ['z-report', branchId] });
  };

  const openMut = useMutation({
    mutationFn: () => openCashSession(branchId, String(Number(openingFloat) || 0)),
    onSuccess: () => {
      setOpeningFloat('');
      invalidate();
      toast.success(t('cashier.opened_ok'));
    },
    onError: onErr,
  });

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

  const current = currentQuery.data;
  const session = current?.session ?? null;
  const summary = current?.summary ?? null;
  const z = zQuery.data;

  return (
    <div className="mx-auto max-w-5xl space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-2xl font-bold text-slate-900">{t('cashier.title')}</h1>
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

      {/* Current session */}
      <Card>
        <CardContent className="pt-6">
          <h2 className="mb-4 text-sm font-bold text-slate-700">{t('cashier.current')}</h2>
          {currentQuery.isLoading ? (
            <div className="flex justify-center py-6">
              <Spinner />
            </div>
          ) : session && summary ? (
            <div className="space-y-4">
              <div className="flex flex-wrap items-center gap-3 text-sm text-slate-600">
                <Badge tone="success">{t('cashier.status_open')}</Badge>
                <span>
                  {t('cashier.opened_at')}:{' '}
                  <span className="tabular-nums">{fmtTime(session.opened_at)}</span>
                </span>
                <span>
                  {t('cashier.float')}:{' '}
                  <span className="tabular-nums">{session.opening_float}</span> {currency}
                </span>
              </div>
              <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-5">
                <Stat
                  label={t('cashier.cash_sales')}
                  value={`${summary.cash_total} (${summary.cash_count})`}
                />
                <Stat
                  label={t('cashier.card_sales')}
                  value={`${summary.card_total} (${summary.card_count})`}
                />
                <Stat label={t('cashier.tendered_total')} value={summary.tendered_total} />
                <Stat label={t('cashier.change_total')} value={summary.change_total} />
                <Stat label={t('cashier.expected')} value={summary.expected_cash} highlight />
              </div>
              {/* Refunds this shift (P2-M7) — hidden when there are none, so a
                  shift with no returns looks exactly as it did before. */}
              {(summary.cash_refund_count > 0 ||
                summary.card_refund_count > 0 ||
                Number(summary.store_credit_refunded) > 0) && (
                <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
                  {summary.cash_refund_count > 0 && (
                    <Stat
                      label={t('cashier.cash_refunded')}
                      value={`${summary.cash_refunded} (${summary.cash_refund_count})`}
                    />
                  )}
                  {summary.card_refund_count > 0 && (
                    <Stat
                      label={t('cashier.card_refunded')}
                      value={`${summary.card_refunded} (${summary.card_refund_count})`}
                    />
                  )}
                  {Number(summary.store_credit_refunded) > 0 && (
                    <Stat
                      label={t('cashier.store_credit_refunded')}
                      value={summary.store_credit_refunded}
                    />
                  )}
                </div>
              )}
              {/* Cash expenses out of the drawer this shift (C5) — hidden when none. */}
              {summary.cash_expense_count > 0 && (
                <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
                  <Stat
                    label={t('cashier.cash_expenses')}
                    value={`${summary.cash_expenses} (${summary.cash_expense_count})`}
                  />
                </div>
              )}
              {canClose && (
                <div className="flex justify-end">
                  <Button variant="danger" onClick={() => setCloseTarget({ session, summary })}>
                    {t('cashier.close')}
                  </Button>
                </div>
              )}
            </div>
          ) : (
            <form
              className="flex flex-wrap items-end gap-3"
              onSubmit={(e) => {
                e.preventDefault();
                if (!openMut.isPending) openMut.mutate();
              }}
            >
              <p className="w-full text-sm text-slate-500">{t('cashier.no_session')}</p>
              <div className="flex flex-col gap-1.5">
                <Label>
                  {t('cashier.opening_float')} ({currency})
                </Label>
                <Input
                  inputMode="decimal"
                  value={openingFloat}
                  onChange={(e) => setOpeningFloat(e.target.value)}
                  placeholder="0.00"
                  autoFocus
                />
              </div>
              <Button type="submit" disabled={openMut.isPending}>
                {openMut.isPending ? t('cashier.opening') : t('cashier.open')}
              </Button>
            </form>
          )}
        </CardContent>
      </Card>

      {/* End-of-day Z report */}
      {canView && (
        <Card>
          <CardContent className="pt-6">
            <h2 className="mb-4 text-sm font-bold text-slate-700">{t('cashier.day_title')}</h2>
            {zQuery.isLoading || !z ? (
              <div className="flex justify-center py-6">
                <Spinner />
              </div>
            ) : (
              <div className="space-y-5">
                <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
                  <Stat label={t('cashier.total_sales')} value={z.total_sales} />
                  <Stat label={t('cashier.net_sales')} value={z.net_total_sales} highlight />
                  <Stat label={t('cashier.invoices')} value={String(z.invoice_count)} />
                  <Stat
                    label={`${t('cashier.cash_sales')} — ${t('cashier.in_session')}`}
                    value={z.cash_in_session.total}
                  />
                  <Stat
                    label={`${t('cashier.card_sales')} — ${t('cashier.in_session')}`}
                    value={z.card_in_session.total}
                  />
                  <Stat
                    label={`${t('cashier.cash_sales')} — ${t('cashier.outside')}`}
                    value={z.cash_outside_sessions.total}
                  />
                  <Stat
                    label={`${t('cashier.card_sales')} — ${t('cashier.outside')}`}
                    value={z.card_outside_sessions.total}
                  />
                  {Number(z.total_refunds) > 0 && (
                    <Stat label={t('cashier.total_refunds')} value={z.total_refunds} />
                  )}
                  {Number(z.total_expenses) > 0 && (
                    <Stat label={t('cashier.total_expenses')} value={z.total_expenses} />
                  )}
                </div>

                <h3 className="text-xs font-bold text-slate-500">{t('cashier.sessions_table')}</h3>
                {z.sessions.length === 0 ? (
                  <p className="py-4 text-center text-sm text-slate-500">
                    {t('cashier.empty_day')}
                  </p>
                ) : (
                  <table className="w-full text-sm">
                    <thead>
                      <tr className="border-b border-border text-xs text-slate-500">
                        <th className="p-2 text-start">{t('cashier.cashier')}</th>
                        <th className="p-2 text-start">{t('users.status')}</th>
                        <th className="p-2 text-start">{t('cashier.float')}</th>
                        <th className="p-2 text-start">{t('cashier.expected')}</th>
                        <th className="p-2 text-start">{t('cashier.counted')}</th>
                        <th className="p-2 text-start">{t('cashier.discrepancy')}</th>
                      </tr>
                    </thead>
                    <tbody>
                      {z.sessions.map((s: CashSessionRow) => (
                        <tr key={s.id} className="border-b border-border/60">
                          <td className="p-2 font-medium text-slate-800">{s.cashier_full_name}</td>
                          <td className="p-2">
                            <Badge tone={s.status === 'open' ? 'success' : 'neutral'}>
                              {s.status === 'open'
                                ? t('cashier.status_open')
                                : t('cashier.status_closed')}
                            </Badge>
                          </td>
                          <td className="p-2 tabular-nums text-slate-600">{s.opening_float}</td>
                          <td className="p-2 tabular-nums text-slate-600">
                            {s.expected_cash ?? '—'}
                          </td>
                          <td className="p-2 tabular-nums text-slate-600">
                            {s.counted_cash ?? '—'}
                          </td>
                          <td className="p-2">
                            {s.discrepancy !== null ? (
                              <DiscrepancyBadge value={s.discrepancy} />
                            ) : (
                              '—'
                            )}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}
              </div>
            )}
          </CardContent>
        </Card>
      )}

      {closeTarget && (
        <CloseModal
          target={closeTarget}
          currency={currency}
          onClose={() => setCloseTarget(null)}
          onClosed={() => {
            setCloseTarget(null);
            invalidate();
          }}
        />
      )}
    </div>
  );
}

function Stat({
  label,
  value,
  highlight = false,
}: {
  label: string;
  value: string;
  highlight?: boolean;
}) {
  return (
    <div
      className={`rounded-[var(--radius-md)] border p-3 ${
        highlight ? 'border-primary-500 bg-primary-50/60' : 'border-border'
      }`}
    >
      <p className="text-[11px] text-slate-500">{label}</p>
      <p className="tabular-nums text-lg font-bold text-slate-900">{value}</p>
    </div>
  );
}

// ------------------------------ close modal ------------------------------

function CloseModal({
  target,
  currency,
  onClose,
  onClosed,
}: {
  target: { session: CashSessionInfo; summary: CashSessionSummary };
  currency: string;
  onClose: () => void;
  onClosed: () => void;
}) {
  const [counted, setCounted] = useState('');
  const [notes, setNotes] = useState('');

  const closeMut = useMutation({
    mutationFn: () => closeCashSession(target.session.id, String(Number(counted)), notes),
    onSuccess: (closed) => {
      toast.success(t('cashier.closed_ok'));
      const d = Number(closed.discrepancy ?? 0);
      if (d !== 0) {
        toast.info(`${t('cashier.discrepancy')}: ${d > 0 ? '+' : ''}${d.toFixed(2)} ${currency}`);
      }
      onClosed();
    },
    onError: onErr,
  });

  const expected = Number(target.summary.expected_cash);
  const countedNum = Number(counted);
  const preview = counted !== '' && Number.isFinite(countedNum) ? countedNum - expected : null;

  return (
    <Modal open onClose={onClose} title={t('cashier.close_title')}>
      <form
        className="space-y-4"
        onSubmit={(e) => {
          e.preventDefault();
          if (counted !== '' && Number.isFinite(countedNum) && countedNum >= 0) closeMut.mutate();
        }}
      >
        <p className="text-sm text-slate-600">
          {t('cashier.expected')}:{' '}
          <span className="tabular-nums font-bold">{target.summary.expected_cash}</span> {currency}
        </p>
        <div className="space-y-1.5">
          <Label>
            {t('cashier.counted')} ({currency})
          </Label>
          <Input
            inputMode="decimal"
            value={counted}
            onChange={(e) => setCounted(e.target.value)}
            autoFocus
            required
            className="h-12 text-center text-lg tabular-nums"
          />
          {preview !== null && (
            <p className="text-sm">
              {t('cashier.discrepancy')}:{' '}
              <span
                className={`tabular-nums font-bold ${
                  preview === 0 ? 'text-success' : preview < 0 ? 'text-danger' : 'text-warning'
                }`}
              >
                {preview > 0 ? '+' : ''}
                {preview.toFixed(2)}
              </span>{' '}
              {currency}
            </p>
          )}
        </div>
        <div className="space-y-1.5">
          <Label>{t('cashier.notes')}</Label>
          <Input value={notes} onChange={(e) => setNotes(e.target.value)} />
        </div>
        <p className="text-xs text-slate-400">{t('cashier.close_hint')}</p>
        <div className="flex justify-end gap-2 border-t border-border pt-4">
          <Button type="button" variant="outline" onClick={onClose}>
            {t('users.cancel')}
          </Button>
          <Button type="submit" variant="danger" disabled={closeMut.isPending}>
            {closeMut.isPending ? t('cashier.closing') : t('cashier.close')}
          </Button>
        </div>
      </form>
    </Modal>
  );
}

'use client';

/**
 * Stocktake (الجرد) — start session / keyboard-only count screen / variance
 * review / history. Count is per BATCH; blind sessions hide the system
 * quantity (and the diff) until review — the mask is server-side, the UI
 * simply renders what it gets.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useRef, useState } from 'react';

import {
  approveStocktakeSession,
  cancelStocktakeSession,
  createStocktakeSession,
  getStocktakeSession,
  listInventoryBranches,
  listStocktakeLines,
  listStocktakeSessions,
  recordStocktakeCount,
  stocktakeExportCsvUrl,
  submitStocktakeSession,
  type StocktakeLine,
  type StocktakeSession,
} from '@/lib/api';
import { useAuth } from '@/lib/auth-store';
import { formatDateTime } from '@/lib/format';
import { formatApiErrorMessage, t } from '@/lib/i18n';
import { toast } from '@/lib/toast-store';
import {
  ConfirmDialog,
  Badge,
  Button,
  Card,
  CardContent,
  Input,
  Label,
  Select,
  Spinner,
} from '@pharmaos/ui';

const REASON_CODES = ['miscount', 'breakage', 'theft', 'found', 'unknown'] as const;

const onErr = (e: unknown) => toast.error(formatApiErrorMessage(e));

function StatusBadge({ status }: { status: StocktakeSession['status'] }) {
  const tone =
    status === 'approved'
      ? 'success'
      : status === 'cancelled'
        ? 'danger'
        : status === 'review'
          ? 'warning'
          : 'neutral';
  return <Badge tone={tone}>{t(`stocktake.status_${status}`)}</Badge>;
}

function TypeLabel({ type }: { type: StocktakeSession['session_type'] }) {
  return <>{t(`stocktake.type_${type}`)}</>;
}

export default function StocktakePage() {
  const canCount = useAuth((s) => s.hasPermission('inventory.count'));
  const canApprove = useAuth((s) => s.hasPermission('inventory.stocktake_approve'));
  const [branchId, setBranchId] = useState('');
  const [activeSessionId, setActiveSessionId] = useState<string | null>(null);
  const [view, setView] = useState<'start' | 'count' | 'review' | 'history'>('start');

  const branchesQuery = useQuery({ queryKey: ['st-branches'], queryFn: listInventoryBranches });
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
    return <p className="py-10 text-center text-slate-500">{t('inventory.no_branch')}</p>;
  }

  return (
    <div className="mx-auto max-w-6xl space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-2xl font-bold text-slate-900">{t('stocktake.title')}</h1>
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

      {!canCount && (
        <p className="py-10 text-center text-slate-500">{t('stocktake.no_permission')}</p>
      )}

      {canCount && view === 'start' && (
        <StartScreen
          branchId={branchId}
          onStarted={(sessionId) => {
            setActiveSessionId(sessionId);
            setView('count');
          }}
          onHistory={() => setView('history')}
        />
      )}
      {canCount && view === 'count' && activeSessionId && (
        <CountScreen
          branchId={branchId}
          sessionId={activeSessionId}
          onExit={() => {
            setActiveSessionId(null);
            setView('start');
          }}
          onReview={() => setView('review')}
        />
      )}
      {canCount && view === 'review' && activeSessionId && (
        <ReviewScreen
          branchId={branchId}
          sessionId={activeSessionId}
          canApprove={canApprove}
          onDone={() => {
            setActiveSessionId(null);
            setView('start');
          }}
          onBackToCount={() => setView('count')}
        />
      )}
      {canCount && view === 'history' && (
        <HistoryScreen
          branchId={branchId}
          onOpen={(id, status) => {
            setActiveSessionId(id);
            setView(status === 'approved' || status === 'cancelled' ? 'review' : 'count');
          }}
          onBack={() => setView('start')}
        />
      )}
    </div>
  );
}

// ------------------------------ start ------------------------------

function StartScreen({
  branchId,
  onStarted,
  onHistory,
}: {
  branchId: string;
  onStarted: (sessionId: string) => void;
  onHistory: () => void;
}) {
  const [sessionType, setSessionType] = useState<'daily_quick' | 'weekly_cycle' | 'custom'>(
    'daily_quick',
  );
  const [blind, setBlind] = useState(true);
  const [zones, setZones] = useState('');
  const [notes, setNotes] = useState('');

  const createMut = useMutation({
    mutationFn: () =>
      createStocktakeSession({
        branchId,
        sessionType,
        blind,
        zones: zones
          .split(/[,\s]+/)
          .map((z) => z.trim().toUpperCase())
          .filter(Boolean),
        notes: notes || undefined,
      }),
    onSuccess: (s) => {
      toast.success(t('stocktake.created'));
      onStarted(s.id);
    },
    onError: onErr,
  });

  return (
    <div className="space-y-4">
      <div className="flex justify-end">
        <Button variant="outline" onClick={onHistory}>
          {t('stocktake.history')}
        </Button>
      </div>
      <Card>
        <CardContent className="space-y-4 p-6">
          <div className="grid gap-4 sm:grid-cols-2">
            <div className="space-y-1">
              <Label>{t('stocktake.session_type')}</Label>
              <Select
                value={sessionType}
                onChange={(e) => setSessionType(e.target.value as typeof sessionType)}
              >
                <option value="daily_quick">{t('stocktake.type_daily_quick')}</option>
                <option value="weekly_cycle">{t('stocktake.type_weekly_cycle')}</option>
                <option value="custom">{t('stocktake.type_custom')}</option>
              </Select>
            </div>
            <div className="space-y-1">
              <Label>{t('stocktake.blind_label')}</Label>
              <Select value={blind ? '1' : '0'} onChange={(e) => setBlind(e.target.value === '1')}>
                <option value="1">{t('stocktake.blind_on')}</option>
                <option value="0">{t('stocktake.blind_off')}</option>
              </Select>
              <p className="text-xs text-slate-500">{t('stocktake.blind_hint')}</p>
            </div>
          </div>
          {sessionType === 'weekly_cycle' && (
            <div className="space-y-1">
              <Label>{t('stocktake.zones')}</Label>
              <Input value={zones} onChange={(e) => setZones(e.target.value)} placeholder="A, B" />
              <p className="text-xs text-slate-500">{t('stocktake.zones_hint')}</p>
            </div>
          )}
          <div className="space-y-1">
            <Label>{t('stocktake.notes')}</Label>
            <Input value={notes} onChange={(e) => setNotes(e.target.value)} />
          </div>
          <Button onClick={() => createMut.mutate()} disabled={createMut.isPending}>
            {createMut.isPending ? <Spinner /> : t('stocktake.start')}
          </Button>
        </CardContent>
      </Card>
    </div>
  );
}

// ------------------------------ count ------------------------------

function CountScreen({
  branchId,
  sessionId,
  onExit,
  onReview,
}: {
  branchId: string;
  sessionId: string;
  onExit: () => void;
  onReview: () => void;
}) {
  const queryClient = useQueryClient();
  const [page, setPage] = useState(0);
  const PAGE = 25;
  const sessionQuery = useQuery({
    queryKey: ['st-session', branchId, sessionId],
    queryFn: () => getStocktakeSession(branchId, sessionId),
  });
  const linesQuery = useQuery({
    queryKey: ['st-lines', branchId, sessionId, page],
    queryFn: () => listStocktakeLines(branchId, sessionId, { skip: page * PAGE, limit: PAGE }),
  });
  const lines = linesQuery.data?.items ?? [];
  const total = linesQuery.data?.total ?? 0;

  // mouse-free: scan (batch number/barcode) → the matching line gets focus
  const scanRef = useRef<HTMLInputElement>(null);
  const qtyRef = useRef<HTMLInputElement>(null);
  const [scan, setScan] = useState('');
  const [targetLineId, setTargetLineId] = useState<string | null>(null);
  const [qty, setQty] = useState('');
  const [reason, setReason] = useState<string>('');

  useEffect(() => {
    scanRef.current?.focus();
  }, []);

  const countMut = useMutation({
    mutationFn: (input: { lineId: string; qty: string }) =>
      recordStocktakeCount(input.lineId, input.qty, reason ? { reasonCode: reason } : {}),
    onSuccess: () => {
      toast.success(t('stocktake.counted'));
      setQty('');
      setTargetLineId(null);
      setScan('');
      queryClient.invalidateQueries({ queryKey: ['st-lines', branchId, sessionId] });
      queryClient.invalidateQueries({ queryKey: ['st-session', branchId, sessionId] });
      scanRef.current?.focus();
    },
    onError: onErr,
  });

  const submitMut = useMutation({
    mutationFn: () => submitStocktakeSession(branchId, sessionId),
    onSuccess: () => {
      toast.success(t('stocktake.submitted'));
      queryClient.invalidateQueries({ queryKey: ['st-session', branchId, sessionId] });
      onReview();
    },
    onError: onErr,
  });

  const session = sessionQuery.data;

  function resolveScan() {
    const code = scan.trim();
    if (!code) return;
    const hit =
      lines.find((ln) => (ln.batch_number ?? '').toLowerCase() === code.toLowerCase()) ??
      lines.find((ln) => (ln.shelf_location ?? '').toLowerCase() === code.toLowerCase());
    if (!hit) {
      toast.error(t('stocktake.scan_not_found'));
      setScan('');
      return;
    }
    setTargetLineId(hit.id);
    setScan('');
    setTimeout(() => qtyRef.current?.focus(), 0);
  }

  function commitQty() {
    if (!targetLineId || !qty.trim()) return;
    countMut.mutate({ lineId: targetLineId, qty: qty.trim() });
  }

  const target = lines.find((ln) => ln.id === targetLineId) ?? null;

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          {session && <StatusBadge status={session.status} />}
          {session && (
            <span className="text-sm text-slate-600">
              <TypeLabel type={session.session_type} /> · {session.lines_counted}/
              {session.lines_expected}
            </span>
          )}
          {session?.blind && <Badge tone="warning">{t('stocktake.blind_badge')}</Badge>}
        </div>
        <div className="flex gap-2">
          <Button variant="ghost" onClick={onExit}>
            {t('stocktake.exit')}
          </Button>
          <Button
            variant="outline"
            onClick={() => submitMut.mutate()}
            disabled={submitMut.isPending}
          >
            {t('stocktake.submit')}
          </Button>
        </div>
      </div>

      {/* scan → qty → Enter, like the POS */}
      <Card>
        <CardContent className="flex flex-wrap items-end gap-3 p-4">
          <div className="space-y-1">
            <Label>{t('stocktake.scan')}</Label>
            <Input
              ref={scanRef}
              className="w-56 tabular-nums"
              value={scan}
              onChange={(e) => setScan(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') resolveScan();
              }}
              placeholder={t('stocktake.scan_placeholder')}
            />
          </div>
          <div className="space-y-1">
            <Label>{target ? countTargetLabel(target) : t('stocktake.qty')}</Label>
            <Input
              ref={qtyRef}
              className="w-32 tabular-nums"
              value={qty}
              disabled={!target}
              onChange={(e) => setQty(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') commitQty();
              }}
              inputMode="decimal"
            />
          </div>
          <div className="space-y-1">
            <Label>{t('stocktake.reason')}</Label>
            <Select value={reason} onChange={(e) => setReason(e.target.value)} className="w-40">
              <option value="">{t('stocktake.reason_none')}</option>
              {REASON_CODES.map((c) => (
                <option key={c} value={c}>
                  {t(`stocktake.reason_${c}`)}
                </option>
              ))}
            </Select>
          </div>
          <Button
            onClick={() => commitQty()}
            disabled={!target || !qty.trim() || !Number.isFinite(Number(qty)) || countMut.isPending}
          >
            {t('stocktake.record')}
          </Button>
        </CardContent>
      </Card>

      <Card>
        <CardContent className="p-0">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-border text-start text-xs text-slate-500">
                <th className="p-2 text-start">{t('stocktake.col_item')}</th>
                <th className="p-2 text-start">{t('stocktake.col_batch')}</th>
                <th className="p-2 text-start">{t('stocktake.col_shelf')}</th>
                <th className="p-2 text-start">{t('stocktake.col_counted')}</th>
              </tr>
            </thead>
            <tbody>
              {lines.map((ln) => (
                <tr
                  key={ln.id}
                  className={`border-b border-border ${ln.id === targetLineId ? 'bg-amber-50' : ''}`}
                >
                  <td className="p-2">
                    {ln.trade_name_ar || ln.trade_name}
                    {ln.controlled_substance && (
                      <Badge tone="danger" className="ms-1">
                        {t('stocktake.controlled')}
                      </Badge>
                    )}
                  </td>
                  <td className="p-2 tabular-nums">{ln.batch_number}</td>
                  <td className="p-2 tabular-nums">{ln.shelf_location ?? '—'}</td>
                  <td className="p-2 tabular-nums">{ln.counted_qty ?? '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </CardContent>
      </Card>

      <div className="flex items-center justify-between">
        <Button variant="ghost" disabled={page === 0} onClick={() => setPage(page - 1)}>
          {t('common.prev')}
        </Button>
        <span className="text-xs text-slate-500">
          {page * PAGE + 1}–{Math.min((page + 1) * PAGE, total)} / {total}
        </span>
        <Button
          variant="ghost"
          disabled={(page + 1) * PAGE >= total}
          onClick={() => setPage(page + 1)}
        >
          {t('common.next')}
        </Button>
      </div>
    </div>
  );
}

function countTargetLabel(ln: StocktakeLine): string {
  const name = ln.trade_name_ar || ln.trade_name || '';
  return `${t('stocktake.qty_for')} ${name}${ln.batch_number ? ` (${ln.batch_number})` : ''}`;
}

// ------------------------------ review ------------------------------

function ReviewScreen({
  branchId,
  sessionId,
  canApprove,
  onDone,
  onBackToCount,
}: {
  branchId: string;
  sessionId: string;
  canApprove: boolean;
  onDone: () => void;
  onBackToCount: () => void;
}) {
  const queryClient = useQueryClient();
  const [page, setPage] = useState(0);
  const PAGE = 50;
  const [reason, setReason] = useState('');
  const sessionQuery = useQuery({
    queryKey: ['st-session', branchId, sessionId],
    queryFn: () => getStocktakeSession(branchId, sessionId),
  });
  const linesQuery = useQuery({
    queryKey: ['st-lines', branchId, sessionId, 'review', page],
    queryFn: () => listStocktakeLines(branchId, sessionId, { skip: page * PAGE, limit: PAGE }),
  });
  const lines = linesQuery.data?.items ?? [];
  const total = linesQuery.data?.total ?? 0;
  const session = sessionQuery.data;
  const final = session?.status === 'approved' || session?.status === 'cancelled';

  const approveMut = useMutation({
    mutationFn: () => approveStocktakeSession(branchId, sessionId, reason),
    onSuccess: () => {
      toast.success(t('stocktake.approved'));
      queryClient.invalidateQueries({ queryKey: ['st-session', branchId, sessionId] });
      onDone();
    },
    onError: onErr,
  });
  const [confirmCancel, setConfirmCancel] = useState(false);
  const cancelMut = useMutation({
    mutationFn: () => cancelStocktakeSession(branchId, sessionId, t('stocktake.cancel_default')),
    onSuccess: () => {
      toast.info(t('stocktake.cancelled'));
      queryClient.invalidateQueries({ queryKey: ['st-session', branchId, sessionId] });
      onDone();
    },
    onError: onErr,
  });

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          {session && <StatusBadge status={session.status} />}
          {session && (
            <span className="text-sm text-slate-600">
              <TypeLabel type={session.session_type} /> · {session.lines_counted}/
              {session.lines_expected}
            </span>
          )}
        </div>
        <div className="flex gap-2">
          <Button variant="ghost" onClick={onBackToCount}>
            {t('stocktake.back_to_count')}
          </Button>
          <a href={stocktakeExportCsvUrl(branchId, sessionId)} download>
            <Button variant="outline">{t('stocktake.export_csv')}</Button>
          </a>
        </div>
      </div>

      <Card>
        <CardContent className="p-0">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-border text-xs text-slate-500">
                <th className="p-2 text-start">{t('stocktake.col_item')}</th>
                <th className="p-2 text-start">{t('stocktake.col_batch')}</th>
                <th className="p-2 text-start">{t('stocktake.col_system')}</th>
                <th className="p-2 text-start">{t('stocktake.col_current')}</th>
                <th className="p-2 text-start">{t('stocktake.col_counted')}</th>
                <th className="p-2 text-start">{t('stocktake.col_diff')}</th>
                <th className="p-2 text-start">{t('stocktake.col_flag')}</th>
              </tr>
            </thead>
            <tbody>
              {lines.map((ln) => {
                const stale =
                  ln.flag === 'needs_recount' ||
                  (ln.system_qty_current !== null &&
                    ln.system_qty_at_count !== null &&
                    ln.system_qty_current !== ln.system_qty_at_count);
                return (
                  <tr key={ln.id} className={`border-b border-border ${stale ? 'bg-red-50' : ''}`}>
                    <td className="p-2">
                      {ln.trade_name_ar || ln.trade_name}
                      {ln.controlled_substance && (
                        <Badge tone="danger" className="ms-1">
                          {t('stocktake.controlled')}
                        </Badge>
                      )}
                    </td>
                    <td className="p-2 tabular-nums">{ln.batch_number}</td>
                    <td className="p-2 tabular-nums">{ln.system_qty_at_count ?? '—'}</td>
                    <td className="p-2 tabular-nums">{ln.system_qty_current ?? '—'}</td>
                    <td className="p-2 tabular-nums">{ln.counted_qty ?? '—'}</td>
                    <td className="p-2 tabular-nums">{ln.diff ?? '—'}</td>
                    <td className="p-2">
                      {ln.flag === 'needs_recount' && (
                        <Badge tone="danger">{t('stocktake.needs_recount')}</Badge>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </CardContent>
      </Card>

      <div className="flex items-center justify-between">
        <Button variant="ghost" disabled={page === 0} onClick={() => setPage(page - 1)}>
          {t('common.prev')}
        </Button>
        <span className="text-xs text-slate-500">
          {page * PAGE + 1}–{Math.min((page + 1) * PAGE, total)} / {total}
        </span>
        <Button
          variant="ghost"
          disabled={(page + 1) * PAGE >= total}
          onClick={() => setPage(page + 1)}
        >
          {t('common.next')}
        </Button>
      </div>

      {!final && canApprove && (
        <Card>
          <CardContent className="flex flex-wrap items-end gap-3 p-4">
            <div className="space-y-1">
              <Label>{t('stocktake.approve_reason')}</Label>
              <Input className="w-72" value={reason} onChange={(e) => setReason(e.target.value)} />
            </div>
            <Button
              onClick={() => approveMut.mutate()}
              disabled={approveMut.isPending || reason.trim().length < 2}
            >
              {t('stocktake.approve')}
            </Button>
            <Button
              variant="danger"
              onClick={() => setConfirmCancel(true)}
              disabled={cancelMut.isPending}
            >
              {t('stocktake.cancel')}
            </Button>
            <ConfirmDialog
              open={confirmCancel}
              onClose={() => setConfirmCancel(false)}
              onConfirm={() => {
                cancelMut.mutate();
                setConfirmCancel(false);
              }}
              pending={cancelMut.isPending}
              title={t('stocktake.cancel')}
              message={t('confirm.cancel_stocktake.body')}
              confirmLabel={t('stocktake.cancel')}
              cancelLabel={t('users.cancel')}
            />
          </CardContent>
        </Card>
      )}
      {!final && !canApprove && (
        <p className="text-sm text-slate-500">{t('stocktake.awaiting_manager')}</p>
      )}
    </div>
  );
}

// ------------------------------ history ------------------------------

function HistoryScreen({
  branchId,
  onOpen,
  onBack,
}: {
  branchId: string;
  onOpen: (sessionId: string, status: StocktakeSession['status']) => void;
  onBack: () => void;
}) {
  const sessionsQuery = useQuery({
    queryKey: ['st-sessions', branchId],
    queryFn: () => listStocktakeSessions(branchId),
  });
  const sessions = sessionsQuery.data?.items ?? [];

  return (
    <div className="space-y-4">
      <div className="flex justify-end">
        <Button variant="outline" onClick={onBack}>
          {t('stocktake.new_session')}
        </Button>
      </div>
      <Card>
        <CardContent className="p-0">
          {sessionsQuery.isError ? (
            <p className="py-6 text-center text-sm text-danger">
              {formatApiErrorMessage(sessionsQuery.error)}
            </p>
          ) : sessionsQuery.isLoading ? (
            <div className="flex justify-center py-10">
              <Spinner />
            </div>
          ) : sessions.length === 0 ? (
            <p className="py-10 text-center text-sm text-slate-500">
              {t('stocktake.history_empty')}
            </p>
          ) : (
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-border text-xs text-slate-500">
                  <th className="p-2 text-start">{t('stocktake.col_type')}</th>
                  <th className="p-2 text-start">{t('stocktake.col_status')}</th>
                  <th className="p-2 text-start">{t('stocktake.col_progress')}</th>
                  <th className="p-2 text-start">{t('stocktake.col_started')}</th>
                </tr>
              </thead>
              <tbody>
                {sessions.map((s) => (
                  <tr
                    key={s.id}
                    className="cursor-pointer border-b border-border hover:bg-slate-50"
                    onClick={() => onOpen(s.id, s.status)}
                  >
                    <td className="p-2">
                      <TypeLabel type={s.session_type} />
                    </td>
                    <td className="p-2">
                      <StatusBadge status={s.status} />
                    </td>
                    <td className="p-2 tabular-nums">
                      {s.lines_counted}/{s.lines_expected}
                    </td>
                    <td className="p-2 text-xs text-slate-500">
                      {s.started_at ? formatDateTime(s.started_at) : '—'}
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

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
  adjustBatch,
  ApiRequestError,
  type Batch,
  type BatchStatus,
  checkDrift,
  createSupplier,
  getBatchReport,
  getExpiryAlerts,
  type InventoryRow,
  listBatches,
  listInventory,
  listInventoryBranches,
  listSuppliers,
  type MedOption,
  parseGs1,
  rebuildCache,
  receiveStock,
  runExpirySweep,
  searchMedications,
  setBatchStatus,
} from '@/lib/api';
import { useAuth } from '@/lib/auth-store';
import { t } from '@/lib/i18n';
import { toast } from '@/lib/toast-store';

const errCode = (e: unknown) => (e instanceof ApiRequestError ? e.code : 'E-SYS-001');
const onErr = (e: unknown) => toast.error(t(`errors.${errCode(e)}`));

const STATUS_TONE: Record<string, 'success' | 'warning' | 'danger' | 'neutral'> = {
  active: 'success',
  quarantined: 'warning',
  recalled: 'danger',
  expired: 'danger',
  depleted: 'neutral',
};
const statusLabel = (s: string) => t(`inventory.status_${s}`);

export default function InventoryPage() {
  const canReceive = useAuth((s) => s.hasPermission('inventory.purchase'));
  const canAdjust = useAuth((s) => s.hasPermission('inventory.adjust'));
  const [tab, setTab] = useState<'stock' | 'expiry' | 'report'>('stock');
  const [branchId, setBranchId] = useState('');

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
    return <p className="py-10 text-center text-slate-500">{t('inventory.no_branch')}</p>;
  }

  return (
    <div className="mx-auto max-w-6xl space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-2xl font-bold text-slate-900">{t('inventory.title')}</h1>
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
        <TabButton active={tab === 'stock'} onClick={() => setTab('stock')}>
          {t('inventory.tab_stock')}
        </TabButton>
        <TabButton active={tab === 'expiry'} onClick={() => setTab('expiry')}>
          {t('inventory.tab_expiry')}
        </TabButton>
        <TabButton active={tab === 'report'} onClick={() => setTab('report')}>
          {t('inventory.tab_report')}
        </TabButton>
      </div>

      {tab === 'stock' && (
        <StockTab branchId={branchId} canReceive={canReceive} canAdjust={canAdjust} />
      )}
      {tab === 'expiry' && <ExpiryAlertsTab branchId={branchId} canAdjust={canAdjust} />}
      {tab === 'report' && <BatchReportTab branchId={branchId} />}
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

function StockTab({
  branchId,
  canReceive,
  canAdjust,
}: {
  branchId: string;
  canReceive: boolean;
  canAdjust: boolean;
}) {
  const [search, setSearch] = useState('');
  const [query, setQuery] = useState('');
  const [lowStock, setLowStock] = useState(false);
  const [showReceive, setShowReceive] = useState(false);
  const [batchesFor, setBatchesFor] = useState<{ id: string; name: string } | null>(null);

  const invQuery = useQuery({
    queryKey: ['inventory', branchId, query, lowStock],
    queryFn: () => listInventory(branchId, { search: query, lowStock }),
    enabled: !!branchId,
  });
  const driftQuery = useQuery({
    queryKey: ['drift', branchId],
    queryFn: () => checkDrift(branchId),
    enabled: !!branchId,
  });

  const rows = invQuery.data ?? [];

  return (
    <div className="space-y-6">
      {canReceive && (
        <div className="flex justify-end">
          <Button onClick={() => setShowReceive(true)}>{t('inventory.receive')}</Button>
        </div>
      )}

      <IntegrityBar branchId={branchId} canAdjust={canAdjust} report={driftQuery.data} />

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
          placeholder={t('inventory.search')}
        />
        <label className="flex cursor-pointer items-center gap-2 text-sm text-slate-700">
          <input
            type="checkbox"
            checked={lowStock}
            onChange={(e) => setLowStock(e.target.checked)}
          />
          {t('inventory.low_stock_only')}
        </label>
      </form>

      <Card>
        <CardContent className="pt-6">
          {invQuery.isLoading ? (
            <div className="flex justify-center py-8">
              <Spinner />
            </div>
          ) : rows.length === 0 ? (
            <p className="py-6 text-center text-sm text-slate-500">{t('inventory.empty')}</p>
          ) : (
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-border text-xs text-slate-500">
                  <th className="p-2 text-start">{t('inventory.medication')}</th>
                  <th className="p-2 text-start">{t('inventory.on_hand')}</th>
                  <th className="p-2 text-start">{t('inventory.reorder_point')}</th>
                  <th className="p-2 text-start">{t('inventory.shelf')}</th>
                  <th className="p-2 text-start"></th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r: InventoryRow) => (
                  <tr key={r.medication_id} className="border-b border-border/60">
                    <td className="p-2 font-medium text-slate-800">
                      {r.trade_name_ar ?? r.trade_name}
                      {r.low_stock && (
                        <Badge tone="warning" className="ms-2">
                          {t('inventory.low')}
                        </Badge>
                      )}
                    </td>
                    <td className="p-2 tabular-nums text-slate-800">{fmt(r.cached_quantity)}</td>
                    <td className="p-2 tabular-nums text-slate-500">
                      {r.reorder_point ? fmt(r.reorder_point) : '—'}
                    </td>
                    <td className="p-2 text-slate-500">{r.shelf_location ?? '—'}</td>
                    <td className="p-2 text-end">
                      <Button
                        size="sm"
                        variant="ghost"
                        onClick={() =>
                          setBatchesFor({
                            id: r.medication_id,
                            name: r.trade_name_ar ?? r.trade_name,
                          })
                        }
                      >
                        {t('inventory.view_batches')}
                      </Button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </CardContent>
      </Card>

      {showReceive && <ReceiveModal branchId={branchId} onClose={() => setShowReceive(false)} />}
      {batchesFor && (
        <BatchesModal
          branchId={branchId}
          medicationId={batchesFor.id}
          medName={batchesFor.name}
          canAdjust={canAdjust}
          onClose={() => setBatchesFor(null)}
        />
      )}
    </div>
  );
}

// tidy up trailing zeros on the wire values (they arrive as e.g. "120.000")
function fmt(n: string): string {
  const num = Number(n);
  return Number.isFinite(num) ? String(num) : n;
}

// ------------------------------ integrity bar ------------------------------

function IntegrityBar({
  branchId,
  canAdjust,
  report,
}: {
  branchId: string;
  canAdjust: boolean;
  report?: { ok: boolean };
}) {
  const qc = useQueryClient();
  const rebuild = useMutation({
    mutationFn: () => rebuildCache(branchId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['drift', branchId] });
      qc.invalidateQueries({ queryKey: ['inventory', branchId] });
      toast.success(t('inventory.rebuilt_ok'));
    },
    onError: onErr,
  });
  if (!report) return null;
  return (
    <div className="flex items-center gap-3 text-sm">
      <span className="text-slate-500">{t('inventory.integrity')}:</span>
      {report.ok ? (
        <Badge tone="success">{t('inventory.drift_ok')}</Badge>
      ) : (
        <>
          <Badge tone="danger">{t('inventory.drift_found')}</Badge>
          {canAdjust && (
            <Button size="sm" variant="outline" onClick={() => rebuild.mutate()}>
              {t('inventory.rebuild')}
            </Button>
          )}
        </>
      )}
    </div>
  );
}

// ------------------------------ batches modal ------------------------------

function BatchesModal({
  branchId,
  medicationId,
  medName,
  canAdjust,
  onClose,
}: {
  branchId: string;
  medicationId: string;
  medName: string;
  canAdjust: boolean;
  onClose: () => void;
}) {
  const batchesQuery = useQuery({
    queryKey: ['batches', branchId, medicationId],
    queryFn: () => listBatches(branchId, { medicationId }),
  });
  const [action, setAction] = useState<{
    batch: Batch;
    kind: 'adjust' | 'quarantine' | 'release';
  } | null>(null);

  const batches = batchesQuery.data ?? [];
  return (
    <Modal
      open
      onClose={onClose}
      title={`${t('inventory.batches')} — ${medName}`}
      className="max-h-[85vh] max-w-3xl overflow-y-auto"
    >
      {batchesQuery.isLoading ? (
        <div className="flex justify-center py-8">
          <Spinner />
        </div>
      ) : batches.length === 0 ? (
        <p className="py-6 text-center text-sm text-slate-500">—</p>
      ) : (
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-border text-xs text-slate-500">
              <th className="p-2 text-start">{t('inventory.batch_number')}</th>
              <th className="p-2 text-start">{t('inventory.expiry')}</th>
              <th className="p-2 text-start">{t('inventory.quantity')}</th>
              <th className="p-2 text-start">{t('inventory.status')}</th>
              {canAdjust && <th className="p-2 text-start"></th>}
            </tr>
          </thead>
          <tbody>
            {batches.map((b) => (
              <tr key={b.id} className="border-b border-border/60">
                <td className="p-2 font-mono text-slate-800">{b.batch_number}</td>
                <td className="p-2 tabular-nums text-slate-600">{b.expiry_date}</td>
                <td className="p-2 tabular-nums text-slate-800">{fmt(b.quantity)}</td>
                <td className="p-2">
                  <Badge tone={STATUS_TONE[b.status] ?? 'neutral'}>{statusLabel(b.status)}</Badge>
                </td>
                {canAdjust && (
                  <td className="flex flex-wrap gap-2 p-2">
                    <Button
                      size="sm"
                      variant="outline"
                      onClick={() => setAction({ batch: b, kind: 'adjust' })}
                    >
                      {t('inventory.adjust')}
                    </Button>
                    {b.status === 'active' ? (
                      <Button
                        size="sm"
                        variant="ghost"
                        onClick={() => setAction({ batch: b, kind: 'quarantine' })}
                      >
                        {t('inventory.quarantine')}
                      </Button>
                    ) : b.status === 'quarantined' ? (
                      <Button
                        size="sm"
                        variant="ghost"
                        onClick={() => setAction({ batch: b, kind: 'release' })}
                      >
                        {t('inventory.release')}
                      </Button>
                    ) : null}
                  </td>
                )}
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {action && (
        <BatchActionModal
          branchId={branchId}
          batch={action.batch}
          kind={action.kind}
          onClose={() => setAction(null)}
        />
      )}
    </Modal>
  );
}

function BatchActionModal({
  branchId,
  batch,
  kind,
  onClose,
}: {
  branchId: string;
  batch: Batch;
  kind: 'adjust' | 'quarantine' | 'release';
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const [delta, setDelta] = useState('');
  const [reason, setReason] = useState('');

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ['batches', branchId] });
    qc.invalidateQueries({ queryKey: ['inventory', branchId] });
    qc.invalidateQueries({ queryKey: ['drift', branchId] });
  };

  const mut = useMutation({
    mutationFn: () => {
      if (kind === 'adjust') return adjustBatch(batch.id, delta, reason);
      if (kind === 'quarantine') return setBatchStatus(batch.id, 'quarantined', reason);
      return setBatchStatus(batch.id, 'active', reason);
    },
    onSuccess: () => {
      invalidate();
      toast.success(t(kind === 'adjust' ? 'inventory.adjusted_ok' : 'inventory.status_changed'));
      onClose();
    },
    onError: onErr,
  });

  const title =
    kind === 'adjust'
      ? t('inventory.adjust_title')
      : kind === 'quarantine'
        ? t('inventory.quarantine')
        : t('inventory.release');

  return (
    <Modal open onClose={onClose} title={`${title} — ${batch.batch_number}`}>
      <form
        className="space-y-4"
        onSubmit={(e) => {
          e.preventDefault();
          if (kind === 'adjust' && (!delta || !reason.trim())) return;
          mut.mutate();
        }}
      >
        {kind === 'adjust' && (
          <>
            <div className="space-y-1.5">
              <Label>{t('inventory.adjust_delta')}</Label>
              <Input
                inputMode="decimal"
                value={delta}
                onChange={(e) => setDelta(e.target.value)}
                placeholder="-5"
                autoFocus
                required
              />
            </div>
            <div className="space-y-1.5">
              <Label>{t('inventory.reason')}</Label>
              <Input value={reason} onChange={(e) => setReason(e.target.value)} required />
              <p className="text-xs text-slate-500">{t('inventory.reason_required')}</p>
            </div>
          </>
        )}
        {kind !== 'adjust' && (
          <div className="space-y-1.5">
            <Label>{t('inventory.reason')}</Label>
            <Input value={reason} onChange={(e) => setReason(e.target.value)} autoFocus />
          </div>
        )}
        <div className="flex justify-end gap-2">
          <Button type="button" variant="outline" onClick={onClose}>
            {t('users.cancel')}
          </Button>
          <Button
            type="submit"
            variant={kind === 'quarantine' ? 'danger' : 'primary'}
            disabled={mut.isPending}
          >
            {title}
          </Button>
        </div>
      </form>
    </Modal>
  );
}

// ------------------------------ receive modal ------------------------------

function ReceiveModal({ branchId, onClose }: { branchId: string; onClose: () => void }) {
  const qc = useQueryClient();
  const [scan, setScan] = useState('');
  const [med, setMed] = useState<MedOption | null>(null);
  const [term, setTerm] = useState('');
  const [batchNumber, setBatchNumber] = useState('');
  const [expiry, setExpiry] = useState('');
  const [quantity, setQuantity] = useState('');
  const [price, setPrice] = useState('');
  const [supplierId, setSupplierId] = useState('');
  // P2-M3 (C2): captured 2D pack serials + their GTIN (EDA track & trace).
  const [serials, setSerials] = useState<string[]>([]);
  const [gtin, setGtin] = useState<string | null>(null);
  const [newSupplier, setNewSupplier] = useState('');
  const [addingSupplier, setAddingSupplier] = useState(false);

  const suppliersQuery = useQuery({ queryKey: ['suppliers'], queryFn: listSuppliers });
  const searchQuery = useQuery({
    queryKey: ['med-search', term],
    queryFn: () => searchMedications(term),
    enabled: term.trim().length >= 2,
  });

  const parseMut = useMutation({
    mutationFn: () => parseGs1(scan.trim()),
    onSuccess: (data) => {
      if (data.expiry_date) setExpiry(data.expiry_date);
      if (data.batch_number) setBatchNumber(data.batch_number);
      if (data.gtin) setGtin(data.gtin);
      // Each scanned 2D pack adds its serial (deduped) for track & trace.
      if (data.serial_number) {
        const s = data.serial_number;
        setSerials((prev) => (prev.includes(s) ? prev : [...prev, s]));
      }
      if (data.medication) {
        setMed({
          id: data.medication.id,
          trade_name: data.medication.trade_name,
          trade_name_ar: data.medication.trade_name_ar,
        });
      } else {
        toast.info(t('inventory.scan_unknown'));
      }
      setScan(''); // ready for the next pack scan
    },
    onError: onErr,
  });

  const addSupplierMut = useMutation({
    mutationFn: () => createSupplier(newSupplier.trim()),
    onSuccess: (s) => {
      setAddingSupplier(false);
      setNewSupplier('');
      setSupplierId(s.id);
      qc.invalidateQueries({ queryKey: ['suppliers'] });
      toast.success(t('inventory.supplier_added'));
    },
    onError: onErr,
  });

  const receiveMut = useMutation({
    mutationFn: () =>
      receiveStock({
        branch_id: branchId,
        medication_id: med!.id,
        batch_number: batchNumber.trim(),
        expiry_date: expiry,
        quantity,
        purchase_price: price,
        supplier_id: supplierId || null,
        ...(gtin ? { gtin } : {}),
        ...(serials.length > 0 ? { serials } : {}),
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['inventory', branchId] });
      qc.invalidateQueries({ queryKey: ['batches', branchId] });
      qc.invalidateQueries({ queryKey: ['drift', branchId] });
      toast.success(t('inventory.received_ok'));
      onClose();
    },
    onError: onErr,
  });

  const ready = med && batchNumber.trim() && expiry && Number(quantity) > 0 && price !== '';
  const suppliers = suppliersQuery.data ?? [];
  const results = searchQuery.data ?? [];

  return (
    <Modal
      open
      onClose={onClose}
      title={t('inventory.receive_title')}
      className="max-h-[85vh] max-w-2xl overflow-y-auto"
    >
      <form
        className="space-y-5"
        onSubmit={(e) => {
          e.preventDefault();
          if (ready) receiveMut.mutate();
        }}
      >
        {/* GS1 scan → prefill */}
        <div className="space-y-1.5 rounded-[var(--radius-md)] bg-primary-50/60 p-3">
          <Label className="text-xs">{t('inventory.scan_gs1')}</Label>
          <Input
            className="font-mono"
            value={scan}
            placeholder={t('inventory.scan_placeholder')}
            onChange={(e) => setScan(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') {
                e.preventDefault();
                if (scan.trim()) parseMut.mutate();
              }
            }}
          />
          <p className="text-xs text-slate-500">{t('inventory.scan_hint')}</p>
          {serials.length > 0 && (
            <div className="space-y-1 pt-1">
              <p className="text-xs font-medium text-slate-600">
                {t('inventory.captured_serials')}: {serials.length}
              </p>
              <div className="flex flex-wrap gap-1.5">
                {serials.map((s) => (
                  <span
                    key={s}
                    className="inline-flex items-center gap-1 rounded-full bg-white px-2 py-0.5 font-mono text-xs text-slate-700 ring-1 ring-border"
                  >
                    {s}
                    <button
                      type="button"
                      className="text-slate-400 hover:text-danger"
                      aria-label={t('inventory.remove_serial')}
                      onClick={() => setSerials((prev) => prev.filter((x) => x !== s))}
                    >
                      ✕
                    </button>
                  </span>
                ))}
              </div>
            </div>
          )}
        </div>

        {/* Medication picker */}
        <div className="space-y-1.5">
          <Label>{t('inventory.medication')}</Label>
          {med ? (
            <div className="flex items-center justify-between rounded-[var(--radius-md)] border border-border px-3 py-2 text-sm">
              <span className="font-medium">{med.trade_name_ar ?? med.trade_name}</span>
              <button
                type="button"
                className="text-xs text-primary-600 hover:underline"
                onClick={() => setMed(null)}
              >
                {t('catalog.edit')}
              </button>
            </div>
          ) : (
            <>
              <Input
                value={term}
                onChange={(e) => setTerm(e.target.value)}
                placeholder={t('inventory.pick_medication')}
              />
              {results.length > 0 && (
                <div className="max-h-40 overflow-y-auto rounded-[var(--radius-md)] border border-border">
                  {results.map((m) => (
                    <button
                      key={m.id}
                      type="button"
                      className="block w-full px-3 py-2 text-start text-sm hover:bg-primary-50"
                      onClick={() => {
                        setMed(m);
                        setTerm('');
                      }}
                    >
                      {m.trade_name_ar ?? m.trade_name}
                    </button>
                  ))}
                </div>
              )}
            </>
          )}
        </div>

        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <div className="space-y-1.5">
            <Label>{t('inventory.batch_number')}</Label>
            <Input value={batchNumber} onChange={(e) => setBatchNumber(e.target.value)} required />
          </div>
          <div className="space-y-1.5">
            <Label>{t('inventory.expiry')}</Label>
            <Input
              type="date"
              value={expiry}
              onChange={(e) => setExpiry(e.target.value)}
              required
            />
          </div>
          <div className="space-y-1.5">
            <Label>{t('inventory.quantity')}</Label>
            <Input
              inputMode="decimal"
              value={quantity}
              onChange={(e) => setQuantity(e.target.value)}
              required
            />
          </div>
          <div className="space-y-1.5">
            <Label>{t('inventory.purchase_price')}</Label>
            <Input
              inputMode="decimal"
              value={price}
              onChange={(e) => setPrice(e.target.value)}
              required
            />
          </div>
        </div>

        {/* Supplier (optional) */}
        <div className="space-y-1.5">
          <Label>{t('inventory.supplier')}</Label>
          {addingSupplier ? (
            <div className="flex items-end gap-2">
              <Input
                value={newSupplier}
                onChange={(e) => setNewSupplier(e.target.value)}
                placeholder={t('inventory.supplier_name')}
                autoFocus
              />
              <Button
                type="button"
                size="sm"
                onClick={() => newSupplier.trim() && addSupplierMut.mutate()}
                disabled={addSupplierMut.isPending}
              >
                {t('users.create')}
              </Button>
              <Button
                type="button"
                size="sm"
                variant="outline"
                onClick={() => setAddingSupplier(false)}
              >
                {t('users.cancel')}
              </Button>
            </div>
          ) : (
            <div className="flex items-center gap-2">
              <Select
                className="flex-1"
                value={supplierId}
                onChange={(e) => setSupplierId(e.target.value)}
              >
                <option value="">{t('inventory.no_supplier')}</option>
                {suppliers.map((s) => (
                  <option key={s.id} value={s.id}>
                    {s.name}
                  </option>
                ))}
              </Select>
              <Button
                type="button"
                size="sm"
                variant="outline"
                onClick={() => setAddingSupplier(true)}
              >
                {t('inventory.add_supplier')}
              </Button>
            </div>
          )}
        </div>

        <div className="flex justify-end gap-2 border-t border-border pt-4">
          <Button type="button" variant="outline" onClick={onClose}>
            {t('users.cancel')}
          </Button>
          <Button type="submit" disabled={!ready || receiveMut.isPending}>
            {t('inventory.receive')}
          </Button>
        </div>
      </form>
    </Modal>
  );
}

// ------------------------------ expiry alerts (P2-M4) ------------------------------

const SEV_TONE: Record<string, 'danger' | 'warning'> = {
  danger: 'danger',
  critical: 'danger',
  warning: 'warning',
};

const ALERT_BUCKETS = ['expired', 'within_30', 'within_60', 'within_90'] as const;

function ExpiryAlertsTab({ branchId, canAdjust }: { branchId: string; canAdjust: boolean }) {
  const qc = useQueryClient();
  const alertsQuery = useQuery({
    queryKey: ['expiry-alerts', branchId],
    queryFn: () => getExpiryAlerts(branchId),
    enabled: !!branchId,
  });
  const sweep = useMutation({
    mutationFn: runExpirySweep,
    onSuccess: (data) => {
      qc.invalidateQueries({ queryKey: ['expiry-alerts', branchId] });
      qc.invalidateQueries({ queryKey: ['inventory', branchId] });
      qc.invalidateQueries({ queryKey: ['batches', branchId] });
      qc.invalidateQueries({ queryKey: ['batch-report', branchId] });
      qc.invalidateQueries({ queryKey: ['drift', branchId] });
      toast.success(`${t('inventory.swept_ok')} (${data.swept})`);
    },
    onError: onErr,
  });

  if (alertsQuery.isLoading) {
    return (
      <div className="flex justify-center py-8">
        <Spinner />
      </div>
    );
  }
  const data = alertsQuery.data;
  if (!data) return null;

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="text-sm text-slate-500">
          {t('inventory.expiry_as_of')}: <span className="tabular-nums">{data.as_of}</span>
        </p>
        {canAdjust && (
          <Button
            variant="outline"
            size="sm"
            onClick={() => sweep.mutate()}
            disabled={sweep.isPending}
          >
            {t('inventory.run_sweep')}
          </Button>
        )}
      </div>

      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        {ALERT_BUCKETS.map((key) => {
          const bucket = data.buckets[key];
          return (
            <Card key={key}>
              <CardContent className="space-y-2 pt-5">
                <div className="text-xs text-slate-500">{t(`inventory.bucket_${key}`)}</div>
                <div className="text-2xl font-bold tabular-nums text-slate-900">{bucket.count}</div>
                <Badge tone={SEV_TONE[bucket.severity] ?? 'neutral'}>
                  {t(`inventory.severity_${bucket.severity}`)}
                </Badge>
                <div className="text-xs text-slate-500">
                  {t('inventory.value')}: <span className="tabular-nums">{bucket.total_value}</span>
                </div>
              </CardContent>
            </Card>
          );
        })}
      </div>

      {data.totals.count === 0 ? (
        <p className="py-6 text-center text-sm text-slate-500">{t('inventory.no_alerts')}</p>
      ) : (
        ALERT_BUCKETS.map((key) => {
          const bucket = data.buckets[key];
          if (bucket.count === 0) return null;
          return (
            <Card key={key}>
              <CardContent className="pt-6">
                <div className="mb-3 flex items-center gap-2">
                  <h2 className="text-sm font-semibold text-slate-800">
                    {t(`inventory.bucket_${key}`)}
                  </h2>
                  <Badge tone={SEV_TONE[bucket.severity] ?? 'neutral'}>{bucket.count}</Badge>
                </div>
                <table className="w-full text-sm">
                  <thead>
                    <tr className="border-b border-border text-xs text-slate-500">
                      <th className="p-2 text-start">{t('inventory.medication')}</th>
                      <th className="p-2 text-start">{t('inventory.batch_number')}</th>
                      <th className="p-2 text-start">{t('inventory.expiry')}</th>
                      <th className="p-2 text-start">{t('inventory.days_left')}</th>
                      <th className="p-2 text-start">{t('inventory.quantity')}</th>
                      <th className="p-2 text-start">{t('inventory.value')}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {bucket.batches.map((b) => (
                      <tr key={b.batch_id} className="border-b border-border/60">
                        <td className="p-2 font-medium text-slate-800">
                          {b.trade_name_ar ?? b.trade_name}
                        </td>
                        <td className="p-2 font-mono text-slate-600">{b.batch_number}</td>
                        <td className="p-2 tabular-nums text-slate-600">{b.expiry_date}</td>
                        <td className="p-2 tabular-nums text-slate-600">{b.days_left}</td>
                        <td className="p-2 tabular-nums text-slate-800">{fmt(b.quantity)}</td>
                        <td className="p-2 tabular-nums text-slate-800">{b.value}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </CardContent>
            </Card>
          );
        })
      )}
    </div>
  );
}

// ------------------------------ batch report (P2-M4) ------------------------------

const REPORT_STATUSES: BatchStatus[] = ['active', 'quarantined', 'expired', 'recalled', 'depleted'];

function BatchReportTab({ branchId }: { branchId: string }) {
  const reportQuery = useQuery({
    queryKey: ['batch-report', branchId],
    queryFn: () => getBatchReport(branchId),
    enabled: !!branchId,
  });
  if (reportQuery.isLoading) {
    return (
      <div className="flex justify-center py-8">
        <Spinner />
      </div>
    );
  }
  const data = reportQuery.data;
  if (!data) return null;

  return (
    <div className="space-y-6">
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
        <KpiCard label={t('inventory.sellable_value')} value={data.sellable_value} tone="success" />
        <KpiCard label={t('inventory.locked_value')} value={data.locked_value} tone="danger" />
        <KpiCard
          label={t('inventory.total_batches')}
          value={String(data.totals.batch_count)}
          tone="neutral"
        />
      </div>

      <Card>
        <CardContent className="pt-6">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-border text-xs text-slate-500">
                <th className="p-2 text-start">{t('inventory.report_status')}</th>
                <th className="p-2 text-start">{t('inventory.report_count')}</th>
                <th className="p-2 text-start">{t('inventory.report_qty')}</th>
                <th className="p-2 text-start">{t('inventory.report_value')}</th>
              </tr>
            </thead>
            <tbody>
              {REPORT_STATUSES.map((s) => {
                const row = data.by_status[s];
                return (
                  <tr key={s} className="border-b border-border/60">
                    <td className="p-2">
                      <Badge tone={STATUS_TONE[s] ?? 'neutral'}>{statusLabel(s)}</Badge>
                    </td>
                    <td className="p-2 tabular-nums text-slate-800">{row.count}</td>
                    <td className="p-2 tabular-nums text-slate-800">{fmt(row.total_quantity)}</td>
                    <td className="p-2 tabular-nums text-slate-800">{row.total_value}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </CardContent>
      </Card>
    </div>
  );
}

function KpiCard({
  label,
  value,
  tone,
}: {
  label: string;
  value: string;
  tone: 'success' | 'danger' | 'neutral';
}) {
  const color =
    tone === 'success' ? 'text-emerald-700' : tone === 'danger' ? 'text-red-700' : 'text-slate-900';
  return (
    <Card>
      <CardContent className="space-y-1 pt-5">
        <div className="text-xs text-slate-500">{label}</div>
        <div className={'text-2xl font-bold tabular-nums ' + color}>{value}</div>
      </CardContent>
    </Card>
  );
}

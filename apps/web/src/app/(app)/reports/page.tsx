'use client';

import { Badge, Card, CardContent, Input, Label, Select, Spinner } from '@pharmaos/ui';
import { useQuery } from '@tanstack/react-query';
import { useEffect, useState } from 'react';
import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';

import {
  getInventoryValuationReport,
  getMovementReport,
  getSalesReport,
  getStockLevelReport,
  listInventoryBranches,
  type MovementType,
  salesReportExportUrl,
  type SalesReport,
  type StockLevelStatus,
  stockLevelExportUrl,
} from '@/lib/api';
import { useAuth } from '@/lib/auth-store';
import { t } from '@/lib/i18n';

// tidy up trailing zeros on the wire values (they arrive as e.g. "120.000")
function fmt(n: string): string {
  const num = Number(n);
  return Number.isFinite(num) ? String(num) : n;
}

function todayIso(): string {
  return new Date().toISOString().slice(0, 10);
}

const STOCK_STATUS_TONE: Record<StockLevelStatus, 'success' | 'warning' | 'danger'> = {
  ok: 'success',
  low_stock: 'warning',
  out_of_stock: 'danger',
};

const MOVEMENT_TYPES: MovementType[] = [
  'purchase_in',
  'sale_out',
  'return_in',
  'return_out',
  'adjustment',
  'quarantine',
  'expiry_writeoff',
  'transfer_in',
  'transfer_out',
];

export default function ReportsPage() {
  const canSales = useAuth((s) => s.hasPermission('reports.sales'));
  const canInventory = useAuth((s) => s.hasPermission('reports.inventory'));
  const canExport = useAuth((s) => s.hasPermission('reports.export'));

  const [branchId, setBranchId] = useState('');
  const [tab, setTab] = useState<'sales' | 'inventory'>(canSales ? 'sales' : 'inventory');

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
        <h1 className="text-2xl font-bold text-slate-900">{t('reports.title')}</h1>
        <div className="flex items-center gap-2">
          <Label className="text-xs">{t('reports.branch')}</Label>
          <Select className="h-9" value={branchId} onChange={(e) => setBranchId(e.target.value)}>
            {branches.map((b) => (
              <option key={b.id} value={b.id}>
                {b.name}
              </option>
            ))}
          </Select>
        </div>
      </div>

      {canSales && canInventory && (
        <div className="flex gap-1 border-b border-border">
          <TabButton active={tab === 'sales'} onClick={() => setTab('sales')}>
            {t('reports.tab_sales')}
          </TabButton>
          <TabButton active={tab === 'inventory'} onClick={() => setTab('inventory')}>
            {t('reports.tab_inventory')}
          </TabButton>
        </div>
      )}

      {tab === 'sales' && canSales && <SalesTab branchId={branchId} canExport={canExport} />}
      {tab === 'inventory' && canInventory && (
        <InventoryTab branchId={branchId} canExport={canExport} />
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

function KpiCard({ label, value, tone }: { label: string; value: string; tone?: 'danger' }) {
  return (
    <Card>
      <CardContent className="space-y-1 pt-5">
        <div className="text-xs text-slate-500">{label}</div>
        <div
          className={
            'text-2xl font-bold tabular-nums ' +
            (tone === 'danger' ? 'text-red-700' : 'text-slate-900')
          }
        >
          {value}
        </div>
      </CardContent>
    </Card>
  );
}

// ------------------------------ sales (P3-M1) ------------------------------

function SalesTab({ branchId, canExport }: { branchId: string; canExport: boolean }) {
  const [dateFrom, setDateFrom] = useState(todayIso());
  const [dateTo, setDateTo] = useState(todayIso());
  const [granularity, setGranularity] = useState<'day' | 'month' | 'year'>('day');

  const reportQuery = useQuery({
    queryKey: ['sales-report', branchId, dateFrom, dateTo, granularity],
    queryFn: () => getSalesReport({ branchId, dateFrom, dateTo, granularity }),
    enabled: !!branchId && dateFrom <= dateTo,
  });

  return (
    <div className="space-y-6">
      <Card>
        <CardContent className="flex flex-wrap items-end gap-3 pt-5">
          <div className="space-y-1.5">
            <Label className="text-xs">{t('reports.date_from')}</Label>
            <Input
              type="date"
              value={dateFrom}
              onChange={(e) => setDateFrom(e.target.value)}
              className="h-9"
            />
          </div>
          <div className="space-y-1.5">
            <Label className="text-xs">{t('reports.date_to')}</Label>
            <Input
              type="date"
              value={dateTo}
              onChange={(e) => setDateTo(e.target.value)}
              className="h-9"
            />
          </div>
          <div className="space-y-1.5">
            <Label className="text-xs">{t('reports.granularity')}</Label>
            <Select
              className="h-9"
              value={granularity}
              onChange={(e) => setGranularity(e.target.value as typeof granularity)}
            >
              <option value="day">{t('reports.granularity_day')}</option>
              <option value="month">{t('reports.granularity_month')}</option>
              <option value="year">{t('reports.granularity_year')}</option>
            </Select>
          </div>
          {canExport && (
            <a
              href={salesReportExportUrl({ branchId, dateFrom, dateTo, granularity })}
              className="ms-auto text-sm font-medium text-primary-600 hover:underline"
            >
              {t('reports.export_csv')}
            </a>
          )}
        </CardContent>
      </Card>

      {reportQuery.isLoading ? (
        <div className="flex justify-center py-8">
          <Spinner />
        </div>
      ) : reportQuery.data ? (
        <SalesReportBody data={reportQuery.data} />
      ) : (
        <p className="py-6 text-center text-sm text-slate-500">{t('reports.empty_range')}</p>
      )}
    </div>
  );
}

function SalesReportBody({ data }: { data: SalesReport }) {
  const { summary, trend, by_payment_method, by_refund_method, top_items } = data;
  const chartData = trend.map((p) => ({ bucket: p.bucket, total: Number(p.total) }));

  return (
    <div className="space-y-6">
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
        <KpiCard label={t('reports.gross_sales')} value={summary.gross_sales} />
        <KpiCard label={t('reports.net_sales')} value={summary.net_sales} />
        <KpiCard label={t('reports.tax_total')} value={summary.tax_total} />
        <KpiCard label={t('reports.refunds_total')} value={summary.refunds_total} tone="danger" />
        <KpiCard label={t('reports.invoice_count')} value={String(summary.invoice_count)} />
        <KpiCard label={t('reports.avg_invoice')} value={summary.avg_invoice} />
      </div>

      <Card>
        <CardContent className="pt-6">
          <h2 className="mb-3 text-sm font-semibold text-slate-800">{t('reports.trend')}</h2>
          {chartData.length === 0 ? (
            <p className="py-6 text-center text-sm text-slate-500">{t('reports.empty_range')}</p>
          ) : (
            <div className="h-64">
              <ResponsiveContainer width="100%" height="100%">
                <LineChart data={chartData}>
                  <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" />
                  <XAxis dataKey="bucket" fontSize={12} stroke="#64748b" />
                  <YAxis fontSize={12} stroke="#64748b" />
                  <Tooltip />
                  <Line
                    type="monotone"
                    dataKey="total"
                    stroke="var(--primary-600)"
                    strokeWidth={2}
                    dot={false}
                  />
                </LineChart>
              </ResponsiveContainer>
            </div>
          )}
        </CardContent>
      </Card>

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
        <Card>
          <CardContent className="pt-6">
            <h2 className="mb-3 text-sm font-semibold text-slate-800">
              {t('reports.by_payment_method')}
            </h2>
            <MethodTable rows={by_payment_method} />
          </CardContent>
        </Card>
        <Card>
          <CardContent className="pt-6">
            <h2 className="mb-3 text-sm font-semibold text-slate-800">
              {t('reports.by_refund_method')}
            </h2>
            <MethodTable rows={by_refund_method} />
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardContent className="pt-6">
          <h2 className="mb-3 text-sm font-semibold text-slate-800">{t('reports.top_items')}</h2>
          {top_items.length === 0 ? (
            <p className="py-4 text-center text-sm text-slate-500">{t('reports.empty_range')}</p>
          ) : (
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-border text-xs text-slate-500">
                  <th className="p-2 text-start">{t('reports.medication')}</th>
                  <th className="p-2 text-start">{t('reports.revenue')}</th>
                  <th className="p-2 text-start">{t('reports.qty_sold')}</th>
                </tr>
              </thead>
              <tbody>
                {top_items.map((row) => (
                  <tr key={row.medication_id} className="border-b border-border/60">
                    <td className="p-2 font-medium text-slate-800">{row.name_ar ?? row.name}</td>
                    <td className="p-2 tabular-nums text-slate-800">{row.revenue}</td>
                    <td className="p-2 tabular-nums text-slate-600">{fmt(row.qty_smallest)}</td>
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

function MethodTable({ rows }: { rows: { method: string; count: number; total: string }[] }) {
  if (rows.length === 0) {
    return <p className="py-4 text-center text-sm text-slate-500">{t('reports.empty_range')}</p>;
  }
  return (
    <table className="w-full text-sm">
      <thead>
        <tr className="border-b border-border text-xs text-slate-500">
          <th className="p-2 text-start">{t('reports.method')}</th>
          <th className="p-2 text-start">{t('reports.count')}</th>
          <th className="p-2 text-start">{t('reports.total')}</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((row) => (
          <tr key={row.method} className="border-b border-border/60">
            <td className="p-2 text-slate-800">{t(`reports.method_${row.method}`)}</td>
            <td className="p-2 tabular-nums text-slate-600">{row.count}</td>
            <td className="p-2 tabular-nums text-slate-800">{row.total}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

// ------------------------------ inventory (P3-M2) ------------------------------

function InventoryTab({ branchId, canExport }: { branchId: string; canExport: boolean }) {
  const [sub, setSub] = useState<'stock' | 'valuation' | 'movement'>('stock');
  return (
    <div className="space-y-4">
      <div className="flex gap-1 border-b border-border">
        <TabButton active={sub === 'stock'} onClick={() => setSub('stock')}>
          {t('reports.inv_tab_stock_level')}
        </TabButton>
        <TabButton active={sub === 'valuation'} onClick={() => setSub('valuation')}>
          {t('reports.inv_tab_valuation')}
        </TabButton>
        <TabButton active={sub === 'movement'} onClick={() => setSub('movement')}>
          {t('reports.inv_tab_movement')}
        </TabButton>
      </div>
      {sub === 'stock' && <StockLevelSubTab branchId={branchId} canExport={canExport} />}
      {sub === 'valuation' && <ValuationSubTab branchId={branchId} />}
      {sub === 'movement' && <MovementSubTab branchId={branchId} />}
    </div>
  );
}

function StockLevelSubTab({ branchId, canExport }: { branchId: string; canExport: boolean }) {
  const [lowStockOnly, setLowStockOnly] = useState(false);
  const query = useQuery({
    queryKey: ['stock-level-report', branchId, lowStockOnly],
    queryFn: () => getStockLevelReport({ branchId, lowStockOnly, limit: 100 }),
    enabled: !!branchId,
  });

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <label className="flex items-center gap-2 text-sm text-slate-700">
          <input
            type="checkbox"
            checked={lowStockOnly}
            onChange={(e) => setLowStockOnly(e.target.checked)}
          />
          {t('reports.low_stock_only')}
        </label>
        {canExport && (
          <a
            href={stockLevelExportUrl({ branchId, lowStockOnly })}
            className="text-sm font-medium text-primary-600 hover:underline"
          >
            {t('reports.export_csv')}
          </a>
        )}
      </div>

      {query.isLoading ? (
        <div className="flex justify-center py-8">
          <Spinner />
        </div>
      ) : query.data ? (
        <>
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
            <KpiCard
              label={t('reports.total_skus')}
              value={String(query.data.summary.total_skus)}
            />
            <KpiCard
              label={t('reports.low_stock_count')}
              value={String(query.data.summary.low_stock_count)}
            />
            <KpiCard
              label={t('reports.out_of_stock_count')}
              value={String(query.data.summary.out_of_stock_count)}
              tone="danger"
            />
          </div>
          <Card>
            <CardContent className="pt-6">
              {query.data.items.length === 0 ? (
                <p className="py-4 text-center text-sm text-slate-500">
                  {t('reports.empty_range')}
                </p>
              ) : (
                <table className="w-full text-sm">
                  <thead>
                    <tr className="border-b border-border text-xs text-slate-500">
                      <th className="p-2 text-start">{t('reports.medication')}</th>
                      <th className="p-2 text-start">{t('reports.on_hand')}</th>
                      <th className="p-2 text-start">{t('reports.reorder_point')}</th>
                      <th className="p-2 text-start">{t('inventory.status')}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {query.data.items.map((row) => (
                      <tr key={row.medication_id} className="border-b border-border/60">
                        <td className="p-2 font-medium text-slate-800">
                          {row.trade_name_ar ?? row.trade_name}
                        </td>
                        <td className="p-2 tabular-nums text-slate-800">
                          {fmt(row.cached_quantity)}
                        </td>
                        <td className="p-2 tabular-nums text-slate-600">
                          {row.reorder_point ? fmt(row.reorder_point) : '—'}
                        </td>
                        <td className="p-2">
                          <Badge tone={STOCK_STATUS_TONE[row.status]}>
                            {t(`reports.status_${row.status}`)}
                          </Badge>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </CardContent>
          </Card>
        </>
      ) : null}
    </div>
  );
}

function ValuationSubTab({ branchId }: { branchId: string }) {
  const query = useQuery({
    queryKey: ['valuation-report', branchId],
    queryFn: () => getInventoryValuationReport({ branchId, limit: 100 }),
    enabled: !!branchId,
  });

  if (query.isLoading) {
    return (
      <div className="flex justify-center py-8">
        <Spinner />
      </div>
    );
  }
  const data = query.data;
  if (!data) return null;

  return (
    <div className="space-y-6">
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <KpiCard label={t('reports.sellable_value')} value={data.totals.sellable_value} />
        <KpiCard label={t('reports.locked_value')} value={data.totals.locked_value} tone="danger" />
      </div>
      <Card>
        <CardContent className="pt-6">
          {data.items.length === 0 ? (
            <p className="py-4 text-center text-sm text-slate-500">{t('reports.empty_range')}</p>
          ) : (
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-border text-xs text-slate-500">
                  <th className="p-2 text-start">{t('reports.medication')}</th>
                  <th className="p-2 text-start">{t('reports.quantity')}</th>
                  <th className="p-2 text-start">{t('reports.value')}</th>
                </tr>
              </thead>
              <tbody>
                {data.items.map((row) => (
                  <tr key={row.medication_id} className="border-b border-border/60">
                    <td className="p-2 font-medium text-slate-800">
                      {row.trade_name_ar ?? row.trade_name}
                    </td>
                    <td className="p-2 tabular-nums text-slate-600">{fmt(row.quantity)}</td>
                    <td className="p-2 tabular-nums text-slate-800">{row.value}</td>
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

function MovementSubTab({ branchId }: { branchId: string }) {
  const [dateFrom, setDateFrom] = useState(todayIso());
  const [dateTo, setDateTo] = useState(todayIso());
  const query = useQuery({
    queryKey: ['movement-report', branchId, dateFrom, dateTo],
    queryFn: () => getMovementReport({ branchId, dateFrom, dateTo }),
    enabled: !!branchId && dateFrom <= dateTo,
  });

  return (
    <div className="space-y-6">
      <Card>
        <CardContent className="flex flex-wrap items-end gap-3 pt-5">
          <div className="space-y-1.5">
            <Label className="text-xs">{t('reports.date_from')}</Label>
            <Input
              type="date"
              value={dateFrom}
              onChange={(e) => setDateFrom(e.target.value)}
              className="h-9"
            />
          </div>
          <div className="space-y-1.5">
            <Label className="text-xs">{t('reports.date_to')}</Label>
            <Input
              type="date"
              value={dateTo}
              onChange={(e) => setDateTo(e.target.value)}
              className="h-9"
            />
          </div>
        </CardContent>
      </Card>

      {query.isLoading ? (
        <div className="flex justify-center py-8">
          <Spinner />
        </div>
      ) : query.data ? (
        <>
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
            <KpiCard
              label={t('reports.total_movements')}
              value={String(query.data.total_movements)}
            />
          </div>

          <Card>
            <CardContent className="pt-6">
              <h2 className="mb-3 text-sm font-semibold text-slate-800">{t('reports.by_type')}</h2>
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-border text-xs text-slate-500">
                    <th className="p-2 text-start">{t('reports.by_type')}</th>
                    <th className="p-2 text-start">{t('reports.count')}</th>
                    <th className="p-2 text-start">{t('reports.quantity')}</th>
                  </tr>
                </thead>
                <tbody>
                  {MOVEMENT_TYPES.map((mt) => {
                    const row = query.data.by_type[mt];
                    return (
                      <tr key={mt} className="border-b border-border/60">
                        <td className="p-2 text-slate-800">{t(`reports.movement_${mt}`)}</td>
                        <td className="p-2 tabular-nums text-slate-600">{row.count}</td>
                        <td className="p-2 tabular-nums text-slate-800">{fmt(row.net_quantity)}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </CardContent>
          </Card>

          <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
            <Card>
              <CardContent className="pt-6">
                <h2 className="mb-3 text-sm font-semibold text-slate-800">
                  {t('reports.fast_movers')}
                </h2>
                <MoverTable rows={query.data.fast_movers} />
              </CardContent>
            </Card>
            <Card>
              <CardContent className="pt-6">
                <h2 className="mb-3 text-sm font-semibold text-slate-800">
                  {t('reports.slow_movers')}
                </h2>
                <MoverTable rows={query.data.slow_movers} />
              </CardContent>
            </Card>
          </div>
        </>
      ) : null}
    </div>
  );
}

function MoverTable({
  rows,
}: {
  rows: {
    medication_id: string;
    trade_name: string;
    trade_name_ar: string | null;
    qty_sold: string;
  }[];
}) {
  if (rows.length === 0) {
    return <p className="py-4 text-center text-sm text-slate-500">{t('reports.no_movers')}</p>;
  }
  return (
    <table className="w-full text-sm">
      <thead>
        <tr className="border-b border-border text-xs text-slate-500">
          <th className="p-2 text-start">{t('reports.medication')}</th>
          <th className="p-2 text-start">{t('reports.qty_sold')}</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((row) => (
          <tr key={row.medication_id} className="border-b border-border/60">
            <td className="p-2 font-medium text-slate-800">
              {row.trade_name_ar ?? row.trade_name}
            </td>
            <td className="p-2 tabular-nums text-slate-800">{fmt(row.qty_sold)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

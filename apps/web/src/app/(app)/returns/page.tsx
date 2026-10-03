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
  createReturn,
  getReturn,
  listInventoryBranches,
  listReturns,
  lookupInvoiceForReturn,
  type RefundMethod,
  type ReturnableInvoice,
  type ReturnDetail,
} from '@/lib/api';
import { useAuth } from '@/lib/auth-store';
import { t } from '@/lib/i18n';
import { toast } from '@/lib/toast-store';

const errCode = (e: unknown) => (e instanceof ApiRequestError ? e.code : 'E-SYS-001');
const onErr = (e: unknown) => toast.error(t(`errors.${errCode(e)}`));

const REFUND_METHODS: RefundMethod[] = ['cash', 'card', 'store_credit'];

export default function ReturnsPage() {
  const [tab, setTab] = useState<'create' | 'history'>('create');
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
    return <p className="py-10 text-center text-slate-500">{t('returns.no_branch')}</p>;
  }

  return (
    <div className="mx-auto max-w-4xl space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-2xl font-bold text-slate-900">{t('returns.title')}</h1>
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
        <TabButton active={tab === 'create'} onClick={() => setTab('create')}>
          {t('returns.tab_create')}
        </TabButton>
        <TabButton active={tab === 'history'} onClick={() => setTab('history')}>
          {t('returns.tab_history')}
        </TabButton>
      </div>

      {tab === 'create' ? (
        <CreateReturnTab branchId={branchId} />
      ) : (
        <HistoryTab branchId={branchId} />
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

// ================================ Create ================================

function CreateReturnTab({ branchId }: { branchId: string }) {
  const canReturn = useAuth((s) => s.hasPermission('sales.return'));
  const qc = useQueryClient();

  const [invoiceNumber, setInvoiceNumber] = useState('');
  const [invoice, setInvoice] = useState<ReturnableInvoice | null>(null);
  const [quantities, setQuantities] = useState<Record<string, string>>({});
  const [reason, setReason] = useState('');
  const [refundMethod, setRefundMethod] = useState<RefundMethod>('cash');
  const [created, setCreated] = useState<ReturnDetail | null>(null);

  const lookupMut = useMutation({
    mutationFn: () => lookupInvoiceForReturn(branchId, invoiceNumber.trim()),
    onSuccess: (data) => {
      setInvoice(data);
      setQuantities({});
      if (data.status !== 'completed') toast.info(t('returns.not_completed'));
    },
    onError: onErr,
  });

  const createMut = useMutation({
    mutationFn: () => {
      if (!invoice) throw new Error('no invoice selected');
      return createReturn({
        original_invoice_id: invoice.invoice_id,
        lines: Object.entries(quantities)
          .filter(([, qty]) => Number(qty) > 0)
          .map(([invoice_item_id, quantity]) => ({ invoice_item_id, quantity })),
        reason: reason.trim() || null,
        refund_method: refundMethod,
      });
    },
    onSuccess: (data) => {
      setCreated(data);
      setInvoice(null);
      setInvoiceNumber('');
      setQuantities({});
      setReason('');
      setRefundMethod('cash');
      qc.invalidateQueries({ queryKey: ['returns', branchId] });
      toast.success(t('returns.created_ok'));
    },
    onError: onErr,
  });

  const totalQty = Object.values(quantities).reduce((sum, v) => sum + (Number(v) || 0), 0);

  if (created) {
    return (
      <div className="space-y-4">
        <Card>
          <CardContent className="pt-6">
            <CreditNoteView detail={created} />
          </CardContent>
        </Card>
        <div className="flex justify-center gap-2">
          <Button variant="outline" onClick={() => window.print()}>
            {t('returns.print')}
          </Button>
          <Button onClick={() => setCreated(null)}>{t('returns.new_return')}</Button>
        </div>
        <PrintableCreditNote detail={created} />
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <form
        className="flex flex-wrap items-end gap-3"
        onSubmit={(e) => {
          e.preventDefault();
          if (invoiceNumber.trim()) lookupMut.mutate();
        }}
      >
        <div className="min-w-56 flex-1 space-y-1.5">
          <Label>{t('returns.invoice_number')}</Label>
          <Input
            className="font-mono"
            value={invoiceNumber}
            onChange={(e) => setInvoiceNumber(e.target.value)}
            placeholder="INV-20260713-0001"
            autoFocus
          />
        </div>
        <Button type="submit" disabled={!invoiceNumber.trim() || lookupMut.isPending}>
          {t('returns.search')}
        </Button>
      </form>

      {invoice && (
        <Card>
          <CardContent className="space-y-4 pt-6">
            <div className="flex items-center justify-between">
              <div className="text-sm text-slate-600">
                {t('returns.invoice_number')}:{' '}
                <span className="font-mono">{invoice.invoice_number}</span>
              </div>
              <Badge tone={invoice.status === 'completed' ? 'success' : 'neutral'}>
                {invoice.status}
              </Badge>
            </div>

            {invoice.status !== 'completed' ? (
              <p className="text-sm text-danger">{t('returns.not_completed')}</p>
            ) : (
              <>
                <table className="w-full text-sm">
                  <thead>
                    <tr className="border-b border-border text-xs text-slate-500">
                      <th className="p-2 text-start">{t('returns.medication')}</th>
                      <th className="p-2 text-start">{t('returns.sold')}</th>
                      <th className="p-2 text-start">{t('returns.already_returned')}</th>
                      <th className="p-2 text-start">{t('returns.returnable')}</th>
                      <th className="p-2 text-start">{t('returns.qty_to_return')}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {invoice.lines.map((line) => {
                      const max = Number(line.returnable_qty);
                      return (
                        <tr key={line.invoice_item_id} className="border-b border-border/60">
                          <td className="p-2 font-medium text-slate-800">
                            {line.trade_name_ar ?? line.trade_name}
                            <span className="ms-1 text-xs text-slate-400">
                              ({line.packaging_name_ar})
                            </span>
                          </td>
                          <td className="p-2 tabular-nums text-slate-600">{line.sold_qty}</td>
                          <td className="p-2 tabular-nums text-slate-600">{line.returned_qty}</td>
                          <td className="p-2 tabular-nums text-slate-800">{line.returnable_qty}</td>
                          <td className="p-2">
                            <Input
                              className="w-24"
                              inputMode="decimal"
                              disabled={!canReturn || max <= 0}
                              value={quantities[line.invoice_item_id] ?? ''}
                              onChange={(e) =>
                                setQuantities((q) => ({
                                  ...q,
                                  [line.invoice_item_id]: e.target.value,
                                }))
                              }
                              placeholder="0"
                            />
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>

                {canReturn && (
                  <form
                    className="space-y-4 border-t border-border pt-4"
                    onSubmit={(e) => {
                      e.preventDefault();
                      if (totalQty > 0) createMut.mutate();
                    }}
                  >
                    <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                      <div className="space-y-1.5">
                        <Label>{t('returns.refund_method')}</Label>
                        <Select
                          value={refundMethod}
                          onChange={(e) => setRefundMethod(e.target.value as RefundMethod)}
                        >
                          {REFUND_METHODS.map((m) => (
                            <option key={m} value={m}>
                              {t(`returns.method_${m}`)}
                            </option>
                          ))}
                        </Select>
                      </div>
                      <div className="space-y-1.5">
                        <Label>{t('returns.reason')}</Label>
                        <Input value={reason} onChange={(e) => setReason(e.target.value)} />
                      </div>
                    </div>
                    <div className="flex justify-end">
                      <Button type="submit" disabled={totalQty <= 0 || createMut.isPending}>
                        {t('returns.create')}
                      </Button>
                    </div>
                  </form>
                )}
              </>
            )}
          </CardContent>
        </Card>
      )}
    </div>
  );
}

// ================================ History ================================

function HistoryTab({ branchId }: { branchId: string }) {
  const [detailId, setDetailId] = useState<string | null>(null);
  const listQuery = useQuery({
    queryKey: ['returns', branchId],
    queryFn: () => listReturns(branchId),
    enabled: !!branchId,
  });
  const rows = listQuery.data ?? [];

  return (
    <>
      <Card>
        <CardContent className="pt-6">
          {listQuery.isLoading ? (
            <div className="flex justify-center py-8">
              <Spinner />
            </div>
          ) : rows.length === 0 ? (
            <p className="py-6 text-center text-sm text-slate-500">{t('returns.empty')}</p>
          ) : (
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-border text-xs text-slate-500">
                  <th className="p-2 text-start">{t('returns.return_number')}</th>
                  <th className="p-2 text-start">{t('returns.against_invoice')}</th>
                  <th className="p-2 text-start">{t('returns.refund_total')}</th>
                  <th className="p-2 text-start">{t('returns.refund_method')}</th>
                  <th className="p-2 text-start">{t('returns.date')}</th>
                  <th className="p-2 text-start"></th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => (
                  <tr key={r.id} className="border-b border-border/60">
                    <td className="p-2 font-mono text-slate-700">{r.return_number}</td>
                    <td className="p-2 font-mono text-slate-500">{r.original_invoice_number}</td>
                    <td className="p-2 tabular-nums text-slate-800">
                      {r.total} {r.currency_code}
                    </td>
                    <td className="p-2">
                      <Badge tone="neutral">{t(`returns.method_${r.refund_method}`)}</Badge>
                    </td>
                    <td className="p-2 text-xs text-slate-400">{r.created_at.slice(0, 10)}</td>
                    <td className="p-2 text-end">
                      <Button size="sm" variant="ghost" onClick={() => setDetailId(r.id)}>
                        {t('returns.view')}
                      </Button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </CardContent>
      </Card>
      {detailId && <ReturnDetailModal returnId={detailId} onClose={() => setDetailId(null)} />}
    </>
  );
}

function ReturnDetailModal({ returnId, onClose }: { returnId: string; onClose: () => void }) {
  const query = useQuery({ queryKey: ['return', returnId], queryFn: () => getReturn(returnId) });
  const detail = query.data;
  return (
    <Modal
      open
      onClose={onClose}
      title={t('returns.detail')}
      className="max-h-[85vh] max-w-2xl overflow-y-auto"
    >
      {query.isLoading || !detail ? (
        <div className="flex justify-center py-8">
          <Spinner />
        </div>
      ) : (
        <>
          <CreditNoteView detail={detail} />
          <div className="mt-4 flex justify-end gap-2 border-t border-border pt-4">
            <Button variant="outline" onClick={() => window.print()}>
              {t('returns.print')}
            </Button>
          </div>
          <PrintableCreditNote detail={detail} />
        </>
      )}
    </Modal>
  );
}

// ================================ shared views ================================

function CreditNoteView({ detail }: { detail: ReturnDetail }) {
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <div className="text-lg font-bold text-slate-900">{detail.return_number}</div>
          <div className="text-xs text-slate-500">
            {t('returns.against_invoice')}:{' '}
            <span className="font-mono">{detail.original_invoice_number}</span>
          </div>
        </div>
        <Badge tone="neutral">{t(`returns.method_${detail.refund_method}`)}</Badge>
      </div>
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-border text-xs text-slate-500">
            <th className="p-2 text-start">{t('returns.medication')}</th>
            <th className="p-2 text-start">{t('returns.quantity')}</th>
            <th className="p-2 text-start">{t('returns.line_total')}</th>
          </tr>
        </thead>
        <tbody>
          {detail.items.map((item) => (
            <tr key={item.id} className="border-b border-border/60">
              <td className="p-2 font-medium text-slate-800">
                {item.trade_name_ar ?? item.trade_name}
                <span className="ms-1 text-xs text-slate-400">({item.packaging_name_ar})</span>
              </td>
              <td className="p-2 tabular-nums text-slate-600">{item.quantity}</td>
              <td className="p-2 tabular-nums text-slate-800">{item.line_total}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="space-y-0.5 border-t border-border pt-2 text-sm">
        <div className="flex justify-between text-slate-600">
          <span>{t('returns.subtotal')}</span>
          <span className="tabular-nums">
            {detail.subtotal} {detail.currency_code}
          </span>
        </div>
        {Number(detail.tax_amount) > 0 && (
          <div className="flex justify-between text-slate-600">
            <span>{t('receipt.tax')}</span>
            <span className="tabular-nums">
              {detail.tax_amount} {detail.currency_code}
            </span>
          </div>
        )}
        <div className="flex justify-between text-base font-bold text-slate-900">
          <span>{t('returns.refund_total')}</span>
          <span className="tabular-nums">
            {detail.total} {detail.currency_code}
          </span>
        </div>
      </div>
      {detail.reason && (
        <p className="text-xs text-slate-500">
          {t('returns.reason')}: {detail.reason}
        </p>
      )}
    </div>
  );
}

/**
 * Browser-print fallback (mirrors PrintableReceipt in pos/page.tsx): hidden on
 * screen, the only visible element in print media (.receipt-print, globals.css).
 */
function PrintableCreditNote({ detail }: { detail: ReturnDetail }) {
  return (
    <div className="receipt-print mx-auto max-w-[300px] bg-white p-4 text-center text-sm text-black">
      <p className="text-lg font-extrabold">{t('returns.credit_note')}</p>
      <hr className="my-2 border-dashed border-black" />
      <div className="flex justify-between text-xs">
        <span className="font-mono">{detail.return_number}</span>
        <span className="tabular-nums">{detail.created_at.slice(0, 10)}</span>
      </div>
      <div className="text-xs">
        {t('returns.against_invoice')}:{' '}
        <span className="font-mono">{detail.original_invoice_number}</span>
      </div>
      <hr className="my-2 border-dashed border-black" />
      <table className="w-full text-xs">
        <tbody>
          {detail.items.map((item) => (
            <tr key={item.id}>
              <td className="py-0.5 text-start">
                {item.trade_name_ar ?? item.trade_name}
                <span className="text-[10px] text-slate-600">
                  {' '}
                  ({Number(item.quantity)} × {item.packaging_name_ar})
                </span>
              </td>
              <td className="py-0.5 text-end tabular-nums">{item.line_total}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <hr className="my-2 border-dashed border-black" />
      <div className="space-y-0.5 text-xs">
        <div className="flex justify-between">
          <span>{t('returns.subtotal')}</span>
          <span className="tabular-nums">
            {detail.subtotal} {detail.currency_code}
          </span>
        </div>
        {Number(detail.tax_amount) > 0 && (
          <div className="flex justify-between">
            <span>{t('receipt.tax')}</span>
            <span className="tabular-nums">
              {detail.tax_amount} {detail.currency_code}
            </span>
          </div>
        )}
        <div className="flex justify-between text-sm font-extrabold">
          <span>{t('returns.refund_total')}</span>
          <span className="tabular-nums">
            {detail.total} {detail.currency_code}
          </span>
        </div>
      </div>
      {detail.reason && (
        <p className="mt-2 text-[10px]">
          {t('returns.reason')}: {detail.reason}
        </p>
      )}
      <p className="mt-2 text-[10px]">{t('returns.print_footer')}</p>
    </div>
  );
}

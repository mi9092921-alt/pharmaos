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
import { useEffect, useRef, useState } from 'react';

import Link from 'next/link';

import {
  ApiRequestError,
  createPosSale,
  type CustomerSummary,
  getCurrentCashSession,
  getInvoiceReceipt,
  getMedication,
  getPrescription,
  type InvoiceReceipt,
  listCustomers,
  listInventoryBranches,
  listPrescriptions,
  type MedOption,
  parseGs1,
  posScan,
  type PosLevel,
  type PosSaleResult,
  type PosScan,
  printInvoice,
  searchMedications,
} from '@/lib/api';
import { useAuth } from '@/lib/auth-store';
import { t } from '@/lib/i18n';
import { toast } from '@/lib/toast-store';

const errCode = (e: unknown) => (e instanceof ApiRequestError ? e.code : 'E-SYS-001');

interface CartLine {
  medicationId: string;
  name: string;
  levels: PosLevel[];
  packagingId: string;
  unitName: string;
  unitPrice: string;
  qty: string;
  requiresPrescription: boolean;
  controlled: boolean;
  /** P2-M8 — set once the cashier links a prescription item; required before
   * checkout when requiresPrescription is true. remainingHint is a display-only
   * snapshot captured at pick time (the server remains authoritative). */
  prescriptionItemId: string | null;
  prescriptionRemainingHint: string | null;
}

const RX_STATUS_TONE: Record<string, 'neutral' | 'warning' | 'success' | 'danger'> = {
  pending: 'neutral',
  partially_fulfilled: 'warning',
  fulfilled: 'success',
  expired: 'danger',
  cancelled: 'danger',
};

interface DoneInfo {
  invoiceId: string;
  invoiceNumber: string;
  total: string;
  currency: string;
  change: string | null;
  paymentMethod: 'cash' | 'card';
  pointsEarned: number | null;
  pointsRedeemed: number;
  discountAmount: string;
  taxAmount: string;
}

type PrintState = 'idle' | 'printing' | 'ok' | 'unconfigured' | 'paper' | 'failed';

const lineTotal = (l: CartLine): number => {
  const q = Number(l.qty);
  return Number.isFinite(q) && q > 0 ? q * Number(l.unitPrice) : NaN;
};

/**
 * POS (P1-M8) — mouse-free, scan-first flow.
 * The scan box stays focused; a hardware scanner types the code and sends
 * Enter. F3 turns the typed text into an Arabic name search. Every cart action
 * has a key: ↑↓ select · +/− quantity · F6 cycle unit · Delete remove · F4 pay.
 */
export default function PosPage() {
  const qc = useQueryClient();
  const scanRef = useRef<HTMLInputElement>(null);

  const [branchId, setBranchId] = useState('');
  const [cart, setCart] = useState<CartLine[]>([]);
  const [sel, setSel] = useState(0);
  const [scanVal, setScanVal] = useState('');
  const [results, setResults] = useState<MedOption[] | null>(null);
  const [resultIdx, setResultIdx] = useState(0);
  const [searching, setSearching] = useState(false);
  const [payOpen, setPayOpen] = useState(false);
  const [rxPickerFor, setRxPickerFor] = useState<number | null>(null);
  const [customer, setCustomer] = useState<CustomerSummary | null>(null);
  const [redeemPoints, setRedeemPoints] = useState(0);
  const [posSerials, setPosSerials] = useState<string[]>([]);
  const [serialScan, setSerialScan] = useState('');
  const [done, setDone] = useState<DoneInfo | null>(null);
  const [printState, setPrintState] = useState<PrintState>('idle');
  const [receiptData, setReceiptData] = useState<InvoiceReceipt | null>(null);

  const branchesQuery = useQuery({ queryKey: ['inv-branches'], queryFn: listInventoryBranches });
  const branches = branchesQuery.data ?? [];
  useEffect(() => {
    const first = branches[0];
    if (!branchId && first) setBranchId(first.id);
  }, [branches, branchId]);
  const currency = branches.find((b) => b.id === branchId)?.currency_code ?? 'EGP';

  // M11 — drawer-accountability hint: a seller who CAN hold a session but has
  // none open gets a nudge (sales still work; they land outside sessions).
  const canOpenSession = useAuth((s) => s.hasPermission('cashier.open_session'));
  const sessionQuery = useQuery({
    queryKey: ['cash-current', branchId],
    queryFn: () => getCurrentCashSession(branchId),
    enabled: !!branchId && canOpenSession,
  });
  const noOpenSession = canOpenSession && sessionQuery.data?.session === null;

  const focusScan = () => {
    scanRef.current?.focus();
    scanRef.current?.select();
  };

  // ---------------- cart operations ----------------

  const addLine = (line: Omit<CartLine, 'qty'>, qty: number) => {
    const idx = cart.findIndex(
      (l) => l.medicationId === line.medicationId && l.packagingId === line.packagingId,
    );
    if (idx >= 0) {
      const existing = cart[idx];
      if (existing) {
        const merged = String((Number(existing.qty) || 0) + qty);
        setCart(cart.map((l, i) => (i === idx ? { ...l, qty: merged } : l)));
        setSel(idx);
      }
    } else {
      setCart([...cart, { ...line, qty: String(qty) }]);
      setSel(cart.length);
    }
    setScanVal('');
    setResults(null);
    focusScan();
  };

  const addFromScan = (scan: PosScan) => {
    addLine(
      {
        medicationId: scan.medication_id,
        name: scan.trade_name_ar ?? scan.trade_name,
        levels: scan.levels,
        packagingId: scan.packaging_id,
        unitName: scan.packaging_name_ar,
        unitPrice: scan.selling_price,
        requiresPrescription: scan.requires_prescription,
        controlled: scan.controlled_substance,
        prescriptionItemId: null,
        prescriptionRemainingHint: null,
      },
      1,
    );
  };

  const setQty = (i: number, qty: string) =>
    setCart((prev) => prev.map((l, idx) => (idx === i ? { ...l, qty } : l)));

  const bumpQty = (i: number, delta: number) =>
    setCart((prev) =>
      prev.map((l, idx) =>
        idx === i ? { ...l, qty: String(Math.max(1, (Number(l.qty) || 0) + delta)) } : l,
      ),
    );

  const removeLine = (i: number) => {
    setCart((prev) => prev.filter((_, idx) => idx !== i));
    setSel((s) => Math.max(0, Math.min(s, cart.length - 2)));
    focusScan();
  };

  const switchUnit = (i: number, packagingId: string) => {
    const line = cart[i];
    const level = line?.levels.find((x) => x.id === packagingId);
    if (!line || !level) return;
    const dupIdx = cart.findIndex(
      (l, idx) =>
        idx !== i && l.medicationId === line.medicationId && l.packagingId === packagingId,
    );
    if (dupIdx >= 0) {
      const dup = cart[dupIdx];
      if (dup) {
        const merged = String((Number(dup.qty) || 0) + (Number(line.qty) || 0));
        setCart(
          cart.filter((_, idx) => idx !== i).map((l) => (l === dup ? { ...l, qty: merged } : l)),
        );
        setSel(dupIdx > i ? dupIdx - 1 : dupIdx);
        return;
      }
    }
    setCart(
      cart.map((l, idx) =>
        idx === i
          ? {
              ...l,
              packagingId: level.id,
              unitName: level.name_ar,
              unitPrice: level.selling_price,
            }
          : l,
      ),
    );
  };

  const cycleUnit = (i: number) => {
    const line = cart[i];
    if (!line || line.levels.length < 2) return;
    const at = line.levels.findIndex((x) => x.id === line.packagingId);
    const next = line.levels[(at + 1) % line.levels.length];
    if (next) switchUnit(i, next.id);
  };

  // ---------------- scan & name search ----------------

  const scanMut = useMutation({
    mutationFn: (code: string) => posScan(code),
    onSuccess: addFromScan,
    onError: (e) => {
      const code = errCode(e);
      toast.error(code === 'E-VAL-001' ? t('pos.unknown_code') : t(`errors.${code}`));
      focusScan();
    },
  });

  const searchByName = async () => {
    const term = scanVal.trim();
    if (term.length < 2 || searching) return;
    setSearching(true);
    try {
      const found = await searchMedications(term);
      setResults(found);
      setResultIdx(0);
    } catch (e) {
      toast.error(t(`errors.${errCode(e)}`));
    } finally {
      setSearching(false);
    }
  };

  const pickResult = async (med: MedOption) => {
    try {
      const detail = await getMedication(med.id);
      const sellable = detail.packaging
        .filter((p) => p.is_sellable)
        .sort((a, b) => a.level - b.level);
      const def = sellable.find((p) => p.is_default_sale) ?? sellable[0];
      if (!def) {
        toast.error(t('pos.no_sellable'));
        return;
      }
      addLine(
        {
          medicationId: detail.id,
          name: detail.trade_name_ar ?? detail.trade_name,
          levels: sellable.map((p) => ({
            id: p.id,
            level: p.level,
            name_ar: p.name_ar,
            selling_price: p.selling_price,
            is_default_sale: p.is_default_sale,
          })),
          packagingId: def.id,
          unitName: def.name_ar,
          unitPrice: def.selling_price,
          requiresPrescription: detail.requires_prescription,
          controlled: detail.controlled_substance,
          prescriptionItemId: null,
          prescriptionRemainingHint: null,
        },
        1,
      );
    } catch (e) {
      toast.error(t(`errors.${errCode(e)}`));
    }
  };

  // ---------------- 2D pack-serial capture (P2-M3 / C2) ----------------

  const captureSerial = async () => {
    const code = serialScan.trim();
    if (!code) return;
    try {
      const parsed = await parseGs1(code);
      const s = parsed.serial_number;
      if (!s) {
        toast.error(t('pos.unknown_code'));
        return;
      }
      setPosSerials((prev) => (prev.includes(s) ? prev : [...prev, s]));
      setSerialScan('');
    } catch (e) {
      toast.error(t(`errors.${errCode(e)}`));
    }
  };

  // ---------------- totals & checkout ----------------

  const totals = cart.map(lineTotal);
  const hasInvalidQty = totals.some((x) => !Number.isFinite(x));
  const hasUnlinkedRx = cart.some((l) => l.requiresPrescription && !l.prescriptionItemId);
  const clientTotal = totals.reduce((a, x) => a + (Number.isFinite(x) ? x : 0), 0);
  // P2-M5 (C3) — loyalty redemption: 1 pt = 1 currency unit, capped by the
  // customer's balance and the sale total. effectiveRedeem clamps stale input
  // (e.g. after the cart shrinks) without needing an effect.
  const maxRedeem = customer ? Math.min(customer.loyalty_points, Math.floor(clientTotal)) : 0;
  const effectiveRedeem = Math.max(0, Math.min(redeemPoints, maxRedeem));
  const netTotal = clientTotal - effectiveRedeem;

  const openPay = () => {
    if (cart.length === 0) return;
    if (hasInvalidQty) {
      toast.error(t('pos.invalid_qty'));
      return;
    }
    if (hasUnlinkedRx) {
      toast.error(t('pos.rx_missing_hint'));
      return;
    }
    setPayOpen(true);
  };

  const newSale = () => {
    setCart([]);
    setSel(0);
    setDone(null);
    setCustomer(null);
    setRedeemPoints(0);
    setPosSerials([]);
    setSerialScan('');
    setPrintState('idle');
    setReceiptData(null);
    setScanVal('');
    setResults(null);
    focusScan();
  };

  // ---------------- receipt printing (M9) ----------------

  const thermalPrint = async (invoiceId: string, opts: { open_drawer?: boolean } = {}) => {
    setPrintState('printing');
    try {
      await printInvoice(invoiceId, opts);
      setPrintState('ok');
    } catch (e) {
      const code = errCode(e);
      if (code === 'E-PRN-001') setPrintState('unconfigured');
      else if (code === 'E-PRN-003') setPrintState('paper');
      else {
        setPrintState('failed');
        toast.error(t(`errors.${code}`));
      }
    }
  };

  /** On completion: load the composed receipt once, then print the right way —
   * thermal when the device is ready (drawer pulse decided server-side by
   * payment method), otherwise surface the browser-print fallback. */
  const startPrintFlow = async (info: DoneInfo) => {
    try {
      const receipt = await getInvoiceReceipt(info.invoiceId);
      setReceiptData(receipt);
      if (receipt.thermal_ready) {
        await thermalPrint(info.invoiceId);
      } else if (receipt.paper_size !== '80mm') {
        setPrintState('paper');
      } else {
        setPrintState('unconfigured');
      }
    } catch (e) {
      setPrintState('failed');
      toast.error(t(`errors.${errCode(e)}`));
    }
  };

  const browserPrint = async () => {
    if (!done) return;
    let data = receiptData;
    if (!data) {
      try {
        data = await getInvoiceReceipt(done.invoiceId);
        setReceiptData(data);
      } catch (e) {
        toast.error(t(`errors.${errCode(e)}`));
        return;
      }
    }
    // Let the hidden printable receipt render before opening the dialog.
    setTimeout(() => window.print(), 80);
  };

  // ---------------- keyboard model (mouse-free flow) ----------------

  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (done) {
        // e.code = physical key — works the same under the Arabic layout.
        if (e.key === 'Enter') {
          e.preventDefault();
          newSale();
        } else if (e.code === 'KeyP' && receiptData?.thermal_ready) {
          e.preventDefault();
          void thermalPrint(done.invoiceId, { open_drawer: false }); // reprint: keep drawer shut
        } else if (e.code === 'KeyB') {
          e.preventDefault();
          void browserPrint();
        }
        return;
      }
      if (payOpen || rxPickerFor !== null) return;

      if (e.key === 'F2') {
        e.preventDefault();
        focusScan();
        return;
      }
      if (e.key === 'F3') {
        e.preventDefault();
        void searchByName();
        return;
      }
      if (e.key === 'F4') {
        e.preventDefault();
        openPay();
        return;
      }
      if (e.key === 'F6') {
        e.preventDefault();
        cycleUnit(sel);
        return;
      }
      if (e.key === 'F8') {
        e.preventDefault();
        removeLine(sel);
        return;
      }

      const el = document.activeElement;
      const isField =
        el instanceof HTMLElement && ['INPUT', 'SELECT', 'TEXTAREA'].includes(el.tagName);
      const scanEmpty = el === scanRef.current && scanVal === '';
      if ((isField && !scanEmpty) || results !== null) return;

      switch (e.key) {
        case 'ArrowDown':
          e.preventDefault();
          setSel((s) => Math.min(s + 1, Math.max(0, cart.length - 1)));
          break;
        case 'ArrowUp':
          e.preventDefault();
          setSel((s) => Math.max(s - 1, 0));
          break;
        case '+':
        case '=':
          e.preventDefault();
          bumpQty(sel, 1);
          break;
        case '-':
          e.preventDefault();
          bumpQty(sel, -1);
          break;
        case 'Delete':
          e.preventDefault();
          removeLine(sel);
          break;
      }
    };
    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  });

  // ---------------- render ----------------

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
    <div className="mx-auto flex max-w-5xl flex-col gap-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-2xl font-bold text-slate-900">{t('pos.title')}</h1>
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

      {noOpenSession && (
        <div className="flex items-center justify-between rounded-[var(--radius-md)] border border-amber-200 bg-amber-50 px-4 py-2 text-sm text-amber-800">
          <span>{t('pos.no_session_hint')}</span>
          <Link href="/cashier" className="font-semibold text-primary-700 hover:underline">
            {t('cashier.open')}
          </Link>
        </div>
      )}

      {/* Scan box + name-search results */}
      <div className="relative">
        <Input
          ref={scanRef}
          className="h-12 text-lg"
          value={scanVal}
          autoFocus
          placeholder={t('pos.scan_placeholder')}
          onChange={(e) => {
            setScanVal(e.target.value);
            if (results) setResults(null);
          }}
          onKeyDown={(e) => {
            if (results) {
              if (e.key === 'ArrowDown') {
                e.preventDefault();
                setResultIdx((i) => Math.min(i + 1, results.length - 1));
              } else if (e.key === 'ArrowUp') {
                e.preventDefault();
                setResultIdx((i) => Math.max(i - 1, 0));
              } else if (e.key === 'Enter') {
                e.preventDefault();
                const med = results[resultIdx];
                if (med) void pickResult(med);
              } else if (e.key === 'Escape') {
                setResults(null);
              }
              return;
            }
            if (e.key === 'Enter') {
              e.preventDefault();
              const code = scanVal.trim();
              if (code) scanMut.mutate(code);
            }
          }}
        />
        {searching && <p className="absolute mt-1 text-xs text-slate-500">{t('pos.searching')}</p>}
        {results && (
          <div className="absolute z-10 mt-1 w-full overflow-hidden rounded-[var(--radius-md)] border border-border bg-white shadow-lg">
            {results.length === 0 ? (
              <p className="px-3 py-2 text-sm text-slate-500">{t('pos.no_results')}</p>
            ) : (
              results.map((m, i) => (
                <button
                  key={m.id}
                  type="button"
                  className={`block w-full px-3 py-2 text-start text-sm ${
                    i === resultIdx ? 'bg-primary-50 text-primary-700' : 'hover:bg-primary-50'
                  }`}
                  onMouseEnter={() => setResultIdx(i)}
                  onClick={() => void pickResult(m)}
                >
                  {m.trade_name_ar ?? m.trade_name}
                </button>
              ))
            )}
          </div>
        )}
      </div>

      {/* Cart */}
      <Card>
        <CardContent className="pt-6">
          {cart.length === 0 ? (
            <p className="py-10 text-center text-sm text-slate-500">{t('pos.empty_cart')}</p>
          ) : (
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-border text-xs text-slate-500">
                  <th className="p-2 text-start">#</th>
                  <th className="p-2 text-start">{t('inventory.medication')}</th>
                  <th className="p-2 text-start">{t('pos.unit')}</th>
                  <th className="p-2 text-start">{t('pos.qty')}</th>
                  <th className="p-2 text-start">{t('pos.unit_price')}</th>
                  <th className="p-2 text-start">{t('pos.line_total')}</th>
                  <th className="p-2"></th>
                </tr>
              </thead>
              <tbody>
                {cart.map((l, i) => {
                  const total = lineTotal(l);
                  return (
                    <tr
                      key={`${l.medicationId}:${l.packagingId}`}
                      onClick={() => setSel(i)}
                      className={`cursor-default border-b border-border/60 ${
                        i === sel ? 'bg-primary-50/70' : ''
                      }`}
                    >
                      <td className="p-2 text-xs text-slate-400">{i + 1}</td>
                      <td className="p-2 font-medium text-slate-800">
                        {l.name}
                        {l.controlled && (
                          <Badge tone="danger" className="ms-2">
                            {t('catalog.controlled')}
                          </Badge>
                        )}
                        {l.requiresPrescription &&
                          (l.prescriptionItemId ? (
                            <button
                              type="button"
                              title={
                                l.prescriptionRemainingHint
                                  ? `${t('prescriptions.remaining')}: ${l.prescriptionRemainingHint}`
                                  : undefined
                              }
                              className="ms-2 inline-flex items-center gap-1 rounded-full bg-green-100 px-2.5 py-0.5 text-xs font-medium text-success hover:bg-green-200"
                              onClick={(e) => {
                                e.stopPropagation();
                                setRxPickerFor(i);
                              }}
                            >
                              ✓ {t('pos.rx_linked')}
                            </button>
                          ) : (
                            <button
                              type="button"
                              className="ms-2 inline-flex items-center gap-1 rounded-full bg-amber-100 px-2.5 py-0.5 text-xs font-medium text-warning hover:bg-amber-200"
                              onClick={(e) => {
                                e.stopPropagation();
                                setRxPickerFor(i);
                              }}
                            >
                              {t('pos.rx_choose')}
                            </button>
                          ))}
                      </td>
                      <td className="p-2">
                        <Select
                          className="h-8"
                          value={l.packagingId}
                          onChange={(e) => switchUnit(i, e.target.value)}
                        >
                          {l.levels.map((lv) => (
                            <option key={lv.id} value={lv.id}>
                              {lv.name_ar}
                            </option>
                          ))}
                        </Select>
                      </td>
                      <td className="p-2">
                        <div className="flex items-center gap-1">
                          <Button size="sm" variant="outline" onClick={() => bumpQty(i, -1)}>
                            −
                          </Button>
                          <Input
                            className="h-8 w-16 text-center"
                            inputMode="decimal"
                            value={l.qty}
                            onChange={(e) => setQty(i, e.target.value)}
                          />
                          <Button size="sm" variant="outline" onClick={() => bumpQty(i, 1)}>
                            +
                          </Button>
                        </div>
                      </td>
                      <td className="p-2 tabular-nums text-slate-600">{l.unitPrice}</td>
                      <td className="p-2 tabular-nums font-semibold text-slate-800">
                        {Number.isFinite(total) ? total.toFixed(2) : '—'}
                      </td>
                      <td className="p-2">
                        <button
                          type="button"
                          className="text-danger hover:underline"
                          aria-label={t('pos.remove')}
                          onClick={() => removeLine(i)}
                        >
                          ✕
                        </button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </CardContent>
      </Card>

      {/* Customer (optional) — loyalty accrual + purchase history */}
      <CustomerPicker
        customer={customer}
        onPick={setCustomer}
        onClear={() => {
          setCustomer(null);
          setRedeemPoints(0);
        }}
      />

      {/* Loyalty redemption (only when a customer with points is attached) */}
      {customer && customer.loyalty_points > 0 && cart.length > 0 && (
        <div className="flex flex-wrap items-center justify-between gap-3 rounded-[var(--radius-lg)] border border-primary-200 bg-primary-50/50 p-3">
          <Label className="text-sm text-slate-700">
            {t('pos.redeem_points')}{' '}
            <span className="text-xs text-slate-500">
              ({t('customers.balance')}: {customer.loyalty_points})
            </span>
          </Label>
          <div className="flex items-center gap-2">
            <Input
              className="h-9 w-28 text-center tabular-nums"
              inputMode="numeric"
              value={redeemPoints === 0 ? '' : String(redeemPoints)}
              placeholder="0"
              onChange={(e) => {
                const n = Math.floor(Number(e.target.value));
                setRedeemPoints(Number.isFinite(n) && n > 0 ? n : 0);
              }}
            />
            <Button size="sm" variant="outline" onClick={() => setRedeemPoints(maxRedeem)}>
              {t('pos.redeem_max')}
            </Button>
          </div>
        </div>
      )}

      {/* 2D pack-serial capture (optional; EDA track & trace) */}
      {cart.length > 0 && (
        <div className="rounded-[var(--radius-lg)] border border-border bg-white p-3">
          <Label className="text-xs text-slate-500">{t('pos.serials_label')}</Label>
          <div className="mt-1 flex items-center gap-2">
            <Input
              className="h-9 font-mono"
              value={serialScan}
              placeholder={t('pos.serials_placeholder')}
              onChange={(e) => setSerialScan(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') {
                  e.preventDefault();
                  void captureSerial();
                }
              }}
            />
          </div>
          {posSerials.length > 0 && (
            <div className="mt-2 space-y-1">
              <p className="text-xs font-medium text-slate-600">
                {t('pos.serials_captured')}: {posSerials.length}
              </p>
              <div className="flex flex-wrap gap-1.5">
                {posSerials.map((s) => (
                  <span
                    key={s}
                    className="inline-flex items-center gap-1 rounded-full bg-primary-50 px-2 py-0.5 font-mono text-xs text-primary-700"
                  >
                    {s}
                    <button
                      type="button"
                      className="text-slate-400 hover:text-danger"
                      onClick={() => setPosSerials((prev) => prev.filter((x) => x !== s))}
                    >
                      ✕
                    </button>
                  </span>
                ))}
              </div>
            </div>
          )}
          <p className="mt-1 text-xs text-slate-400">{t('pos.serials_hint')}</p>
        </div>
      )}

      {/* Totals + pay */}
      <div className="flex items-center justify-between rounded-[var(--radius-lg)] border border-border bg-white p-4">
        <div className="text-sm text-slate-500">
          {t('pos.items_count')}: <span className="tabular-nums">{cart.length}</span>
        </div>
        <div className="flex items-center gap-4">
          <div className="text-end">
            {effectiveRedeem > 0 && (
              <div className="text-xs text-emerald-700">
                {t('pos.redeem_discount')}: −{effectiveRedeem.toFixed(2)} {currency}
              </div>
            )}
            <div className="text-xl font-bold text-slate-900">
              {t('pos.total')}: <span className="tabular-nums">{netTotal.toFixed(2)}</span>{' '}
              <span className="text-sm font-normal text-slate-500">{currency}</span>
            </div>
            <div className="text-xs text-slate-400">{t('pos.vat_inclusive')}</div>
          </div>
          <Button
            size="lg"
            disabled={cart.length === 0 || hasInvalidQty || hasUnlinkedRx}
            onClick={openPay}
          >
            {t('pos.pay')}
          </Button>
        </div>
      </div>

      {hasUnlinkedRx && (
        <p className="text-center text-xs text-danger">{t('pos.rx_missing_hint')}</p>
      )}
      <p className="text-center text-xs text-slate-400">{t('pos.shortcuts')}</p>

      {payOpen && (
        <PaymentModal
          branchId={branchId}
          cart={cart}
          clientTotal={netTotal}
          redeemPoints={effectiveRedeem}
          serials={posSerials}
          currency={currency}
          customerId={customer?.id ?? null}
          onClose={() => {
            setPayOpen(false);
            focusScan();
          }}
          onDone={(info) => {
            setPayOpen(false);
            setDone(info);
            qc.invalidateQueries({ queryKey: ['cash-current', branchId] }); // drawer summary moved
            void startPrintFlow(info);
          }}
        />
      )}

      {rxPickerFor !== null && cart[rxPickerFor] && (
        <PrescriptionPickerModal
          branchId={branchId}
          medicationId={cart[rxPickerFor].medicationId}
          medName={cart[rxPickerFor].name}
          customerId={customer?.id ?? null}
          onPick={(itemId, remainingHint) => {
            setCart((prev) =>
              prev.map((l, idx) =>
                idx === rxPickerFor
                  ? { ...l, prescriptionItemId: itemId, prescriptionRemainingHint: remainingHint }
                  : l,
              ),
            );
            setRxPickerFor(null);
            focusScan();
          }}
          onClose={() => {
            setRxPickerFor(null);
            focusScan();
          }}
        />
      )}

      {done && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4">
          <div className="w-full max-w-md rounded-[var(--radius-lg)] border border-border bg-white p-8 text-center shadow-xl">
            <div className="mx-auto mb-4 flex h-14 w-14 items-center justify-center rounded-full bg-green-100 text-2xl">
              ✓
            </div>
            <h2 className="mb-2 text-xl font-bold text-slate-900">{t('pos.sale_done')}</h2>
            <p className="mb-1 text-sm text-slate-600">
              {t('pos.invoice_no')}: <span className="font-mono">{done.invoiceNumber}</span>
            </p>
            {done.pointsRedeemed > 0 && (
              <p className="mb-1 text-xs text-emerald-700">
                {t('pos.redeem_discount')}: −{done.discountAmount} {done.currency} (
                {done.pointsRedeemed} {t('customers.points')})
              </p>
            )}
            <p className="mb-1 text-lg font-semibold text-slate-900">
              {t('pos.total')}:{' '}
              <span className="tabular-nums">
                {done.total} {done.currency}
              </span>
            </p>
            {Number(done.taxAmount) > 0 && (
              <p className="mb-1 text-xs text-slate-500">
                {t('pos.vat_included')}:{' '}
                <span className="tabular-nums">
                  {done.taxAmount} {done.currency}
                </span>
              </p>
            )}
            {done.change !== null && (
              <p className="mb-3 text-lg text-primary-700">
                {t('pos.change')}:{' '}
                <span className="tabular-nums font-bold">
                  {done.change} {done.currency}
                </span>
              </p>
            )}
            {done.pointsEarned !== null && done.pointsEarned > 0 && (
              <p className="mb-3 text-sm text-emerald-700">
                ★ {t('pos.points_earned')}:{' '}
                <span className="tabular-nums font-bold">{done.pointsEarned}</span>
              </p>
            )}

            {/* Receipt print status (M9) */}
            <p className="mb-4 min-h-5 text-sm">
              {printState === 'printing' && (
                <span className="text-slate-500">{t('pos.printing')}</span>
              )}
              {printState === 'ok' && <span className="text-success">✓ {t('pos.printed_ok')}</span>}
              {printState === 'unconfigured' && (
                <span className="text-slate-500">{t('pos.printer_missing')}</span>
              )}
              {printState === 'paper' && (
                <span className="text-slate-500">{t('pos.paper_browser')}</span>
              )}
              {printState === 'failed' && (
                <span className="text-danger">{t('pos.print_failed')}</span>
              )}
            </p>
            <div className="mb-3 flex justify-center gap-2">
              {receiptData?.thermal_ready && (
                <Button
                  variant="outline"
                  size="sm"
                  disabled={printState === 'printing'}
                  onClick={() => void thermalPrint(done.invoiceId, { open_drawer: false })}
                >
                  {t('pos.reprint')}
                </Button>
              )}
              <Button variant="outline" size="sm" onClick={() => void browserPrint()}>
                {t('pos.print_browser')}
              </Button>
            </div>
            <Button size="lg" onClick={newSale}>
              {t('pos.new_sale')}
            </Button>
          </div>
        </div>
      )}

      {receiptData && <PrintableReceipt receipt={receiptData} />}
    </div>
  );
}

// --------------------------- printable receipt (M9) ---------------------------

/**
 * Browser-print fallback: hidden on screen, the only visible element in print
 * media (globals.css). Renders the SAME composed receipt the thermal path
 * prints, so the two outputs cannot drift. The QR symbol is thermal-only.
 */
function PrintableReceipt({ receipt }: { receipt: InvoiceReceipt }) {
  const width = receipt.paper_size === '80mm' ? 'max-w-[300px]' : 'max-w-[420px]';
  return (
    <div className={`receipt-print mx-auto ${width} bg-white p-4 text-center text-sm text-black`}>
      <p className="text-lg font-extrabold">{receipt.pharmacy_name}</p>
      <p>{receipt.branch_name}</p>
      {receipt.address && <p>{receipt.address}</p>}
      {receipt.phone && (
        <p>
          {t('receipt.phone')}: <span className="tabular-nums">{receipt.phone}</span>
        </p>
      )}
      {receipt.license_number && (
        <p>
          {t('receipt.license')}: {receipt.license_number}
        </p>
      )}
      {receipt.tax_registration_no && (
        <p>
          {t('receipt.tax_no')}: {receipt.tax_registration_no}
        </p>
      )}
      <hr className="my-2 border-dashed border-black" />
      <div className="flex justify-between text-xs">
        <span>
          {t('receipt.invoice')}: <span className="font-mono">{receipt.invoice_number}</span>
        </span>
        <span className="tabular-nums">{receipt.created_at_display}</span>
      </div>
      <hr className="my-2 border-dashed border-black" />
      <table className="w-full text-xs">
        <tbody>
          {receipt.lines.map((line, i) => (
            <tr key={i}>
              <td className="py-0.5 text-start">
                {line.name}
                <span className="text-[10px] text-slate-600">
                  {' '}
                  ({Number(line.quantity)} × {line.unit_name})
                </span>
              </td>
              <td className="py-0.5 text-end tabular-nums">{line.line_total}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <hr className="my-2 border-dashed border-black" />
      <div className="space-y-0.5 text-xs">
        <div className="flex justify-between">
          <span>{t('receipt.subtotal')}</span>
          <span className="tabular-nums">
            {receipt.subtotal} {receipt.currency_symbol}
          </span>
        </div>
        {Number(receipt.discount) > 0 && (
          <div className="flex justify-between">
            <span>{t('receipt.discount')}</span>
            <span className="tabular-nums">
              {receipt.discount} {receipt.currency_symbol}
            </span>
          </div>
        )}
        {Number(receipt.tax) > 0 && (
          <div className="flex justify-between">
            <span>{t('receipt.tax')}</span>
            <span className="tabular-nums">
              {receipt.tax} {receipt.currency_symbol}
            </span>
          </div>
        )}
        <div className="flex justify-between text-sm font-extrabold">
          <span>{t('receipt.total')}</span>
          <span className="tabular-nums">
            {receipt.total} {receipt.currency_symbol}
          </span>
        </div>
        <div className="flex justify-between">
          <span>{t('receipt.payment')}</span>
          <span>{receipt.payment_method_display}</span>
        </div>
        {receipt.tendered_amount !== null && (
          <div className="flex justify-between">
            <span>{t('receipt.tendered')}</span>
            <span className="tabular-nums">
              {receipt.tendered_amount} {receipt.currency_symbol}
            </span>
          </div>
        )}
        {receipt.change_amount !== null && (
          <div className="flex justify-between">
            <span>{t('receipt.change')}</span>
            <span className="tabular-nums">
              {receipt.change_amount} {receipt.currency_symbol}
            </span>
          </div>
        )}
      </div>
      {receipt.show_pharmacist_signature && (
        <div className="mt-6">
          <p>{'.'.repeat(24)}</p>
          <p className="text-xs">{t('receipt.signature')}</p>
        </div>
      )}
      <p className="mt-4">{receipt.thank_you_message}</p>
      {receipt.return_policy && <p className="mt-1 text-[10px]">{receipt.return_policy}</p>}
    </div>
  );
}

// ------------------------------ customer picker (M5) ------------------------------

function CustomerPicker({
  customer,
  onPick,
  onClear,
}: {
  customer: CustomerSummary | null;
  onPick: (c: CustomerSummary) => void;
  onClear: () => void;
}) {
  const [term, setTerm] = useState('');
  const [open, setOpen] = useState(false);
  const searchQuery = useQuery({
    queryKey: ['pos-customer-search', term],
    queryFn: () => listCustomers({ search: term, activeOnly: true }),
    enabled: open && term.trim().length >= 2,
  });
  const results = searchQuery.data ?? [];

  if (customer) {
    return (
      <div className="flex items-center justify-between rounded-[var(--radius-lg)] border border-primary-200 bg-primary-50/60 p-3">
        <div className="flex flex-wrap items-center gap-2 text-sm">
          <span className="font-semibold text-slate-800">{customer.name}</span>
          {customer.phone && <span className="text-slate-500">{customer.phone}</span>}
          <Badge tone="primary">
            {t('customers.points')}: {customer.loyalty_points}
          </Badge>
        </div>
        <Button size="sm" variant="ghost" onClick={onClear}>
          {t('pos.clear_customer')}
        </Button>
      </div>
    );
  }

  return (
    <div className="rounded-[var(--radius-lg)] border border-border bg-white p-3">
      <Label className="text-xs text-slate-500">{t('pos.customer')}</Label>
      <div className="relative mt-1">
        <Input
          value={term}
          onChange={(e) => {
            setTerm(e.target.value);
            setOpen(true);
          }}
          placeholder={t('pos.search_customer')}
        />
        {open && term.trim().length >= 2 && results.length > 0 && (
          <div className="absolute z-10 mt-1 max-h-48 w-full overflow-y-auto rounded-[var(--radius-md)] border border-border bg-white shadow-lg">
            {results.map((c) => (
              <button
                key={c.id}
                type="button"
                className="flex w-full items-center justify-between px-3 py-2 text-start text-sm hover:bg-primary-50"
                onClick={() => {
                  onPick(c);
                  setTerm('');
                  setOpen(false);
                }}
              >
                <span className="text-slate-800">{c.name}</span>
                <span className="text-xs text-slate-400">{c.phone ?? ''}</span>
              </button>
            ))}
          </div>
        )}
      </div>
      <p className="mt-1 text-xs text-slate-400">{t('pos.walk_in_hint')}</p>
    </div>
  );
}

// ------------------------------ payment modal ------------------------------

function PaymentModal({
  branchId,
  cart,
  clientTotal,
  redeemPoints,
  serials,
  currency,
  customerId,
  onClose,
  onDone,
}: {
  branchId: string;
  cart: CartLine[];
  clientTotal: number;
  redeemPoints: number;
  serials: string[];
  currency: string;
  customerId: string | null;
  onClose: () => void;
  onDone: (info: DoneInfo) => void;
}) {
  const [method, setMethod] = useState<'cash' | 'card'>('cash');
  const [tendered, setTendered] = useState('');

  const saleMut = useMutation({
    mutationFn: () =>
      createPosSale({
        branch_id: branchId,
        lines: cart.map((l) => ({
          medication_id: l.medicationId,
          packaging_id: l.packagingId,
          quantity: String(Number(l.qty)),
          ...(l.prescriptionItemId ? { prescription_item_id: l.prescriptionItemId } : {}),
        })),
        payment_method: method,
        // M10 — persist the customer cash math on the invoice (cash only).
        ...(method === 'cash' && tendered ? { tendered: String(Number(tendered)) } : {}),
        // M5 — attach the customer for loyalty accrual + purchase history.
        ...(customerId ? { customer_id: customerId } : {}),
        // M5 (C3) — redeem loyalty points as a discount (needs a customer).
        ...(customerId && redeemPoints > 0 ? { redeem_points: redeemPoints } : {}),
        // M3 (C2) — scanned 2D pack serials for track & trace.
        ...(serials.length > 0 ? { serials } : {}),
      }),
    onSuccess: (result: PosSaleResult) => {
      // Server change_amount is authoritative (M10); fall back to local math.
      const change =
        method === 'cash'
          ? (result.change_amount ?? (Number(tendered) - Number(result.total)).toFixed(2))
          : null;
      onDone({
        invoiceId: result.invoice_id,
        invoiceNumber: result.invoice_number,
        total: result.total,
        currency: result.currency_code,
        change,
        paymentMethod: method,
        pointsEarned: result.points_earned,
        pointsRedeemed: result.points_redeemed,
        discountAmount: result.discount_amount,
        taxAmount: result.tax_amount,
      });
    },
    onError: (e) => toast.error(t(`errors.${errCode(e)}`)),
  });

  const tenderedNum = Number(tendered);
  const cashShort =
    method === 'cash' && (!Number.isFinite(tenderedNum) || tenderedNum < clientTotal);
  const change = method === 'cash' && !cashShort ? tenderedNum - clientTotal : null;

  return (
    <Modal open onClose={onClose} title={t('pos.payment')}>
      <form
        className="space-y-4"
        onSubmit={(e) => {
          e.preventDefault();
          if (!cashShort && !saleMut.isPending) saleMut.mutate();
        }}
      >
        <p className="text-center text-2xl font-bold text-slate-900">
          <span className="tabular-nums">{clientTotal.toFixed(2)}</span>{' '}
          <span className="text-sm font-normal text-slate-500">{currency}</span>
        </p>

        <div className="space-y-1.5">
          <Label>{t('pos.method')}</Label>
          <div className="flex gap-2">
            <Button
              type="button"
              variant={method === 'cash' ? 'primary' : 'outline'}
              onClick={() => setMethod('cash')}
            >
              {t('pos.cash')}
            </Button>
            <Button
              type="button"
              variant={method === 'card' ? 'primary' : 'outline'}
              onClick={() => setMethod('card')}
            >
              {t('pos.card')}
            </Button>
          </div>
        </div>

        {method === 'cash' && (
          <div className="space-y-1.5">
            <Label>{t('pos.tendered')}</Label>
            <Input
              inputMode="decimal"
              value={tendered}
              onChange={(e) => setTendered(e.target.value)}
              autoFocus
              className="h-12 text-center text-lg tabular-nums"
            />
            {change !== null && (
              <p className="text-sm text-primary-700">
                {t('pos.change')}:{' '}
                <span className="tabular-nums font-bold">{change.toFixed(2)}</span> {currency}
              </p>
            )}
            {cashShort && tendered !== '' && (
              <p className="text-sm text-danger">{t('pos.insufficient_cash')}</p>
            )}
          </div>
        )}

        <div className="flex justify-end gap-2 border-t border-border pt-4">
          <Button type="button" variant="outline" onClick={onClose}>
            {t('users.cancel')}
          </Button>
          <Button type="submit" disabled={cashShort || saleMut.isPending}>
            {saleMut.isPending ? t('pos.completing') : t('pos.complete_sale')}
          </Button>
        </div>
      </form>
    </Modal>
  );
}

// ------------------------------ prescription picker (P2-M8) ------------------------------

/**
 * Two-step picker: (1) an open prescription for this branch/customer, then
 * (2) the ONE item within it that matches the scanned medication and still
 * has remaining quantity. Only pending/partially_fulfilled prescriptions are
 * listed — cancelled/expired/fulfilled ones can never receive a new dispense
 * (sales_service enforces this server-side regardless; this is a UX filter).
 */
function PrescriptionPickerModal({
  branchId,
  medicationId,
  medName,
  customerId,
  onPick,
  onClose,
}: {
  branchId: string;
  medicationId: string;
  medName: string;
  customerId: string | null;
  onPick: (itemId: string, remainingHint: string) => void;
  onClose: () => void;
}) {
  const [openId, setOpenId] = useState<string | null>(null);

  const listQuery = useQuery({
    queryKey: ['pos-rx-open', branchId, customerId],
    queryFn: () => listPrescriptions(branchId, customerId ? { customerId } : {}),
    enabled: !!branchId,
  });
  const openPrescriptions = (listQuery.data ?? []).filter(
    (p) => p.status === 'pending' || p.status === 'partially_fulfilled',
  );

  const detailQuery = useQuery({
    queryKey: ['prescription', openId],
    queryFn: () => getPrescription(openId as string),
    enabled: !!openId,
  });

  return (
    <Modal
      open
      onClose={onClose}
      title={t('pos.rx_picker_title')}
      className="max-h-[80vh] max-w-lg overflow-y-auto"
    >
      <p className="mb-3 text-sm text-slate-600">
        {t('pos.rx_for_item')}: <span className="font-semibold text-slate-800">{medName}</span>
      </p>

      {!openId ? (
        listQuery.isLoading ? (
          <div className="flex justify-center py-8">
            <Spinner />
          </div>
        ) : openPrescriptions.length === 0 ? (
          <p className="py-6 text-center text-sm text-slate-500">{t('pos.rx_none_open')}</p>
        ) : (
          <div className="space-y-1">
            {openPrescriptions.map((p) => (
              <button
                key={p.id}
                type="button"
                className="flex w-full items-center justify-between rounded-[var(--radius-md)] border border-border px-3 py-2 text-start text-sm hover:bg-primary-50"
                onClick={() => setOpenId(p.id)}
              >
                <span className="font-medium text-slate-800">{p.doctor_name}</span>
                <span className="flex items-center gap-2 text-xs text-slate-500">
                  {p.prescription_date}
                  <Badge tone={RX_STATUS_TONE[p.status] ?? 'neutral'}>
                    {t(`prescriptions.status_${p.status}`)}
                  </Badge>
                </span>
              </button>
            ))}
          </div>
        )
      ) : (
        <div className="space-y-3">
          <Button size="sm" variant="ghost" onClick={() => setOpenId(null)}>
            ← {t('pos.rx_back')}
          </Button>
          {detailQuery.isLoading || !detailQuery.data ? (
            <div className="flex justify-center py-8">
              <Spinner />
            </div>
          ) : (
            <div className="space-y-2">
              {detailQuery.data.items.map((item) => {
                const matches = item.medication_id === medicationId;
                const remaining = Number(item.remaining_qty_smallest);
                const selectable = matches && remaining > 0;
                return (
                  <div
                    key={item.id}
                    className={`flex items-center justify-between rounded-[var(--radius-md)] border px-3 py-2 text-sm ${
                      selectable
                        ? 'border-primary-200 bg-primary-50/40'
                        : 'border-border opacity-60'
                    }`}
                  >
                    <div>
                      <div className="font-medium text-slate-800">
                        {item.trade_name_ar ?? item.trade_name}
                      </div>
                      <div className="text-xs text-slate-500">
                        {t('prescriptions.prescribed')}: {item.prescribed_qty}{' '}
                        {item.packaging_name_ar} · {t('prescriptions.remaining')}:{' '}
                        {item.remaining_qty_smallest}
                      </div>
                    </div>
                    {selectable ? (
                      <Button
                        size="sm"
                        onClick={() => onPick(item.id, item.remaining_qty_smallest)}
                      >
                        {t('pos.rx_select')}
                      </Button>
                    ) : (
                      <span className="text-xs text-slate-400">
                        {matches ? t('pos.rx_fulfilled') : t('pos.rx_no_match')}
                      </span>
                    )}
                  </div>
                );
              })}
            </div>
          )}
        </div>
      )}
    </Modal>
  );
}

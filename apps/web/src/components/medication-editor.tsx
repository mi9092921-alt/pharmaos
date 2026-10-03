'use client';

import { Badge, Button, Input, Label, Modal, Select, Spinner } from '@pharmaos/ui';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useState } from 'react';

import {
  addBarcode,
  ApiRequestError,
  deleteBarcode,
  deleteMedication,
  getMedication,
  listUnits,
  type PackagingLevelInput,
  putPackaging,
  updateMedication,
} from '@/lib/api';
import { useAuth } from '@/lib/auth-store';
import { t } from '@/lib/i18n';
import { toast } from '@/lib/toast-store';

const errCode = (e: unknown) => (e instanceof ApiRequestError ? e.code : 'E-SYS-001');
const LEVEL_LABEL: Record<number, string> = {
  1: 'catalog.level_box',
  2: 'catalog.level_strip',
  3: 'catalog.level_tablet',
};
const levelLabel = (n: number) => t(LEVEL_LABEL[n] ?? 'catalog.level');
const BARCODE_TYPES = ['EAN13', 'GS1_DATAMATRIX', 'CODE128'] as const;

interface MedForm {
  trade_name: string;
  trade_name_ar: string;
  scientific_name: string;
  manufacturer: string;
  gtin: string;
  requires_prescription: boolean;
  controlled_substance: boolean;
}

/**
 * Medication detail editor (P1-M7 UI): edits the medication fields, the 1–3
 * packaging levels & their prices (the M5 API writes price history), and the
 * barcodes. Backend permission is authoritative; the caller only opens this for
 * users who hold inventory.edit.
 */
export function MedicationEditor({
  medId,
  open,
  onClose,
}: {
  medId: string;
  open: boolean;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const canDelete = useAuth((s) => s.hasPermission('inventory.delete'));

  const detailQuery = useQuery({
    queryKey: ['medication', medId],
    queryFn: () => getMedication(medId),
    enabled: open,
  });
  const unitsQuery = useQuery({ queryKey: ['units'], queryFn: listUnits, enabled: open });

  const [form, setForm] = useState<MedForm | null>(null);
  const [levels, setLevels] = useState<PackagingLevelInput[]>([]);
  const [newBarcode, setNewBarcode] = useState('');
  const [newBarcodeType, setNewBarcodeType] = useState<string>('EAN13');

  const detail = detailQuery.data;
  useEffect(() => {
    if (!detail) return;
    setForm({
      trade_name: detail.trade_name,
      trade_name_ar: detail.trade_name_ar ?? '',
      scientific_name: detail.scientific_name ?? '',
      manufacturer: detail.manufacturer ?? '',
      gtin: detail.gtin ?? '',
      requires_prescription: detail.requires_prescription,
      controlled_substance: detail.controlled_substance,
    });
    setLevels(
      detail.packaging.map((p) => ({
        level: p.level,
        unit_id: p.unit_id,
        name_ar: p.name_ar,
        qty_in_parent: p.qty_in_parent,
        selling_price: p.selling_price,
        is_sellable: p.is_sellable,
        is_default_sale: p.is_default_sale,
      })),
    );
  }, [detail]);

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ['medication', medId] });
    qc.invalidateQueries({ queryKey: ['medications'] });
  };

  const saveFields = useMutation({
    mutationFn: () =>
      updateMedication(medId, {
        ...form,
        gtin: form?.gtin ? form.gtin : null,
      }),
    onSuccess: () => {
      invalidate();
      toast.success(t('common.saved'));
    },
    onError: (e) => toast.error(t(`errors.${errCode(e)}`)),
  });

  const saveLevels = useMutation({
    mutationFn: () => putPackaging(medId, levels),
    onSuccess: () => {
      invalidate();
      toast.success(t('catalog.saved_ok'));
    },
    onError: (e) => toast.error(t(`errors.${errCode(e)}`)),
  });

  const addBc = useMutation({
    mutationFn: () =>
      addBarcode(medId, {
        barcode: newBarcode.trim(),
        barcode_type: newBarcodeType,
        is_primary: (detail?.barcodes.length ?? 0) === 0,
      }),
    onSuccess: () => {
      setNewBarcode('');
      invalidate();
      toast.success(t('catalog.barcode_added'));
    },
    onError: (e) => toast.error(t(`errors.${errCode(e)}`)),
  });

  const delBc = useMutation({
    mutationFn: (barcodeId: string) => deleteBarcode(medId, barcodeId),
    onSuccess: () => {
      invalidate();
      toast.success(t('catalog.barcode_deleted'));
    },
    onError: (e) => toast.error(t(`errors.${errCode(e)}`)),
  });

  const delMed = useMutation({
    mutationFn: () => deleteMedication(medId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['medications'] });
      toast.success(t('catalog.deleted_ok'));
      onClose();
    },
    onError: (e) => toast.error(t(`errors.${errCode(e)}`)),
  });

  const setLevel = (i: number, patch: Partial<PackagingLevelInput>) =>
    setLevels((prev) => prev.map((l, idx) => (idx === i ? { ...l, ...patch } : l)));

  const addLevel = () => {
    const used = new Set(levels.map((l) => l.level));
    const next = [1, 2, 3].find((n) => !used.has(n)) ?? 3;
    setLevels((prev) => [
      ...prev,
      {
        level: next,
        unit_id: '',
        name_ar: levelLabel(next),
        qty_in_parent: next === 1 ? null : '1',
        selling_price: '0',
        is_sellable: true,
        is_default_sale: prev.length === 0,
      },
    ]);
  };

  const units = unitsQuery.data ?? [];

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={detail ? (detail.trade_name_ar ?? detail.trade_name) : t('catalog.details')}
      className="max-h-[85vh] max-w-3xl overflow-y-auto"
    >
      {detailQuery.isLoading || !form ? (
        <div className="flex justify-center py-10">
          <Spinner />
        </div>
      ) : (
        <div className="space-y-8">
          {/* --- Medication fields --- */}
          <section className="space-y-4">
            <h3 className="text-sm font-bold text-slate-700">{t('catalog.edit_med')}</h3>
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
              <Field label={t('catalog.trade_name_ar')}>
                <Input
                  value={form.trade_name_ar}
                  onChange={(e) => setForm({ ...form, trade_name_ar: e.target.value })}
                />
              </Field>
              <Field label={t('catalog.trade_name')}>
                <Input
                  value={form.trade_name}
                  onChange={(e) => setForm({ ...form, trade_name: e.target.value })}
                />
              </Field>
              <Field label={t('catalog.scientific_name')}>
                <Input
                  value={form.scientific_name}
                  onChange={(e) => setForm({ ...form, scientific_name: e.target.value })}
                />
              </Field>
              <Field label={t('catalog.manufacturer')}>
                <Input
                  value={form.manufacturer}
                  onChange={(e) => setForm({ ...form, manufacturer: e.target.value })}
                />
              </Field>
              <Field label={t('catalog.gtin')}>
                <Input
                  value={form.gtin}
                  inputMode="numeric"
                  maxLength={14}
                  onChange={(e) => setForm({ ...form, gtin: e.target.value })}
                />
              </Field>
              <div className="flex items-end gap-4 pb-1">
                <Toggle
                  label={t('catalog.requires_prescription')}
                  checked={form.requires_prescription}
                  onChange={(v) => setForm({ ...form, requires_prescription: v })}
                />
                <Toggle
                  label={t('catalog.controlled')}
                  checked={form.controlled_substance}
                  onChange={(v) => setForm({ ...form, controlled_substance: v })}
                />
              </div>
            </div>
            <Button size="sm" onClick={() => saveFields.mutate()} disabled={saveFields.isPending}>
              {t('common.save')}
            </Button>
          </section>

          {/* --- Packaging levels & prices --- */}
          <section className="space-y-3">
            <div className="flex items-center justify-between">
              <h3 className="text-sm font-bold text-slate-700">{t('catalog.levels')}</h3>
              <Button size="sm" variant="outline" onClick={addLevel} disabled={levels.length >= 3}>
                {t('catalog.add_level')}
              </Button>
            </div>
            {levels.length === 0 ? (
              <p className="text-sm text-slate-500">{t('catalog.no_levels')}</p>
            ) : (
              <div className="space-y-2">
                {levels.map((lvl, i) => (
                  <div
                    key={i}
                    className="grid grid-cols-2 items-end gap-2 rounded-[var(--radius-md)] border border-border p-3 sm:grid-cols-6"
                  >
                    <Field label={t('catalog.level')}>
                      <Select
                        className="h-9 w-full"
                        value={String(lvl.level)}
                        onChange={(e) => setLevel(i, { level: Number(e.target.value) })}
                      >
                        {[1, 2, 3].map((n) => (
                          <option key={n} value={n}>
                            {levelLabel(n)}
                          </option>
                        ))}
                      </Select>
                    </Field>
                    <Field label={t('catalog.unit')}>
                      <Select
                        className="h-9 w-full"
                        value={lvl.unit_id}
                        onChange={(e) => setLevel(i, { unit_id: e.target.value })}
                      >
                        <option value="" disabled>
                          {t('catalog.select_unit')}
                        </option>
                        {units.map((u) => (
                          <option key={u.id} value={u.id}>
                            {u.name_ar}
                          </option>
                        ))}
                      </Select>
                    </Field>
                    <Field label={t('catalog.qty_in_parent')}>
                      <Input
                        className="h-9"
                        inputMode="decimal"
                        value={lvl.qty_in_parent ?? ''}
                        onChange={(e) => setLevel(i, { qty_in_parent: e.target.value || null })}
                      />
                    </Field>
                    <Field label={t('catalog.price')}>
                      <Input
                        className="h-9"
                        inputMode="decimal"
                        value={lvl.selling_price}
                        onChange={(e) => setLevel(i, { selling_price: e.target.value })}
                      />
                    </Field>
                    <Toggle
                      label={t('catalog.sellable')}
                      checked={lvl.is_sellable}
                      onChange={(v) => setLevel(i, { is_sellable: v })}
                    />
                    <div className="flex items-center justify-between gap-2">
                      <Toggle
                        label={t('catalog.default_sale')}
                        checked={lvl.is_default_sale}
                        onChange={(v) =>
                          setLevels((prev) =>
                            prev.map((l, idx) => ({
                              ...l,
                              is_default_sale: idx === i ? v : false,
                            })),
                          )
                        }
                      />
                      <button
                        type="button"
                        className="text-xs text-danger hover:underline"
                        onClick={() => setLevels((prev) => prev.filter((_, idx) => idx !== i))}
                      >
                        {t('catalog.remove')}
                      </button>
                    </div>
                  </div>
                ))}
              </div>
            )}
            <Button
              size="sm"
              onClick={() => saveLevels.mutate()}
              disabled={saveLevels.isPending || levels.length === 0}
            >
              {t('catalog.save_levels')}
            </Button>
          </section>

          {/* --- Barcodes --- */}
          <section className="space-y-3">
            <h3 className="text-sm font-bold text-slate-700">{t('catalog.barcodes')}</h3>
            <div className="flex flex-wrap gap-2">
              {(detail?.barcodes ?? []).map((b) => (
                <span
                  key={b.id}
                  className="inline-flex items-center gap-2 rounded-full border border-border px-3 py-1 text-xs"
                >
                  <span className="font-mono">{b.barcode}</span>
                  {b.is_primary && <Badge tone="primary">{t('catalog.primary')}</Badge>}
                  <button
                    type="button"
                    className="text-danger hover:underline"
                    onClick={() => delBc.mutate(b.id)}
                  >
                    ✕
                  </button>
                </span>
              ))}
              {(detail?.barcodes ?? []).length === 0 && (
                <span className="text-sm text-slate-500">—</span>
              )}
            </div>
            <form
              className="flex flex-wrap items-end gap-2"
              onSubmit={(e) => {
                e.preventDefault();
                if (newBarcode.trim()) addBc.mutate();
              }}
            >
              <Field label={t('catalog.barcode')}>
                <Input
                  className="h-9 font-mono"
                  value={newBarcode}
                  onChange={(e) => setNewBarcode(e.target.value)}
                />
              </Field>
              <Field label={t('catalog.barcode_type')}>
                <Select
                  className="h-9"
                  value={newBarcodeType}
                  onChange={(e) => setNewBarcodeType(e.target.value)}
                >
                  {BARCODE_TYPES.map((bt) => (
                    <option key={bt} value={bt}>
                      {bt}
                    </option>
                  ))}
                </Select>
              </Field>
              <Button size="sm" type="submit" disabled={addBc.isPending || !newBarcode.trim()}>
                {t('catalog.barcode_add')}
              </Button>
            </form>
          </section>

          <div className="flex justify-between border-t border-border pt-4">
            {canDelete ? (
              <Button variant="danger" size="sm" onClick={() => delMed.mutate()}>
                {t('catalog.delete')}
              </Button>
            ) : (
              <span />
            )}
            <Button variant="outline" size="sm" onClick={onClose}>
              {t('catalog.close')}
            </Button>
          </div>
        </div>
      )}
    </Modal>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex min-w-0 flex-col gap-1.5">
      <Label className="text-xs">{label}</Label>
      {children}
    </div>
  );
}

function Toggle({
  label,
  checked,
  onChange,
}: {
  label: string;
  checked: boolean;
  onChange: (v: boolean) => void;
}) {
  return (
    <label className="flex cursor-pointer items-center gap-2 text-xs text-slate-700">
      <input type="checkbox" checked={checked} onChange={(e) => onChange(e.target.checked)} />
      {label}
    </label>
  );
}

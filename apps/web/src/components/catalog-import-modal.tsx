'use client';

import { Modal, Button, Label, Select, Spinner } from '@pharmaos/ui';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useRef, useState } from 'react';

import {
  ApiRequestError,
  catalogImportTemplateUrl,
  importCatalog,
  listInventoryBranches,
  type CatalogImportReport,
} from '@/lib/api';
import { formatApiErrorMessage, t } from '@/lib/i18n';
import { toast } from '@/lib/toast-store';

const MAX_IMPORT_BYTES = 10 * 1024 * 1024;

/**
 * Catalog import (super_admin only): branch + file, PREVIEW (dry_run) first —
 * the button that writes stays disabled until one preview succeeded — then
 * apply. Follows the license-upload file-input and medication-editor modal
 * conventions (CLAUDE.md UX rule: never the browser's default dialogs).
 */
export function CatalogImportModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const qc = useQueryClient();
  const fileRef = useRef<HTMLInputElement>(null);
  const [file, setFile] = useState<File | null>(null);
  const [branchId, setBranchId] = useState('');
  const [report, setReport] = useState<CatalogImportReport | null>(null);
  const [showHelp, setShowHelp] = useState(false);
  // One successful execution per file — re-running the same file would double
  // the batch quantities (the DB deliberately has no batch-number uniqueness).
  const [applied, setApplied] = useState(false);

  const branchesQuery = useQuery({
    queryKey: ['inv-branches'],
    queryFn: listInventoryBranches,
    enabled: open,
  });
  const branches = branchesQuery.data ?? [];
  const effectiveBranch = branchId || branches[0]?.id || '';

  const previewMut = useMutation({
    mutationFn: () => importCatalog(file as File, effectiveBranch, true),
    onSuccess: (data) => {
      setReport(data);
      if (data.rejected.length === 0) toast.success(t('catalog.import_preview') + ' ✓');
    },
    onError: (e) =>
      toast.error(e instanceof ApiRequestError ? t(`errors.${e.code}`) : formatApiErrorMessage(e)),
  });

  const applyMut = useMutation({
    mutationFn: () => importCatalog(file as File, effectiveBranch, false),
    onSuccess: (data) => {
      setReport(data);
      setApplied(true);
      toast.success(t('catalog.import_result_ok'));
      qc.invalidateQueries({ queryKey: ['medications'] });
      qc.invalidateQueries({ queryKey: ['inventory'] });
      qc.invalidateQueries({ queryKey: ['inv-branches'] });
    },
    onError: (e) =>
      toast.error(e instanceof ApiRequestError ? t(`errors.${e.code}`) : formatApiErrorMessage(e)),
  });

  const onFile = (e: React.ChangeEvent<HTMLInputElement>) => {
    const chosen = e.target.files?.[0];
    e.target.value = '';
    if (!chosen) return;
    if (chosen.size > MAX_IMPORT_BYTES) {
      toast.error(t('errors.E-VAL-001'));
      return;
    }
    setFile(chosen);
    setReport(null); // a new file invalidates the previous preview
    setApplied(false);
  };

  const busy = previewMut.isPending || applyMut.isPending;
  // Execute stays locked until a PREVIEW of the CURRENT file succeeded (and
  // only once per file) — never a blind write.
  const canApply = report !== null && report.dry_run === true && !applied;
  const canPreview = file !== null && effectiveBranch !== '';

  const close = () => {
    if (busy) return;
    setFile(null);
    setReport(null);
    onClose();
  };

  return (
    <Modal open={open} onClose={close} title={t('catalog.import_title')} className="max-w-2xl">
      <div className="space-y-4">
        <button
          type="button"
          className="text-sm font-medium text-primary-600 hover:underline"
          onClick={() => setShowHelp((v) => !v)}
        >
          {showHelp ? '▾' : '▸'} {t('catalog.import_help_title')}
        </button>
        {showHelp && (
          <div className="space-y-2 rounded-[var(--radius-md)] bg-slate-50 p-3 text-sm leading-6 text-slate-700">
            <p>{t('catalog.import_help_body')}</p>
            <a
              href={catalogImportTemplateUrl()}
              className="inline-block font-medium text-primary-600 hover:underline"
              onClick={(e) => e.stopPropagation()}
            >
              {t('catalog.import_template')} ↓
            </a>
          </div>
        )}

        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
          <div className="flex flex-col gap-1.5">
            <Label>{t('catalog.import_branch')}</Label>
            <Select value={effectiveBranch} onChange={(e) => setBranchId(e.target.value)}>
              {branches.length === 0 && <option value="">…</option>}
              {branches.map((b) => (
                <option key={b.id} value={b.id}>
                  {b.name}
                </option>
              ))}
            </Select>
          </div>
          <div className="flex flex-col gap-1.5">
            <Label>{t('catalog.import_pick_file')}</Label>
            <div className="flex items-center gap-2">
              <input
                ref={fileRef}
                type="file"
                accept=".xlsx,.xlsm,.csv"
                className="hidden"
                onChange={onFile}
              />
              <Button
                type="button"
                variant="outline"
                onClick={() => fileRef.current?.click()}
                disabled={busy}
              >
                {t('catalog.import_pick_file')}
              </Button>
              {file && (
                <span className="truncate text-xs text-slate-500" title={file.name}>
                  {file.name}
                </span>
              )}
            </div>
          </div>
        </div>

        {report && (
          <div className="space-y-3 rounded-[var(--radius-md)] border border-border p-3 text-sm">
            {report.dry_run && (
              <p className="rounded-[var(--radius-sm)] bg-amber-50 p-2 text-xs text-amber-800">
                {t('catalog.import_dry_note')}
              </p>
            )}
            <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
              {(report.dry_run ? report.to_create : report.created) !== undefined && (
                <Stat
                  label={t('catalog.import_will_create')}
                  value={String(report.dry_run ? report.to_create : report.created)}
                />
              )}
              {(report.dry_run ? report.to_update : report.updated) !== undefined && (
                <Stat
                  label={t('catalog.import_will_update')}
                  value={String(report.dry_run ? report.to_update : report.updated)}
                />
              )}
              <Stat label={t('catalog.import_batches')} value={String(report.batches)} />
              <Stat label={t('catalog.import_qty_total')} value={report.quantity_total} />
            </div>
            {report.rejected.length > 0 && (
              <div>
                <p className="mb-1 font-medium text-danger-600">
                  {t('catalog.import_rejected')} ({report.rejected.length}
                  {report.rejected.length >= 50 ? '+' : ''})
                </p>
                <div className="max-h-48 overflow-y-auto rounded-[var(--radius-sm)] border border-border">
                  <table className="w-full text-xs">
                    <thead>
                      <tr className="border-b border-border text-slate-500">
                        <th className="p-1.5 text-start">{t('catalog.import_row')}</th>
                        <th className="p-1.5 text-start">{t('catalog.import_name')}</th>
                        <th className="p-1.5 text-start"></th>
                      </tr>
                    </thead>
                    <tbody>
                      {report.rejected.map((r, i) => (
                        <tr key={`${r.row}-${i}`} className="border-b border-border/60">
                          <td className="p-1.5" dir="ltr">
                            {r.row}
                          </td>
                          <td className="max-w-40 truncate p-1.5">{r.trade_name ?? '—'}</td>
                          <td className="p-1.5 text-slate-600">
                            {t(`catalog.reject.${r.reason}`)}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            )}
          </div>
        )}

        <div className="flex items-center justify-end gap-2">
          {busy && <Spinner />}
          <Button variant="outline" onClick={close} disabled={busy}>
            {t('users.cancel')}
          </Button>
          <Button
            variant="outline"
            onClick={() => previewMut.mutate()}
            disabled={!canPreview || busy}
          >
            {previewMut.isPending ? t('catalog.import_previewing') : t('catalog.import_preview')}
          </Button>
          <Button onClick={() => applyMut.mutate()} disabled={!canApply || busy}>
            {applyMut.isPending ? t('catalog.import_applying') : t('catalog.import_apply')}
          </Button>
        </div>
      </div>
    </Modal>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-[var(--radius-sm)] bg-slate-50 p-2">
      <p className="text-xs text-slate-500">{label}</p>
      <p className="font-semibold text-slate-900" dir="ltr">
        {value}
      </p>
    </div>
  );
}

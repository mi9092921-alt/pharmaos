'use client';

import { Badge, Button, Card, CardContent, Input, Label, QueryState, Select } from '@pharmaos/ui';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';

import { CatalogImportModal } from '@/components/catalog-import-modal';
import { MedicationEditor } from '@/components/medication-editor';
import { ApiRequestError, apiFetch, catalogExportUrl, listInventoryBranches } from '@/lib/api';
import { useAuth } from '@/lib/auth-store';
import { formatApiErrorMessage, t } from '@/lib/i18n';
import { toast } from '@/lib/toast-store';

interface Med {
  id: string;
  trade_name: string;
  trade_name_ar: string | null;
  scientific_name: string | null;
  manufacturer: string | null;
  requires_prescription: boolean;
  controlled_substance: boolean;
}

const EMPTY = { trade_name: '', trade_name_ar: '', scientific_name: '', manufacturer: '' };

export default function CatalogPage() {
  const qc = useQueryClient();
  const canAdd = useAuth((s) => s.hasPermission('inventory.add'));
  const canEdit = useAuth((s) => s.hasPermission('inventory.edit'));
  // استيراد/تصدير الكاتلوج — مالك النظام فقط (catalog.import / catalog.export)
  const canImport = useAuth((s) => s.hasPermission('catalog.import'));
  const canExport = useAuth((s) => s.hasPermission('catalog.export'));
  const [search, setSearch] = useState('');
  const [query, setQuery] = useState('');
  const [showForm, setShowForm] = useState(false);
  const [form, setForm] = useState(EMPTY);
  const [editId, setEditId] = useState<string | null>(null);
  const [showImport, setShowImport] = useState(false);
  const [exportBranch, setExportBranch] = useState(''); // '' = pure catalog

  const medsQuery = useQuery({
    queryKey: ['medications', query],
    queryFn: () =>
      apiFetch<Med[]>(
        `/api/v1/medications?limit=50${query ? `&search=${encodeURIComponent(query)}` : ''}`,
      ),
  });

  const exportBranchesQuery = useQuery({
    queryKey: ['inv-branches'],
    queryFn: listInventoryBranches,
    enabled: canExport,
  });
  const exportBranches = exportBranchesQuery.data ?? [];

  const createMut = useMutation({
    mutationFn: () =>
      apiFetch<Med>('/api/v1/medications', { method: 'POST', body: JSON.stringify(form) }),
    onSuccess: () => {
      setShowForm(false);
      setForm(EMPTY);
      toast.success(t('catalog.created_ok'));
      qc.invalidateQueries({ queryKey: ['medications'] });
    },
    onError: (e) => toast.error(t(`errors.${e instanceof ApiRequestError ? e.code : 'E-SYS-001'}`)),
  });

  return (
    <div className="mx-auto max-w-5xl space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-2xl font-bold text-slate-900">{t('catalog.title')}</h1>
        <div className="flex flex-wrap items-center gap-2">
          {/* استيراد/تصدير — تظهران لمالك النظام فقط عبر catalog.import/export */}
          {canExport && (
            <>
              <Select
                className="h-9 w-52"
                value={exportBranch}
                onChange={(e) => setExportBranch(e.target.value)}
                aria-label={t('catalog.export_branch')}
              >
                <option value="">{t('catalog.export_all')}</option>
                {exportBranches.map((b) => (
                  <option key={b.id} value={b.id}>
                    {b.name}
                  </option>
                ))}
              </Select>
              <a
                href={catalogExportUrl(exportBranch || undefined)}
                className={
                  'rounded-[var(--radius-md)] border border-border px-3 py-2 text-sm font-medium ' +
                  'text-primary-600 hover:bg-primary-50'
                }
              >
                {t('catalog.export')} ↓
              </a>
            </>
          )}
          {canImport && (
            <Button variant="outline" onClick={() => setShowImport(true)}>
              {t('catalog.import')}
            </Button>
          )}
          {canAdd && (
            <Button onClick={() => setShowForm((v) => !v)}>
              {showForm ? t('users.cancel') : t('catalog.add')}
            </Button>
          )}
        </div>
      </div>
      {canExport && exportBranches.length === 0 && (
        <p className="text-xs text-slate-500">{t('catalog.export_no_branch')}</p>
      )}

      {/* Arabic search — Enter submits; same normalization applied server-side */}
      <form
        onSubmit={(e) => {
          e.preventDefault();
          setQuery(search.trim());
        }}
      >
        <Input
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder={t('catalog.search')}
          autoFocus
        />
      </form>

      {showForm && (
        <Card>
          <CardContent className="pt-6">
            <form
              className="grid grid-cols-1 gap-4 sm:grid-cols-2"
              onSubmit={(e) => {
                e.preventDefault();
                createMut.mutate();
              }}
            >
              {(
                [
                  ['trade_name', t('catalog.trade_name'), true],
                  ['trade_name_ar', t('catalog.trade_name_ar'), false],
                  ['scientific_name', t('catalog.scientific_name'), false],
                  ['manufacturer', t('catalog.manufacturer'), false],
                ] as const
              ).map(([key, label, required]) => (
                <div key={key} className="flex flex-col gap-1.5">
                  <Label>{label}</Label>
                  <Input
                    value={form[key]}
                    required={required}
                    onChange={(e) => setForm({ ...form, [key]: e.target.value })}
                  />
                </div>
              ))}
              <div className="flex items-end">
                <Button type="submit" disabled={createMut.isPending}>
                  {createMut.isPending ? t('users.creating') : t('users.create')}
                </Button>
              </div>
            </form>
          </CardContent>
        </Card>
      )}

      <Card>
        <CardContent className="pt-6">
          <QueryState
            isLoading={medsQuery.isLoading}
            isError={medsQuery.isError}
            onRetry={() => void medsQuery.refetch()}
            errorText={medsQuery.error ? formatApiErrorMessage(medsQuery.error) : ''}
            retryLabel={t('common.retry')}
            isEmpty={(medsQuery.data ?? []).length === 0}
            emptyText={t('catalog.empty')}
          >
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-border text-xs text-slate-500">
                  <th className="p-2 text-start">{t('catalog.trade_name_ar')}</th>
                  <th className="p-2 text-start">{t('catalog.trade_name')}</th>
                  <th className="p-2 text-start">{t('catalog.scientific_name')}</th>
                  <th className="p-2 text-start">{t('catalog.manufacturer')}</th>
                  <th className="p-2 text-start"></th>
                  <th className="p-2 text-start">{t('catalog.actions')}</th>
                </tr>
              </thead>
              <tbody>
                {(medsQuery.data ?? []).map((m) => (
                  <tr key={m.id} className="border-b border-border/60">
                    <td className="p-2 font-medium text-slate-800">{m.trade_name_ar ?? '—'}</td>
                    <td className="p-2 text-slate-700">{m.trade_name}</td>
                    <td className="p-2 text-slate-600">{m.scientific_name ?? '—'}</td>
                    <td className="p-2 text-slate-600">{m.manufacturer ?? '—'}</td>
                    <td className="p-2">
                      {m.controlled_substance && (
                        <Badge tone="danger">{t('catalog.controlled')}</Badge>
                      )}{' '}
                      {m.requires_prescription && (
                        <Badge tone="warning">{t('catalog.requires_prescription')}</Badge>
                      )}
                    </td>
                    <td className="p-2">
                      {canEdit && (
                        <Button size="sm" variant="outline" onClick={() => setEditId(m.id)}>
                          {t('catalog.edit')}
                        </Button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </QueryState>
        </CardContent>
      </Card>

      {editId && <MedicationEditor medId={editId} open onClose={() => setEditId(null)} />}
      {canImport && <CatalogImportModal open={showImport} onClose={() => setShowImport(false)} />}
    </div>
  );
}

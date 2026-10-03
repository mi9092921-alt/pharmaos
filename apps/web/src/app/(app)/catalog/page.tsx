'use client';

import { Badge, Button, Card, CardContent, Input, Label, Spinner } from '@pharmaos/ui';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';

import { MedicationEditor } from '@/components/medication-editor';
import { ApiRequestError, apiFetch } from '@/lib/api';
import { useAuth } from '@/lib/auth-store';
import { t } from '@/lib/i18n';
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
  const [search, setSearch] = useState('');
  const [query, setQuery] = useState('');
  const [showForm, setShowForm] = useState(false);
  const [form, setForm] = useState(EMPTY);
  const [editId, setEditId] = useState<string | null>(null);

  const medsQuery = useQuery({
    queryKey: ['medications', query],
    queryFn: () =>
      apiFetch<Med[]>(
        `/api/v1/medications?limit=50${query ? `&search=${encodeURIComponent(query)}` : ''}`,
      ),
  });

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
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold text-slate-900">{t('catalog.title')}</h1>
        {canAdd && (
          <Button onClick={() => setShowForm((v) => !v)}>
            {showForm ? t('users.cancel') : t('catalog.add')}
          </Button>
        )}
      </div>

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
          {medsQuery.isLoading ? (
            <div className="flex justify-center py-8">
              <Spinner />
            </div>
          ) : (medsQuery.data ?? []).length === 0 ? (
            <p className="py-6 text-center text-sm text-slate-500">{t('catalog.empty')}</p>
          ) : (
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
          )}
        </CardContent>
      </Card>

      {editId && <MedicationEditor medId={editId} open onClose={() => setEditId(null)} />}
    </div>
  );
}

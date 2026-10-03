'use client';

import {
  Button,
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  Input,
  Label,
  Select,
  Spinner,
} from '@pharmaos/ui';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useState } from 'react';

import {
  ApiRequestError,
  getSettings,
  getTaxProfile,
  listBranches,
  putSettings,
  updateTaxProfile,
  type BranchSettings,
} from '@/lib/api';
import { useAuth } from '@/lib/auth-store';
import { t } from '@/lib/i18n';
import { toast } from '@/lib/toast-store';

type Form = Omit<BranchSettings, 'id' | 'branch_id'>;

const BLANK: Form = {
  pharmacy_name: '',
  pharmacy_logo: null,
  license_number: '',
  address: '',
  phone: '',
  tax_registration_no: '',
  return_policy: '',
  thank_you_message: '',
  paper_size: '80mm',
  show_pharmacist_signature: false,
  show_qr_code: false,
  max_discount_percent: '0',
};

export default function SettingsPage() {
  const qc = useQueryClient();
  const canEdit = useAuth((s) => s.hasPermission('settings.edit'));
  const [form, setForm] = useState<Form>(BLANK);

  const branchesQuery = useQuery({ queryKey: ['branches'], queryFn: listBranches });
  const branch = branchesQuery.data?.[0]; // Phase 1: single branch
  const branchId = branch?.id;

  const settingsQuery = useQuery({
    queryKey: ['settings', branchId],
    queryFn: () => getSettings(branchId as string),
    enabled: !!branchId,
  });

  useEffect(() => {
    if (settingsQuery.data) {
      const { id: _id, branch_id: _b, ...rest } = settingsQuery.data;
      setForm({ ...BLANK, ...rest });
    }
  }, [settingsQuery.data]);

  const saveMut = useMutation({
    mutationFn: () => putSettings(branchId as string, form as unknown as Record<string, unknown>),
    onSuccess: () => {
      toast.success(t('common.saved'));
      qc.invalidateQueries({ queryKey: ['settings', branchId] });
    },
    onError: (e) => toast.error(t(`errors.${e instanceof ApiRequestError ? e.code : 'E-SYS-001'}`)),
  });

  if (branchesQuery.isLoading) {
    return (
      <div className="flex justify-center py-12">
        <Spinner />
      </div>
    );
  }
  if (!branch) {
    return <p className="text-slate-500">{t('settings.no_branch')}</p>;
  }

  const set = <K extends keyof Form>(k: K, v: Form[K]) => setForm((f) => ({ ...f, [k]: v }));

  return (
    <div className="mx-auto max-w-3xl space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold text-slate-900">{t('settings.title')}</h1>
        {!canEdit && <span className="text-sm text-warning">{t('settings.readonly')}</span>}
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">{t('settings.branch')}</CardTitle>
        </CardHeader>
        <CardContent className="grid grid-cols-2 gap-4 pt-0 text-sm">
          <Info label={t('settings.branch_name')} value={branch.name} />
          <Info label={t('settings.currency')} value={branch.currency_code} />
          <Info label={t('settings.country')} value={branch.country_code} />
        </CardContent>
      </Card>

      <form
        onSubmit={(e) => {
          e.preventDefault();
          saveMut.mutate();
        }}
      >
        <Card>
          <CardHeader>
            <CardTitle className="text-base">{t('settings.invoice_template')}</CardTitle>
          </CardHeader>
          <CardContent className="grid grid-cols-1 gap-4 pt-0 sm:grid-cols-2">
            <FieldInput
              label={t('settings.pharmacy_name')}
              value={form.pharmacy_name}
              onChange={(v) => set('pharmacy_name', v)}
              disabled={!canEdit}
              required
            />
            <FieldInput
              label={t('settings.license_number')}
              value={form.license_number ?? ''}
              onChange={(v) => set('license_number', v)}
              disabled={!canEdit}
            />
            <FieldInput
              label={t('settings.address')}
              value={form.address ?? ''}
              onChange={(v) => set('address', v)}
              disabled={!canEdit}
            />
            <FieldInput
              label={t('settings.phone')}
              value={form.phone ?? ''}
              onChange={(v) => set('phone', v)}
              disabled={!canEdit}
              numeric
            />
            <FieldInput
              label={t('settings.tax_registration_no')}
              value={form.tax_registration_no ?? ''}
              onChange={(v) => set('tax_registration_no', v)}
              disabled={!canEdit}
              numeric
            />
            <FieldInput
              label={t('settings.thank_you_message')}
              value={form.thank_you_message ?? ''}
              onChange={(v) => set('thank_you_message', v)}
              disabled={!canEdit}
            />
            <div className="flex flex-col gap-1.5">
              <Label>{t('settings.paper_size')}</Label>
              <Select
                value={form.paper_size}
                disabled={!canEdit}
                onChange={(e) => set('paper_size', e.target.value as Form['paper_size'])}
              >
                <option value="80mm">80mm</option>
                <option value="A4">A4</option>
                <option value="A5">A5</option>
              </Select>
            </div>
          </CardContent>
        </Card>

        <Card className="mt-6">
          <CardHeader>
            <CardTitle className="text-base">{t('settings.pos_options')}</CardTitle>
          </CardHeader>
          <CardContent className="grid grid-cols-1 gap-4 pt-0 sm:grid-cols-2">
            <FieldInput
              label={t('settings.max_discount')}
              value={form.max_discount_percent}
              onChange={(v) => set('max_discount_percent', v)}
              disabled={!canEdit}
              numeric
            />
            <Checkbox
              label={t('settings.show_qr_code')}
              checked={form.show_qr_code}
              onChange={(v) => set('show_qr_code', v)}
              disabled={!canEdit}
            />
            <Checkbox
              label={t('settings.show_signature')}
              checked={form.show_pharmacist_signature}
              onChange={(v) => set('show_pharmacist_signature', v)}
              disabled={!canEdit}
            />
          </CardContent>
        </Card>

        {canEdit && (
          <div className="mt-6 flex justify-end">
            <Button type="submit" disabled={saveMut.isPending}>
              {t('common.save')}
            </Button>
          </div>
        )}
      </form>

      <TaxProfileCard branchId={branch.id} canEdit={canEdit} />
    </div>
  );
}

function TaxProfileCard({ branchId, canEdit }: { branchId: string; canEdit: boolean }) {
  const qc = useQueryClient();
  const query = useQuery({
    queryKey: ['tax-profile', branchId],
    queryFn: () => getTaxProfile(branchId),
  });

  const [name, setName] = useState('');
  const [vatRate, setVatRate] = useState('');
  const [medicineRate, setMedicineRate] = useState('');
  const [einvoice, setEinvoice] = useState('');
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    if (!loaded && query.data) {
      setName(query.data.name);
      setVatRate(query.data.vat_rate);
      setMedicineRate(query.data.medicine_vat_rate ?? '');
      setEinvoice(query.data.einvoice_system ?? '');
      setLoaded(true);
    }
  }, [loaded, query.data]);

  const saveMut = useMutation({
    mutationFn: () =>
      updateTaxProfile(query.data?.id ?? '', {
        name: name.trim(),
        vat_rate: vatRate.trim(),
        medicine_vat_rate: medicineRate.trim() || null,
        einvoice_system: einvoice || null,
      }),
    onSuccess: () => {
      toast.success(t('common.saved'));
      qc.invalidateQueries({ queryKey: ['tax-profile', branchId] });
    },
    onError: (e) => toast.error(t(`errors.${e instanceof ApiRequestError ? e.code : 'E-SYS-001'}`)),
  });

  if (query.isLoading) return null;
  const profile = query.data;

  return (
    <Card className="mt-6">
      <CardHeader>
        <CardTitle className="text-base">{t('settings.tax_profile')}</CardTitle>
      </CardHeader>
      <CardContent className="pt-0">
        {!profile ? (
          <p className="text-sm text-slate-500">{t('settings.tax_none')}</p>
        ) : (
          <form
            className="grid grid-cols-1 gap-4 sm:grid-cols-2"
            onSubmit={(e) => {
              e.preventDefault();
              if (name.trim() && vatRate.trim()) saveMut.mutate();
            }}
          >
            <FieldInput
              label={t('settings.tax_name')}
              value={name}
              onChange={setName}
              disabled={!canEdit}
              required
            />
            <FieldInput
              label={t('settings.vat_rate')}
              value={vatRate}
              onChange={setVatRate}
              disabled={!canEdit}
              numeric
              required
            />
            <FieldInput
              label={t('settings.medicine_vat_rate')}
              value={medicineRate}
              onChange={setMedicineRate}
              disabled={!canEdit}
              numeric
            />
            <div className="flex flex-col gap-1.5">
              <Label>{t('settings.einvoice_system')}</Label>
              <Select
                value={einvoice}
                disabled={!canEdit}
                onChange={(e) => setEinvoice(e.target.value)}
              >
                <option value="">{t('settings.einvoice_none')}</option>
                <option value="eta_ereceipt">{t('settings.einvoice_eta')}</option>
                <option value="zatca">{t('settings.einvoice_zatca')}</option>
              </Select>
            </div>
            <p className="text-xs text-slate-500 sm:col-span-2">
              {t('settings.medicine_vat_hint')}
            </p>
            {canEdit && (
              <div className="flex justify-end sm:col-span-2">
                <Button type="submit" disabled={saveMut.isPending}>
                  {t('common.save')}
                </Button>
              </div>
            )}
          </form>
        )}
      </CardContent>
    </Card>
  );
}

function Info({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div className="text-xs text-slate-500">{label}</div>
      <div className="font-medium text-slate-800">{value}</div>
    </div>
  );
}

function FieldInput({
  label,
  value,
  onChange,
  disabled,
  required,
  numeric,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  disabled?: boolean;
  required?: boolean;
  numeric?: boolean;
}) {
  return (
    <div className="flex flex-col gap-1.5">
      <Label>{label}</Label>
      <Input
        value={value}
        onChange={(e) => onChange(e.target.value)}
        disabled={disabled}
        required={required}
        className={numeric ? 'numeric' : undefined}
      />
    </div>
  );
}

function Checkbox({
  label,
  checked,
  onChange,
  disabled,
}: {
  label: string;
  checked: boolean;
  onChange: (v: boolean) => void;
  disabled?: boolean;
}) {
  return (
    <label className="flex items-center gap-2 text-sm text-slate-700">
      <input
        type="checkbox"
        checked={checked}
        disabled={disabled}
        onChange={(e) => onChange(e.target.checked)}
        className="size-4"
      />
      {label}
    </label>
  );
}

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
  createExpense,
  createExpenseCategory,
  deleteExpense,
  type ExpenseCategory,
  type ExpensePaymentMethod,
  type ExpenseRow,
  listExpenseCategories,
  listExpenses,
  listInventoryBranches,
  updateExpense,
  updateExpenseCategory,
} from '@/lib/api';
import { useAuth } from '@/lib/auth-store';
import { t } from '@/lib/i18n';
import { toast } from '@/lib/toast-store';

const errCode = (e: unknown) => (e instanceof ApiRequestError ? e.code : 'E-SYS-001');
const onErr = (e: unknown) => toast.error(t(`errors.${errCode(e)}`));

const PAYMENT_METHODS: ExpensePaymentMethod[] = ['cash', 'card', 'bank_transfer'];

export default function FinancePage() {
  const [tab, setTab] = useState<'expenses' | 'categories'>('expenses');

  return (
    <div className="mx-auto max-w-5xl space-y-6">
      <h1 className="text-2xl font-bold text-slate-900">{t('finance.title')}</h1>

      <div className="flex gap-1 border-b border-border">
        <TabButton active={tab === 'expenses'} onClick={() => setTab('expenses')}>
          {t('finance.tab_expenses')}
        </TabButton>
        <TabButton active={tab === 'categories'} onClick={() => setTab('categories')}>
          {t('finance.tab_categories')}
        </TabButton>
      </div>

      {tab === 'expenses' ? <ExpensesTab /> : <CategoriesTab />}
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

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex flex-col gap-1.5">
      <Label>{label}</Label>
      {children}
    </div>
  );
}

// ================================ Expenses ================================

function ExpensesTab() {
  const canManage = useAuth((s) => s.hasPermission('finance.expenses'));
  const qc = useQueryClient();

  const [branchId, setBranchId] = useState('');
  const [categoryFilter, setCategoryFilter] = useState('');
  const [dateFrom, setDateFrom] = useState('');
  const [dateTo, setDateTo] = useState('');
  const [showCreate, setShowCreate] = useState(false);
  const [editTarget, setEditTarget] = useState<ExpenseRow | null>(null);

  const branchesQuery = useQuery({ queryKey: ['inv-branches'], queryFn: listInventoryBranches });
  const branches = branchesQuery.data ?? [];
  useEffect(() => {
    const first = branches[0];
    if (!branchId && first) setBranchId(first.id);
  }, [branches, branchId]);

  const categoriesQuery = useQuery({
    queryKey: ['expense-categories', true],
    queryFn: () => listExpenseCategories({ activeOnly: true }),
  });
  const categories = categoriesQuery.data ?? [];

  const listQuery = useQuery({
    queryKey: ['expenses', branchId, categoryFilter, dateFrom, dateTo],
    queryFn: () =>
      listExpenses(branchId, {
        categoryId: categoryFilter || undefined,
        dateFrom: dateFrom || undefined,
        dateTo: dateTo || undefined,
      }),
    enabled: !!branchId,
  });
  const rows = listQuery.data ?? [];
  const total = rows.reduce((sum, r) => sum + Number(r.amount), 0);
  const currency = rows[0]?.currency_code ?? branches.find((b) => b.id === branchId)?.currency_code;

  const invalidate = () => qc.invalidateQueries({ queryKey: ['expenses', branchId] });

  const deleteMut = useMutation({
    mutationFn: (id: string) => deleteExpense(id),
    onSuccess: () => {
      invalidate();
      toast.success(t('finance.expense_deleted_ok'));
    },
    onError: onErr,
  });

  const clearFilters = () => {
    setCategoryFilter('');
    setDateFrom('');
    setDateTo('');
  };

  if (branchesQuery.isLoading) {
    return (
      <div className="flex justify-center py-16">
        <Spinner />
      </div>
    );
  }
  if (branches.length === 0) {
    return <p className="py-10 text-center text-slate-500">{t('finance.no_branch')}</p>;
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div className="flex flex-wrap items-end gap-3">
          <div className="space-y-1">
            <Label className="text-xs">{t('inventory.branch')}</Label>
            <Select className="h-9" value={branchId} onChange={(e) => setBranchId(e.target.value)}>
              {branches.map((b) => (
                <option key={b.id} value={b.id}>
                  {b.name}
                </option>
              ))}
            </Select>
          </div>
          <div className="space-y-1">
            <Label className="text-xs">{t('finance.category')}</Label>
            <Select
              className="h-9 w-44"
              value={categoryFilter}
              onChange={(e) => setCategoryFilter(e.target.value)}
            >
              <option value="">{t('finance.category_filter_all')}</option>
              {categories.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.name_ar}
                </option>
              ))}
            </Select>
          </div>
          <div className="space-y-1">
            <Label className="text-xs">{t('finance.date_from')}</Label>
            <Input
              className="h-9"
              type="date"
              value={dateFrom}
              onChange={(e) => setDateFrom(e.target.value)}
            />
          </div>
          <div className="space-y-1">
            <Label className="text-xs">{t('finance.date_to')}</Label>
            <Input
              className="h-9"
              type="date"
              value={dateTo}
              onChange={(e) => setDateTo(e.target.value)}
            />
          </div>
          {(categoryFilter || dateFrom || dateTo) && (
            <Button size="sm" variant="ghost" onClick={clearFilters}>
              {t('finance.clear_filters')}
            </Button>
          )}
        </div>
        {canManage && (
          <Button
            onClick={() => setShowCreate(true)}
            disabled={categories.length === 0 && !categoriesQuery.isLoading}
          >
            {t('finance.add_expense')}
          </Button>
        )}
      </div>

      {canManage && categories.length === 0 && !categoriesQuery.isLoading && (
        <p className="text-xs text-amber-700">{t('finance.no_active_categories')}</p>
      )}

      <Card>
        <CardContent className="pt-6">
          {listQuery.isLoading ? (
            <div className="flex justify-center py-8">
              <Spinner />
            </div>
          ) : rows.length === 0 ? (
            <p className="py-6 text-center text-sm text-slate-500">{t('finance.expenses_empty')}</p>
          ) : (
            <>
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-border text-xs text-slate-500">
                    <th className="p-2 text-start">{t('finance.date')}</th>
                    <th className="p-2 text-start">{t('finance.category')}</th>
                    <th className="p-2 text-start">{t('finance.description')}</th>
                    <th className="p-2 text-start">{t('finance.payment_method')}</th>
                    <th className="p-2 text-start">{t('finance.amount')}</th>
                    <th className="p-2 text-start"></th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((r) => (
                    <tr key={r.id} className="border-b border-border/60">
                      <td className="p-2 text-xs text-slate-500">{r.expense_date}</td>
                      <td className="p-2 font-medium text-slate-800">{r.category_name_ar}</td>
                      <td className="p-2 text-slate-600">{r.description ?? '—'}</td>
                      <td className="p-2">
                        <Badge tone="neutral">{t(`finance.method_${r.payment_method}`)}</Badge>
                      </td>
                      <td className="p-2 tabular-nums font-semibold text-slate-800">
                        {r.amount} {r.currency_code}
                      </td>
                      <td className="flex flex-wrap justify-end gap-2 p-2">
                        {canManage && (
                          <>
                            <Button size="sm" variant="outline" onClick={() => setEditTarget(r)}>
                              {t('finance.edit')}
                            </Button>
                            <Button
                              size="sm"
                              variant="danger"
                              disabled={deleteMut.isPending}
                              onClick={() => deleteMut.mutate(r.id)}
                            >
                              {t('finance.delete')}
                            </Button>
                          </>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <div className="mt-3 flex justify-end border-t border-border pt-3 text-sm font-bold text-slate-900">
                {t('finance.total')}:{' '}
                <span className="ms-1 tabular-nums">
                  {total.toFixed(2)} {currency}
                </span>
              </div>
            </>
          )}
        </CardContent>
      </Card>

      {showCreate && (
        <ExpenseFormModal
          target="new"
          branchId={branchId}
          categories={categories}
          onClose={() => setShowCreate(false)}
          onSaved={() => {
            setShowCreate(false);
            invalidate();
            toast.success(t('finance.expense_created_ok'));
          }}
        />
      )}
      {editTarget && (
        <ExpenseFormModal
          target={editTarget}
          branchId={branchId}
          categories={categories}
          onClose={() => setEditTarget(null)}
          onSaved={() => {
            setEditTarget(null);
            invalidate();
            toast.success(t('finance.expense_updated_ok'));
          }}
        />
      )}
    </div>
  );
}

function ExpenseFormModal({
  target,
  branchId,
  categories,
  onClose,
  onSaved,
}: {
  target: ExpenseRow | 'new';
  branchId: string;
  categories: ExpenseCategory[];
  onClose: () => void;
  onSaved: () => void;
}) {
  const isNew = target === 'new';
  const [categoryId, setCategoryId] = useState(
    isNew ? (categories[0]?.id ?? '') : target.expense_category_id,
  );
  const [amount, setAmount] = useState(isNew ? '' : target.amount);
  const [date, setDate] = useState(
    isNew ? new Date().toISOString().slice(0, 10) : target.expense_date,
  );
  const [description, setDescription] = useState(isNew ? '' : (target.description ?? ''));
  const [method, setMethod] = useState<ExpensePaymentMethod>(
    isNew ? 'cash' : target.payment_method,
  );
  const [error, setError] = useState<string | null>(null);

  const mut = useMutation({
    mutationFn: () => {
      const body = {
        expense_category_id: categoryId,
        amount,
        expense_date: date,
        description: description.trim() || null,
        payment_method: method,
      };
      return isNew
        ? createExpense({ branch_id: branchId, ...body })
        : updateExpense(target.id, body);
    },
    onSuccess: onSaved,
    onError: (e) => setError(errCode(e)),
  });

  const canSubmit = categoryId !== '' && Number(amount) > 0 && date !== '';

  return (
    <Modal
      open
      onClose={onClose}
      title={t(isNew ? 'finance.create_expense_title' : 'finance.edit_expense_title')}
      className="max-w-lg"
    >
      <form
        className="space-y-4"
        onSubmit={(e) => {
          e.preventDefault();
          if (canSubmit) {
            setError(null);
            mut.mutate();
          }
        }}
      >
        <Field label={t('finance.category')}>
          <Select value={categoryId} onChange={(e) => setCategoryId(e.target.value)} autoFocus>
            {categories.map((c) => (
              <option key={c.id} value={c.id}>
                {c.name_ar}
              </option>
            ))}
          </Select>
        </Field>
        <div className="grid grid-cols-2 gap-3">
          <Field label={t('finance.amount')}>
            <Input inputMode="decimal" value={amount} onChange={(e) => setAmount(e.target.value)} />
          </Field>
          <Field label={t('finance.date')}>
            <Input type="date" value={date} onChange={(e) => setDate(e.target.value)} />
          </Field>
        </div>
        <Field label={t('finance.payment_method')}>
          <Select
            value={method}
            onChange={(e) => setMethod(e.target.value as ExpensePaymentMethod)}
          >
            {PAYMENT_METHODS.map((m) => (
              <option key={m} value={m}>
                {t(`finance.method_${m}`)}
              </option>
            ))}
          </Select>
        </Field>
        <Field label={t('finance.description')}>
          <Input value={description} onChange={(e) => setDescription(e.target.value)} />
        </Field>

        {error && <p className="text-sm text-danger">{t(`errors.${error}`)}</p>}
        <div className="flex justify-end gap-2 border-t border-border pt-4">
          <Button type="button" variant="outline" onClick={onClose}>
            {t('users.cancel')}
          </Button>
          <Button type="submit" disabled={!canSubmit || mut.isPending}>
            {mut.isPending ? t('finance.creating') : t('finance.save')}
          </Button>
        </div>
      </form>
    </Modal>
  );
}

// ================================ Categories ================================

function CategoriesTab() {
  const canManage = useAuth((s) => s.hasPermission('finance.expenses'));
  const qc = useQueryClient();
  const [activeOnly, setActiveOnly] = useState(false);
  const [showCreate, setShowCreate] = useState(false);
  const [editTarget, setEditTarget] = useState<ExpenseCategory | null>(null);

  const listQuery = useQuery({
    queryKey: ['expense-categories', activeOnly],
    queryFn: () => listExpenseCategories({ activeOnly }),
  });
  const rows = listQuery.data ?? [];

  const invalidate = () => qc.invalidateQueries({ queryKey: ['expense-categories'] });

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <label className="flex cursor-pointer items-center gap-2 text-sm text-slate-700">
          <input
            type="checkbox"
            checked={activeOnly}
            onChange={(e) => setActiveOnly(e.target.checked)}
          />
          {t('finance.active_only')}
        </label>
        {canManage && (
          <Button onClick={() => setShowCreate(true)}>{t('finance.add_category')}</Button>
        )}
      </div>

      <Card>
        <CardContent className="pt-6">
          {listQuery.isLoading ? (
            <div className="flex justify-center py-8">
              <Spinner />
            </div>
          ) : rows.length === 0 ? (
            <p className="py-6 text-center text-sm text-slate-500">
              {t('finance.categories_empty')}
            </p>
          ) : (
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-border text-xs text-slate-500">
                  <th className="p-2 text-start">{t('finance.category_name_ar')}</th>
                  <th className="p-2 text-start">{t('finance.category_name_en')}</th>
                  <th className="p-2 text-start">{t('finance.status')}</th>
                  <th className="p-2 text-start"></th>
                </tr>
              </thead>
              <tbody>
                {rows.map((c) => (
                  <tr key={c.id} className="border-b border-border/60">
                    <td className="p-2 font-medium text-slate-800">{c.name_ar}</td>
                    <td className="p-2 text-slate-600">{c.name_en ?? '—'}</td>
                    <td className="p-2">
                      <Badge tone={c.is_active ? 'success' : 'neutral'}>
                        {c.is_active ? t('finance.active') : t('finance.inactive')}
                      </Badge>
                    </td>
                    <td className="p-2 text-end">
                      {canManage && (
                        <Button size="sm" variant="outline" onClick={() => setEditTarget(c)}>
                          {t('finance.edit')}
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

      {showCreate && (
        <CategoryFormModal
          target="new"
          onClose={() => setShowCreate(false)}
          onSaved={() => {
            setShowCreate(false);
            invalidate();
            toast.success(t('finance.category_created_ok'));
          }}
        />
      )}
      {editTarget && (
        <CategoryFormModal
          target={editTarget}
          onClose={() => setEditTarget(null)}
          onSaved={() => {
            setEditTarget(null);
            invalidate();
            toast.success(t('finance.category_updated_ok'));
          }}
        />
      )}
    </div>
  );
}

function CategoryFormModal({
  target,
  onClose,
  onSaved,
}: {
  target: ExpenseCategory | 'new';
  onClose: () => void;
  onSaved: () => void;
}) {
  const isNew = target === 'new';
  const [nameAr, setNameAr] = useState(isNew ? '' : target.name_ar);
  const [nameEn, setNameEn] = useState(isNew ? '' : (target.name_en ?? ''));
  const [isActive, setIsActive] = useState(isNew ? true : target.is_active);
  const [error, setError] = useState<string | null>(null);

  const mut = useMutation({
    mutationFn: () =>
      isNew
        ? createExpenseCategory({ name_ar: nameAr.trim(), name_en: nameEn.trim() || null })
        : updateExpenseCategory(target.id, {
            name_ar: nameAr.trim(),
            name_en: nameEn.trim() || null,
            is_active: isActive,
          }),
    onSuccess: onSaved,
    onError: (e) => setError(errCode(e)),
  });

  return (
    <Modal
      open
      onClose={onClose}
      title={t(isNew ? 'finance.create_category_title' : 'finance.edit_category_title')}
      className="max-w-md"
    >
      <form
        className="space-y-4"
        onSubmit={(e) => {
          e.preventDefault();
          if (nameAr.trim()) {
            setError(null);
            mut.mutate();
          }
        }}
      >
        <Field label={t('finance.category_name_ar')}>
          <Input value={nameAr} onChange={(e) => setNameAr(e.target.value)} autoFocus required />
        </Field>
        <Field label={t('finance.category_name_en')}>
          <Input value={nameEn} onChange={(e) => setNameEn(e.target.value)} />
        </Field>
        {!isNew && (
          <label className="flex cursor-pointer items-center gap-2 text-sm text-slate-700">
            <input
              type="checkbox"
              checked={isActive}
              onChange={(e) => setIsActive(e.target.checked)}
            />
            {t('finance.active')}
          </label>
        )}

        {error && <p className="text-sm text-danger">{t(`errors.${error}`)}</p>}
        <div className="flex justify-end gap-2 border-t border-border pt-4">
          <Button type="button" variant="outline" onClick={onClose}>
            {t('users.cancel')}
          </Button>
          <Button type="submit" disabled={!nameAr.trim() || mut.isPending}>
            {mut.isPending ? t('finance.saving') : t('finance.save')}
          </Button>
        </div>
      </form>
    </Modal>
  );
}

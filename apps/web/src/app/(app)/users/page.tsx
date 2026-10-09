'use client';

import { SystemRole } from '@pharmaos/shared';
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
import { useState } from 'react';

import {
  changeUserRole,
  createUser,
  deleteUser,
  listUsers,
  resetUserPassword,
  setUserActive,
  updateUserProfile,
  type CreateUserInput,
  type ManagedUser,
} from '@/lib/api';
import { useAuth } from '@/lib/auth-store';
import { formatApiErrorMessage, t } from '@/lib/i18n';
import { toast } from '@/lib/toast-store';

const ROLE_CODES = Object.values(SystemRole);

const EMPTY_FORM: CreateUserInput = {
  username: '',
  full_name: '',
  password: '',
  role_code: SystemRole.CASHIER,
  phone: '',
};

export default function UsersPage() {
  const qc = useQueryClient();
  const currentUser = useAuth((s) => s.user);
  const [showForm, setShowForm] = useState(false);
  const [form, setForm] = useState<CreateUserInput>(EMPTY_FORM);
  const [formError, setFormError] = useState<string | null>(null);
  const [resetTarget, setResetTarget] = useState<ManagedUser | null>(null);
  const [newPassword, setNewPassword] = useState('');
  const [editTarget, setEditTarget] = useState<ManagedUser | null>(null);
  const [editFullName, setEditFullName] = useState('');
  const [editPhone, setEditPhone] = useState('');
  const [deleteTarget, setDeleteTarget] = useState<ManagedUser | null>(null);

  const usersQuery = useQuery({ queryKey: ['users'], queryFn: listUsers });
  const invalidate = () => qc.invalidateQueries({ queryKey: ['users'] });

  const editMut = useMutation({
    mutationFn: () =>
      updateUserProfile(editTarget!.id, {
        full_name: editFullName.trim(),
        phone: editPhone.trim() || null,
        set_phone: true,
      }),
    onSuccess: () => {
      setEditTarget(null);
      invalidate();
      toast.success(t('users.updated_ok'));
    },
    onError: (e) => toast.error(formatApiErrorMessage(e)),
  });

  const createMut = useMutation({
    mutationFn: () => createUser({ ...form, phone: form.phone || null }),
    onSuccess: () => {
      setShowForm(false);
      setForm(EMPTY_FORM);
      setFormError(null);
      invalidate();
      toast.success(t('users.created_ok'));
    },
    onError: (e) => {
      const msg = formatApiErrorMessage(e);
      setFormError(msg);
      toast.error(msg);
    },
  });

  const roleMut = useMutation({
    mutationFn: (v: { id: string; role: string }) => changeUserRole(v.id, v.role),
    onSuccess: invalidate,
    onError: (e) => toast.error(formatApiErrorMessage(e)),
  });
  const activeMut = useMutation({
    mutationFn: (v: { id: string; active: boolean }) => setUserActive(v.id, v.active),
    onSuccess: invalidate,
    onError: (e) => toast.error(formatApiErrorMessage(e)),
  });
  const resetMut = useMutation({
    mutationFn: (v: { id: string; pw: string }) => resetUserPassword(v.id, v.pw),
    onSuccess: () => {
      setResetTarget(null);
      setNewPassword('');
      toast.success(t('users.reset_done'));
    },
    onError: (e) => toast.error(formatApiErrorMessage(e)),
  });
  const deleteMut = useMutation({
    mutationFn: (id: string) => deleteUser(id),
    onSuccess: () => {
      setDeleteTarget(null);
      invalidate();
      toast.success(t('users.deleted_ok'));
    },
    onError: (e) => toast.error(formatApiErrorMessage(e)),
  });

  return (
    <div className="mx-auto max-w-5xl space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold text-slate-900">{t('users.title')}</h1>
        <Button onClick={() => setShowForm((v) => !v)}>
          {showForm ? t('users.cancel') : t('users.add')}
        </Button>
      </div>

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
              <Field label={t('users.username')}>
                <Input
                  value={form.username}
                  onChange={(e) => setForm({ ...form, username: e.target.value })}
                  autoComplete="off"
                  required
                />
              </Field>
              <Field label={t('users.full_name')}>
                <Input
                  value={form.full_name}
                  onChange={(e) => setForm({ ...form, full_name: e.target.value })}
                  required
                />
              </Field>
              <Field label={t('users.password')}>
                <Input
                  type="password"
                  value={form.password}
                  onChange={(e) => setForm({ ...form, password: e.target.value })}
                  autoComplete="new-password"
                  required
                />
              </Field>
              <Field label={t('users.phone')}>
                <Input
                  value={form.phone ?? ''}
                  onChange={(e) => setForm({ ...form, phone: e.target.value })}
                  className="numeric"
                />
              </Field>
              <Field label={t('users.role')}>
                <Select
                  value={form.role_code}
                  onChange={(e) => setForm({ ...form, role_code: e.target.value })}
                >
                  {ROLE_CODES.map((code) => (
                    <option key={code} value={code}>
                      {t(`role.${code}`)}
                    </option>
                  ))}
                </Select>
              </Field>

              <div className="flex items-end gap-3">
                <Button type="submit" disabled={createMut.isPending}>
                  {createMut.isPending ? t('users.creating') : t('users.create')}
                </Button>
                {formError && (
                  <span className="text-sm text-danger">{t(`errors.${formError}`)}</span>
                )}
              </div>
            </form>
          </CardContent>
        </Card>
      )}

      <Card>
        <CardContent className="pt-6">
          {usersQuery.isLoading ? (
            <div className="flex justify-center py-8">
              <Spinner />
            </div>
          ) : (
            <table className="w-full text-start text-sm">
              <thead>
                <tr className="border-b border-border text-start text-xs text-slate-500">
                  <th className="p-2 text-start">{t('users.username')}</th>
                  <th className="p-2 text-start">{t('users.full_name')}</th>
                  <th className="p-2 text-start">{t('users.role')}</th>
                  <th className="p-2 text-start">{t('users.status')}</th>
                  <th className="p-2 text-start">{t('users.actions')}</th>
                </tr>
              </thead>
              <tbody>
                {(usersQuery.data ?? []).map((u) => {
                  const isSelf = u.id === currentUser?.id || u.username === currentUser?.username;
                  return (
                    <tr key={u.id} className="border-b border-border/60">
                      <td className="p-2 font-medium text-slate-800">
                        <div className="flex items-center gap-2">
                          <span>{u.username}</span>
                          {isSelf && (
                            <Badge tone="primary" className="text-[10px]">
                              {t('users.current_user')}
                            </Badge>
                          )}
                        </div>
                      </td>
                      <td className="p-2 text-slate-700">{u.full_name}</td>
                      <td className="p-2">
                        <Select
                          className="h-8"
                          value={u.role ?? ''}
                          disabled={isSelf}
                          title={isSelf ? t('users.cannot_modify_self') : undefined}
                          onChange={(e) => roleMut.mutate({ id: u.id, role: e.target.value })}
                        >
                          {ROLE_CODES.map((code) => (
                            <option key={code} value={code}>
                              {t(`role.${code}`)}
                            </option>
                          ))}
                        </Select>
                      </td>
                      <td className="p-2">
                        <Badge tone={u.is_active ? 'success' : 'neutral'}>
                          {u.is_active ? t('users.active') : t('users.inactive')}
                        </Badge>
                      </td>
                      <td className="flex flex-wrap gap-2 p-2">
                        <Button
                          size="sm"
                          variant="ghost"
                          onClick={() => {
                            setEditTarget(u);
                            setEditFullName(u.full_name);
                            setEditPhone(u.phone ?? '');
                          }}
                        >
                          {t('users.edit')}
                        </Button>
                        <Button
                          size="sm"
                          variant={u.is_active ? 'outline' : 'primary'}
                          disabled={isSelf}
                          title={isSelf ? t('users.cannot_modify_self') : undefined}
                          onClick={() => activeMut.mutate({ id: u.id, active: !u.is_active })}
                        >
                          {u.is_active ? t('users.deactivate') : t('users.activate')}
                        </Button>
                        <Button size="sm" variant="ghost" onClick={() => setResetTarget(u)}>
                          {t('users.reset_password')}
                        </Button>
                        {!isSelf && (
                          <Button
                            size="sm"
                            variant="ghost"
                            className="text-danger hover:bg-red-50 hover:text-danger"
                            onClick={() => setDeleteTarget(u)}
                          >
                            {t('users.delete')}
                          </Button>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </CardContent>
      </Card>

      {/* Password reset — a proper modal (never a browser prompt; CLAUDE.md UX rule) */}
      <Modal
        open={resetTarget !== null}
        onClose={() => setResetTarget(null)}
        title={`${t('users.reset_password')} — ${resetTarget?.username ?? ''}`}
      >
        <form
          className="space-y-4"
          onSubmit={(e) => {
            e.preventDefault();
            if (resetTarget && newPassword) {
              resetMut.mutate({ id: resetTarget.id, pw: newPassword });
            }
          }}
        >
          <div className="space-y-1.5">
            <Label htmlFor="new-pw">{t('users.reset_prompt')}</Label>
            <Input
              id="new-pw"
              type="password"
              value={newPassword}
              onChange={(e) => setNewPassword(e.target.value)}
              autoComplete="new-password"
              autoFocus
              required
            />
          </div>
          <div className="flex justify-end gap-2">
            <Button type="button" variant="outline" onClick={() => setResetTarget(null)}>
              {t('users.cancel')}
            </Button>
            <Button type="submit" variant="danger" disabled={resetMut.isPending}>
              {t('users.reset_password')}
            </Button>
          </div>
        </form>
      </Modal>

      {/* Edit user profile modal */}
      <Modal
        open={editTarget !== null}
        onClose={() => setEditTarget(null)}
        title={`${t('users.edit_profile')} — ${editTarget?.username ?? ''}`}
      >
        <form
          className="space-y-4"
          onSubmit={(e) => {
            e.preventDefault();
            if (editTarget) {
              editMut.mutate();
            }
          }}
        >
          <div className="space-y-1.5">
            <Label htmlFor="edit-name">{t('users.full_name')}</Label>
            <Input
              id="edit-name"
              value={editFullName}
              onChange={(e) => setEditFullName(e.target.value)}
              required
            />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="edit-phone">{t('users.phone')}</Label>
            <Input
              id="edit-phone"
              value={editPhone}
              onChange={(e) => setEditPhone(e.target.value)}
              className="numeric"
            />
          </div>
          <div className="flex justify-end gap-2">
            <Button type="button" variant="outline" onClick={() => setEditTarget(null)}>
              {t('users.cancel')}
            </Button>
            <Button type="submit" disabled={editMut.isPending}>
              {t('common.save')}
            </Button>
          </div>
        </form>
      </Modal>

      {/* Delete user confirmation modal */}
      <Modal
        open={deleteTarget !== null}
        onClose={() => setDeleteTarget(null)}
        title={t('users.delete')}
      >
        <div className="space-y-4">
          <p className="text-sm text-slate-700">{t('users.delete_confirm')}</p>
          <div className="rounded-[var(--radius-md)] bg-slate-50 p-3 text-sm">
            <p className="font-semibold text-slate-900">{deleteTarget?.full_name}</p>
            <p className="font-mono text-xs text-slate-500">@{deleteTarget?.username}</p>
          </div>
          <div className="flex justify-end gap-2 pt-2">
            <Button
              type="button"
              variant="outline"
              disabled={deleteMut.isPending}
              onClick={() => setDeleteTarget(null)}
            >
              {t('users.cancel')}
            </Button>
            <Button
              type="button"
              variant="danger"
              disabled={deleteMut.isPending}
              onClick={() => {
                if (deleteTarget) deleteMut.mutate(deleteTarget.id);
              }}
            >
              {deleteMut.isPending ? t('users.creating') : t('users.delete')}
            </Button>
          </div>
        </div>
      </Modal>
    </div>
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

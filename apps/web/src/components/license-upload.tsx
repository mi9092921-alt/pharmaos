'use client';

import { useRef } from 'react';
import { useMutation } from '@tanstack/react-query';

import { Button } from '@pharmaos/ui';

import { ApiRequestError, activateLicense } from '@/lib/api';
import { formatApiErrorMessage, t } from '@/lib/i18n';
import { toast } from '@/lib/toast-store';

/** The gate enforces 64 KiB — checked client-side first (UX rules). */
export const MAX_LICENSE_BYTES = 64 * 1024;

/**
 * Shared .license upload → POST /license/activate.
 * The endpoint reads the RAW request body, so the file goes as octet-stream,
 * NOT multipart/JSON. Allowlisted pre-session: usable from the license gate,
 * the login page, and settings alike.
 */
export function LicenseUploadButton({
  onActivated,
  className,
}: {
  onActivated?: () => void;
  className?: string;
}) {
  const fileRef = useRef<HTMLInputElement>(null);

  const mut = useMutation({
    mutationFn: (file: File) => activateLicense(file),
    onSuccess: () => {
      toast.success(t('settings.software_license.activated_ok'));
      onActivated?.();
    },
    onError: (e) =>
      toast.error(e instanceof ApiRequestError ? t(`errors.${e.code}`) : formatApiErrorMessage(e)),
  });

  const onFile = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    e.target.value = '';
    if (!file) return;
    if (file.size > MAX_LICENSE_BYTES) {
      toast.error(t('settings.software_license.too_large'));
      return;
    }
    mut.mutate(file);
  };

  return (
    <>
      <input ref={fileRef} type="file" accept=".license" className="hidden" onChange={onFile} />
      <Button
        variant="outline"
        className={className}
        disabled={mut.isPending}
        onClick={() => fileRef.current?.click()}
      >
        {mut.isPending
          ? t('settings.software_license.activating')
          : t('settings.software_license.upload')}
      </Button>
    </>
  );
}

export function licenseStatusTone(
  status: string | null,
): 'success' | 'warning' | 'danger' | 'neutral' {
  switch (status) {
    case 'active':
      return 'success';
    case 'grace':
      return 'warning';
    case 'unlicensed':
      return 'neutral';
    default:
      return 'danger';
  }
}

export function licenseStatusLabel(status: string | null): string {
  switch (status) {
    case 'active':
      return t('settings.software_license.st_active');
    case 'grace':
      return t('settings.software_license.st_grace');
    case 'read_only':
      return t('settings.software_license.st_read_only');
    case 'unlicensed':
      return t('settings.software_license.st_unlicensed');
    case 'tamper':
    case 'clock_error':
      return t('errors.E-LIC-006');
    case 'key_lost':
      return t('errors.E-LIC-008');
    default:
      return t('errors.E-LIC-007');
  }
}

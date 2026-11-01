'use client';

import * as React from 'react';

import { Button } from './button';
import { Spinner } from './spinner';

export interface QueryStateProps {
  /** The list query is still loading its first page. */
  isLoading: boolean;
  /** The list query failed — renders a translated error + retry instead of
   * an "empty" state (network failure must never look like no-data). */
  isError: boolean;
  onRetry: () => void;
  /** Data arrived but the list is empty. */
  isEmpty: boolean;
  emptyText: string;
  /** Optional CTA rendered under the empty message. */
  emptyAction?: React.ReactNode;
  /** Error text for the message (caller translates via formatApiErrorMessage). */
  errorText: string;
  /** Injected retry label (caller translates — e.g. t('common.retry')). */
  retryLabel: string;
  /** Rendered when loaded and non-empty. */
  children: React.ReactNode;
}

/**
 * The three states every list owes its user (CLAUDE.md UX rules): loading /
 * error-with-retry / empty-with-message. Replaces the repeated
 * `data ?? []`-renders-as-empty pattern across pages.
 */
export function QueryState({
  isLoading,
  isError,
  onRetry,
  isEmpty,
  emptyText,
  emptyAction,
  errorText,
  retryLabel,
  children,
}: QueryStateProps) {
  if (isLoading) {
    return (
      <div className="flex justify-center py-10">
        <Spinner />
      </div>
    );
  }
  if (isError) {
    return (
      <div className="space-y-3 py-10 text-center">
        <p className="text-sm font-medium text-danger">{errorText}</p>
        <Button variant="outline" size="sm" onClick={onRetry}>
          {retryLabel}
        </Button>
      </div>
    );
  }
  if (isEmpty) {
    return (
      <div className="space-y-3 py-10 text-center">
        <p className="text-sm text-slate-500">{emptyText}</p>
        {emptyAction}
      </div>
    );
  }
  return <>{children}</>;
}

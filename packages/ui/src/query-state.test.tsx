import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { QueryState } from './query-state';

describe('QueryState', () => {
  it('renders the spinner while loading', () => {
    const { container } = render(
      <QueryState
        isLoading
        isError={false}
        onRetry={vi.fn()}
        isEmpty={false}
        emptyText="empty"
        errorText="err"
        retryLabel="Retry"
      >
        <p>data</p>
      </QueryState>,
    );
    expect(container.querySelector('.animate-spin, [role="status"], svg')).toBeTruthy();
    expect(screen.queryByText('data')).toBeNull();
  });

  it('renders the error text + retry button on failure — never the empty copy', () => {
    const onRetry = vi.fn();
    render(
      <QueryState
        isLoading={false}
        isError
        onRetry={onRetry}
        isEmpty
        emptyText="empty"
        errorText="فشل الاتصال"
        retryLabel="Retry"
      >
        <p>data</p>
      </QueryState>,
    );
    expect(screen.getByText('فشل الاتصال')).toBeInTheDocument();
    expect(screen.queryByText('empty')).toBeNull();
    expect(screen.getByRole('button', { name: 'Retry' })).toBeInTheDocument();
  });

  it('renders the empty message when loaded empty', () => {
    render(
      <QueryState
        isLoading={false}
        isError={false}
        onRetry={vi.fn()}
        isEmpty
        emptyText="لا بيانات"
        errorText="err"
        retryLabel="Retry"
      >
        <p>data</p>
      </QueryState>,
    );
    expect(screen.getByText('لا بيانات')).toBeInTheDocument();
    expect(screen.queryByText('data')).toBeNull();
  });

  it('renders children when loaded and non-empty', () => {
    render(
      <QueryState
        isLoading={false}
        isError={false}
        onRetry={vi.fn()}
        isEmpty={false}
        emptyText="empty"
        errorText="err"
        retryLabel="Retry"
      >
        <p>data</p>
      </QueryState>,
    );
    expect(screen.getByText('data')).toBeInTheDocument();
  });
});

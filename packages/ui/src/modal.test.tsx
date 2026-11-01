import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { ConfirmDialog } from './confirm-dialog';
import { Modal } from './modal';

/** Wave UX: the overlay registry — Escape and backdrop close belong to the
 * TOPMOST overlay only, so a ConfirmDialog stacked over an editor Modal never
 * closes both layers at once. */
describe('Modal overlay stack', () => {
  it('Escape closes only the topmost modal', () => {
    const closeBottom = vi.fn();
    const closeTop = vi.fn();
    render(
      <>
        <Modal open onClose={closeBottom} title="bottom">
          <p>bottom content</p>
        </Modal>
        <Modal open onClose={closeTop} title="top">
          <p>top content</p>
        </Modal>
      </>,
    );
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(closeTop).toHaveBeenCalledTimes(1);
    expect(closeBottom).not.toHaveBeenCalled();
  });

  it('after the top modal unmounts, Escape reaches the next one', () => {
    const closeBottom = vi.fn();
    const { rerender } = render(
      <>
        <Modal open onClose={closeBottom} title="bottom">
          x
        </Modal>
        <Modal open onClose={vi.fn()} title="top">
          y
        </Modal>
      </>,
    );
    rerender(
      <>
        <Modal open onClose={closeBottom} title="bottom">
          x
        </Modal>
      </>,
    );
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(closeBottom).toHaveBeenCalledTimes(1);
  });
});

describe('ConfirmDialog', () => {
  it('calls onConfirm once per confirm click and onClose on cancel', () => {
    const onConfirm = vi.fn();
    const onClose = vi.fn();
    render(
      <ConfirmDialog
        open
        onClose={onClose}
        onConfirm={onConfirm}
        title="t"
        message="m"
        confirmLabel="OK"
        cancelLabel="Cancel"
      />,
    );
    fireEvent.click(screen.getByRole('button', { name: 'OK' }));
    expect(onConfirm).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('disables both buttons while pending', () => {
    render(
      <ConfirmDialog
        open
        onClose={vi.fn()}
        onConfirm={vi.fn()}
        title="t"
        message="m"
        confirmLabel="OK"
        cancelLabel="Cancel"
        pending
      />,
    );
    expect(screen.getByRole('button', { name: 'OK' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Cancel' })).toBeDisabled();
  });
});

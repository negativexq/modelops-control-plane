"use client";

import type { ReactNode } from "react";

interface AsyncBoundaryProps<T> {
  loading: boolean;
  error: Error | null;
  data: T | null | undefined;
  onRetry?: () => void;
  children: (data: T) => ReactNode;
}

/**
 * Consistent loading/error/empty handling for every page that fetches from the
 * control plane. `data` from a previous successful fetch is kept on screen while a
 * refetch is in flight (loading spinner only shows before the first successful load).
 */
export function AsyncBoundary<T>({
  loading,
  error,
  data,
  onRetry,
  children,
}: AsyncBoundaryProps<T>) {
  if (error) {
    return (
      <div className="rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-800 dark:border-red-900 dark:bg-red-950 dark:text-red-200">
        <p className="font-medium">Couldn&apos;t load this data</p>
        <p className="mt-1 text-red-700 dark:text-red-300">{error.message}</p>
        {onRetry ? (
          <button
            type="button"
            onClick={onRetry}
            className="mt-2 rounded-md border border-red-300 px-3 py-1 text-xs font-medium hover:bg-red-100 dark:border-red-800 dark:hover:bg-red-900"
          >
            Retry
          </button>
        ) : null}
      </div>
    );
  }

  if (data == null) {
    if (loading) {
      return (
        <div className="flex items-center gap-2 py-6 text-sm text-zinc-500 dark:text-zinc-400">
          <span
            className="h-4 w-4 animate-spin rounded-full border-2 border-zinc-300 border-t-zinc-600 dark:border-zinc-700 dark:border-t-zinc-300"
            aria-hidden
          />
          Loading…
        </div>
      );
    }
    return <p className="py-6 text-sm text-zinc-500 dark:text-zinc-400">No data.</p>;
  }

  return <>{children(data)}</>;
}

"use client";

import { useCallback, useEffect, useState } from "react";

interface AsyncState<T> {
  data: T | null;
  error: Error | null;
  loading: boolean;
}

/**
 * Fetches on mount (and whenever `deps` changes), with an explicit `refetch()`.
 * Deliberately no caching/optimistic updates - every refetch re-requests the server
 * and replaces state with whatever comes back, which is what promote/rollback need:
 * the displayed state should always be what the control plane actually did, not a
 * local guess.
 *
 * `fetcher` is intentionally excluded from the effect's dependency array - callers
 * pass a fresh closure on every render (e.g. `() => listDeployments()`), and `deps`
 * is the actual, caller-declared re-fetch contract (mirrors how query keys work in
 * data-fetching libraries).
 */
export function useAsync<T>(fetcher: () => Promise<T>, deps: React.DependencyList = []) {
  const [state, setState] = useState<AsyncState<T>>({
    data: null,
    error: null,
    loading: true,
  });
  const [reloadKey, setReloadKey] = useState(0);

  const refetch = useCallback(() => setReloadKey((key) => key + 1), []);

  useEffect(() => {
    let cancelled = false;

    fetcher()
      .then((data) => {
        if (!cancelled) setState({ data, error: null, loading: false });
      })
      .catch((error: unknown) => {
        if (!cancelled) {
          setState({
            data: null,
            error: error instanceof Error ? error : new Error(String(error)),
            loading: false,
          });
        }
      });

    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, reloadKey]);

  return { ...state, refetch };
}

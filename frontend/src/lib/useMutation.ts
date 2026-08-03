"use client";

import { useCallback, useState } from "react";

interface MutationResult {
  kind: "success" | "error";
  message: string;
}

interface MutationState {
  submitting: boolean;
  result: MutationResult | null;
}

/**
 * Wraps an API call (promote, rollback, create deployment, ...). Deliberately does
 * NOT touch any list/detail state optimistically - callers should call their own
 * `refetch()` inside `onSuccess` once the real request has resolved, so the UI always
 * reflects what the control plane actually persisted rather than a local guess.
 */
export function useMutation<Args extends unknown[], R>(
  action: (...args: Args) => Promise<R>,
  successMessage: string = "Done.",
) {
  const [state, setState] = useState<MutationState>({ submitting: false, result: null });

  const run = useCallback(
    async (...args: Args): Promise<R | undefined> => {
      setState({ submitting: true, result: null });
      try {
        const data = await action(...args);
        setState({ submitting: false, result: { kind: "success", message: successMessage } });
        return data;
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        setState({ submitting: false, result: { kind: "error", message } });
        return undefined;
      }
    },
    [action, successMessage],
  );

  const clearResult = useCallback(() => setState((previous) => ({ ...previous, result: null })), []);

  return { ...state, run, clearResult };
}

"use client";

interface ResultBannerProps {
  result: { kind: "success" | "error"; message: string } | null;
  onDismiss: () => void;
}

/** Shown after a promote/rollback/create-deployment call resolves - never optimistic,
 * always reflects the real API response (see lib/useMutation.ts). */
export function ResultBanner({ result, onDismiss }: ResultBannerProps) {
  if (!result) return null;

  const isSuccess = result.kind === "success";
  return (
    <div
      role="status"
      className={`flex items-start justify-between gap-3 rounded-lg border px-4 py-3 text-sm ${
        isSuccess
          ? "border-emerald-200 bg-emerald-50 text-emerald-800 dark:border-emerald-900 dark:bg-emerald-950 dark:text-emerald-200"
          : "border-red-200 bg-red-50 text-red-800 dark:border-red-900 dark:bg-red-950 dark:text-red-200"
      }`}
    >
      <span>{result.message}</span>
      <button
        type="button"
        onClick={onDismiss}
        className="shrink-0 text-xs font-medium underline-offset-2 hover:underline"
      >
        Dismiss
      </button>
    </div>
  );
}

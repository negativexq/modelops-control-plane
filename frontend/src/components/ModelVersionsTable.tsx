"use client";

import { getModelVersionEvaluation, getModelVersionMetadata, getModelVersions } from "@/lib/api";
import { useAsync } from "@/lib/useAsync";
import { AsyncBoundary } from "@/components/AsyncBoundary";
import { formatNumber } from "@/lib/format";
import type { ModelVersionEvaluation, ModelVersionMetadata } from "@/lib/types";

interface VersionRow {
  version: string;
  metadata: ModelVersionMetadata;
  evaluation: ModelVersionEvaluation;
}

async function loadVersionRows(modelName: string): Promise<VersionRow[]> {
  const versions = await getModelVersions(modelName);
  return Promise.all(
    versions.map(async (version) => {
      const [metadata, evaluation] = await Promise.all([
        getModelVersionMetadata(modelName, version),
        getModelVersionEvaluation(modelName, version),
      ]);
      return { version, metadata, evaluation };
    }),
  );
}

export function ModelVersionsTable({ modelName }: { modelName: string }) {
  const { data, error, loading, refetch } = useAsync(
    () => loadVersionRows(modelName),
    [modelName],
  );

  return (
    <AsyncBoundary data={data} error={error} loading={loading} onRetry={refetch}>
      {(rows) => (
        <div className="overflow-x-auto">
          <table className="w-full min-w-[640px] text-left text-sm">
            <thead>
              <tr className="border-b border-zinc-200 text-xs uppercase tracking-wide text-zinc-500 dark:border-zinc-800 dark:text-zinc-400">
                <th className="py-2 pr-4">Version</th>
                <th className="py-2 pr-4">Algorithm</th>
                <th className="py-2 pr-4">Role</th>
                <th className="py-2 pr-4">Precision</th>
                <th className="py-2 pr-4">Recall</th>
                <th className="py-2 pr-4">F1</th>
                <th className="py-2 pr-4">ROC AUC</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-zinc-100 dark:divide-zinc-900">
              {rows.map((row) => (
                <tr key={row.version}>
                  <td className="py-2 pr-4 font-medium">{row.version}</td>
                  <td className="py-2 pr-4">{row.metadata.algorithm ?? "—"}</td>
                  <td className="py-2 pr-4">{row.metadata.role ?? "—"}</td>
                  <td className="py-2 pr-4">{formatNumber(row.evaluation.precision)}</td>
                  <td className="py-2 pr-4">{formatNumber(row.evaluation.recall)}</td>
                  <td className="py-2 pr-4">{formatNumber(row.evaluation.f1)}</td>
                  <td className="py-2 pr-4">{formatNumber(row.evaluation.roc_auc)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </AsyncBoundary>
  );
}

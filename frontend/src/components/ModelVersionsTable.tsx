"use client";

import { useModelVersionRows } from "@/lib/queries";
import { formatNumber } from "@/lib/format";
import { QueryState } from "@/components/QueryState";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";

const METRICS = ["precision", "recall", "f1", "roc_auc"] as const;

export function ModelVersionsTable({ modelName }: { modelName: string }) {
  const query = useModelVersionRows(modelName);

  return (
    <QueryState query={query} skeleton={<Skeleton className="h-28 w-full" />}>
      {(rows) => (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Version</TableHead>
              <TableHead>Algorithm</TableHead>
              <TableHead>Role</TableHead>
              <TableHead className="text-right">Precision</TableHead>
              <TableHead className="text-right">Recall</TableHead>
              <TableHead className="text-right">F1</TableHead>
              <TableHead className="text-right">ROC AUC</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {rows.map((row) => (
              <TableRow key={row.version}>
                <TableCell className="font-mono font-medium">{row.version}</TableCell>
                <TableCell className="text-muted-foreground">{row.metadata.algorithm ?? "—"}</TableCell>
                <TableCell className="text-muted-foreground">{row.metadata.role ?? "—"}</TableCell>
                {METRICS.map((metric) => (
                  <TableCell key={metric} className="text-right font-mono text-xs tabular">
                    {formatNumber(row.evaluation[metric] as number | undefined)}
                  </TableCell>
                ))}
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
    </QueryState>
  );
}

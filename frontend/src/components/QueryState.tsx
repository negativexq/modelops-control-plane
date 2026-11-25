"use client";

import type { ReactNode } from "react";
import type { UseQueryResult } from "@tanstack/react-query";
import { AlertTriangle, RotateCw } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";

/**
 * Loading / error handling for a query. Data from a previous successful fetch
 * stays on screen during background refetches (polling), so the skeleton only
 * shows on the very first load.
 */
export function QueryState<T>({
  query,
  skeleton,
  children,
}: {
  query: UseQueryResult<T>;
  skeleton?: ReactNode;
  children: (data: T) => ReactNode;
}) {
  if (query.data !== undefined) return <>{children(query.data)}</>;

  if (query.isError) {
    return (
      <div className="flex items-start gap-3 rounded-lg border border-danger/30 bg-danger/8 px-4 py-3 text-sm">
        <AlertTriangle className="mt-0.5 size-4 shrink-0 text-danger" aria-hidden />
        <div className="min-w-0 flex-1">
          <p className="font-medium">Couldn&apos;t load this data</p>
          <p className="mt-0.5 text-muted-foreground">{query.error.message}</p>
        </div>
        <Button size="sm" variant="outline" onClick={() => query.refetch()}>
          <RotateCw /> Retry
        </Button>
      </div>
    );
  }

  return (
    <>
      {skeleton ?? (
        <div className="space-y-3">
          <Skeleton className="h-4 w-1/3" />
          <Skeleton className="h-24 w-full" />
        </div>
      )}
    </>
  );
}

"use client";

import Link from "next/link";
import { useMemo, useState } from "react";
import {
  createColumnHelper,
  createSortedRowModel,
  rowSortingFeature,
  tableFeatures,
  useTable,
  type SortingState,
} from "@tanstack/react-table";
import { ArrowDown, ArrowUp, ArrowUpDown, GitBranch, Search } from "lucide-react";
import { isLive, useDeployments } from "@/lib/queries";
import { canaryPercent, decisionOf, durationSeconds, formatDuration, relativeTime } from "@/lib/derive";
import { formatDate, parseApiDate } from "@/lib/format";
import type { DeploymentOut, DeploymentStatus } from "@/lib/types";
import { cn } from "@/lib/utils";
import { PageHeader } from "@/components/PageHeader";
import { QueryState } from "@/components/QueryState";
import { AutomationPausedBadge } from "@/components/AutomationPausedBadge";
import { BenchmarkBadge } from "@/components/BenchmarkBadge";
import { DeploymentActions } from "@/components/DeploymentActions";
import { StatusBadge } from "@/components/StatusBadge";
import { TrafficBar } from "@/components/TrafficBar";
import { ActorBadge } from "@/components/viz/ActorBadge";
import { DecisionPanel } from "@/components/viz/DecisionPanel";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Empty, EmptyContent, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from "@/components/ui/empty";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from "@/components/ui/sheet";
import { Switch } from "@/components/ui/switch";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";

type View = "all" | "live" | "PROMOTED" | "ROLLED_BACK" | "INCONCLUSIVE" | "FAILED";

const VIEWS: Array<{ key: View; label: string; match: (s: DeploymentStatus) => boolean }> = [
  { key: "all", label: "All", match: () => true },
  { key: "live", label: "Live", match: (s) => isLive(s) },
  { key: "PROMOTED", label: "Promoted", match: (s) => s === "PROMOTED" },
  { key: "ROLLED_BACK", label: "Rolled back", match: (s) => s === "ROLLED_BACK" },
  { key: "INCONCLUSIVE", label: "Inconclusive", match: (s) => s === "INCONCLUSIVE" },
  { key: "FAILED", label: "Failed", match: (s) => s === "FAILED" },
];

const STATUS_ORDER: Record<DeploymentStatus, number> = {
  CANARY_RUNNING: 0, EVALUATING: 1, PROMOTING: 2, ROLLING_BACK: 3, DEPLOYING: 4, PENDING: 5,
  INCONCLUSIVE: 6, FAILED: 7, ROLLED_BACK: 8, PROMOTED: 9,
};

// TanStack Table v9: features are explicit and tree-shaken. Only client-side
// sorting is delegated to the table; the status tabs, benchmark toggle and text
// search are plain filters applied before the data reaches it.
const features = tableFeatures({
  rowSortingFeature,
  sortedRowModel: createSortedRowModel(),
});

const col = createColumnHelper<typeof features, DeploymentOut>();

const columns = col.columns([
  col.accessor((d) => `${d.model_name} ${d.stable_version} ${d.canary_version} ${d.id}`, {
    id: "model",
    header: "Rollout",
    sortFn: (a, b) => a.original.model_name.localeCompare(b.original.model_name),
    cell: ({ row: { original: d } }) => (
      <div className="min-w-0 space-y-0.5">
        <div className="flex items-center gap-2">
          <Link
            href={`/deployments/${d.id}`}
            onClick={(e) => e.stopPropagation()}
            className="truncate font-mono font-medium hover:underline"
          >
            {d.model_name}
          </Link>
          {d.is_benchmark ? <BenchmarkBadge /> : null}
          {d.automation_paused ? <AutomationPausedBadge /> : null}
        </div>
        <p className="font-mono text-xs text-muted-foreground">
          {d.stable_version} → <span className="text-foreground/80">{d.canary_version}</span>
        </p>
      </div>
    ),
  }),
  col.accessor("status", {
    header: "Status",
    sortFn: (a, b) => STATUS_ORDER[a.original.status] - STATUS_ORDER[b.original.status],
    cell: (info) => <StatusBadge status={info.getValue()} />,
  }),
  col.accessor((d) => canaryPercent(d) ?? -1, {
    id: "traffic",
    header: "Canary",
    cell: ({ row: { original: d } }) =>
      d.traffic_allocation ? (
        <div className="flex w-32 items-center gap-2">
          <div className="flex-1">
            <TrafficBar size="sm" showLegend={false} stableVersion={d.stable_version} targets={d.traffic_allocation.targets} />
          </div>
          <span className="w-9 text-right font-mono text-xs tabular">{(canaryPercent(d) ?? 0).toFixed(0)}%</span>
        </div>
      ) : null,
  }),
  col.accessor((d) => decisionOf(d)?.actor ?? "", {
    id: "actor",
    header: "Decided by",
    cell: ({ row: { original: d } }) => <ActorBadge actor={decisionOf(d)?.actor ?? null} by={decisionOf(d)?.by} />,
  }),
  col.accessor((d) => durationSeconds(d) ?? -1, {
    id: "duration",
    header: "Duration",
    cell: ({ row: { original: d } }) => (
      <span className="font-mono text-xs tabular">{formatDuration(durationSeconds(d))}</span>
    ),
  }),
  col.accessor((d) => (d.started_at ? parseApiDate(d.started_at).getTime() : 0), {
    id: "started",
    header: "Started",
    cell: ({ row: { original: d } }) => (
      <span
        className="font-mono text-xs whitespace-nowrap text-muted-foreground tabular"
        title={formatDate(d.started_at)}
      >
        {relativeTime(d.started_at ? parseApiDate(d.started_at) : null)}
      </span>
    ),
  }),
  col.display({
    id: "actions",
    header: () => <span className="sr-only">Actions</span>,
    cell: ({ row: { original: d } }) => (
      <div className="flex justify-end" onClick={(e) => e.stopPropagation()}>
        <DeploymentActions deployment={d} size="sm" />
      </div>
    ),
  }),
]);

function SortIcon({ dir }: { dir: false | "asc" | "desc" }) {
  if (dir === "asc") return <ArrowUp className="size-3" />;
  if (dir === "desc") return <ArrowDown className="size-3" />;
  return <ArrowUpDown className="size-3 opacity-40" />;
}

function DeploymentsTable({ deployments }: { deployments: DeploymentOut[] }) {
  const [view, setView] = useState<View>("all");
  const [hideBench, setHideBench] = useState(false);
  const [search, setSearch] = useState("");
  const [sorting, setSorting] = useState<SortingState>([{ id: "started", desc: true }]);
  const [inspecting, setInspecting] = useState<string | null>(null);

  const base = useMemo(() => deployments.filter((d) => !hideBench || !d.is_benchmark), [deployments, hideBench]);
  const counts = useMemo(
    () => Object.fromEntries(VIEWS.map((v) => [v.key, base.filter((d) => v.match(d.status)).length])) as Record<View, number>,
    [base],
  );
  const data = useMemo(() => {
    const match = VIEWS.find((v) => v.key === view)!.match;
    const needle = search.trim().toLowerCase();
    return base.filter(
      (d) =>
        match(d.status) &&
        (!needle ||
          `${d.model_name} ${d.stable_version} ${d.canary_version} ${d.id} ${d.status}`.toLowerCase().includes(needle)),
    );
  }, [base, view, search]);

  const table = useTable({
    features,
    columns,
    data,
    state: { sorting },
    onSortingChange: setSorting,
  });
  const rows = table.getRowModel().rows;
  const inspected = deployments.find((d) => d.id === inspecting) ?? null;

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <Tabs value={view} onValueChange={(v) => setView(v as View)}>
          <TabsList className="h-auto flex-wrap">
            {VIEWS.filter((v) => v.key === "all" || v.key === "live" || counts[v.key] > 0).map((v) => (
              <TabsTrigger key={v.key} value={v.key} className="gap-1.5">
                {v.label}
                <span className="rounded bg-foreground/8 px-1 font-mono text-[10px] tabular">{counts[v.key]}</span>
              </TabsTrigger>
            ))}
          </TabsList>
        </Tabs>
        <div className="flex items-center gap-4">
          <div className="flex items-center gap-2">
            <Switch id="hide-bench" checked={hideBench} onCheckedChange={setHideBench} />
            <Label htmlFor="hide-bench" className="text-xs font-normal text-muted-foreground">
              Hide benchmarks
            </Label>
          </div>
          <div className="relative">
            <Search className="pointer-events-none absolute top-1/2 left-2.5 size-3.5 -translate-y-1/2 text-muted-foreground" />
            <Input
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Filter model, version, id…"
              className="h-8 w-56 pl-8 text-sm"
            />
          </div>
        </div>
      </div>

      {/* Desktop: sortable table */}
      <Card className="hidden py-0 md:flex">
        <Table>
          <TableHeader>
            {table.getHeaderGroups().map((group) => (
              <TableRow key={group.id} className="hover:bg-transparent">
                {group.headers.map((header) => (
                  <TableHead key={header.id} className="first:pl-4 last:pr-4">
                    {header.column.getCanSort() ? (
                      <button
                        type="button"
                        onClick={header.column.getToggleSortingHandler()}
                        className="inline-flex items-center gap-1 hover:text-foreground"
                      >
                        <table.FlexRender header={header} />
                        <SortIcon dir={header.column.getIsSorted()} />
                      </button>
                    ) : (
                      <table.FlexRender header={header} />
                    )}
                  </TableHead>
                ))}
              </TableRow>
            ))}
          </TableHeader>
          <TableBody>
            {rows.length === 0 ? (
              <TableRow>
                <TableCell colSpan={columns.length} className="h-24 text-center text-sm text-muted-foreground">
                  No rollouts match.
                </TableCell>
              </TableRow>
            ) : (
              rows.map((row) => (
                <TableRow
                  key={row.id}
                  data-state={inspecting === row.original.id ? "selected" : undefined}
                  className={cn("cursor-pointer", isLive(row.original.status) && "bg-canary/[0.03]")}
                  onClick={() => setInspecting(row.original.id)}
                >
                  {row.getAllCells().map((cell) => (
                    <TableCell key={cell.id} className="first:pl-4 last:pr-4">
                      <table.FlexRender cell={cell} />
                    </TableCell>
                  ))}
                </TableRow>
              ))
            )}
          </TableBody>
        </Table>
      </Card>

      {/* Mobile: cards */}
      <div className="space-y-2 md:hidden">
        {rows.map(({ original: d }) => (
          <Link key={d.id} href={`/deployments/${d.id}`} className="block rounded-xl border bg-card p-4 transition-colors active:bg-accent">
            <div className="flex items-start justify-between gap-3">
              <div className="min-w-0">
                <p className="truncate font-mono text-sm font-medium">{d.model_name}</p>
                <p className="font-mono text-xs text-muted-foreground">
                  {d.stable_version} → {d.canary_version}
                </p>
              </div>
              <StatusBadge status={d.status} />
            </div>
            {d.traffic_allocation ? (
              <div className="mt-3">
                <TrafficBar size="sm" showLegend={false} stableVersion={d.stable_version} targets={d.traffic_allocation.targets} />
              </div>
            ) : null}
            <div className="mt-3 flex items-center justify-between text-xs text-muted-foreground">
              <ActorBadge actor={decisionOf(d)?.actor ?? null} by={decisionOf(d)?.by} />
              <span className="font-mono tabular">{formatDuration(durationSeconds(d))}</span>
            </div>
          </Link>
        ))}
        {rows.length === 0 ? <p className="py-8 text-center text-sm text-muted-foreground">No rollouts match.</p> : null}
      </div>

      <Sheet open={inspected !== null} onOpenChange={(open) => !open && setInspecting(null)}>
        <SheetContent className="w-full sm:max-w-md">
          <SheetHeader>
            <SheetTitle>Rollout</SheetTitle>
            <SheetDescription>Decision, evidence and timing at a glance.</SheetDescription>
          </SheetHeader>
          <div className="min-h-0 flex-1 overflow-y-auto px-4 pb-4">
            {inspected ? <DecisionPanel deployment={inspected} /> : null}
          </div>
        </SheetContent>
      </Sheet>
    </div>
  );
}

export default function DeploymentsPage() {
  const query = useDeployments();

  return (
    <div className="space-y-6">
      <PageHeader
        title="Deployments"
        description="Every canary rollout. Click a row to inspect its decision."
        actions={
          <Button asChild size="sm">
            <Link href="/models">New rollout</Link>
          </Button>
        }
      />
      <QueryState query={query}>
        {(deployments) =>
          deployments.length === 0 ? (
            <Empty className="border">
              <EmptyHeader>
                <EmptyMedia variant="icon">
                  <GitBranch />
                </EmptyMedia>
                <EmptyTitle>No deployments yet</EmptyTitle>
                <EmptyDescription>Start a canary rollout to see it tracked here.</EmptyDescription>
              </EmptyHeader>
              <EmptyContent>
                <Button asChild size="sm">
                  <Link href="/models">Start a rollout</Link>
                </Button>
              </EmptyContent>
            </Empty>
          ) : (
            <DeploymentsTable deployments={deployments} />
          )
        }
      </QueryState>
    </div>
  );
}

"use client";

import { usePolicyDefaults } from "@/lib/queries";
import type { PolicyConfig } from "@/lib/types";
import { cn } from "@/lib/utils";
import { EVIDENCE_FIELDS, GATE_FIELDS, type ThresholdField } from "@/components/PolicyThresholds";

/** The thresholds this rollout is actually judged by (snapshotted on the
 * deployment at creation), with anything that differs from today's defaults
 * marked: "custom" is what an operator chose on purpose. */
export function AppliedPolicy({ config }: { config: PolicyConfig | null }) {
  const defaults = usePolicyDefaults().data;
  if (!config) return null;

  const row = (field: ThresholdField) => {
    const value = field.get(config);
    const custom = defaults != null && value !== field.get(defaults);
    return (
      <div key={field.key} className="flex items-baseline justify-between gap-3 py-1.5">
        <dt className="text-xs text-muted-foreground">{field.label}</dt>
        <dd className="flex items-baseline gap-2">
          {custom ? (
            <span
              className="rounded bg-canary/15 px-1.5 text-[10px] text-canary"
              title={`Default is ${defaults ? field.get(defaults) : "?"}${field.unit}`}
            >
              custom
            </span>
          ) : null}
          <span className={cn("font-mono text-xs tabular", custom && "text-foreground")}>
            {value}
            {field.unit ? <span className="text-muted-foreground"> {field.unit}</span> : null}
          </span>
        </dd>
      </div>
    );
  };

  return (
    <div className="grid gap-x-8 gap-y-2 rounded-lg border p-4 md:grid-cols-2" data-testid="applied-policy">
      <div>
        <h3 className="mb-1 text-[11px] font-medium tracking-wide text-muted-foreground uppercase">Decision gates</h3>
        <dl className="divide-y">{GATE_FIELDS.map(row)}</dl>
      </div>
      <div>
        <h3 className="mb-1 text-[11px] font-medium tracking-wide text-muted-foreground uppercase">Evidence required</h3>
        <dl className="divide-y">{EVIDENCE_FIELDS.map(row)}</dl>
      </div>
    </div>
  );
}

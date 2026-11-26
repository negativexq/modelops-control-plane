"use client";

import { useState } from "react";
import { ChevronDown, RotateCcw } from "lucide-react";
import type { PolicyConfig } from "@/lib/types";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

/**
 * Editable policy thresholds for a new rollout. Values are kept as the strings
 * the operator typed and only parsed into a PolicyConfig when they're valid, so
 * a half-typed "0." never gets silently coerced. Ranges mirror the backend's
 * PolicyConfig validation; the backend stays the authority.
 */

export interface ThresholdField {
  key: string;
  label: string;
  hint: string;
  unit: string;
  min: number;
  max?: number;
  step: number;
  integer?: boolean;
  /** Left empty, the check is switched off (sent as null). */
  optional?: boolean;
  get: (c: PolicyConfig) => number | null;
  set: (c: PolicyConfig, v: number | null) => PolicyConfig;
}

export const GATE_FIELDS: ThresholdField[] = [
  {
    key: "latency",
    label: "Max p95 latency increase",
    hint: "Canary p95 may exceed stable's by at most this much.",
    unit: "%",
    min: 0,
    step: 1,
    get: (c) => c.latency.p95_max_increase_percent,
    set: (c, v) => ({ ...c, latency: { p95_max_increase_percent: v ?? 0 } }),
  },
  {
    key: "errors",
    label: "Max error rate",
    hint: "Share of canary requests allowed to fail.",
    unit: "%",
    min: 0,
    max: 100,
    step: 0.5,
    get: (c) => c.reliability.max_error_rate_percent,
    set: (c, v) => ({ ...c, reliability: { max_error_rate_percent: v ?? 0 } }),
  },
  {
    key: "recall",
    label: "Minimum recall",
    hint: "On labeled traffic, once enough labels have arrived.",
    unit: "",
    min: 0,
    max: 1,
    step: 0.01,
    get: (c) => c.quality.minimum_recall,
    set: (c, v) => ({ ...c, quality: { minimum_recall: v ?? 0 } }),
  },
  {
    key: "drift",
    label: "Max feature drift (PSI)",
    hint: "Canary inputs vs its training data, per feature. Empty switches it off.",
    unit: "",
    min: 0,
    step: 0.05,
    optional: true,
    // Rollouts created before the drift check have no "drift" in their snapshot.
    get: (c) => (c.drift as PolicyConfig["drift"] | undefined)?.max_feature_psi ?? null,
    set: (c, v) => ({ ...c, drift: { ...c.drift, max_feature_psi: v } }),
  },
  {
    key: "prediction_drift",
    label: "Max prediction drift (PSI)",
    hint: "Canary fraud scores vs its validation scores. Empty switches it off.",
    unit: "",
    min: 0,
    step: 0.05,
    optional: true,
    get: (c) => (c.drift as PolicyConfig["drift"] | undefined)?.max_prediction_psi ?? null,
    set: (c, v) => ({ ...c, drift: { ...c.drift, max_prediction_psi: v } }),
  },
];

export const EVIDENCE_FIELDS: ThresholdField[] = [
  {
    key: "minimum_requests",
    label: "Minimum requests",
    hint: "Per version, before latency and errors are judged.",
    unit: "req",
    min: 0,
    step: 10,
    integer: true,
    get: (c) => c.minimum_requests,
    set: (c, v) => ({ ...c, minimum_requests: v ?? 0 }),
  },
  {
    key: "evaluation_window_seconds",
    label: "Evaluation window",
    hint: "How much recent traffic each check looks at.",
    unit: "s",
    min: 1,
    step: 30,
    integer: true,
    get: (c) => c.evaluation_window_seconds,
    set: (c, v) => ({ ...c, evaluation_window_seconds: v ?? 0 }),
  },
  {
    key: "confidence_level",
    label: "Confidence level",
    hint: "A breach fails only when its whole interval is past the limit. Spent across the rollout's evaluations, so later ones need more evidence.",
    unit: "",
    min: 0.5,
    max: 0.999,
    step: 0.01,
    get: (c) => c.confidence_level,
    set: (c, v) => ({ ...c, confidence_level: v ?? 0 }),
  },
  {
    key: "minimum_labeled_samples",
    label: "Minimum labeled samples",
    hint: "Before recall is judged at all.",
    unit: "",
    min: 0,
    step: 5,
    integer: true,
    get: (c) => c.minimum_labeled_samples,
    set: (c, v) => ({ ...c, minimum_labeled_samples: v ?? 0 }),
  },
  {
    key: "minimum_label_coverage",
    label: "Minimum label coverage",
    hint: "Share of predictions that must have a label.",
    unit: "",
    min: 0,
    max: 1,
    step: 0.05,
    get: (c) => c.minimum_label_coverage,
    set: (c, v) => ({ ...c, minimum_label_coverage: v ?? 0 }),
  },
  {
    key: "minimum_positive_labels",
    label: "Minimum positive labels",
    hint: "Recall's denominator; too few and it means nothing.",
    unit: "",
    min: 0,
    step: 5,
    integer: true,
    get: (c) => c.minimum_positive_labels,
    set: (c, v) => ({ ...c, minimum_positive_labels: v ?? 0 }),
  },
  {
    key: "label_maturity_seconds",
    label: "Label maturity",
    hint: "Quality checks read a window this much older, so labels can land.",
    unit: "s",
    min: 0,
    step: 15,
    integer: true,
    get: (c) => c.label_maturity_seconds,
    set: (c, v) => ({ ...c, label_maturity_seconds: v ?? 0 }),
  },
  {
    key: "max_inconclusive_retries",
    label: "Max inconclusive retries",
    hint: "Then the rollout freezes for a human.",
    unit: "",
    min: 1,
    step: 1,
    integer: true,
    get: (c) => c.max_inconclusive_retries,
    set: (c, v) => ({ ...c, max_inconclusive_retries: v ?? 0 }),
  },
];

const ALL_FIELDS = [...GATE_FIELDS, ...EVIDENCE_FIELDS];

export type ThresholdDrafts = Record<string, string>;

/** The typed value, null for an optional field left empty (switched off), or
 * undefined when it isn't valid. */
function parse(field: ThresholdField, raw: string): number | null | undefined {
  if (raw.trim() === "") return field.optional ? null : undefined;
  const value = Number(raw);
  if (!Number.isFinite(value)) return undefined;
  if (field.integer && !Number.isInteger(value)) return undefined;
  if (value < field.min || (field.max != null && value > field.max)) return undefined;
  return value;
}

/** The config to send (null = nothing changed, let the server apply its
 * defaults), plus which fields are invalid or differ from the defaults. */
export function resolveThresholds(defaults: PolicyConfig, drafts: ThresholdDrafts) {
  let config = defaults;
  const invalid = new Set<string>();
  const changed = new Set<string>();
  for (const field of ALL_FIELDS) {
    const raw = drafts[field.key];
    if (raw == null) continue;
    const value = parse(field, raw);
    if (value === undefined) {
      invalid.add(field.key);
      continue;
    }
    if (value !== field.get(defaults)) {
      changed.add(field.key);
      config = field.set(config, value);
    }
  }
  return { config: changed.size ? config : null, invalid, changed };
}

function FieldRow({
  field,
  defaults,
  drafts,
  onChange,
  invalid,
  changed,
}: {
  field: ThresholdField;
  defaults: PolicyConfig;
  drafts: ThresholdDrafts;
  onChange: (key: string, raw: string) => void;
  invalid: boolean;
  changed: boolean;
}) {
  const id = `policy-${field.key}`;
  const fallback = field.get(defaults);
  return (
    <div className="space-y-1">
      <div className="flex items-center justify-between gap-2">
        <Label htmlFor={id} className="text-xs">
          {field.label}
        </Label>
        {changed ? (
          <span className="font-mono text-[10px] text-muted-foreground tabular">default {fallback ?? "off"}</span>
        ) : null}
      </div>
      <div className="relative">
        <Input
          id={id}
          inputMode="decimal"
          className={cn("h-8 pr-9 font-mono text-sm tabular", changed && "border-canary/60")}
          value={drafts[field.key] ?? (fallback == null ? "" : String(fallback))}
          placeholder={field.optional ? "off" : undefined}
          aria-invalid={invalid}
          onChange={(event) => onChange(field.key, event.target.value)}
        />
        {field.unit ? (
          <span className="pointer-events-none absolute inset-y-0 right-2.5 flex items-center text-xs text-muted-foreground">
            {field.unit}
          </span>
        ) : null}
      </div>
      <p className={cn("text-[11px] leading-snug", invalid ? "text-danger" : "text-muted-foreground")}>
        {invalid
          ? `Must be ${field.integer ? "a whole number " : ""}≥ ${field.min}${field.max != null ? ` and ≤ ${field.max}` : ""}.`
          : field.hint}
      </p>
    </div>
  );
}

export function PolicyThresholds({
  defaults,
  drafts,
  onChange,
  onReset,
}: {
  defaults: PolicyConfig;
  drafts: ThresholdDrafts;
  onChange: (key: string, raw: string) => void;
  onReset: () => void;
}) {
  const [open, setOpen] = useState(false);
  const { invalid, changed } = resolveThresholds(defaults, drafts);

  return (
    <div className="rounded-lg border">
      <button
        type="button"
        className="flex w-full items-center gap-2 px-3 py-2.5 text-left text-sm"
        aria-expanded={open}
        aria-controls="policy-thresholds"
        onClick={() => setOpen((v) => !v)}
      >
        <span className="font-medium">Policy thresholds</span>
        <span
          className={cn(
            "rounded-full px-2 py-0.5 text-[11px]",
            invalid.size
              ? "bg-danger/15 text-danger"
              : changed.size
                ? "bg-canary/15 text-canary"
                : "bg-muted text-muted-foreground",
          )}
        >
          {invalid.size ? `${invalid.size} invalid` : changed.size ? `${changed.size} custom` : "defaults"}
        </span>
        <ChevronDown className={cn("ml-auto size-4 text-muted-foreground transition-transform", open && "rotate-180")} />
      </button>
      {open ? (
        <div id="policy-thresholds" className="space-y-4 border-t px-3 py-3">
          <section className="space-y-2">
            <h4 className="text-[11px] font-medium tracking-wide text-muted-foreground uppercase">Decision gates</h4>
            <div className="grid gap-3 sm:grid-cols-2">
              {GATE_FIELDS.map((field) => (
                <FieldRow
                  key={field.key}
                  field={field}
                  defaults={defaults}
                  drafts={drafts}
                  onChange={onChange}
                  invalid={invalid.has(field.key)}
                  changed={changed.has(field.key)}
                />
              ))}
            </div>
          </section>
          <section className="space-y-2">
            <h4 className="text-[11px] font-medium tracking-wide text-muted-foreground uppercase">
              Evidence required before deciding
            </h4>
            <div className="grid gap-3 sm:grid-cols-3">
              {EVIDENCE_FIELDS.map((field) => (
                <FieldRow
                  key={field.key}
                  field={field}
                  defaults={defaults}
                  drafts={drafts}
                  onChange={onChange}
                  invalid={invalid.has(field.key)}
                  changed={changed.has(field.key)}
                />
              ))}
            </div>
          </section>
          <div className="flex justify-end">
            <Button type="button" variant="ghost" size="xs" onClick={onReset} disabled={!Object.keys(drafts).length}>
              <RotateCcw /> Reset to defaults
            </Button>
          </div>
        </div>
      ) : null}
    </div>
  );
}

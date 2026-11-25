"use client";

import { useState } from "react";
import { Loader2, Rocket } from "lucide-react";
import { useCreateDeployment, useModelVersions } from "@/lib/queries";
import type { DeploymentOut } from "@/lib/types";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { TickSlider } from "@/components/viz/TickSlider";
import { TrafficBar } from "@/components/TrafficBar";

interface NewDeploymentFormProps {
  modelNames: string[];
  onCreated: (deployment: DeploymentOut) => void;
}

export function NewDeploymentForm({ modelNames, onCreated }: NewDeploymentFormProps) {
  const [modelName, setModelName] = useState(modelNames[0] ?? "");
  // `null` = no explicit choice yet; default to the first (stable) / second
  // (canary) fetched version. Computed during render rather than synced via an
  // effect, so switching models can't leave the previous model's versions behind.
  const [stableOverride, setStableOverride] = useState<string | null>(null);
  const [canaryOverride, setCanaryOverride] = useState<string | null>(null);
  const [canaryWeightPercent, setCanaryWeightPercent] = useState(10);

  const versionsQuery = useModelVersions(modelName);
  const versions = versionsQuery.data;
  const create = useCreateDeployment();

  const stableVersion = stableOverride ?? versions?.[0] ?? "";
  const canaryVersion = canaryOverride ?? versions?.[1] ?? versions?.[0] ?? "";
  const sameVersion = stableVersion !== "" && stableVersion === canaryVersion;

  const canSubmit =
    modelName.length > 0 &&
    stableVersion.length > 0 &&
    canaryVersion.length > 0 &&
    !sameVersion &&
    !create.isPending;

  function handleSubmit(event: React.FormEvent) {
    event.preventDefault();
    create.mutate(
      {
        model_name: modelName,
        stable_version: stableVersion,
        canary_version: canaryVersion,
        canary_weight: canaryWeightPercent / 100,
      },
      { onSuccess: onCreated },
    );
  }

  const versionsDisabled = versionsQuery.isPending || !versions?.length;

  return (
    <form onSubmit={handleSubmit} className="space-y-5">
      <div className="grid gap-4 sm:grid-cols-3">
        <div className="space-y-1.5">
          <Label htmlFor="model">Model</Label>
          <Select
            value={modelName}
            onValueChange={(next) => {
              setModelName(next);
              setStableOverride(null);
              setCanaryOverride(null);
            }}
          >
            <SelectTrigger id="model" className="w-full font-mono">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {modelNames.map((name) => (
                <SelectItem key={name} value={name} className="font-mono">
                  {name}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>

        <div className="space-y-1.5">
          <Label htmlFor="stable">Stable version</Label>
          <Select value={stableVersion} onValueChange={setStableOverride} disabled={versionsDisabled}>
            <SelectTrigger id="stable" className="w-full font-mono">
              <SelectValue placeholder="—" />
            </SelectTrigger>
            <SelectContent>
              {(versions ?? []).map((version) => (
                <SelectItem key={version} value={version} className="font-mono">
                  {version}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>

        <div className="space-y-1.5">
          <Label htmlFor="canary">Canary version</Label>
          <Select value={canaryVersion} onValueChange={setCanaryOverride} disabled={versionsDisabled}>
            <SelectTrigger id="canary" className="w-full font-mono" aria-invalid={sameVersion}>
              <SelectValue placeholder="—" />
            </SelectTrigger>
            <SelectContent>
              {(versions ?? []).map((version) => (
                <SelectItem key={version} value={version} className="font-mono">
                  {version}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
      </div>

      <div className="space-y-3">
        <div className="flex items-baseline justify-between">
          <Label htmlFor="weight">Initial canary weight</Label>
          <span className="font-mono text-2xl font-semibold text-canary tabular">{canaryWeightPercent}%</span>
        </div>
        <TickSlider id="weight" min={1} max={50} value={canaryWeightPercent} onChange={setCanaryWeightPercent} />
        {stableVersion && canaryVersion ? (
          <TrafficBar
            size="sm"
            stableVersion={stableVersion}
            targets={[
              { version: stableVersion, weight: 100 - canaryWeightPercent },
              ...(sameVersion ? [] : [{ version: canaryVersion, weight: canaryWeightPercent }]),
            ]}
          />
        ) : null}
      </div>

      {versionsQuery.isError ? (
        <p className="text-sm text-danger">{versionsQuery.error.message}</p>
      ) : null}
      {sameVersion ? (
        <p className="text-sm text-warning">Stable and canary versions must differ.</p>
      ) : null}

      <Button type="submit" disabled={!canSubmit}>
        {create.isPending ? <Loader2 className="animate-spin" /> : <Rocket />}
        {create.isPending ? "Starting…" : "Start canary rollout"}
      </Button>
    </form>
  );
}

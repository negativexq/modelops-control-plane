"use client";

import { useState } from "react";
import { createDeployment, getModelVersions } from "@/lib/api";
import { useAsync } from "@/lib/useAsync";
import { useMutation } from "@/lib/useMutation";
import { ResultBanner } from "@/components/ResultBanner";
import type { DeploymentOut } from "@/lib/types";

interface NewDeploymentFormProps {
  modelNames: string[];
  onCreated: (deployment: DeploymentOut) => void;
}

export function NewDeploymentForm({ modelNames, onCreated }: NewDeploymentFormProps) {
  const [modelName, setModelName] = useState(modelNames[0] ?? "");
  // `null` means "no explicit user choice yet - default to the first (stable) /
  // second (canary) fetched version". Computed during render rather than synced via
  // an effect, so changing models can't leave stale state from the previous model's
  // version list hanging around.
  const [stableOverride, setStableOverride] = useState<string | null>(null);
  const [canaryOverride, setCanaryOverride] = useState<string | null>(null);
  const [canaryWeightPercent, setCanaryWeightPercent] = useState(10);

  const {
    data: versions,
    error: versionsError,
    loading: versionsLoading,
  } = useAsync(() => (modelName ? getModelVersions(modelName) : Promise.resolve([])), [modelName]);

  const stableVersion = stableOverride ?? versions?.[0] ?? "";
  const canaryVersion = canaryOverride ?? versions?.[1] ?? versions?.[0] ?? "";

  function handleModelChange(nextModelName: string) {
    setModelName(nextModelName);
    setStableOverride(null);
    setCanaryOverride(null);
  }

  const mutation = useMutation(
    () =>
      createDeployment(
        {
          model_name: modelName,
          stable_version: stableVersion,
          canary_version: canaryVersion,
          canary_weight: canaryWeightPercent / 100,
        },
        crypto.randomUUID(),
      ),
    "Deployment created.",
  );

  async function handleSubmit(event: React.FormEvent) {
    event.preventDefault();
    const deployment = await mutation.run();
    if (deployment) onCreated(deployment);
  }

  const canSubmit =
    modelName.length > 0 &&
    stableVersion.length > 0 &&
    canaryVersion.length > 0 &&
    stableVersion !== canaryVersion &&
    !mutation.submitting;

  return (
    <form onSubmit={handleSubmit} className="space-y-4">
      <div className="grid gap-4 sm:grid-cols-2">
        <label className="flex flex-col gap-1 text-sm">
          <span className="font-medium text-zinc-700 dark:text-zinc-300">Model</span>
          <select
            value={modelName}
            onChange={(event) => handleModelChange(event.target.value)}
            className="rounded-md border border-zinc-300 bg-white px-3 py-1.5 dark:border-zinc-700 dark:bg-zinc-900"
          >
            {modelNames.map((name) => (
              <option key={name} value={name}>
                {name}
              </option>
            ))}
          </select>
        </label>

        <label className="flex flex-col gap-1 text-sm">
          <span className="font-medium text-zinc-700 dark:text-zinc-300">
            Canary traffic weight ({canaryWeightPercent}%)
          </span>
          <input
            type="range"
            min={1}
            max={50}
            value={canaryWeightPercent}
            onChange={(event) => setCanaryWeightPercent(Number(event.target.value))}
          />
        </label>

        <label className="flex flex-col gap-1 text-sm">
          <span className="font-medium text-zinc-700 dark:text-zinc-300">Stable version</span>
          <select
            value={stableVersion}
            onChange={(event) => setStableOverride(event.target.value)}
            disabled={versionsLoading || !versions?.length}
            className="rounded-md border border-zinc-300 bg-white px-3 py-1.5 dark:border-zinc-700 dark:bg-zinc-900"
          >
            {(versions ?? []).map((version) => (
              <option key={version} value={version}>
                {version}
              </option>
            ))}
          </select>
        </label>

        <label className="flex flex-col gap-1 text-sm">
          <span className="font-medium text-zinc-700 dark:text-zinc-300">Canary version</span>
          <select
            value={canaryVersion}
            onChange={(event) => setCanaryOverride(event.target.value)}
            disabled={versionsLoading || !versions?.length}
            className="rounded-md border border-zinc-300 bg-white px-3 py-1.5 dark:border-zinc-700 dark:bg-zinc-900"
          >
            {(versions ?? []).map((version) => (
              <option key={version} value={version}>
                {version}
              </option>
            ))}
          </select>
        </label>
      </div>

      {versionsError ? (
        <p className="text-sm text-red-600 dark:text-red-400">{versionsError.message}</p>
      ) : null}
      {stableVersion && stableVersion === canaryVersion ? (
        <p className="text-sm text-amber-600 dark:text-amber-400">
          Stable and canary versions must differ.
        </p>
      ) : null}

      <button
        type="submit"
        disabled={!canSubmit}
        className="rounded-md bg-zinc-900 px-4 py-2 text-sm font-medium text-white disabled:cursor-not-allowed disabled:opacity-50 dark:bg-zinc-100 dark:text-black"
      >
        {mutation.submitting ? "Starting deployment…" : "Start canary deployment"}
      </button>

      <ResultBanner result={mutation.result} onDismiss={mutation.clearResult} />
    </form>
  );
}

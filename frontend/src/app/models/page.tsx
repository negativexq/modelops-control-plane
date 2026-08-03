"use client";

import { useRouter } from "next/navigation";
import { getModels } from "@/lib/api";
import { useAsync } from "@/lib/useAsync";
import { AsyncBoundary } from "@/components/AsyncBoundary";
import { Card } from "@/components/Card";
import { ModelVersionsTable } from "@/components/ModelVersionsTable";
import { NewDeploymentForm } from "@/components/NewDeploymentForm";

export default function ModelsPage() {
  const router = useRouter();
  const { data, error, loading, refetch } = useAsync(() => getModels());

  return (
    <div className="space-y-6">
      <h1 className="text-xl font-semibold tracking-tight">Models</h1>

      <AsyncBoundary data={data} error={error} loading={loading} onRetry={refetch}>
        {(modelNames) =>
          modelNames.length === 0 ? (
            <Card title="No models found">
              <p className="text-sm text-zinc-600 dark:text-zinc-400">
                Run <code className="rounded bg-zinc-100 px-1 dark:bg-zinc-800">make prepare-models</code>{" "}
                to generate the fraud-model artifacts.
              </p>
            </Card>
          ) : (
            <div className="space-y-6">
              {modelNames.map((modelName) => (
                <Card key={modelName} title={modelName}>
                  <ModelVersionsTable modelName={modelName} />
                </Card>
              ))}

              <Card title="Start a new canary deployment">
                <NewDeploymentForm
                  modelNames={modelNames}
                  onCreated={(deployment) => router.push(`/deployments/${deployment.id}`)}
                />
              </Card>
            </div>
          )
        }
      </AsyncBoundary>
    </div>
  );
}

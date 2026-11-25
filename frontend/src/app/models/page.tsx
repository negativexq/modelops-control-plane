"use client";

import { useRouter } from "next/navigation";
import { Boxes } from "lucide-react";
import { useModels } from "@/lib/queries";
import { PageHeader } from "@/components/PageHeader";
import { QueryState } from "@/components/QueryState";
import { ModelVersionsTable } from "@/components/ModelVersionsTable";
import { NewDeploymentForm } from "@/components/NewDeploymentForm";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Empty, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from "@/components/ui/empty";

export default function ModelsPage() {
  const router = useRouter();
  const query = useModels();

  return (
    <div className="space-y-6">
      <PageHeader title="Models" description="Registered model versions and their offline evaluation." />

      <QueryState query={query}>
        {(modelNames) =>
          modelNames.length === 0 ? (
            <Empty className="border">
              <EmptyHeader>
                <EmptyMedia variant="icon">
                  <Boxes />
                </EmptyMedia>
                <EmptyTitle>No models in the registry</EmptyTitle>
                <EmptyDescription>
                  Run <code className="font-mono">make prepare-models</code> to generate the
                  fraud-model artifacts.
                </EmptyDescription>
              </EmptyHeader>
            </Empty>
          ) : (
            <div className="space-y-6">
              <Card>
                <CardHeader>
                  <CardTitle>Start a canary rollout</CardTitle>
                  <CardDescription>
                    Route a slice of live traffic to a candidate version. The worker evaluates
                    policies and promotes or rolls back on its own.
                  </CardDescription>
                </CardHeader>
                <CardContent>
                  <NewDeploymentForm
                    modelNames={modelNames}
                    onCreated={(deployment) => router.push(`/deployments/${deployment.id}`)}
                  />
                </CardContent>
              </Card>

              {modelNames.map((modelName) => (
                <Card key={modelName}>
                  <CardHeader>
                    <CardTitle className="font-mono">{modelName}</CardTitle>
                  </CardHeader>
                  <CardContent>
                    <ModelVersionsTable modelName={modelName} />
                  </CardContent>
                </Card>
              ))}
            </div>
          )
        }
      </QueryState>
    </div>
  );
}

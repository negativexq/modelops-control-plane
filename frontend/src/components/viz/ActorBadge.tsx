import { Bot, Hand } from "lucide-react";
import type { Actor } from "@/lib/derive";

export function ActorBadge({ actor, by = null }: { actor: Actor | null; by?: string | null }) {
  if (!actor) return null;
  const Icon = actor === "automatic" ? Bot : Hand;
  const label = actor === "automatic" ? "Policy engine" : (by ?? "Operator");
  return (
    <span className="inline-flex min-w-0 items-center gap-1 text-xs text-muted-foreground" title={by ? `Operator: ${by}` : undefined}>
      <Icon className="size-3.5 shrink-0" aria-hidden />
      <span className="truncate">{label}</span>
    </span>
  );
}

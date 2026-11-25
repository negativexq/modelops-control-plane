import { Bot, Hand } from "lucide-react";
import type { Actor } from "@/lib/derive";

export function ActorBadge({ actor }: { actor: Actor | null }) {
  if (!actor) return null;
  const Icon = actor === "automatic" ? Bot : Hand;
  return (
    <span className="inline-flex items-center gap-1 text-xs text-muted-foreground">
      <Icon className="size-3.5" aria-hidden />
      {actor === "automatic" ? "Policy engine" : "Operator"}
    </span>
  );
}

import type { DeploymentStatus, PolicyEvaluationResult } from "./types";

/** Every rollout state maps to one of five semantic tones (see globals.css
 * --stable/--canary/--success/--warning/--danger). Colour is meaning here, so
 * the mapping lives in one place instead of per-component class soup. */
export type Tone = "neutral" | "canary" | "success" | "warning" | "danger";

export const STATUS_META: Record<
  DeploymentStatus,
  { tone: Tone; label: string; description: string }
> = {
  PENDING: { tone: "neutral", label: "Pending", description: "Created, not yet routed." },
  DEPLOYING: { tone: "canary", label: "Deploying", description: "Pushing the initial split to the router." },
  CANARY_RUNNING: { tone: "canary", label: "Canary running", description: "Canary is receiving live traffic." },
  EVALUATING: { tone: "warning", label: "Evaluating", description: "Policy checks are deciding the outcome." },
  PROMOTING: { tone: "canary", label: "Promoting", description: "Shifting all traffic to the canary." },
  PROMOTED: { tone: "success", label: "Promoted", description: "Canary serves 100% of traffic." },
  ROLLING_BACK: { tone: "warning", label: "Rolling back", description: "Shifting all traffic back to stable." },
  ROLLED_BACK: { tone: "danger", label: "Rolled back", description: "Stable serves 100% of traffic." },
  FAILED: { tone: "danger", label: "Failed", description: "The rollout hit an unrecoverable error." },
  INCONCLUSIVE: {
    tone: "warning",
    label: "Inconclusive",
    description: "Not enough evidence to decide; frozen for a human.",
  },
};

export const POLICY_TONE: Record<PolicyEvaluationResult, Tone> = {
  PASS: "success",
  FAIL: "danger",
  INCONCLUSIVE: "warning",
};

/** Soft pill background + readable text for a tone. */
export const TONE_PILL: Record<Tone, string> = {
  neutral: "bg-stable/12 text-stable border-stable/20",
  canary: "bg-canary/12 text-canary border-canary/25",
  success: "bg-success/12 text-success border-success/25",
  warning: "bg-warning/12 text-warning border-warning/25",
  danger: "bg-danger/12 text-danger border-danger/25",
};

export const TONE_DOT: Record<Tone, string> = {
  neutral: "bg-stable",
  canary: "bg-canary",
  success: "bg-success",
  warning: "bg-warning",
  danger: "bg-danger",
};

export const TONE_TEXT: Record<Tone, string> = {
  neutral: "text-muted-foreground",
  canary: "text-canary",
  success: "text-success",
  warning: "text-warning",
  danger: "text-danger",
};

import { Activity, Boxes, FlaskConical, GitBranch, Waypoints, type LucideIcon } from "lucide-react";

export interface NavItem {
  href: string;
  label: string;
  icon: LucideIcon;
  description: string;
}

export const NAV_ITEMS: NavItem[] = [
  { href: "/", label: "Overview", icon: Activity, description: "Fleet status and recent decisions" },
  { href: "/deployments", label: "Deployments", icon: GitBranch, description: "Every canary rollout" },
  { href: "/gateway", label: "Gateway", icon: Waypoints, description: "Live request flow and the control loop" },
  { href: "/models", label: "Models", icon: Boxes, description: "Registry and new rollouts" },
  { href: "/benchmarks", label: "Benchmarks", icon: FlaskConical, description: "Scripted failure scenarios" },
];

export function isActivePath(pathname: string, href: string) {
  return href === "/" ? pathname === "/" : pathname.startsWith(href);
}

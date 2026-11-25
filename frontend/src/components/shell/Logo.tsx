/** Mark: a traffic split - a long stable bar and a short canary bar. */
export function Logo({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 24 24" className={className} aria-hidden>
      <rect x="2" y="5" width="20" height="14" rx="4" className="fill-foreground/8 stroke-foreground/15" />
      <rect x="5" y="9" width="10" height="2.5" rx="1.25" className="fill-stable" />
      <rect x="5" y="12.5" width="5" height="2.5" rx="1.25" className="fill-canary" />
      <circle cx="18" cy="12" r="1.6" className="fill-success" />
    </svg>
  );
}

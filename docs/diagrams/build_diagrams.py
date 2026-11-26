"""Generates the README's diagrams as self-contained SVGs, in the dashboard's own
light palette. Beams are animated with CSS (the same dash-flow as the Gateway
page); GitHub plays the animation when the SVG is shown as an image.

    python3 docs/diagrams/build_diagrams.py
"""

from __future__ import annotations

from pathlib import Path
from xml.sax.saxutils import escape

OUT = Path(__file__).resolve().parent

BG, FG, MUTED, BORDER = "#f9fafb", "#12161d", "#646972", "#dfe1e5"
STABLE, CANARY, SUCCESS, WARNING, DANGER = "#6b727e", "#0086d8", "#009956", "#de9300", "#e23439"
SANS = "ui-sans-serif, -apple-system, BlinkMacSystemFont, 'Segoe UI', Inter, Helvetica, Arial, sans-serif"
MONO = "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace"

STYLE = f"""
  .t {{ font-family: {SANS}; fill: {FG}; }}
  .m {{ font-family: {MONO}; fill: {FG}; }}
  .mut {{ fill: {MUTED}; }}
  .flow {{ stroke-dasharray: 6 8; animation: flow 1.6s linear infinite; }}
  .fast {{ animation-duration: 0.8s; }}
  .slow {{ animation-duration: 3s; }}
  @keyframes flow {{ to {{ stroke-dashoffset: -28; }} }}
  .pulse {{ animation: pulse 2.4s ease-in-out infinite; transform-box: fill-box; transform-origin: center; }}
  @keyframes pulse {{ 0%, 100% {{ opacity: 1; }} 50% {{ opacity: .35; }} }}
  @media (prefers-reduced-motion: reduce) {{ .flow, .pulse {{ animation: none; }} }}
"""


def svg(width: int, height: int, body: list[str], title: str) -> str:
    return "\n".join(
        [
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
            f'width="{width}" height="{height}" role="img" aria-label="{escape(title)}">',
            f"<title>{escape(title)}</title>",
            f"<style>{STYLE}</style>",
            "<defs>",
            '  <filter id="sh" x="-10%" y="-10%" width="120%" height="140%">'
            '<feDropShadow dx="0" dy="1" stdDeviation="1.6" flood-color="#0f172a" flood-opacity=".08"/></filter>',
            f'  <marker id="ah" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
            f'<path d="M0,0 L10,5 L0,10 z" fill="{MUTED}"/></marker>',
            "</defs>",
            f'<rect x="0.5" y="0.5" width="{width - 1}" height="{height - 1}" rx="16" fill="{BG}" stroke="{BORDER}"/>',
            *body,
            "</svg>",
            "",
        ]
    )


def text(x: float, y: float, s: str, *, size: float = 13, weight: int = 400, cls: str = "t",
         anchor: str = "start", fill: str | None = None) -> str:
    f = f' style="fill:{fill}"' if fill else ""
    return (f'<text x="{x}" y="{y}" class="{cls}" font-size="{size}" font-weight="{weight}" '
            f'text-anchor="{anchor}"{f}>{escape(s)}</text>')


def box(x: float, y: float, w: float, h: float, *, rx: float = 10, stroke: str = BORDER,
        fill: str = "#ffffff", dash: bool = False) -> str:
    d = ' stroke-dasharray="4 4"' if dash else ""
    return (f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" fill="{fill}" '
            f'stroke="{stroke}"{d} filter="url(#sh)"/>')


def dot(x: float, y: float, color: str, r: float = 4.5, pulse: bool = False) -> str:
    c = ' class="pulse"' if pulse else ""
    return f'<circle cx="{x}" cy="{y}" r="{r}" fill="{color}"{c}/>'


def node(x: float, y: float, w: float, h: float, title: str, sub: str = "", *,
         color: str | None = None, mono: bool = False, stroke: str = BORDER) -> list[str]:
    out = [box(x, y, w, h, stroke=stroke)]
    tx = x + 16
    if color:
        out.append(dot(x + 18, y + 24, color))
        tx = x + 30
    out.append(text(tx, y + 28.5, title, size=14, weight=600, cls="m" if mono else "t"))
    if sub:
        for i, line in enumerate(sub.split("\n")):
            out.append(text(x + 16, y + 49 + i * 17, line, size=12, cls="t mut"))
    return out


def beam(d: str, color: str, width: float, *, speed: str = "", idle: bool = False,
         arrow: bool = False) -> list[str]:
    track = (f'<path d="{d}" fill="none" stroke="{BORDER}" stroke-width="{width + 2}" '
             f'stroke-linecap="round"/>')
    if idle:
        return [track]
    m = ' marker-end="url(#ah)"' if arrow else ""
    return [track, f'<path d="{d}" fill="none" stroke="{color}" stroke-width="{width}" '
                   f'stroke-linecap="round" class="flow {speed}"{m}/>']


def pill(x: float, y: float, s: str, *, color: str = MUTED, mono: bool = True,
         anchor: str = "middle") -> list[str]:
    w = len(s) * (6.9 if mono else 6.4) + 16
    left = x - w / 2 if anchor == "middle" else x
    return [f'<rect x="{left}" y="{y - 12}" width="{w}" height="20" rx="10" fill="#ffffff" stroke="{BORDER}"/>',
            text(left + w / 2, y + 2, s, size=11.5, cls="m" if mono else "t", anchor="middle", fill=color)]


def curve(x1: float, y1: float, x2: float, y2: float) -> str:
    mx = (x1 + x2) / 2
    return f"M{x1},{y1} C{mx},{y1} {mx},{y2} {x2},{y2}"


# --- architecture ---------------------------------------------------------------

def architecture() -> str:
    b: list[str] = []
    W, H = 1240, 660

    # Top tier: who talks to the control plane.
    b += node(40, 70, 210, 82, "Label source", "delayed, partial ground truth", color=WARNING)
    b += [box(340, 40, 540, 220, rx=14)]
    b += [text(364, 72, "Control plane", size=16, weight=650), text(364, 92, "backend :8000 · FastAPI", size=12, cls="t mut")]
    for i, (t, s, c) in enumerate([
        ("REST API", "+ live stream (SSE)", CANARY),
        ("Policy engine", "CIs · p-values", SUCCESS),
        ("SQLite", "desired state, revisioned", STABLE),
    ]):
        x = 364 + i * 170
        b += [f'<rect x="{x}" y="118" width="154" height="70" rx="9" fill="{BG}" stroke="{BORDER}"/>',
              dot(x + 16, 140, c, 4), text(x + 27, 144.5, t, size=13, weight=600), text(x + 14, 168, s, size=11.5, cls="t mut")]
    b += [text(364, 232, "7 checks per evaluation  ·  one metric row per request  ·  audit trail", size=11.5, cls="t mut")]
    b += node(990, 40, 210, 82, "Worker", "promote · roll back · reconcile", color=SUCCESS)
    b += node(990, 170, 210, 82, "Dashboard", "Next.js :3000", color=CANARY)

    # Bottom tier: the request path.
    b += node(40, 470, 170, 82, "Clients", "POST /predict")
    b += [box(340, 330, 330, 290, rx=14)]
    b += [text(364, 362, "Gateway", size=16, weight=650), text(364, 382, "router :8080", size=12, cls="t mut")]
    stages = [("Rate limit", "token bucket per model → 429"),
              ("Sticky routing", "weighted rendezvous hash"),
              ("Circuit breaker", "per version → 503 fail fast")]
    for i, (t, s) in enumerate(stages):
        y = 402 + i * 72
        b += [f'<rect x="364" y="{y}" width="282" height="54" rx="9" fill="{BG}" stroke="{BORDER}"/>',
              text(380, y + 22, t, size=13, weight=600), text(380, y + 41, s, size=11.5, cls="t mut")]
        if i:
            b.append(f'<path d="M505,{y - 17} L505,{y - 3}" stroke="{MUTED}" stroke-width="1.4" marker-end="url(#ah)"/>')

    versions = [("v1", "stable · 75%", STABLE, 6, ""),
                ("v2-good", "canary · 25%", CANARY, 3, ""),
                ("v2-quality-bad", "idle · 0%", STABLE, 0, ""),
                ("v2-good-*-fault", "latency / error injection", DANGER, 0, "")]
    for i, (t, s, c, wdt, _) in enumerate(versions):
        y = 330 + i * 76
        b += node(990, y, 210, 62, t, "", color=c, mono=True)
        b.append(text(1004, y + 50, s, size=11.5, cls="t mut"))
        b += beam(curve(646, 573, 990, y + 31), c, wdt or 1, idle=wdt == 0,
                  speed="fast" if i == 0 else "")

    # Edges.
    b += beam("M210,511 L340,511", FG, 3, speed="fast")
    b += beam(curve(250, 111, 340, 111), WARNING, 2, speed="slow")
    b.append(text(295, 100, "labels", size=11.5, cls="m", anchor="middle", fill=WARNING))
    b += beam("M990,81 L880,81", SUCCESS, 2)
    b.append(text(935, 70, "evaluate · act", size=11.5, cls="m", anchor="middle", fill=SUCCESS))
    b += beam("M880,211 L990,211", CANARY, 2, speed="slow")
    b.append(text(935, 200, "live stream", size=11.5, cls="m", anchor="middle", fill=CANARY))
    # Gateway <-> control plane: metrics up, revisioned config down.
    b += beam("M440,330 L440,260", CANARY, 2.5, speed="fast")
    b.append(text(428, 299, "metric per request", size=11.5, cls="m", anchor="end", fill=CANARY))
    b += beam("M590,260 L590,330", SUCCESS, 2.5, speed="slow")
    b.append(text(602, 299, "PUT /router/config · r7", size=11.5, cls="m", anchor="start", fill=SUCCESS))

    b += [text(40, 640, "Width is the share of traffic, speed is the request rate.", size=12, cls="t mut")]
    return svg(W, H, b, "ModelOps architecture: clients, gateway, model versions, control plane, worker, dashboard")


# --- control loop ---------------------------------------------------------------

def control_loop() -> str:
    b: list[str] = []
    W, H = 1240, 470
    y, h = 60, 230
    cols = [(40, 200), (300, 230), (590, 350), (1010, 190)]

    # Gateway
    x, w = cols[0]
    b += [box(x, y, w, h, rx=12)]
    b += [dot(x + 18, y + 26, SUCCESS, pulse=True), text(x + 32, y + 31, "Gateway", size=15, weight=650)]
    b += [text(x + 16, y + 60, "every request becomes", size=12, cls="t mut"),
          text(x + 16, y + 77, "one metric row", size=12, cls="t mut"),
          text(x + 16, y + 120, "100.3", size=30, weight=650, cls="m"), text(x + 16, y + 142, "req/s · last 10 s", size=12, cls="t mut"),
          text(x + 16, y + 180, "latency · status ·", size=12, cls="m mut"), text(x + 16, y + 197, "prediction_id", size=12, cls="m mut")]

    # Control plane
    x, w = cols[1]
    b += [box(x, y, w, h, rx=12)]
    b += [dot(x + 18, y + 26, CANARY), text(x + 32, y + 31, "Control plane", size=15, weight=650)]
    b += [text(x + 16, y + 60, "windows the samples", size=12, cls="t mut")]
    for i, (t, s, c) in enumerate([("reliability", "last 60 s", CANARY), ("quality", "matured labels", WARNING)]):
        yy = y + 80 + i * 66
        b += [f'<rect x="{x + 16}" y="{yy}" width="{w - 32}" height="52" rx="8" fill="{BG}" stroke="{BORDER}"/>',
              text(x + 30, yy + 21, t, size=13, weight=600), text(x + 30, yy + 39, s, size=11.5, cls="t mut")]
        # a mini sample strip
        for k in range(10):
            b.append(f'<rect x="{x + 120 + k * 9}" y="{yy + 15}" width="5" height="22" rx="1.5" fill="{c}" opacity="{0.25 + 0.07 * k:.2f}"/>')

    # Policy engine
    x, w = cols[2]
    b += [box(x, y, w, h, rx=12)]
    b += [dot(x + 18, y + 26, SUCCESS), text(x + 32, y + 31, "Policy engine", size=15, weight=650),
          text(x + w - 16, y + 31, "95% CI", size=12, cls="m mut", anchor="end")]
    checks = [("error rate", "0.4%", "[0.2, 0.9]", SUCCESS),
              ("p95 Δ", "+3126%", "[2984, 3289]", DANGER),
              ("recall", "0.943", "[0.898, 0.969]", SUCCESS),
              ("label coverage", "—", "waiting", WARNING)]
    for i, (n, v, ci, c) in enumerate(checks):
        yy = y + 66 + i * 38
        b += [dot(x + 22, yy - 4, c, 4), text(x + 34, yy, n, size=13),
              text(x + 190, yy, v, size=13, weight=600, cls="m", anchor="end"),
              text(x + w - 16, yy, ci, size=12, cls="m mut", anchor="end")]
    b += [f'<line x1="{x + 16}" y1="{y + 200}" x2="{x + w - 16}" y2="{y + 200}" stroke="{BORDER}"/>',
          text(x + 16, y + 220, "FAIL > INCONCLUSIVE > PASS", size=12, weight=600, cls="m")]

    # Worker
    x, w = cols[3]
    b += [box(x, y, w, h, rx=12)]
    b += [dot(x + 18, y + 26, STABLE), text(x + 32, y + 31, "Worker", size=15, weight=650),
          text(x + 16, y + 60, "acts on the verdict", size=12, cls="t mut")]
    for i, (v, act, c) in enumerate([("PASS", "next step / promote", SUCCESS),
                                     ("FAIL", "roll back", DANGER),
                                     ("INCONCLUSIVE", "retry, then freeze", WARNING)]):
        yy = y + 92 + i * 44
        b += [text(x + 16, yy, v, size=12, weight=650, cls="m", fill=c), text(x + 16, yy + 18, act, size=12, cls="t mut")]

    # Forward links
    for (x1, w1), (x2, _), label, speed in [(cols[0], cols[1], "metrics", "fast"),
                                           (cols[1], cols[2], "evaluate", ""),
                                           (cols[2], cols[3], "verdict", "")]:
        a, z = x1 + w1, x2
        b += beam(f"M{a},{y + h / 2} L{z},{y + h / 2}", CANARY, 2.5, speed=speed)
        b.append(text((a + z) / 2, y + h / 2 - 12, label, size=11.5, cls="m mut", anchor="middle"))

    # Return arc: the decision becomes the gateway's next split.
    rx1, rx2, top, bottom = 1105, 140, y + h, y + h + 90
    d = f"M{rx1},{top} L{rx1},{bottom - 18} Q{rx1},{bottom} {rx1 - 18},{bottom} L{rx2 + 18},{bottom} Q{rx2},{bottom} {rx2},{bottom - 18} L{rx2},{top}"
    b += beam(d, SUCCESS, 2.5, speed="slow")
    b += pill((rx1 + rx2) / 2, bottom, "config push · routing revision r7 → r8", color=SUCCESS)

    # Labels feed the quality window from below.
    b += beam(f"M415,{H - 40} L415,{y + h}", WARNING, 2, speed="slow")
    b += pill(415, H - 40, "delayed labels · POST /api/labels", color=WARNING)
    b += [text(W - 40, H - 36, "polls every 15 s · evaluates each rollout once per window · same API an operator uses", size=12, cls="t mut", anchor="end")]
    return svg(W, H, b, "The control loop: gateway metrics, control plane windows, policy engine, worker, config push back to the gateway")


# --- decision rule --------------------------------------------------------------

def decision() -> str:
    b: list[str] = []
    W, H = 1240, 400
    x0, x1 = 300, 860  # axis: 0% .. 12% error rate
    def sx(v: float) -> float:
        return x0 + (x1 - x0) * v / 12

    b += [text(40, 52, "How one check decides", size=17, weight=650),
          text(40, 76, "Canary error rate with its 95% confidence interval, against a 5% limit.", size=13, cls="t mut")]
    limit = sx(5)
    b += [f'<rect x="{limit}" y="100" width="{sx(12) - limit}" height="240" fill="{DANGER}" opacity=".05"/>',
          f'<line x1="{limit}" y1="96" x2="{limit}" y2="344" stroke="{DANGER}" stroke-width="1.5" stroke-dasharray="5 4"/>',
          text(limit + 8, 112, "limit 5%", size=12, weight=600, cls="m", fill=DANGER)]
    for v in range(0, 13, 2):
        b += [f'<line x1="{sx(v)}" y1="344" x2="{sx(v)}" y2="350" stroke="{MUTED}"/>',
              text(sx(v), 368, f"{v}%", size=11.5, cls="m mut", anchor="middle")]
    b.append(f'<line x1="{x0}" y1="344" x2="{x1}" y2="344" stroke="{BORDER}" stroke-width="1.5"/>')

    rows = [
        ("PASS", SUCCESS, 1.3, 0.7, 2.4, "estimate within the limit", "keep going: next traffic step"),
        ("INCONCLUSIVE", WARNING, 6.2, 3.4, 10.1, "past the limit, but the interval", "still reaches below it: could be noise"),
        ("FAIL", DANGER, 9.1, 7.3, 11.2, "the whole interval is", "past the limit: roll back"),
    ]
    for i, (verdict, c, est, lo, hi, l1, l2) in enumerate(rows):
        y = 160 + i * 70
        w = len(verdict) * 7.6 + 22
        b += [f'<rect x="40" y="{y - 14}" width="{w}" height="26" rx="13" fill="{c}" opacity=".12"/>',
              text(40 + w / 2, y + 4, verdict, size=12, weight=700, cls="m", anchor="middle", fill=c)]
        b += [f'<line x1="{sx(lo)}" y1="{y}" x2="{sx(hi)}" y2="{y}" stroke="{c}" stroke-width="6" stroke-linecap="round" opacity=".35"/>',
              f'<line x1="{sx(lo)}" y1="{y - 9}" x2="{sx(lo)}" y2="{y + 9}" stroke="{c}" stroke-width="2"/>',
              f'<line x1="{sx(hi)}" y1="{y - 9}" x2="{sx(hi)}" y2="{y + 9}" stroke="{c}" stroke-width="2"/>',
              dot(sx(est), y, c, 6.5, pulse=verdict == "INCONCLUSIVE"),
              f'<circle cx="{sx(est)}" cy="{y}" r="6.5" fill="none" stroke="#ffffff" stroke-width="2"/>',
              text(sx(est), y - 16, f"{est}%", size=11.5, weight=600, cls="m", anchor="middle", fill=c)]
        b += [text(900, y - 3, l1, size=13), text(900, y + 15, l2, size=13, cls="t mut")]
    return svg(W, H, b, "Decision rule: PASS inside the limit, INCONCLUSIVE when the interval straddles it, FAIL when the whole interval is past it")


# --- reconcile ------------------------------------------------------------------

def reconcile() -> str:
    b: list[str] = []
    W, H = 1240, 330
    b += [text(40, 52, "Desired state vs observed state", size=17, weight=650),
          text(40, 76, "The database's split is the truth. The router's copy can vanish at any time; the worker puts it back.", size=13, cls="t mut")]
    x0, x1 = 230, 1190
    lanes = [("Desired", "database", 140), ("Observed", "router", 230)]
    for name, sub, y in lanes:
        b += [text(40, y - 2, name, size=14, weight=650), text(40, y + 16, sub, size=12, cls="t mut"),
              f'<line x1="{x0}" y1="{y + 4}" x2="{x1}" y2="{y + 4}" stroke="{BORDER}" stroke-width="1.5"/>']

    def seg(xa: float, xb: float, y: float, label: str, c: str, *, flow: bool = False) -> list[str]:
        out = [f'<rect x="{xa}" y="{y - 13}" width="{xb - xa - 6}" height="34" rx="8" fill="{c}" opacity=".12"/>',
               f'<rect x="{xa}" y="{y - 13}" width="4" height="34" rx="2" fill="{c}"/>',
               text(xa + 14, y + 9, label, size=12.5, weight=600, cls="m", fill=c)]
        return out

    # desired: r6 -> r7
    b += seg(x0, 520, 140, "r6 · canary 10%", STABLE)
    b += seg(520, x1, 140, "r7 · canary 25%", CANARY)
    # observed: r6 -> r7 -> r0 (restart) -> r7 (reconciled)
    b += seg(x0, 540, 230, "r6", STABLE)
    b += seg(540, 760, 230, "r7", CANARY)
    b += seg(760, 960, 230, "r0 bootstrap", DANGER)
    b += seg(960, x1, 230, "r7 · router_reconciled", SUCCESS)

    for x, label, c in [(520, "worker: PASS → 25%", CANARY), (760, "router restarts", DANGER), (960, "next worker tick", SUCCESS)]:
        b += [f'<line x1="{x}" y1="104" x2="{x}" y2="270" stroke="{c}" stroke-width="1.2" stroke-dasharray="3 4"/>',
              text(x, 290, label, size=12, cls="t", anchor="middle", fill=c)]
    b += beam("M528,154 L548,216", CANARY, 1.5, speed="fast")
    b += beam("M968,154 L968,216", SUCCESS, 2, speed="")
    b += [text(1190, 316, "A push older than what the router holds is rejected (409), so a late push can't bring back an old split.", size=11.5, cls="t mut", anchor="end")]
    return svg(W, H, b, "Reconciliation timeline: desired revision r7, router restarts to r0, worker re-pushes r7")


if __name__ == "__main__":
    for name, fn in [("architecture", architecture), ("control-loop", control_loop),
                     ("decision", decision), ("reconcile", reconcile)]:
        (OUT / f"{name}.svg").write_text(fn())
        print(f"wrote docs/diagrams/{name}.svg")

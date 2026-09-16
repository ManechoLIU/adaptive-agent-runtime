"""Self-contained HTML renderer for project model intelligence reports."""

from __future__ import annotations

import html
import json
import math
from typing import Any


def _e(value: Any) -> str:
    return html.escape(str(value if value is not None else "—"), quote=True)


def _score(value: Any) -> str:
    if isinstance(value, (int, float)):
        return f"{float(value):.1f}"
    return "—"


def _pct(value: Any) -> float:
    if not isinstance(value, (int, float)):
        return 0.0
    return max(0.0, min(100.0, float(value)))


def _benchmark_score(group: dict[str, Any]) -> Any:
    benchmark = group.get("benchmark")
    return benchmark.get("score") if isinstance(benchmark, dict) else None


def _route_lookup(report: dict[str, Any]) -> dict[tuple[str, str, str, str], dict[str, Any]]:
    result: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for item in report.get("route_groups", []):
        if not isinstance(item, dict) or not isinstance(item.get("route"), dict):
            continue
        route = item["route"]
        key = tuple(str(route.get(field) or "unknown") for field in ("provider", "model", "auth_mode", "execution_transport"))
        result[key] = item
    return result


def _model_key(identity: dict[str, Any]) -> tuple[str, str, str, str]:
    return tuple(str(identity.get(field) or "unknown") for field in ("provider", "model", "auth_mode", "execution_transport"))


def _comparison_rows(report: dict[str, Any]) -> str:
    routes = _route_lookup(report)
    rows: list[str] = []
    for group in report.get("model_groups", []):
        if not isinstance(group, dict):
            continue
        identity = group.get("identity") if isinstance(group.get("identity"), dict) else {}
        route = routes.get(_model_key(identity), {})
        project = group.get("project_model_score")
        reliability = route.get("route_reliability_score")
        external = _benchmark_score(group)
        match = (group.get("benchmark") or {}).get("match") if isinstance(group.get("benchmark"), dict) else "none"
        route_note = f"{identity.get('provider','unknown')} · {identity.get('auth_mode','unknown')} · {identity.get('reasoning_effort','unknown')}"
        rows.append(
            f"""
            <article class="score-row">
              <div class="score-identity">
                <span class="eyebrow">{_e(identity.get('execution_role','unknown'))} · {_e(identity.get('policy_class','unknown'))}</span>
                <h3>{_e(identity.get('model','unknown'))}</h3>
                <p>{_e(route_note)}</p>
              </div>
              <div class="score-tracks">
                <div class="track-line"><span>Project score</span><strong>{_score(project)}</strong><i class="bar project" style="--v:{_pct(project)}%"></i></div>
                <div class="track-line"><span>Route reliability</span><strong>{_score(reliability)}</strong><i class="bar route" style="--v:{_pct(reliability)}%"></i></div>
                <div class="track-line"><span>External baseline</span><strong>{_score(external)}</strong><i class="bar external" style="--v:{_pct(external)}%"></i></div>
              </div>
              <div class="score-meta">
                <span>{_e(group.get('confidence','low'))} confidence</span>
                <span>{_e(group.get('quality_sample_count',0))} quality samples</span>
                <span>baseline {_e(match)}</span>
              </div>
            </article>
            """
        )
    if not rows:
        return '<p class="empty">No scorable model evidence in this window.</p>'
    return "\n".join(rows)


def _orbit_svg(report: dict[str, Any]) -> str:
    groups = [g for g in report.get("model_groups", []) if isinstance(g, dict)]
    if not groups:
        return '<svg class="model-orbit" viewBox="0 0 520 360" role="img" aria-label="No model orbit data"></svg>'
    cx, cy = 260.0, 180.0
    ring_radii = [72, 112, 150]
    circles = "".join(f'<circle cx="{cx}" cy="{cy}" r="{r}" class="orbit-ring" />' for r in ring_radii)
    nodes: list[str] = []
    labels: list[str] = []
    count = len(groups)
    for idx, group in enumerate(groups):
        identity = group.get("identity") if isinstance(group.get("identity"), dict) else {}
        score = _pct(group.get("project_model_score"))
        angle = (-math.pi / 2) + (2 * math.pi * idx / max(1, count))
        radius = 78 + (score / 100.0) * 72
        x = cx + math.cos(angle) * radius
        y = cy + math.sin(angle) * radius
        nodes.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="7" class="orbit-node" />')
        labels.append(f'<text x="{x:.1f}" y="{y + 20:.1f}" text-anchor="middle" class="orbit-label">{_e(identity.get("model","unknown"))}</text>')
    return f'''<svg class="model-orbit" viewBox="0 0 520 360" role="img" aria-label="Model project-score orbit">
      <defs>
        <radialGradient id="coreGlow" cx="40%" cy="35%">
          <stop offset="0%" stop-color="#dbe4ff"/>
          <stop offset="38%" stop-color="#4469ff"/>
          <stop offset="76%" stop-color="#3a27d7"/>
          <stop offset="100%" stop-color="#17164f"/>
        </radialGradient>
      </defs>
      {circles}
      <circle cx="{cx}" cy="{cy}" r="50" fill="url(#coreGlow)" class="orbit-core" />
      {''.join(nodes)}
      {''.join(labels)}
    </svg>'''


def _decision_cards(report: dict[str, Any]) -> str:
    action_copy = {
        "KEEP": "Keep current configuration",
        "TUNE_EFFORT": "Tune reasoning effort",
        "CHANGE_ROUTE": "Repair or change execution route",
        "CHANGE_ROLE": "Use model in a different role",
        "SWITCH_MODEL": "Consider switching model",
        "INSUFFICIENT_EVIDENCE": "Collect more evidence",
    }
    cards: list[str] = []
    for decision in report.get("decisions", []):
        if not isinstance(decision, dict):
            continue
        identity = decision.get("identity") if isinstance(decision.get("identity"), dict) else {}
        action = str(decision.get("action") or "INSUFFICIENT_EVIDENCE")
        reasons = decision.get("reason_codes") if isinstance(decision.get("reason_codes"), list) else []
        target = decision.get("suggested_target") if isinstance(decision.get("suggested_target"), dict) else None
        target_html = ""
        if target:
            target_html = f'<p class="decision-target">Suggested → <b>{_e(target.get("model",""))}</b> · {_e(target.get("reasoning_effort",""))} · {_e(target.get("execution_role",""))}</p>'
        cards.append(
            f'''<article class="decision-card" data-action="{_e(action)}">
              <div class="decision-top"><span class="action-pill">{_e(action)}</span><span>{_e(decision.get('confidence','low'))} confidence</span></div>
              <h3>{_e(identity.get('model','unknown'))}</h3>
              <p class="decision-title">{_e(action_copy.get(action, action))}</p>
              <div class="decision-scores"><span>Project <b>{_score(decision.get('project_model_score'))}</b></span><span>Route <b>{_score(decision.get('route_reliability_score'))}</b></span><span>n <b>{_e(decision.get('quality_sample_count',0))}</b></span></div>
              <p class="decision-reason">{_e(' · '.join(str(r) for r in reasons) or 'no machine reason')}</p>
              {target_html}
            </article>'''
        )
    return "\n".join(cards) or '<p class="empty">No decisions available.</p>'


def _attribution_svg(report: dict[str, Any]) -> str:
    counts = report.get("attribution_counts") if isinstance(report.get("attribution_counts"), dict) else {}
    keys = ["model", "infrastructure", "external", "mixed", "unknown"]
    values = [max(0, int(counts.get(key) or 0)) for key in keys]
    total = sum(values) or 1
    x = 0.0
    parts: list[str] = []
    labels: list[str] = []
    classes = ["attr-model", "attr-infra", "attr-external", "attr-mixed", "attr-unknown"]
    for key, value, cls in zip(keys, values, classes):
        width = 600 * value / total
        if width > 0:
            parts.append(f'<rect x="{x:.2f}" y="22" width="{width:.2f}" height="24" rx="12" class="{cls}" />')
        labels.append(f'<span><i class="legend-dot {cls}"></i>{_e(key)} <b>{value}</b></span>')
        x += width
    return f'''<div class="attribution-visual">
      <svg viewBox="0 0 600 70" role="img" aria-label="Failure attribution distribution">{''.join(parts)}</svg>
      <div class="legend">{''.join(labels)}</div>
    </div>'''


def _route_health(report: dict[str, Any]) -> str:
    rows: list[str] = []
    for group in report.get("route_groups", []):
        if not isinstance(group, dict):
            continue
        route = group.get("route") if isinstance(group.get("route"), dict) else {}
        score = group.get("route_reliability_score")
        status = "healthy"
        if isinstance(score, (int, float)) and float(score) < 75:
            status = "degraded"
        if int(group.get("result_unknown_count") or 0) > 0:
            status += " · result unknown"
        rows.append(
            f'''<div class="route-row">
              <div><span class="eyebrow">{_e(route.get('provider','unknown'))} · {_e(route.get('auth_mode','unknown'))}</span><h3>{_e(route.get('model','unknown'))}</h3></div>
              <div class="route-score">{_score(score)}</div>
              <div><span class="route-state {('bad' if 'degraded' in status else 'good')}">{_e(status)}</span><p>{_e(group.get('successful_transport_attempts',0))}/{_e(group.get('eligible_attempts',0))} clean attempts</p></div>
            </div>'''
        )
    return "\n".join(rows) or '<p class="empty">No route evidence available.</p>'


def _evidence_rows(report: dict[str, Any]) -> str:
    rows: list[str] = []
    for sample in list(report.get("samples", []))[-40:]:
        if not isinstance(sample, dict):
            continue
        identity = sample.get("identity") if isinstance(sample.get("identity"), dict) else {}
        rows.append(
            f'''<tr>
              <td>{_e(sample.get('assignment_id',''))}</td>
              <td>{_e(identity.get('model','unknown'))}<small>{_e(identity.get('reasoning_effort','unknown'))} · {_e(identity.get('execution_role','unknown'))}</small></td>
              <td>{_e(sample.get('attribution','unknown'))}</td>
              <td>{_e(sample.get('delivery_outcome','unknown'))}</td>
              <td>{_e(sample.get('transport_outcome','unknown'))}</td>
              <td>{_e(sample.get('terminal_at',''))}</td>
            </tr>'''
        )
    return "\n".join(rows) or '<tr><td colspan="6">No terminal samples.</td></tr>'


def render_dashboard(report: dict[str, Any]) -> str:
    """Render one offline HTML document from normalized report data."""

    summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
    project = _e(report.get("project", "Project"))
    generated = _e(report.get("generated_at", ""))
    raw_json = json.dumps(report, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    safe_json = raw_json.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")

    return f'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{project} · Model Intelligence</title>
<style>
:root{{--paper:#f7f7f3;--paper-2:#efefeb;--ink:#101633;--muted:#75798d;--line:#d9dae2;--cobalt:#2949ff;--electric:#5576ff;--violet:#6639e6;--amber:#ef9e35;--good:#1e7d66;--bad:#b64b55;--radius:26px}}
*{{box-sizing:border-box}}
html{{scroll-behavior:smooth}}
body{{margin:0;background:var(--paper);color:var(--ink);font-family:Inter,ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;line-height:1.45}}
a{{color:inherit;text-decoration:none}}
.shell{{max-width:1440px;margin:0 auto;padding:0 44px 80px}}
.topbar{{height:78px;display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid var(--line);font-size:11px;letter-spacing:.16em;text-transform:uppercase}}
.brand{{font-weight:800}}.nav{{display:flex;gap:28px;color:#3c405a}}.nav a:hover{{color:var(--cobalt)}}
.hero{{min-height:620px;padding:78px 0 42px;display:grid;grid-template-columns:minmax(0,1.25fr) minmax(310px,.75fr);gap:36px;position:relative;overflow:hidden}}
.hero h1{{font-family:Georgia,"Times New Roman",serif;font-size:clamp(58px,8vw,118px);font-weight:500;letter-spacing:-.055em;line-height:.86;margin:0;max-width:900px}}
.hero-copy{{align-self:start;padding-top:8px;max-width:390px}}.hero-copy p{{font-size:16px;color:#555a72;margin:14px 0 28px}}.hero-copy .stamp{{font-size:10px;letter-spacing:.17em;text-transform:uppercase;color:var(--muted)}}
.energy-wrap{{position:absolute;right:5%;bottom:-118px;width:520px;height:420px;pointer-events:none}}
.energy-core{{position:absolute;right:72px;bottom:36px;width:295px;height:295px;border-radius:50%;background:radial-gradient(circle at 31% 26%,#d9e1ff 0,#6681ff 17%,#3452ff 38%,#4829d1 66%,#151348 100%);box-shadow:0 24px 75px rgba(40,55,220,.27),0 0 110px rgba(84,84,255,.15)}}
.energy-core:before{{content:"";position:absolute;inset:-27px;border:1px solid rgba(39,65,214,.24);border-radius:50%;transform:scaleY(.62) rotate(-17deg)}}
.energy-core:after{{content:"";position:absolute;width:46px;height:46px;border-radius:50%;background:radial-gradient(circle at 30% 25%,#e9ecff,#5861ee 55%,#282359);top:-8px;right:-42px;box-shadow:0 10px 30px rgba(57,56,148,.24)}}
.energy-grid{{position:absolute;inset:0;background:repeating-radial-gradient(ellipse at 73% 94%,transparent 0 17px,rgba(31,42,110,.09) 18px 19px);opacity:.8}}
.hero-stats{{margin-top:34px;display:flex;gap:38px;font-size:11px;letter-spacing:.12em;text-transform:uppercase;position:relative;z-index:2}}.hero-stats b{{display:block;font-family:Georgia,serif;font-size:28px;letter-spacing:-.02em;text-transform:none;margin-bottom:2px}}
.section{{padding:92px 0;border-top:1px solid var(--line)}}
.section-head{{display:grid;grid-template-columns:180px minmax(0,1fr);gap:34px;margin-bottom:48px}}.section-index{{font-size:11px;letter-spacing:.16em;text-transform:uppercase;color:var(--muted)}}.section-head h2{{font-family:Georgia,serif;font-size:clamp(40px,5vw,72px);font-weight:500;letter-spacing:-.045em;line-height:.95;margin:0;max-width:920px}}
.overview-grid{{display:grid;grid-template-columns:1.2fr .8fr;gap:54px;align-items:center}}
.model-orbit{{width:100%;min-height:380px;overflow:visible}}.orbit-ring{{fill:none;stroke:#c8cad8;stroke-width:1}}.orbit-core{{filter:drop-shadow(0 20px 28px rgba(42,55,190,.23))}}.orbit-node{{fill:var(--ink);stroke:var(--paper);stroke-width:4}}.orbit-label{{font-size:11px;fill:#333852}} 
.metric-stack{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:1px;background:var(--line)}}.metric{{background:var(--paper);padding:26px 22px;min-height:126px}}.metric small{{font-size:10px;letter-spacing:.14em;text-transform:uppercase;color:var(--muted)}}.metric strong{{display:block;font-family:Georgia,serif;font-size:48px;font-weight:500;letter-spacing:-.04em;margin-top:8px}}
.score-row{{display:grid;grid-template-columns:260px 1fr 190px;gap:34px;padding:30px 0;border-top:1px solid var(--line);align-items:center}}.score-row:last-child{{border-bottom:1px solid var(--line)}}.score-identity h3,.route-row h3{{font-size:24px;margin:6px 0 2px;letter-spacing:-.03em}}.score-identity p,.route-row p{{font-size:12px;color:var(--muted);margin:0}}.eyebrow{{font-size:10px;letter-spacing:.15em;text-transform:uppercase;color:var(--muted)}}
.score-tracks{{display:grid;gap:13px}}.track-line{{display:grid;grid-template-columns:128px 48px minmax(100px,1fr);align-items:center;gap:12px;font-size:12px}}.track-line strong{{font-size:13px}}.bar{{height:7px;border-radius:999px;background:linear-gradient(90deg,var(--cobalt) 0 var(--v),#dedfe6 var(--v) 100%);display:block}}.bar.route{{background:linear-gradient(90deg,#171d47 0 var(--v),#dedfe6 var(--v) 100%)}}.bar.external{{background:linear-gradient(90deg,var(--violet) 0 var(--v),#dedfe6 var(--v) 100%)}}.score-meta{{display:flex;flex-direction:column;gap:7px;font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.08em}}
.decision-grid{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:18px}}.decision-card{{border-top:2px solid var(--ink);padding:20px 0 28px;min-height:250px}}.decision-card[data-action="CHANGE_ROUTE"],.decision-card[data-action="SWITCH_MODEL"]{{border-color:var(--cobalt)}}.decision-top{{display:flex;justify-content:space-between;align-items:center;font-size:10px;letter-spacing:.1em;text-transform:uppercase;color:var(--muted)}}.action-pill{{color:var(--ink);font-weight:800}}.decision-card h3{{font-family:Georgia,serif;font-size:33px;font-weight:500;margin:24px 0 3px}}.decision-title{{font-size:15px;margin:0 0 24px}}.decision-scores{{display:flex;gap:22px;padding:15px 0;border-top:1px solid var(--line);border-bottom:1px solid var(--line);font-size:11px}}.decision-reason,.decision-target{{font-size:11px;color:var(--muted)}}
.attribution-visual svg{{width:100%;height:auto}}.attr-model{{fill:var(--cobalt);background:var(--cobalt)}}.attr-infra{{fill:#1c2145;background:#1c2145}}.attr-external{{fill:var(--amber);background:var(--amber)}}.attr-mixed{{fill:var(--violet);background:var(--violet)}}.attr-unknown{{fill:#b6b8c4;background:#b6b8c4}}.legend{{display:flex;gap:24px;flex-wrap:wrap;font-size:11px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted)}}.legend span{{display:flex;align-items:center;gap:7px}}.legend-dot{{width:8px;height:8px;border-radius:50%;display:inline-block}}
.route-row{{display:grid;grid-template-columns:1fr 130px 260px;gap:28px;padding:23px 0;border-top:1px solid var(--line);align-items:center}}.route-score{{font-family:Georgia,serif;font-size:52px;letter-spacing:-.04em}}.route-state{{font-size:10px;letter-spacing:.12em;text-transform:uppercase}}.route-state.good{{color:var(--good)}}.route-state.bad{{color:var(--bad)}}
.table-wrap{{overflow:auto;border-top:1px solid var(--line)}}table{{width:100%;border-collapse:collapse;font-size:12px}}th{{text-align:left;padding:13px 10px 13px 0;color:var(--muted);font-size:9px;letter-spacing:.14em;text-transform:uppercase;border-bottom:1px solid var(--line)}}td{{padding:15px 10px 15px 0;border-bottom:1px solid #e2e2e7;vertical-align:top}}td small{{display:block;color:var(--muted);margin-top:3px}}.empty{{color:var(--muted);font-style:italic}}
.footer{{padding-top:30px;border-top:1px solid var(--line);display:flex;justify-content:space-between;font-size:10px;letter-spacing:.1em;text-transform:uppercase;color:var(--muted)}}
@media(max-width:960px){{.shell{{padding:0 22px 55px}}.nav{{display:none}}.hero{{grid-template-columns:1fr;min-height:720px}}.hero h1{{font-size:68px}}.energy-wrap{{right:-110px}}.overview-grid{{grid-template-columns:1fr}}.score-row{{grid-template-columns:1fr}}.score-meta{{flex-direction:row;flex-wrap:wrap}}.decision-grid{{grid-template-columns:1fr 1fr}}.route-row{{grid-template-columns:1fr 90px}}.route-row>div:last-child{{grid-column:1/-1}}}}
@media(max-width:620px){{.hero h1{{font-size:51px}}.section-head{{grid-template-columns:1fr}}.decision-grid{{grid-template-columns:1fr}}.metric-stack{{grid-template-columns:1fr}}.energy-core{{width:230px;height:230px}}.energy-wrap{{bottom:-40px}}}}
</style>
</head>
<body>
<div class="shell">
  <header class="topbar"><a class="brand" href="#overview">MODEL ORBIT</a><nav class="nav"><a href="#model-comparison">Scores</a><a href="#decisions">Decisions</a><a href="#route-health">Routes</a><a href="#evidence">Evidence</a></nav><span>{_e(report.get('window_days',30))}D WINDOW</span></header>
  <main>
    <section class="hero">
      <div><span class="eyebrow">Project intelligence / {project}</span><h1>SEE WHICH MODELS<br>ACTUALLY WORK<br>IN YOUR WORLD.</h1><div class="hero-stats"><span><b>{_e(summary.get('observed_model_configurations',0))}</b>configs</span><span><b>{_e(summary.get('terminal_samples',0))}</b>terminal samples</span><span><b>{_e(summary.get('quality_scored_samples',0))}</b>quality samples</span></div></div>
      <div class="hero-copy"><span class="stamp">Generated {generated}</span><p>Project evidence is separated from execution-chain reliability, then compared with external baselines before any routing recommendation is made.</p><span class="eyebrow">Project truth first. Benchmark context second.</span></div>
      <div class="energy-wrap"><div class="energy-grid"></div><div class="energy-core"></div></div>
    </section>

    <section id="overview" class="section">
      <div class="section-head"><div class="section-index">01 / Overview</div><h2>One system, three truths: project quality, route reliability, external baseline.</h2></div>
      <div class="overview-grid">{_orbit_svg(report)}<div class="metric-stack">
        <div class="metric"><small>Model configurations</small><strong>{_e(summary.get('observed_model_configurations',0))}</strong></div>
        <div class="metric"><small>Scored project samples</small><strong>{_e(summary.get('quality_scored_samples',0))}</strong></div>
        <div class="metric"><small>Infra excluded</small><strong>{_e(summary.get('infrastructure_failures_excluded_from_model_score',0))}</strong></div>
        <div class="metric"><small>Unknown evidence</small><strong>{_e(summary.get('unknown_samples',0))}</strong></div>
      </div></div>
    </section>

    <section id="model-comparison" class="section">
      <div class="section-head"><div class="section-index">02 / Compare</div><h2>Compare model performance without blaming the model for a broken route.</h2></div>
      {_comparison_rows(report)}
    </section>

    <section id="decisions" class="section">
      <div class="section-head"><div class="section-index">03 / Decisions</div><h2>Keep, tune, reroute, change role, or switch — with machine reasons.</h2></div>
      <div class="decision-grid">{_decision_cards(report)}</div>
    </section>

    <section id="attribution" class="section">
      <div class="section-head"><div class="section-index">04 / Attribution</div><h2>Where performance was lost: model quality or the machinery around it.</h2></div>
      {_attribution_svg(report)}
    </section>

    <section id="route-health" class="section">
      <div class="section-head"><div class="section-index">05 / Routes</div><h2>Route reliability is its own score — OAuth, API, host, and transport stay visible.</h2></div>
      {_route_health(report)}
    </section>

    <section id="evidence" class="section">
      <div class="section-head"><div class="section-index">06 / Evidence</div><h2>Every recommendation can be traced back to terminal Runtime evidence.</h2></div>
      <div class="table-wrap"><table><thead><tr><th>Assignment</th><th>Model config</th><th>Attribution</th><th>Delivery</th><th>Transport</th><th>Terminal</th></tr></thead><tbody>{_evidence_rows(report)}</tbody></table></div>
    </section>
  </main>
  <footer class="footer"><span>Adaptive Agent Runtime · Model Intelligence</span><span>Read-only advisory surface</span></footer>
</div>
<script id="report-data" type="application/json">{safe_json}</script>
</body>
</html>'''

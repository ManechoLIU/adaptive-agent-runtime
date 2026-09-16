"""Self-contained Chinese HTML renderer for project model intelligence reports."""

from __future__ import annotations

import html
import json
from collections import defaultdict
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


def _weighted(values: list[tuple[float, int]]) -> float | None:
    total_weight = sum(max(1, int(weight)) for _, weight in values)
    if not values or total_weight <= 0:
        return None
    return round(sum(float(value) * max(1, int(weight)) for value, weight in values) / total_weight, 1)


_ACTION_LABELS = {
    "KEEP": "保持使用",
    "TUNE_EFFORT": "调整推理强度",
    "CHANGE_ROUTE": "优化路由",
    "CHANGE_ROLE": "调整角色",
    "SWITCH_MODEL": "考虑换模",
    "INSUFFICIENT_EVIDENCE": "继续观察",
}

_ACTION_PRIORITY = {
    "SWITCH_MODEL": 6,
    "CHANGE_ROUTE": 5,
    "CHANGE_ROLE": 4,
    "TUNE_EFFORT": 3,
    "KEEP": 2,
    "INSUFFICIENT_EVIDENCE": 1,
}

_REASON_LABELS = {
    "project_evidence_supports_current_route": "项目实战表现稳定，当前配置可继续使用。",
    "route_reliability_degraded": "模型能力不是主要问题，优先修复执行链稳定性。",
    "insufficient_project_samples": "有效样本不足，暂不建议换模型。",
    "model_stronger_in_other_role": "同一模型在其他角色表现更好，优先调整分工。",
    "project_quality_materially_weaker": "同类任务项目表现持续偏弱。",
    "stronger_observed_alternative": "已有同类任务更强的替代模型。",
    "effort_not_cost_effective": "更高推理强度没有带来足够质量增益。",
    "lower_effort_similar_quality": "较低推理强度已达到接近质量，可降低耗时。",
    "route_evidence_insufficient": "路由样本不足，继续积累真实执行证据。",
}


def _model_summaries(report: dict[str, Any]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = defaultdict(lambda: {
        "project": [], "baseline": [], "route": [], "samples": 0, "decisions": [], "providers": set(), "efforts": set(),
    })

    for group in report.get("model_groups", []):
        if not isinstance(group, dict):
            continue
        identity = group.get("identity") if isinstance(group.get("identity"), dict) else {}
        model = str(identity.get("model") or "unknown")
        item = grouped[model]
        item["providers"].add(str(identity.get("provider") or "unknown"))
        effort = str(identity.get("reasoning_effort") or "unknown")
        if effort != "unknown":
            item["efforts"].add(effort)
        n = max(0, int(group.get("quality_sample_count") or 0))
        item["samples"] += n
        project = group.get("project_model_score")
        if isinstance(project, (int, float)) and n > 0:
            item["project"].append((float(project), n))
        benchmark = group.get("benchmark") if isinstance(group.get("benchmark"), dict) else {}
        baseline = benchmark.get("score")
        if benchmark.get("match") != "none" and isinstance(baseline, (int, float)):
            item["baseline"].append((float(baseline), max(1, n)))

    for route_group in report.get("route_groups", []):
        if not isinstance(route_group, dict):
            continue
        route = route_group.get("route") if isinstance(route_group.get("route"), dict) else {}
        model = str(route.get("model") or "unknown")
        score = route_group.get("route_reliability_score")
        attempts = max(1, int(route_group.get("eligible_attempts") or 0))
        if isinstance(score, (int, float)):
            grouped[model]["route"].append((float(score), attempts))

    for decision in report.get("decisions", []):
        if not isinstance(decision, dict):
            continue
        identity = decision.get("identity") if isinstance(decision.get("identity"), dict) else {}
        model = str(identity.get("model") or "unknown")
        grouped[model]["decisions"].append(decision)

    result: list[dict[str, Any]] = []
    for model, raw in grouped.items():
        decisions = sorted(
            raw["decisions"],
            key=lambda d: (
                _ACTION_PRIORITY.get(str(d.get("action") or "INSUFFICIENT_EVIDENCE"), 0),
                int(d.get("quality_sample_count") or 0),
            ),
            reverse=True,
        )
        decision = decisions[0] if decisions else {"action": "INSUFFICIENT_EVIDENCE", "reason_codes": ["insufficient_project_samples"]}
        action = str(decision.get("action") or "INSUFFICIENT_EVIDENCE")
        reasons = decision.get("reason_codes") if isinstance(decision.get("reason_codes"), list) else []
        reason = next((_REASON_LABELS.get(str(code)) for code in reasons if _REASON_LABELS.get(str(code))), None)
        if not reason:
            reason = "基于当前项目证据继续观察。"
        result.append({
            "model": model,
            "project": _weighted(raw["project"]),
            "baseline": _weighted(raw["baseline"]),
            "route": _weighted(raw["route"]),
            "samples": raw["samples"],
            "action": action,
            "action_label": _ACTION_LABELS.get(action, action),
            "reason": reason,
            "providers": sorted(raw["providers"]),
            "efforts": sorted(raw["efforts"]),
        })

    result.sort(key=lambda item: (
        -1 if item["project"] is None else -float(item["project"]),
        item["model"],
    ))
    return result


def _metric_card(label: str, value: Any, note: str) -> str:
    return f'''<article class="metric-card"><span>{_e(label)}</span><strong>{_e(value)}</strong><small>{_e(note)}</small></article>'''


def _model_cards(summaries: list[dict[str, Any]]) -> str:
    cards: list[str] = []
    for idx, item in enumerate(summaries):
        score = item["project"] if item["project"] is not None else item["baseline"]
        secondary = "项目实战" if item["project"] is not None else "外部基线"
        tags = []
        if item["efforts"]:
            tags.append("/".join(item["efforts"]))
        if item["providers"]:
            tags.append(item["providers"][0])
        cards.append(f'''<article class="model-card tone-{idx % 4}" data-model="{_e(item['model'])}">
          <div class="model-card-top"><div class="model-icon"></div><span class="status-pill status-{_e(item['action'].lower())}">{_e(item['action_label'])}</span></div>
          <h3>{_e(item['model'])}</h3><p>{_e(' · '.join(tags) or '项目实战')}</p>
          <div class="hero-score"><strong>{_score(score)}</strong><span>/100<br>{_e(secondary)}</span></div>
          <div class="mini-stats"><span>基线 <b>{_score(item['baseline'])}</b></span><span>稳定 <b>{_score(item['route'])}</b></span><span>样本 <b>{_e(item['samples'])}</b></span></div>
        </article>''')
    return "\n".join(cards)


def _comparison_chart(summaries: list[dict[str, Any]]) -> str:
    rows: list[str] = []
    for item in summaries:
        rows.append(f'''<div class="chart-row">
          <div class="chart-model">{_e(item['model'])}</div>
          <div class="chart-bars">
            <div class="chart-line"><span>项目实战</span><i><b class="bar-project" style="width:{_pct(item['project'])}%"></b></i><em>{_score(item['project'])}</em></div>
            <div class="chart-line"><span>外部基线</span><i><b class="bar-baseline" style="width:{_pct(item['baseline'])}%"></b></i><em>{_score(item['baseline'])}</em></div>
            <div class="chart-line"><span>执行稳定</span><i><b class="bar-route" style="width:{_pct(item['route'])}%"></b></i><em>{_score(item['route'])}</em></div>
          </div>
        </div>''')
    return "\n".join(rows)


def _decision_cards(summaries: list[dict[str, Any]]) -> str:
    cards: list[str] = []
    for item in summaries:
        cards.append(f'''<article class="advice-card">
          <div class="advice-icon">✦</div><span class="advice-action">{_e(item['action_label'])}</span>
          <h3>{_e(item['model'])}</h3><p>{_e(item['reason'])}</p>
        </article>''')
    return "\n".join(cards)


def render_dashboard(report: dict[str, Any]) -> str:
    """Render a concise offline dashboard from normalized report data."""
    summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
    summaries = _model_summaries(report)
    project = _e(report.get("project", "Project"))
    generated = _e(str(report.get("generated_at", "")).replace("T", " ").replace("+00:00", " UTC"))
    raw_json = json.dumps(report, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    safe_json = raw_json.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")

    return f'''<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{project} · 模型智能看板</title>
<style>
:root{{--bg:#0b0718;--bg2:#120b27;--panel:rgba(30,20,57,.76);--panel2:rgba(43,27,75,.68);--line:rgba(220,189,255,.16);--text:#f8f5ff;--muted:#aaa0c3;--pink:#ff72d2;--pink2:#ff9be2;--purple:#a868ff;--violet:#6b54ff;--blue:#5aa8ff;--cyan:#72d5ff;--gold:#ffb866;--good:#76e6bc;--warn:#ffc46c;--bad:#ff7f9f}}
*{{box-sizing:border-box}}
html{{scroll-behavior:smooth;background:var(--bg)}}
body{{margin:0;color:var(--text);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;background:radial-gradient(circle at 78% 8%,rgba(132,65,255,.17),transparent 28%),radial-gradient(circle at 12% 58%,rgba(255,72,193,.08),transparent 23%),linear-gradient(180deg,#090615 0%,#0f0820 48%,#0a0616 100%);line-height:1.5}}
body:before{{content:"";position:fixed;inset:0;pointer-events:none;opacity:.34;background-image:radial-gradient(circle at 8% 20%,rgba(255,255,255,.45) 0 1px,transparent 1.4px),radial-gradient(circle at 68% 15%,rgba(255,255,255,.3) 0 1px,transparent 1.3px),radial-gradient(circle at 92% 63%,rgba(255,255,255,.22) 0 1px,transparent 1.2px);background-size:190px 180px,260px 240px,330px 310px}}
a{{color:inherit;text-decoration:none}}.shell{{max-width:1420px;margin:0 auto;padding:0 26px 44px}}
.topbar{{height:78px;display:flex;align-items:center;gap:42px;border-bottom:1px solid var(--line);position:relative;z-index:5}}.brand{{display:flex;align-items:center;gap:12px;font-weight:760;letter-spacing:.22em;font-size:14px}}.brand-mark{{width:32px;height:32px;border:1px solid var(--pink);border-radius:50% 50% 45% 45%;position:relative;box-shadow:0 0 22px rgba(255,114,210,.25)}}.brand-mark:before{{content:"";position:absolute;left:50%;top:-4px;width:1px;height:40px;background:var(--pink);transform:rotate(-20deg)}}.nav{{display:flex;gap:30px;margin-left:auto;font-size:13px;color:#c8bddb}}.nav a.active,.nav a:hover{{color:white}}.updated{{font-size:12px;color:#9085a8}}.live-pill{{border:1px solid rgba(255,114,210,.7);padding:9px 18px;border-radius:999px;font-size:12px;box-shadow:inset 0 0 20px rgba(255,114,210,.08)}}
.cosmic-hero{{min-height:520px;display:grid;grid-template-columns:1.02fr .98fr;align-items:center;position:relative;padding:56px 0 34px;overflow:hidden}}.hero-copy{{position:relative;z-index:2;max-width:650px}}.eyebrow{{font-size:13px;color:#d49cd3;letter-spacing:.12em;margin-bottom:18px}}.hero-copy h1{{font-size:clamp(44px,5.4vw,76px);line-height:1.12;letter-spacing:-.04em;font-weight:540;margin:0 0 22px}}.hero-copy h1 em{{font-family:ui-serif,"Songti SC","STSong",serif;font-weight:500;font-style:italic;color:var(--pink);text-shadow:0 0 30px rgba(255,114,210,.28)}}.hero-copy p{{color:#b5abc8;font-size:17px;max-width:560px;margin:0 0 26px}}.hero-tags{{display:flex;gap:24px;color:#c6bad8;font-size:13px}}.hero-tags span:before{{content:"✦";color:var(--pink);margin-right:8px}}
.cosmic-art{{height:430px;position:relative}}.planet{{position:absolute;width:310px;height:310px;border-radius:50%;right:16%;top:44px;background:radial-gradient(circle at 34% 28%,#f5c5ff 0,#d77cff 8%,#934eff 22%,#4f2daf 48%,#25145c 70%,#100928 100%);box-shadow:0 0 38px rgba(198,103,255,.55),0 0 120px rgba(124,66,255,.28),inset -35px -30px 50px rgba(10,5,25,.55)}}.planet:before{{content:"";position:absolute;inset:-26px;border:1px solid rgba(235,184,255,.35);border-radius:50%;transform:rotate(-14deg) scaleY(.58);box-shadow:0 0 22px rgba(255,114,210,.13)}}.planet:after{{content:"";position:absolute;left:-70px;top:34px;width:58px;height:58px;border-radius:50%;background:radial-gradient(circle at 35% 30%,#e9b7ff,#8e4fff 52%,#25104d 100%);box-shadow:0 0 30px rgba(196,100,255,.35)}}.moon{{position:absolute;width:66px;height:66px;border-radius:50%;right:7%;bottom:80px;background:radial-gradient(circle at 35% 30%,#ffd7ef,#bc67ff 42%,#28104f 100%);box-shadow:0 0 28px rgba(255,124,216,.28)}}.orbit{{position:absolute;right:2%;top:84px;width:420px;height:250px;border:1px solid rgba(255,169,233,.24);border-radius:50%;transform:rotate(-11deg)}}.hero-note{{position:absolute;right:0;top:168px;color:#cba7da;font-family:ui-serif,"Songti SC",serif;font-style:italic;font-size:18px;line-height:1.7}}
#overview{{margin-top:-4px}}.metric-grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:16px;margin:0 0 36px}}.metric-card,.model-card,.chart-panel,.advice-card,.footer-card{{background:linear-gradient(145deg,rgba(41,27,73,.84),rgba(20,13,43,.82));border:1px solid var(--line);box-shadow:inset 0 1px 0 rgba(255,255,255,.035),0 18px 50px rgba(2,0,12,.25);backdrop-filter:blur(18px)}}.metric-card{{border-radius:18px;padding:21px 22px;min-height:116px}}.metric-card span{{display:block;font-size:12px;color:#aaa0c3;letter-spacing:.07em}}.metric-card strong{{display:block;font-family:ui-serif,"Songti SC",serif;font-size:43px;font-weight:500;margin-top:8px}}.metric-card small{{color:#786e91;font-size:11px}}
.section{{padding:38px 0 24px}}.section-head{{display:flex;align-items:flex-end;justify-content:space-between;margin-bottom:18px}}.section-head h2{{font-size:25px;margin:0;font-weight:620}}.section-head h2:before{{content:"";display:inline-block;width:3px;height:26px;background:linear-gradient(var(--pink),var(--purple));border-radius:999px;margin-right:12px;vertical-align:-5px;box-shadow:0 0 14px rgba(255,114,210,.45)}}.section-head p{{margin:0;color:#887d9e;font-size:12px}}.model-grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:16px}}.model-card{{border-radius:18px;padding:20px;min-height:235px;position:relative;overflow:hidden}}.model-card:after{{content:"";position:absolute;width:130px;height:130px;border-radius:50%;right:-50px;top:-52px;filter:blur(2px);opacity:.28;background:radial-gradient(circle,var(--pink),transparent 68%)}}.model-card-top{{display:flex;justify-content:space-between;align-items:center}}.model-icon{{width:46px;height:46px;border-radius:50%;background:radial-gradient(circle at 36% 30%,#bff2ff 0,#5ca8ff 20%,#6d4bff 48%,#21104c 100%);box-shadow:0 0 26px rgba(83,140,255,.4)}}.tone-1 .model-icon{{background:radial-gradient(circle at 36% 30%,#ffd2ff 0,#c468ff 24%,#7837d6 48%,#221044 100%)}}.tone-2 .model-icon{{background:radial-gradient(circle at 36% 30%,#d7c9ff 0,#795dff 25%,#4934c4 52%,#17103b 100%)}}.tone-3 .model-icon{{background:radial-gradient(circle at 36% 30%,#ffd8c6 0,#ff8a7c 25%,#a54d89 55%,#261039 100%)}}.status-pill{{font-size:11px;padding:5px 9px;border-radius:8px;background:rgba(153,99,255,.14);color:#dacdff;border:1px solid rgba(165,117,255,.2)}}.status-change_route,.status-switch_model{{color:#ffd4e6;background:rgba(255,107,184,.12)}}.model-card h3{{font-size:18px;margin:17px 0 2px}}.model-card>p{{margin:0;color:#897d9f;font-size:12px}}.hero-score{{display:flex;align-items:flex-end;gap:8px;margin:16px 0 15px}}.hero-score strong{{font-family:ui-serif,"Songti SC",serif;font-size:46px;font-weight:500;line-height:1}}.hero-score span{{font-size:10px;color:#7d7294;line-height:1.4}}.mini-stats{{display:flex;gap:12px;padding-top:12px;border-top:1px solid rgba(255,255,255,.08);font-size:11px;color:#9186a9}}.mini-stats b{{color:#e5dcf5;font-weight:550}}
.chart-panel{{border-radius:20px;padding:24px;margin-top:18px}}.chart-legend{{display:flex;gap:22px;color:#a397b9;font-size:11px;margin:4px 0 20px}}.chart-legend i{{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px}}.lg-project{{background:var(--purple)}}.lg-baseline{{background:var(--blue)}}.lg-route{{background:var(--pink)}}.chart-row{{display:grid;grid-template-columns:150px 1fr;gap:22px;padding:14px 0;border-top:1px solid rgba(255,255,255,.055)}}.chart-row:first-of-type{{border-top:0}}.chart-model{{font-size:13px;color:#ddd3ea;padding-top:4px}}.chart-bars{{display:grid;gap:8px}}.chart-line{{display:grid;grid-template-columns:72px 1fr 42px;gap:9px;align-items:center;font-size:10px;color:#8f84a6}}.chart-line i{{height:7px;background:rgba(255,255,255,.07);border-radius:99px;overflow:hidden}}.chart-line b{{display:block;height:100%;border-radius:99px;box-shadow:0 0 13px currentColor}}.bar-project{{background:linear-gradient(90deg,#b26dff,#8e62ff)}}.bar-baseline{{background:linear-gradient(90deg,#5e8fff,#6fd4ff)}}.bar-route{{background:linear-gradient(90deg,#ff78c8,#ff9fe0)}}.chart-line em{{font-style:normal;color:#d7cde6;text-align:right}}
.advice-grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:16px}}.advice-card{{border-radius:18px;padding:20px;min-height:190px}}.advice-icon{{font-size:24px;color:var(--pink);text-shadow:0 0 14px rgba(255,114,210,.5)}}.advice-action{{display:inline-block;margin:12px 0 8px;color:#f3e9ff;font-weight:700}}.advice-card h3{{margin:0 0 8px;font-size:16px}}.advice-card p{{margin:0;color:#9b90b0;font-size:12px;line-height:1.7}}
.footer-card{{margin-top:34px;border-radius:24px;padding:30px 34px;display:grid;grid-template-columns:1fr auto;align-items:center;min-height:150px;position:relative;overflow:hidden}}.footer-card:after{{content:"";position:absolute;width:230px;height:230px;border-radius:50%;right:4%;top:-96px;background:radial-gradient(circle at 40% 30%,#f9cfff,#a95eff 28%,#4b2391 58%,transparent 71%);opacity:.5}}.footer-card h2{{font-family:ui-serif,"Songti SC",serif;font-weight:500;font-size:30px;margin:0 0 8px;color:#f0c7ec}}.footer-card p{{margin:0;color:#9d91b2;font-size:13px}}.footer-meta{{position:relative;z-index:2;text-align:right;color:#837793;font-size:11px}}
@media(max-width:1050px){{.model-grid,.advice-grid{{grid-template-columns:repeat(2,1fr)}}.cosmic-hero{{grid-template-columns:1fr}}.cosmic-art{{height:360px}}.nav{{display:none}}}}
@media(max-width:680px){{.shell{{padding:0 15px 30px}}.updated,.live-pill{{display:none}}.cosmic-hero{{padding-top:36px}}.hero-copy h1{{font-size:43px}}.metric-grid,.model-grid,.advice-grid{{grid-template-columns:1fr}}.hero-tags{{flex-direction:column;gap:8px}}.planet{{right:4%;width:250px;height:250px}}.chart-row{{grid-template-columns:1fr}}.footer-card{{grid-template-columns:1fr}}.footer-meta{{text-align:left;margin-top:20px}}}}
</style>
</head>
<body>
<div class="shell">
<header class="topbar"><a class="brand" href="#overview"><span class="brand-mark"></span><span>MODEL INTELLIGENCE</span></a><nav class="nav"><a class="active" href="#overview">总览</a><a href="#model-comparison">模型表现</a><a href="#decisions">建议方案</a></nav><span class="updated">更新于 {generated}</span><span class="live-pill">项目实战数据</span></header>
<main>
<section class="cosmic-hero">
  <div class="hero-copy"><div class="eyebrow">让 AI 真正创造价值</div><h1 aria-label="更好的模型组合 创造更大的可能">更好的模型组合<br>创造<em>更大的可能</em></h1><p>基于项目真实任务表现与 ModelDial 外部基线，帮你判断模型强弱、执行链问题，以及是否需要调整调用策略。</p><div class="hero-tags"><span>真实项目数据</span><span>外部基线评测</span><span>智能决策建议</span></div></div>
  <div class="cosmic-art"><div class="orbit"></div><div class="planet"></div><div class="moon"></div><div class="hero-note">找到更适合你的<br>AI 工作方式</div></div>
</section>

<section id="overview">
  <div class="metric-grid">
    {_metric_card('已评估模型配置', summary.get('observed_model_configurations',0), '当前窗口')}
    {_metric_card('有效项目样本', summary.get('quality_scored_samples',0), '进入能力评分')}
    {_metric_card('基础设施剔除', summary.get('infrastructure_failures_excluded_from_model_score',0), '不误伤模型分')}
    {_metric_card('未知证据', summary.get('unknown_samples',0), '暂不下结论')}
  </div>
</section>

<section id="model-comparison" class="section">
  <div class="section-head"><div><h2>模型表现对比</h2><p>同一模型多配置已合并，避免重复信息。</p></div><p>项目实战 + ModelDial + 执行稳定性</p></div>
  <div class="model-grid">{_model_cards(summaries)}</div>
  <div class="chart-panel"><div class="chart-legend"><span><i class="lg-project"></i>项目实战</span><span><i class="lg-baseline"></i>外部基线</span><span><i class="lg-route"></i>执行稳定</span></div>{_comparison_chart(summaries)}</div>
</section>

<section id="decisions" class="section">
  <div class="section-head"><div><h2>智能建议</h2><p>先区分“模型不行”还是“执行链不行”，再决定是否换模。</p></div></div>
  <div class="advice-grid">{_decision_cards(summaries)}</div>
</section>
</main>
<footer class="footer-card"><div><h2>让更好的模型，去做更适合它的工作。</h2><p>项目实战优先，外部基线辅助；样本不足时，不做激进换模。</p></div><div class="footer-meta"><b>{project}</b><br>Adaptive Agent Runtime · 模型智能看板</div></footer>
</div>
<script id="report-data" type="application/json">{safe_json}</script>
</body>
</html>'''

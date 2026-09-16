"""Self-contained Chinese HTML renderer for project/global model intelligence."""

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


def _project_score(value: Any) -> str:
    return _score(value) if isinstance(value, (int, float)) else "待积累"


def _pct(value: Any) -> float:
    if not isinstance(value, (int, float)):
        return 0.0
    return max(0.0, min(100.0, float(value)))


def _weighted(values: list[tuple[float, int]]) -> float | None:
    if not values:
        return None
    total_weight = sum(max(1, int(weight)) for _, weight in values)
    if total_weight <= 0:
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
    "SWITCH_MODEL": 6, "CHANGE_ROUTE": 5, "CHANGE_ROLE": 4,
    "TUNE_EFFORT": 3, "KEEP": 2, "INSUFFICIENT_EVIDENCE": 1,
}
_REASON_LABELS = {
    "project_evidence_supports_current_route": "项目实战表现稳定，当前配置可继续使用。",
    "route_reliability_degraded": "模型能力不是主要问题，优先修复执行链稳定性。",
    "insufficient_project_samples": "有效样本不足，暂不建议换模型。",
    "model_stronger_in_other_role": "同一模型在其他角色表现更好，优先调整分工。",
    "project_quality_materially_weaker": "同类任务项目表现持续偏弱。",
    "stronger_observed_alternative": "已有同类任务更强的替代模型。",
    "similar_quality_lower_effort": "较低推理强度已达到接近质量，可降低耗时。",
    "material_latency_reduction": "降低推理档位可显著缩短任务耗时。",
}
_ROLE_LABELS = {
    "controller": "总控",
    "reviewer": "审查",
    "writer": "执行",
    "uiux": "前端设计",
    "researcher": "调研",
    "research": "调研",
    "backend": "后端",
    "frontend": "前端",
}


def _project_model_summaries(report: dict[str, Any]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = defaultdict(lambda: {
        "project": [], "baseline": [], "route": [], "samples": 0, "decisions": [],
        "providers": set(), "efforts": set(), "roles": defaultdict(list),
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
        project_score = group.get("project_model_score")
        if isinstance(project_score, (int, float)) and n > 0:
            item["project"].append((float(project_score), n))
            role = str(identity.get("execution_role") or "unknown")
            item["roles"][role].append((float(project_score), n))
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
        if isinstance(decision, dict):
            model = str((decision.get("identity") or {}).get("model") or "unknown")
            grouped[model]["decisions"].append(decision)

    result: list[dict[str, Any]] = []
    for model, raw in grouped.items():
        decisions = sorted(raw["decisions"], key=lambda d: (
            _ACTION_PRIORITY.get(str(d.get("action") or "INSUFFICIENT_EVIDENCE"), 0),
            int(d.get("quality_sample_count") or 0),
        ), reverse=True)
        decision = decisions[0] if decisions else {"action": "INSUFFICIENT_EVIDENCE", "reason_codes": ["insufficient_project_samples"]}
        action = str(decision.get("action") or "INSUFFICIENT_EVIDENCE")
        reasons = decision.get("reason_codes") if isinstance(decision.get("reason_codes"), list) else []
        reason = next((_REASON_LABELS.get(str(code)) for code in reasons if _REASON_LABELS.get(str(code))), None) or "基于当前项目证据继续观察。"
        role_scores = [(role, _weighted(values)) for role, values in raw["roles"].items()]
        role_scores = [(role, score) for role, score in role_scores if score is not None and role != "unknown"]
        role_scores.sort(key=lambda item: (-float(item[1]), item[0]))
        result.append({
            "model": model,
            "project": _weighted(raw["project"]),
            "baseline": _weighted(raw["baseline"]),
            "route": _weighted(raw["route"]),
            "route_status": "稳定" if raw["route"] else "待积累",
            "samples": raw["samples"],
            "projects": 1,
            "action": action,
            "action_label": _ACTION_LABELS.get(action, action),
            "reason": reason,
            "providers": sorted(raw["providers"]),
            "efforts": sorted(raw["efforts"]),
            "preferred_roles": [role for role, _ in role_scores[:2]],
        })
    result.sort(key=lambda item: (-float(item["project"]) if item["project"] is not None else 1, item["model"]))
    return result


def _global_model_summaries(report: dict[str, Any]) -> list[dict[str, Any]]:
    config_by_model: dict[str, dict[str, Any]] = defaultdict(lambda: {"baseline": [], "providers": set(), "efforts": set()})
    for group in report.get("configuration_groups", []):
        if not isinstance(group, dict):
            continue
        identity = group.get("identity") if isinstance(group.get("identity"), dict) else {}
        model = str(identity.get("model") or "unknown")
        info = config_by_model[model]
        info["providers"].add(str(identity.get("provider") or "unknown"))
        effort = str(identity.get("reasoning_effort") or "unknown")
        if effort != "unknown":
            info["efforts"].add(effort)
        benchmark = group.get("benchmark") if isinstance(group.get("benchmark"), dict) else {}
        baseline = benchmark.get("score")
        if benchmark.get("match") != "none" and isinstance(baseline, (int, float)):
            info["baseline"].append((float(baseline), max(1, int(group.get("quality_sample_count") or 0))))
    route_by_model: dict[str, list[tuple[float, int]]] = defaultdict(list)
    for route_group in report.get("route_groups", []):
        if not isinstance(route_group, dict):
            continue
        route = route_group.get("route") if isinstance(route_group.get("route"), dict) else {}
        model = str(route.get("model") or "unknown")
        score = route_group.get("route_reliability_score")
        if isinstance(score, (int, float)):
            route_by_model[model].append((float(score), max(1, int(route_group.get("eligible_attempts") or 0))))

    result: list[dict[str, Any]] = []
    for summary in report.get("model_summaries", []):
        if not isinstance(summary, dict):
            continue
        model = str((summary.get("identity") or {}).get("model") or "unknown")
        info = config_by_model[model]
        action = str(summary.get("recommended_action") or "INSUFFICIENT_EVIDENCE")
        result.append({
            "model": model,
            "project": summary.get("project_model_score"),
            "baseline": _weighted(info["baseline"]),
            "route": _weighted(route_by_model.get(model, [])),
            "route_status": str(summary.get("route_status") or "待确认"),
            "samples": int(summary.get("raw_quality_sample_count") or summary.get("quality_sample_count") or 0),
            "projects": int(summary.get("projects_observed") or 0),
            "action": action,
            "action_label": _ACTION_LABELS.get(action, action),
            "reason": str(summary.get("recommended_reason") or "基于跨项目证据继续观察。"),
            "providers": sorted(info["providers"]),
            "efforts": sorted(info["efforts"]),
            "preferred_roles": list(summary.get("preferred_roles") or []),
        })
    result.sort(key=lambda item: (-float(item["project"]) if item["project"] is not None else 1, item["model"]))
    return result


def _model_summaries(report: dict[str, Any]) -> list[dict[str, Any]]:
    return _global_model_summaries(report) if report.get("scope") == "global" else _project_model_summaries(report)


def _metric_card(label: str, value: Any, note: str) -> str:
    return f'<article class="metric-card"><span>{_e(label)}</span><strong>{_e(value)}</strong><small>{_e(note)}</small></article>'


def _route_display(item: dict[str, Any]) -> str:
    status = str(item.get("route_status") or "")
    if status and status != "稳定":
        return status
    return _score(item.get("route")) if item.get("route") is not None else "待积累"


def _model_cards(summaries: list[dict[str, Any]], *, global_scope: bool, data_attr: str = "data-model") -> str:
    cards: list[str] = []
    score_label = "全局实战" if global_scope else "项目实战"
    for idx, item in enumerate(summaries):
        tags = []
        if item.get("efforts"):
            tags.append("/".join(item["efforts"]))
        if item.get("providers"):
            tags.append(item["providers"][0])
        project_count = f"{item.get('projects', 1)} 项目" if global_scope else None
        meta = [f"{item.get('samples',0)} 样本"]
        if project_count:
            meta.insert(0, project_count)
        cards.append(f'''<article class="model-card tone-{idx % 4}" {data_attr}="{_e(item['model'])}">
          <div class="model-card-top"><div class="model-icon"></div><span class="status-pill status-{_e(item['action'].lower())}">{_e(item['action_label'])}</span></div>
          <h3>{_e(item['model'])}</h3><p>{_e(' · '.join(tags) or score_label)}</p>
          <div class="hero-score"><strong>{_project_score(item.get('project'))}</strong><span>/100<br>{_e(score_label)}</span></div>
          <div class="mini-stats"><span>基线 <b>{_score(item.get('baseline'))}</b></span><span>稳定 <b>{_e(_route_display(item))}</b></span></div>
          <div class="card-meta">{_e(' · '.join(meta))}</div>
        </article>''')
    return "\n".join(cards)


def _comparison_chart(summaries: list[dict[str, Any]], *, global_scope: bool) -> str:
    rows: list[str] = []
    project_label = "全局实战" if global_scope else "项目实战"
    for item in summaries:
        route_text = _route_display(item)
        rows.append(f'''<div class="chart-row">
          <div class="chart-model">{_e(item['model'])}</div>
          <div class="chart-bars">
            <div class="chart-line"><span>{_e(project_label)}</span><i><b class="bar-project" style="width:{_pct(item.get('project'))}%"></b></i><em>{_project_score(item.get('project'))}</em></div>
            <div class="chart-line"><span>外部基线</span><i><b class="bar-baseline" style="width:{_pct(item.get('baseline'))}%"></b></i><em>{_score(item.get('baseline'))}</em></div>
            <div class="chart-line"><span>执行稳定</span><i><b class="bar-route" style="width:{_pct(item.get('route'))}%"></b></i><em>{_e(route_text)}</em></div>
          </div>
        </div>''')
    return "\n".join(rows)


def _decision_cards(summaries: list[dict[str, Any]]) -> str:
    return "\n".join(f'''<article class="advice-card"><div class="advice-icon">✦</div><span class="advice-action">{_e(item['action_label'])}</span><h3>{_e(item['model'])}</h3><p>{_e(item['reason'])}</p></article>''' for item in summaries)


def _role_cards(summaries: list[dict[str, Any]]) -> str:
    cards: list[str] = []
    for item in summaries:
        roles = [_ROLE_LABELS.get(str(role), str(role)) for role in item.get("preferred_roles", [])]
        label = " / ".join(roles) if roles else "待积累"
        cards.append(f'''<article class="role-card"><span>{_e(item['model'])}</span><strong>{_e(label)}</strong></article>''')
    return "\n".join(cards)


def _metrics(report: dict[str, Any]) -> list[tuple[str, Any, str]]:
    summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
    if report.get("scope") == "global":
        return [
            ("覆盖项目", summary.get("projects_observed", 0), "跨项目真实证据"),
            ("已评估模型", summary.get("models_observed", 0), "自动发现"),
            ("有效项目样本", summary.get("quality_scored_samples", 0), "进入能力评分"),
            ("基础设施剔除", summary.get("infrastructure_failures_excluded_from_model_score", 0), "不误伤模型分"),
        ]
    return [
        ("已评估模型", len(_project_model_summaries(report)), "当前项目"),
        ("有效项目样本", summary.get("quality_scored_samples", 0), "进入能力评分"),
        ("基础设施剔除", summary.get("infrastructure_failures_excluded_from_model_score", 0), "不误伤模型分"),
        ("未知证据", summary.get("unknown_samples", 0), "暂不下结论"),
    ]


def _view_data(report: dict[str, Any]) -> dict[str, Any]:
    global_scope = report.get("scope") == "global"
    data: dict[str, Any] = {
        "global": {
            "summaries": _model_summaries(report),
            "metrics": _metrics(report),
            "global_scope": global_scope,
        },
        "projects": {},
    }
    if global_scope:
        for name, project_report in sorted((report.get("project_reports") or {}).items()):
            if isinstance(project_report, dict):
                data["projects"][name] = {
                    "summaries": _project_model_summaries(project_report),
                    "metrics": _metrics(project_report),
                    "global_scope": False,
                }
    return data


def render_dashboard(report: dict[str, Any]) -> str:
    """Render a concise offline dashboard from project or global report data."""
    global_scope = report.get("scope") == "global"
    summaries = _model_summaries(report)
    metrics = _metrics(report)
    project = "全局模型情报" if global_scope else str(report.get("project", "Project"))
    generated = _e(str(report.get("generated_at", "")).replace("T", " ").replace("+00:00", " UTC"))
    raw_json = json.dumps(report, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    safe_json = raw_json.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    view_json = json.dumps(_view_data(report), ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    safe_view_json = view_json.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    project_options = "".join(f'<option value="{_e(name)}">{_e(name)}</option>' for name in sorted((report.get("project_reports") or {}).keys()))
    controls = ""
    if global_scope:
        controls = f'''<div class="scope-controls"><button class="scope-btn active" data-scope="global">全局视角</button><button class="scope-btn" data-scope="project">当前项目</button><select id="project-select">{project_options}</select></div>'''

    return f'''<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_e(project)} · 模型智能看板</title>
<style>
:root{{--bg:#0b0718;--bg2:#120b27;--panel:rgba(30,20,57,.76);--panel2:rgba(43,27,75,.68);--line:rgba(220,189,255,.16);--text:#f8f5ff;--muted:#aaa0c3;--pink:#ff72d2;--pink2:#ff9be2;--purple:#a868ff;--violet:#6b54ff;--blue:#5aa8ff;--cyan:#72d5ff;--gold:#ffb866;--good:#76e6bc;--warn:#ffc46c;--bad:#ff7f9f}}
*{{box-sizing:border-box}}html{{scroll-behavior:smooth;background:var(--bg)}}
body{{margin:0;color:var(--text);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;background:radial-gradient(circle at 78% 8%,rgba(132,65,255,.17),transparent 28%),radial-gradient(circle at 12% 58%,rgba(255,72,193,.08),transparent 23%),linear-gradient(180deg,#090615 0%,#0f0820 48%,#0a0616 100%);line-height:1.5}}
body:before{{content:"";position:fixed;inset:0;pointer-events:none;opacity:.34;background-image:radial-gradient(circle at 8% 20%,rgba(255,255,255,.45) 0 1px,transparent 1.4px),radial-gradient(circle at 68% 15%,rgba(255,255,255,.3) 0 1px,transparent 1.3px),radial-gradient(circle at 92% 63%,rgba(255,255,255,.22) 0 1px,transparent 1.2px);background-size:190px 180px,260px 240px,330px 310px}}
a{{color:inherit;text-decoration:none}}.shell{{max-width:1420px;margin:0 auto;padding:0 26px 44px}}
.topbar{{height:78px;display:flex;align-items:center;gap:32px;border-bottom:1px solid var(--line);position:relative;z-index:5}}.brand{{display:flex;align-items:center;gap:12px;font-weight:760;letter-spacing:.22em;font-size:14px}}.brand-mark{{width:32px;height:32px;border:1px solid var(--pink);border-radius:50%;position:relative;box-shadow:0 0 22px rgba(255,114,210,.25)}}.brand-mark:before{{content:"";position:absolute;left:50%;top:-4px;width:1px;height:40px;background:var(--pink);transform:rotate(-20deg)}}.nav{{display:flex;gap:26px;margin-left:auto;font-size:13px;color:#c8bddb}}.nav a.active,.nav a:hover{{color:white}}.updated{{font-size:12px;color:#9085a8}}.live-pill{{border:1px solid rgba(255,114,210,.7);padding:9px 18px;border-radius:999px;font-size:12px}}
.scope-controls{{display:flex;gap:8px;align-items:center;margin:24px 0 0}}.scope-btn,#project-select{{background:rgba(41,27,73,.72);border:1px solid var(--line);color:#cfc3df;border-radius:999px;padding:9px 15px;font-size:12px}}.scope-btn{{cursor:pointer}}.scope-btn.active{{background:linear-gradient(90deg,rgba(255,114,210,.22),rgba(132,86,255,.25));border-color:rgba(255,114,210,.55);color:#fff}}#project-select{{border-radius:10px;max-width:230px}}
.cosmic-hero{{min-height:500px;display:grid;grid-template-columns:1.02fr .98fr;align-items:center;position:relative;padding:48px 0 25px;overflow:hidden}}.hero-copy{{position:relative;z-index:2;max-width:650px}}.eyebrow{{font-size:13px;color:#d49cd3;letter-spacing:.12em;margin-bottom:18px}}.hero-copy h1{{font-size:clamp(44px,5.4vw,76px);line-height:1.12;letter-spacing:-.04em;font-weight:540;margin:0 0 22px}}.hero-copy h1 em{{font-family:ui-serif,"Songti SC","STSong",serif;font-weight:500;font-style:italic;color:var(--pink);text-shadow:0 0 30px rgba(255,114,210,.28)}}.hero-copy p{{color:#b5abc8;font-size:17px;max-width:560px;margin:0 0 26px}}.hero-tags{{display:flex;gap:24px;color:#c6bad8;font-size:13px}}.hero-tags span:before{{content:"✦";color:var(--pink);margin-right:8px}}
.cosmic-art{{height:410px;position:relative}}.planet{{position:absolute;width:310px;height:310px;border-radius:50%;right:16%;top:44px;background:radial-gradient(circle at 34% 28%,#f5c5ff 0,#d77cff 8%,#934eff 22%,#4f2daf 48%,#25145c 70%,#100928 100%);box-shadow:0 0 38px rgba(198,103,255,.55),0 0 120px rgba(124,66,255,.28),inset -35px -30px 50px rgba(10,5,25,.55)}}.planet:before{{content:"";position:absolute;inset:-26px;border:1px solid rgba(235,184,255,.35);border-radius:50%;transform:rotate(-14deg) scaleY(.58)}}.planet:after{{content:"";position:absolute;left:-70px;top:34px;width:58px;height:58px;border-radius:50%;background:radial-gradient(circle at 35% 30%,#e9b7ff,#8e4fff 52%,#25104d 100%)}}.moon{{position:absolute;width:66px;height:66px;border-radius:50%;right:7%;bottom:80px;background:radial-gradient(circle at 35% 30%,#ffd7ef,#bc67ff 42%,#28104f 100%)}}.orbit{{position:absolute;right:2%;top:84px;width:420px;height:250px;border:1px solid rgba(255,169,233,.24);border-radius:50%;transform:rotate(-11deg)}}.hero-note{{position:absolute;right:0;top:168px;color:#cba7da;font-family:ui-serif,"Songti SC",serif;font-style:italic;font-size:18px;line-height:1.7}}
.metric-grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:16px;margin:0 0 30px}}.metric-card,.model-card,.chart-panel,.advice-card,.role-card,.footer-card{{background:linear-gradient(145deg,rgba(41,27,73,.84),rgba(20,13,43,.82));border:1px solid var(--line);box-shadow:inset 0 1px 0 rgba(255,255,255,.035),0 18px 50px rgba(2,0,12,.25);backdrop-filter:blur(18px)}}.metric-card{{border-radius:18px;padding:19px 22px;min-height:110px}}.metric-card span{{display:block;font-size:12px;color:#aaa0c3}}.metric-card strong{{display:block;font-family:ui-serif,"Songti SC",serif;font-size:40px;font-weight:500;margin-top:6px}}.metric-card small{{color:#786e91;font-size:11px}}
.section{{padding:34px 0 20px}}.section-head{{display:flex;align-items:flex-end;justify-content:space-between;margin-bottom:18px}}.section-head h2{{font-size:25px;margin:0;font-weight:620}}.section-head h2:before{{content:"";display:inline-block;width:3px;height:26px;background:linear-gradient(var(--pink),var(--purple));border-radius:999px;margin-right:12px;vertical-align:-5px;box-shadow:0 0 14px rgba(255,114,210,.45)}}.section-head p{{margin:0;color:#887d9e;font-size:12px}}.model-grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:16px}}.model-card{{border-radius:18px;padding:20px;min-height:244px;position:relative;overflow:hidden}}.model-card:after{{content:"";position:absolute;width:130px;height:130px;border-radius:50%;right:-50px;top:-52px;opacity:.28;background:radial-gradient(circle,var(--pink),transparent 68%)}}.model-card-top{{display:flex;justify-content:space-between;align-items:center}}.model-icon{{width:46px;height:46px;border-radius:50%;background:radial-gradient(circle at 36% 30%,#bff2ff 0,#5ca8ff 20%,#6d4bff 48%,#21104c 100%);box-shadow:0 0 26px rgba(83,140,255,.4)}}.tone-1 .model-icon{{background:radial-gradient(circle at 36% 30%,#ffd2ff 0,#c468ff 24%,#7837d6 48%,#221044 100%)}}.tone-2 .model-icon{{background:radial-gradient(circle at 36% 30%,#d7c9ff 0,#795dff 25%,#4934c4 52%,#17103b 100%)}}.tone-3 .model-icon{{background:radial-gradient(circle at 36% 30%,#ffd8c6 0,#ff8a7c 25%,#a54d89 55%,#261039 100%)}}.status-pill{{font-size:11px;padding:5px 9px;border-radius:8px;background:rgba(153,99,255,.14);color:#dacdff;border:1px solid rgba(165,117,255,.2)}}.status-change_route,.status-switch_model{{color:#ffd4e6;background:rgba(255,107,184,.12)}}.model-card h3{{font-size:18px;margin:17px 0 2px}}.model-card>p{{margin:0;color:#897d9f;font-size:12px}}.hero-score{{display:flex;align-items:flex-end;gap:8px;margin:16px 0 15px}}.hero-score strong{{font-family:ui-serif,"Songti SC",serif;font-size:42px;font-weight:500;line-height:1}}.hero-score span{{font-size:10px;color:#7d7294;line-height:1.4}}.mini-stats{{display:flex;gap:12px;padding-top:12px;border-top:1px solid rgba(255,255,255,.08);font-size:11px;color:#9186a9}}.mini-stats b{{color:#e5dcf5;font-weight:550}}.card-meta{{margin-top:10px;color:#716684;font-size:10px}}
.chart-panel{{border-radius:20px;padding:24px;margin-top:18px}}.chart-legend{{display:flex;gap:22px;color:#a397b9;font-size:11px;margin:4px 0 20px}}.chart-legend i{{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px}}.lg-project{{background:var(--purple)}}.lg-baseline{{background:var(--blue)}}.lg-route{{background:var(--pink)}}.chart-row{{display:grid;grid-template-columns:150px 1fr;gap:22px;padding:14px 0;border-top:1px solid rgba(255,255,255,.055)}}.chart-model{{font-size:13px;color:#ddd3ea;padding-top:4px}}.chart-bars{{display:grid;gap:8px}}.chart-line{{display:grid;grid-template-columns:78px 1fr 105px;gap:9px;align-items:center;font-size:10px;color:#8f84a6}}.chart-line i{{height:7px;background:rgba(255,255,255,.07);border-radius:99px;overflow:hidden}}.chart-line b{{display:block;height:100%;border-radius:99px}}.bar-project{{background:linear-gradient(90deg,#b26dff,#8e62ff)}}.bar-baseline{{background:linear-gradient(90deg,#5e8fff,#6fd4ff)}}.bar-route{{background:linear-gradient(90deg,#ff78c8,#ff9fe0)}}.chart-line em{{font-style:normal;color:#d7cde6;text-align:right}}
.role-grid,.advice-grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:16px}}.role-card,.advice-card{{border-radius:18px;padding:19px;min-height:120px}}.role-card span{{color:#8f84a6;font-size:12px}}.role-card strong{{display:block;margin-top:14px;font-size:19px}}.advice-card{{min-height:180px}}.advice-icon{{font-size:24px;color:var(--pink)}}.advice-action{{display:inline-block;margin:10px 0 8px;color:#f3e9ff;font-weight:700}}.advice-card h3{{margin:0 0 8px;font-size:16px}}.advice-card p{{margin:0;color:#9b90b0;font-size:12px;line-height:1.7}}
.footer-card{{margin-top:30px;border-radius:24px;padding:28px 34px;display:grid;grid-template-columns:1fr auto;align-items:center;min-height:135px;position:relative;overflow:hidden}}.footer-card:after{{content:"";position:absolute;width:230px;height:230px;border-radius:50%;right:4%;top:-96px;background:radial-gradient(circle at 40% 30%,#f9cfff,#a95eff 28%,#4b2391 58%,transparent 71%);opacity:.5}}.footer-card h2{{font-family:ui-serif,"Songti SC",serif;font-weight:500;font-size:30px;margin:0 0 8px;color:#f0c7ec}}.footer-card p{{margin:0;color:#9d91b2;font-size:13px}}.footer-meta{{position:relative;z-index:2;text-align:right;color:#837793;font-size:11px}}
@media(max-width:1050px){{.model-grid,.advice-grid,.role-grid{{grid-template-columns:repeat(2,1fr)}}.cosmic-hero{{grid-template-columns:1fr}}.cosmic-art{{height:360px}}.nav{{display:none}}}}@media(max-width:680px){{.shell{{padding:0 15px 30px}}.updated,.live-pill{{display:none}}.hero-copy h1{{font-size:43px}}.metric-grid,.model-grid,.advice-grid,.role-grid{{grid-template-columns:1fr}}.hero-tags{{flex-direction:column;gap:8px}}.planet{{right:4%;width:250px;height:250px}}.chart-row{{grid-template-columns:1fr}}.footer-card{{grid-template-columns:1fr}}}}
</style>
</head>
<body><div class="shell">
<header class="topbar"><a class="brand" href="#overview"><span class="brand-mark"></span><span>MODEL INTELLIGENCE</span></a><nav class="nav"><a class="active" href="#overview">总览</a><a href="#model-comparison">模型表现</a><a href="#roles">最佳岗位</a><a href="#decisions">建议方案</a></nav><span class="updated">更新于 {generated}</span><span class="live-pill">真实项目数据</span></header>
{controls}
<main><section class="cosmic-hero"><div class="hero-copy"><div class="eyebrow">让 AI 真正创造价值</div><h1 aria-label="更好的模型组合 创造更大的可能">更好的模型组合<br>创造<em>更大的可能</em></h1><p>基于真实项目表现与 ModelDial 外部基线，判断模型强弱、执行链问题，以及是否值得调整调用策略。</p><div class="hero-tags"><span>真实项目数据</span><span>外部基线评测</span><span>智能决策建议</span></div></div><div class="cosmic-art"><div class="orbit"></div><div class="planet"></div><div class="moon"></div><div class="hero-note">找到更适合你的<br>AI 工作方式</div></div></section>
<section id="overview"><div id="metric-grid" class="metric-grid">{''.join(_metric_card(*item) for item in metrics)}</div></section>
<section id="model-comparison" class="section"><div class="section-head"><div><h2>模型表现对比</h2><p>一个模型一张卡，去掉配置噪音。</p></div><p>实战 + ModelDial + 执行稳定</p></div><div id="model-grid" class="model-grid">{_model_cards(summaries, global_scope=global_scope)}</div><div class="chart-panel"><div class="section-head"><div><h2>能力对比</h2></div></div><div class="chart-legend"><span><i class="lg-project"></i>{'全局实战' if global_scope else '项目实战'}</span><span><i class="lg-baseline"></i>外部基线</span><span><i class="lg-route"></i>执行稳定</span></div><div id="chart-rows">{_comparison_chart(summaries, global_scope=global_scope)}</div></div></section>
<section id="roles" class="section"><div class="section-head"><div><h2>最佳岗位</h2><p>只展示有实战证据支持的角色倾向。</p></div></div><div id="role-grid" class="role-grid">{_role_cards(summaries)}</div></section>
<section id="decisions" class="section"><div class="section-head"><div><h2>智能建议</h2><p>先判断是模型问题还是执行链问题，再决定是否换模。</p></div></div><div id="advice-grid" class="advice-grid">{_decision_cards(summaries)}</div></section>
</main>
<footer class="footer-card"><div><h2>让更好的模型，去做更适合它的工作。</h2><p>项目实战优先，外部基线辅助；样本不足时，不做激进换模。</p></div><div class="footer-meta"><b id="view-name">{_e(project)}</b><br>Adaptive Agent Runtime · 模型智能看板</div></footer>
</div>
<script id="report-data" type="application/json">{safe_json}</script>
<script id="view-data" type="application/json">{safe_view_json}</script>
<script>
(() => {{
  const dataNode=document.getElementById('view-data'); if(!dataNode) return;
  const views=JSON.parse(dataNode.textContent); const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]));
  const score=v=>typeof v==='number'?v.toFixed(1):'待积累'; const plain=v=>typeof v==='number'?v.toFixed(1):'—';
  const roleLabel=r=>({{controller:'总控',reviewer:'审查',writer:'执行',uiux:'前端设计',researcher:'调研',research:'调研'}}[r]||r);
  function routeText(x){{return x.route_status&&x.route_status!=='稳定'?x.route_status:(typeof x.route==='number'?x.route.toFixed(1):'待积累')}}
  function render(view,name){{
    document.getElementById('metric-grid').innerHTML=view.metrics.map(m=>`<article class="metric-card"><span>${{esc(m[0])}}</span><strong>${{esc(m[1])}}</strong><small>${{esc(m[2])}}</small></article>`).join('');
    document.getElementById('model-grid').innerHTML=view.summaries.map((x,i)=>`<article class="model-card tone-${{i%4}}"><div class="model-card-top"><div class="model-icon"></div><span class="status-pill">${{esc(x.action_label)}}</span></div><h3>${{esc(x.model)}}</h3><p>${{esc([...(x.efforts||[]),(x.providers||[])[0]].filter(Boolean).join(' · ')||'实战')}}</p><div class="hero-score"><strong>${{score(x.project)}}</strong><span>/100<br>${{view.global_scope?'全局实战':'项目实战'}}</span></div><div class="mini-stats"><span>基线 <b>${{plain(x.baseline)}}</b></span><span>稳定 <b>${{esc(routeText(x))}}</b></span></div><div class="card-meta">${{view.global_scope?esc(`${{x.projects||0}} 项目 · ${{x.samples||0}} 样本`):esc(`${{x.samples||0}} 样本`)}}</div></article>`).join('');
    document.getElementById('chart-rows').innerHTML=view.summaries.map(x=>`<div class="chart-row"><div class="chart-model">${{esc(x.model)}}</div><div class="chart-bars"><div class="chart-line"><span>${{view.global_scope?'全局实战':'项目实战'}}</span><i><b class="bar-project" style="width:${{typeof x.project==='number'?Math.max(0,Math.min(100,x.project)):0}}%"></b></i><em>${{score(x.project)}}</em></div><div class="chart-line"><span>外部基线</span><i><b class="bar-baseline" style="width:${{typeof x.baseline==='number'?Math.max(0,Math.min(100,x.baseline)):0}}%"></b></i><em>${{plain(x.baseline)}}</em></div><div class="chart-line"><span>执行稳定</span><i><b class="bar-route" style="width:${{typeof x.route==='number'?Math.max(0,Math.min(100,x.route)):0}}%"></b></i><em>${{esc(routeText(x))}}</em></div></div></div>`).join('');
    document.getElementById('role-grid').innerHTML=view.summaries.map(x=>`<article class="role-card"><span>${{esc(x.model)}}</span><strong>${{esc((x.preferred_roles||[]).map(roleLabel).join(' / ')||'待积累')}}</strong></article>`).join('');
    document.getElementById('advice-grid').innerHTML=view.summaries.map(x=>`<article class="advice-card"><div class="advice-icon">✦</div><span class="advice-action">${{esc(x.action_label)}}</span><h3>${{esc(x.model)}}</h3><p>${{esc(x.reason)}}</p></article>`).join('');
    document.getElementById('view-name').textContent=name;
  }}
  const buttons=[...document.querySelectorAll('.scope-btn')], select=document.getElementById('project-select');
  buttons.forEach(btn=>btn.addEventListener('click',()=>{{buttons.forEach(b=>b.classList.remove('active'));btn.classList.add('active');if(btn.dataset.scope==='global')render(views.global,'全局模型情报');else if(select&&views.projects[select.value])render(views.projects[select.value],select.value)}}));
  if(select) select.addEventListener('change',()=>{{const projectBtn=document.querySelector('[data-scope="project"]');if(projectBtn){{buttons.forEach(b=>b.classList.remove('active'));projectBtn.classList.add('active')}}if(views.projects[select.value])render(views.projects[select.value],select.value)}});
}})();
</script>
</body></html>'''

"""
Context formatting helpers — converts raw MCP JSON into compact LLM-readable text.
Ported from the Streamlit app.py _fmt_* functions and summarise_context().
"""
from __future__ import annotations
import json

_MAX_SECTION_CHARS = 8_000


def _short_image(img: str) -> str:
    if not img:
        return "?"
    return img.rsplit("/", 1)[-1]


def fmt_pods(pods: list) -> str:
    if not isinstance(pods, list) or not pods:
        return "(no pods)"
    rows = [
        "| NAME | PHASE | READY | RESTARTS | NODE | CONTAINERS |",
        "|------|-------|-------|----------|------|-----------|",
    ]
    for p in pods:
        if not isinstance(p, dict):
            continue
        conts      = p.get("containers") or []
        init_conts = p.get("initContainers") or []
        ready      = (
            f"{sum(1 for c in conts if isinstance(c, dict) and c.get('ready', False))}"
            f"/{len(conts)}"
        ) if conts else p.get("ready", "?")
        restarts   = sum(c.get("restarts", c.get("restartCount", 0)) for c in conts if isinstance(c, dict))
        cont_parts = [f"{c['name']}({_short_image(c.get('image',''))})" for c in conts if isinstance(c, dict) and c.get("name")]
        init_parts = [f"[init]{c['name']}({_short_image(c.get('image',''))})" for c in init_conts if isinstance(c, dict) and c.get("name")]
        rows.append(
            f"| {p.get('name','?')} | {p.get('phase','?')} | {ready} | {restarts} "
            f"| {p.get('nodeName', p.get('node','?'))} | {', '.join(cont_parts + init_parts) or '?'} |"
        )
    return "\n".join(rows)


def fmt_events(events: list) -> str:
    if not isinstance(events, list) or not events:
        return "(no events)"
    rows = ["| TYPE | REASON | OBJECT | MESSAGE |", "|------|--------|--------|---------|"]
    for e in sorted(events, key=lambda e: e.get("type", "") != "Warning")[:40]:
        if not isinstance(e, dict):
            continue
        obj = e.get("involvedObject", {})
        obj_str = f"{obj.get('kind','')}/{obj.get('name','')}" if isinstance(obj, dict) else str(obj)
        msg = (e.get("message") or "")[:120].replace("|", "\\|")
        rows.append(f"| {e.get('type','?')} | {e.get('reason','?')} | {obj_str} | {msg} |")
    return "\n".join(rows)


def fmt_deployments(deps: list) -> str:
    if not isinstance(deps, list) or not deps:
        return "(no deployments)"
    rows = [
        "| NAME | DESIRED | READY | UP-TO-DATE | AVAILABLE | IMAGE |",
        "|------|---------|-------|------------|-----------|-------|",
    ]
    for d in deps:
        if not isinstance(d, dict):
            continue
        spec   = d.get("spec", {})
        status = d.get("status", {})
        containers = spec.get("template", {}).get("spec", {}).get("containers", [])
        image = containers[0].get("image", "?") if containers else "?"
        rows.append(
            f"| {d.get('name','?')} | {spec.get('replicas','?')} "
            f"| {status.get('readyReplicas',0)} | {status.get('updatedReplicas',0)} "
            f"| {status.get('availableReplicas',0)} | {image} |"
        )
    return "\n".join(rows)


def summarise_context(ctx: dict) -> str:
    lines = []
    for key, val in ctx.items():
        header = f"[{key.upper()}]"
        if val is None:
            continue
        if isinstance(val, dict) and "error" in val and len(val) == 1:
            lines.append(f"{header}\n⚠ MCP endpoint failed: {val['error']}")
            continue
        if key == "pods" and isinstance(val, list):
            lines.append(f"{header}\n{fmt_pods(val)}")
            continue
        if key.startswith("logs"):
            text = str(val)
            if len(text) > _MAX_SECTION_CHARS:
                text = f"[... truncated — showing last {_MAX_SECTION_CHARS} chars ...]\n" + text[-_MAX_SECTION_CHARS:]
            lines.append(f"{header}\n```\n{text}\n```")
            continue
        if key in ("namespace_events", "events") and isinstance(val, list):
            lines.append(f"{header}\n{fmt_events(val)}")
            continue
        if key == "deployments" and isinstance(val, list):
            lines.append(f"{header}\n{fmt_deployments(val)}")
            continue
        if key == "pods_by_namespace" and isinstance(val, dict):
            parts = [f"**{ns}**\n{fmt_pods(pod_list)}" for ns, pod_list in val.items()]
            lines.append(f"{header}\n" + "\n\n".join(parts))
            continue
        raw = json.dumps(val, indent=2, default=str) if isinstance(val, (dict, list)) else str(val)
        if len(raw) > _MAX_SECTION_CHARS:
            raw = raw[:_MAX_SECTION_CHARS] + f"\n... [truncated at {_MAX_SECTION_CHARS} chars]"
        lines.append(f"{header}\n{raw}")
    return "\n\n".join(lines)

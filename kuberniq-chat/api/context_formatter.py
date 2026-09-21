"""Compact evidence that preserves source identity, failures and coverage limits."""
from __future__ import annotations
import json

MAX_CONTEXT_CHARS = 48000
MAX_SECTION_CHARS = 6000
MAX_ROWS = 60


def cell(value):
    return str(value if value is not None else '?').replace('|', '\\|').replace('\n', ' ')


def table(headers, rows, total):
    result = ['| ' + ' | '.join(headers) + ' |', '|' + '|'.join('---' for _ in headers) + '|']
    result.extend('| ' + ' | '.join(cell(v) for v in row) + ' |' for row in rows)
    if total > len(rows):
        result.append(f'[Coverage: showing {len(rows)} of {total} records.]')
    return '\n'.join(result)


def fmt_pods(pods):
    from rag_retrieval import pod_priority
    items = sorted([p for p in pods if isinstance(p, dict)], key=pod_priority)
    rows = []
    for p in items[:MAX_ROWS]:
        containers = [c for c in (p.get('containers') or []) if isinstance(c, dict)]
        init = [c for c in (p.get('initContainers') or []) if isinstance(c, dict)]
        ready = p.get('ready', f'{sum(bool(c.get("ready")) for c in containers)}/{len(containers)}')
        restarts = p.get('restarts', sum(c.get('restarts', c.get('restartCount', 0)) or 0 for c in containers + init))
        details = [f'{"[init] " if c in init else ""}{c.get("name", "?")} image={c.get("image", "?")} state={c.get("state", "?")}' for c in containers + init]
        rows.append([p.get('name'), p.get('phase'), ready, restarts, p.get('nodeName', p.get('node')), '; '.join(details)])
    return table(['NAME', 'PHASE', 'READY', 'RESTARTS', 'NODE', 'CONTAINERS'], rows, len(items)) if items else '(no pods returned)'


def fmt_events(events):
    items = sorted([e for e in events if isinstance(e, dict)], key=lambda e: e.get('type') != 'Warning')
    rows = []
    for e in items[:MAX_ROWS]:
        obj = e.get('regarding') or e.get('involvedObject') or {}
        obj = f'{obj.get("kind", "")}/{obj.get("name", "")}' if isinstance(obj, dict) else obj
        rows.append([e.get('type'), e.get('reason'), obj, e.get('message'), e.get('count'), e.get('lastTime', e.get('lastTimestamp'))])
    return table(['TYPE', 'REASON', 'OBJECT', 'MESSAGE', 'COUNT', 'LAST SEEN'], rows, len(items)) if items else '(no events returned)'


def fmt_deployments(deployments):
    items = [d for d in deployments if isinstance(d, dict)]
    rows = []
    for d in items[:MAX_ROWS]:
        spec, status = d.get('spec') or {}, d.get('status') or {}
        containers = d.get('containers') or spec.get('template', {}).get('spec', {}).get('containers', [])
        rows.append([d.get('name'), d.get('replicas', spec.get('replicas')), d.get('ready', status.get('readyReplicas')),
                     d.get('available', status.get('availableReplicas')), ', '.join(c.get('image', '?') for c in containers), d.get('selector', {})])
    return table(['NAME', 'DESIRED', 'READY', 'AVAILABLE', 'IMAGES', 'SELECTOR'], rows, len(items)) if items else '(no deployments returned)'


def render(key, value):
    kind = key.split(' ')[0]
    if isinstance(value, dict) and 'error' in value:
        return 'Unavailable evidence: ' + str(value['error'])
    if isinstance(value, list):
        if kind == 'pods':
            return fmt_pods(value)
        if kind == 'events':
            return fmt_events(value)
        if kind == 'deployments':
            return fmt_deployments(value)
        total = len(value)
        text = json.dumps(value[:MAX_ROWS], ensure_ascii=False, default=str)
        return text + (f'\n[Coverage: showing {MAX_ROWS} of {total} records.]' if total > MAX_ROWS else '')
    return json.dumps(value, ensure_ascii=False, default=str) if isinstance(value, dict) else str(value)


def summarise_context(ctx, max_chars=MAX_CONTEXT_CHARS):
    # Access/coverage metadata precedes bulk evidence so truncation cannot hide it.
    priority = ['namespace_denied', 'permission_denied', 'clarification', 'retrieval', 'query_plan', 'log_time_window']
    keys = [k for k in priority if k in ctx] + [k for k in ctx if k not in priority]
    sections = []
    for key in keys:
        value = ctx[key]
        source = ''
        if isinstance(value, dict) and 'source' in value and 'data' in value:
            source = f'Source: {value["source"]}\n'
            if 'count' in value:
                source += f'Records returned by this endpoint: {value["count"]}\n'
            value = value['data']
        raw = render(key, value)
        if len(raw) > MAX_SECTION_CHARS:
            raw = raw[:MAX_SECTION_CHARS] + '\n[Coverage: section truncated; more data was returned.]'
        sections.append(f'[{key.upper()}]\n{source}{raw}')
    # Allocate spare space to diagnostic details/logs while keeping every source
    # represented. Small metadata sections return their unused share to the pool.
    weights = [3 if key in priority or 'detail' in key or key.startswith('logs ') else 1 for key in keys]
    allocations = [0] * len(sections)
    remaining = max(0, max_chars - 200 - 2 * max(0, len(sections) - 1))
    pending = set(range(len(sections)))
    while remaining and pending:
        unit = max(1, remaining // sum(weights[i] for i in pending))
        for i in sorted(pending):
            share = min(remaining, len(sections[i]) - allocations[i], unit * weights[i])
            allocations[i] += share
            remaining -= share
            if allocations[i] == len(sections[i]):
                pending.remove(i)
    marker = '\n[Coverage: section shortened for budget.]'
    output = '\n\n'.join(
        s if len(s) <= limit else (s[:max(0, limit - len(marker))] + marker)[:limit]
        for s, limit in zip(sections, allocations))
    if any(len(s) > limit for s, limit in zip(sections, allocations)):
        output += '\n[Coverage: context budget reached. Do not infer exhaustive totals from sampled evidence.]'
    return output[:max_chars]

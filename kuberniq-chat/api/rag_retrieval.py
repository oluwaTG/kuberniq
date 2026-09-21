"""Bounded, authorized retrieval with provenance and partial-result preservation."""
from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from urllib.parse import quote, urlencode

import auth
from rag_plan import CLUSTER_SCOPED, DETAIL_ROUTES, NAMESPACED, QueryPlan

MAX_CLUSTERS = 3
MAX_NAMESPACES = 20
MAX_REQUESTS = 80
MAX_LOG_PODS = 6
MAX_DETAILS = 12
REQUEST_TIMEOUT = 8
RETRIEVAL_TIMEOUT = 35


def name_of(item):
    metadata = item.get('metadata')
    return item.get('name') or (metadata.get('name', '') if isinstance(metadata, dict) else '')


def pod_priority(pod):
    containers = (pod.get('containers') or []) + (pod.get('initContainers') or [])
    bad = pod.get('phase') not in ('Running', 'Succeeded') or any(
        c.get('ready') is False or c.get('state', '').startswith('Waiting') or ('Terminated' in c.get('state', '') and 'Completed' not in c.get('state', ''))
        for c in containers if isinstance(c, dict))
    restarts = sum(c.get('restarts', c.get('restartCount', 0)) or 0 for c in containers if isinstance(c, dict))
    return (not bad, -restarts, name_of(pod))


def filter_log_window(value, since, until):
    """Filter timestamped lines in code; never ask the LLM to enforce a time range."""
    if isinstance(value, dict):
        if 'error' in value:
            return value
        return {key: filter_log_window(text, since, until) for key, text in value.items()}
    if isinstance(value, str) and value.startswith('[error fetching logs:'):
        return {'error': value}
    if not isinstance(value, str) or not (since or until):
        return value
    kept, excluded = [], 0
    for line in value.splitlines():
        timestamp = line.partition(' ')[0]
        try:
            instant = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
            if instant.tzinfo is None:
                raise ValueError('Timestamp has no timezone')
        except ValueError:
            excluded += 1
            continue
        if (since is None or instant >= since) and (until is None or instant <= until):
            kept.append(line)
    if excluded:
        kept.append(f'[Coverage: {excluded} untimestamped lines excluded; their time could not be verified.]')
    return '\n'.join(kept) or '(no matching log lines in the retrieved tail; older lines may be outside the sample)'


class Retriever:
    def __init__(self, getter, user):
        self.getter = getter
        self.user = user
        self.context = {'retrieval': {'started_at': datetime.now(timezone.utc).isoformat(), 'metadata_cache_ttl_seconds': 60, 'notices': []}}
        self.notices = self.context['retrieval']['notices']
        self.endpoints = []
        self.tasks = {}
        self.records = {}
        self.semaphore = asyncio.Semaphore(6)
        self.log_count = 0
        self.detail_count = 0

    def path(self, resource, cluster='local', ns=None, name=None, **query):
        parts = ['namespaces', ns, resource] if ns is not None else [resource]
        path = '/' + '/'.join(quote(p, safe='/') if p == resource else quote(p, safe='') for p in parts)
        if name:
            path += '/' + quote(name, safe='')
        if cluster != 'local':
            query['cluster'] = cluster
        return path + ('?' + urlencode({k: v for k, v in query.items() if v is not None}) if any(v is not None for v in query.values()) else '')

    async def get(self, path, text=False):
        key = (path, text)
        if key not in self.tasks:
            if len(self.tasks) >= MAX_REQUESTS:
                self.notices.append(f'Request budget reached; skipped {path}. Narrow the scope for complete coverage.')
                return {'error': 'Request budget reached; not fetched'}

            async def run():
                async with self.semaphore:
                    self.endpoints.append(path)
                    try:
                        result = await asyncio.wait_for(self.getter(path, text=text), REQUEST_TIMEOUT)
                    except asyncio.TimeoutError:
                        result = {'error': 'Endpoint timed out'}
                    except Exception as exc:
                        result = {'error': f'Endpoint failed ({type(exc).__name__})'}
                    if isinstance(result, dict) and 'error' in result:
                        self.notices.append(f'Unavailable evidence: {path}')
                    return result
            self.tasks[key] = asyncio.create_task(run())
        return await self.tasks[key]

    async def evidence(self, kind, cluster, ns=None, name=None, resource=None, text=False, **query):
        path = self.path(resource or NAMESPACED.get(kind, kind), cluster, ns, name, **query)
        value = await self.get(path, text)
        key = f'{kind} cluster={cluster}' + (f' namespace={ns}' if ns else '') + (f' name={name}' if name else '')
        self.context[key] = {'source': path, 'data': value}
        if isinstance(value, list):
            self.context[key]['count'] = len(value)
        if not name:
            self.records[(cluster, ns, kind)] = value
        return value, key

    async def cluster(self, cluster, plan, intents):
        namespaces, _ = await self.evidence('namespaces', cluster)
        if not isinstance(namespaces, list) or any(not isinstance(n, str) for n in namespaces):
            self.notices.append(f'Namespace discovery failed for {cluster}; no namespaced requests were made.')
            namespaces = []
        allowed = auth.filter_namespaces(namespaces, self.user)
        # Never expose the unfiltered namespace inventory to the answer model/client.
        self.context[f'namespaces cluster={cluster}']['data'] = allowed
        self.context[f'namespaces cluster={cluster}']['count'] = len(allowed)
        self.context['namespaces'] = allowed if len(plan.clusters) <= 1 else self.context.get('namespaces', [])
        denied = [ns for ns in plan.namespaces if ns not in allowed]
        if denied:
            self.context['namespace_denied'] = f'Namespaces unavailable or unauthorized in {cluster}: {", ".join(denied)}. No resource data was fetched for this cluster.'
            return
        selected = plan.namespaces or allowed
        per_namespace = sum(kind in NAMESPACED for kind in intents) + (2 if 'rbac' in intents else 0)
        global_reads = 1 + sum(kind in CLUSTER_SCOPED for kind in intents) + (1 if 'rbac' in intents else 0)
        reserve = (MAX_LOG_PODS if 'logs' in intents else 0) + (MAX_DETAILS if 'troubleshoot' in intents or plan.targets else 0)
        remaining_clusters = max(1, len(plan.clusters[:MAX_CLUSTERS]) - plan.clusters.index(cluster))
        cluster_budget = (MAX_REQUESTS - len(self.tasks)) // remaining_clusters
        affordable = max(1, (cluster_budget - global_reads - reserve) // max(1, per_namespace))
        scope_limit = min(MAX_NAMESPACES, affordable)
        if len(selected) > scope_limit:
            self.notices.append(f'{cluster}: searched {scope_limit} of {len(selected)} accessible namespaces, reserving budget for diagnostic details. Narrow the scope for complete results.')
        selected = selected[:scope_limit]
        jobs = [self.evidence('cluster_info', cluster, resource='cluster/info')]
        for kind in intents:
            if kind in CLUSTER_SCOPED:
                jobs.append(self.evidence(kind, cluster, resource=CLUSTER_SCOPED[kind]))
                if kind == 'rbac':
                    jobs.append(self.evidence('clusterrolebindings', cluster, resource='clusterrolebindings'))
            if kind in NAMESPACED:
                jobs.extend(self.evidence(kind, cluster, ns) for ns in selected)
            if kind == 'rbac':
                jobs.extend(self.evidence(kind, cluster, ns, resource=kind)
                            for ns in selected for kind in ('roles', 'rolebindings'))
        await asyncio.gather(*jobs)
        await self.resolve_targets(cluster, selected, plan, intents)

    def items(self, cluster, ns, kind):
        value = self.records.get((cluster, ns, kind))
        return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []

    async def resolve_targets(self, cluster, namespaces, plan, intents):
        # Resolve each explicit name across the whole permitted scope before fetching details/logs.
        selected_pods = []
        selectors = {}
        workload_prefixes = {}
        ambiguous = False
        for kind, names in plan.targets.items():
            if kind not in intents:
                continue
            scope = [None] if kind == 'nodes' else namespaces
            for target in names:
                matches = [(ns, item) for ns in scope
                           for item in self.items(cluster, ns, kind)
                           if isinstance(item, dict) and name_of(item) == target]
                if len({ns for ns, _ in matches}) > 1 and not plan.namespaces:
                    self.context.setdefault('clarification', []).append(
                        f'{kind} {target!r} exists in multiple namespaces in {cluster}: '
                        + ', '.join(str(ns) for ns, _ in matches) + '. Specify a namespace.')
                    ambiguous = True
                    continue
                if not matches:
                    self.notices.append(f'No exact {kind} match for {target!r} in the searched scope on {cluster}; discovery may be incomplete if endpoints failed.')
                for ns, item in matches:
                    self.context[f'{kind}_match cluster={cluster} namespace={ns} name={target}'] = {
                        'source': self.path(NAMESPACED.get(kind, kind), cluster, ns), 'data': item}
                    if kind == 'pods':
                        selected_pods.append((ns, item))
                    if kind in ('services', 'deployments') and isinstance(item.get('selector'), dict):
                        selectors.setdefault(ns, []).append(item['selector'])
                    if kind in ('statefulsets', 'daemonsets', 'jobs', 'cronjobs') or (kind in ('services', 'deployments') and not item.get('selector')):
                        workload_prefixes.setdefault(ns, []).append(target)
                    if kind in DETAIL_ROUTES and self.detail_count < MAX_DETAILS:
                        self.detail_count += 1
                        await self.evidence(kind + '_detail', cluster, ns, target, resource=NAMESPACED.get(kind, kind))
        if ambiguous:
            return

        candidates = [(ns, pod) for ns in namespaces
                      for pod in self.items(cluster, ns, 'pods') if isinstance(pod, dict)]
        workload_names = [name for kind, names in plan.targets.items()
                          if kind in ('deployments', 'services', 'statefulsets', 'daemonsets', 'jobs', 'cronjobs') for name in names]
        application = plan.application
        if not plan.targets.get('pods'):
            for ns, pod in candidates:
                labels = pod.get('labels') or {}
                label_match = bool(plan.labels) and all(labels.get(k) == v for k, v in plan.labels.items())
                selector_match = any(sel and all(labels.get(k) == v for k, v in sel.items()) for sel in selectors.get(ns, []))
                app_match = application and (labels.get('app') == application or labels.get('app.kubernetes.io/name') == application)
                # A workload prefix is a fallback, never an arbitrary substring match.
                prefix_match = any(name_of(pod).startswith(n + '-') for n in workload_prefixes.get(ns, []) + ([application] if application else []))
                if (plan.labels and label_match) or (not plan.labels and (selector_match or app_match or prefix_match)):
                    selected_pods.append((ns, pod))
            if not (application or workload_names or plan.labels) and ('logs' in intents or 'troubleshoot' in intents):
                selected_pods = candidates
                if selected_pods:
                    self.notices.append(f'{cluster}: no pod target specified; inspecting a bounded sample, prioritizing unhealthy/restarting pods.')
        if application and len({ns for ns, _ in selected_pods}) > 1 and not plan.namespaces:
            self.context.setdefault('clarification', []).append(f'Application {application!r} matches several namespaces on {cluster}; specify a namespace before fetching logs.')
            return
        if plan.labels:
            selected_pods = [(ns, p) for ns, p in selected_pods if all((p.get('labels') or {}).get(k) == v for k, v in plan.labels.items())]
        selected_pods = list({(ns, name_of(p)): (ns, p) for ns, p in selected_pods}.values())
        selected_pods.sort(key=lambda pair: pod_priority(pair[1]))
        if (application or workload_names or plan.labels or plan.targets.get('pods')) and not selected_pods and 'logs' in intents:
            self.notices.append(f'{cluster}: no matching pods found; no unrelated pod logs were fetched.')
        for ns, pod in selected_pods:
            name = name_of(pod)
            if not re.fullmatch(r'[a-z0-9][a-z0-9.-]*', name):
                continue
            if 'troubleshoot' in intents and self.detail_count < MAX_DETAILS:
                self.detail_count += 1
                await self.evidence('pod_detail', cluster, ns, name, resource='pods')
            if 'logs' not in intents:
                continue
            if self.log_count >= MAX_LOG_PODS:
                self.notices.append('Log sample limit reached; not every matching pod was inspected.')
                break
            self.log_count += 1
            container = plan.container
            containers = (pod.get('containers') or []) + (pod.get('initContainers') or [])
            if container and container not in [c.get('name') for c in containers if isinstance(c, dict)]:
                self.notices.append(f'Container {container!r} not found in {cluster}/{ns}/{name}; logs skipped.')
                continue
            route = f'pods/{quote(name, safe="")}/' + (
                f'containers/{quote(container, safe="")}/logs' if container else 'logs/all')
            value, key = await self.evidence(
                'logs', cluster, ns, resource=route, text=bool(container),
                tail=plan.tail, sinceTime=plan.since.isoformat() if plan.since else None,
                previous='true' if plan.previous else 'false',
            )
            # Include pod identity in keys so multiple log samples cannot overwrite each other.
            record = self.context.pop(key)
            record['data'] = filter_log_window(value, plan.since, plan.until)
            self.context[f'logs cluster={cluster} namespace={ns} pod={name}'] = record


async def retrieve(question, history, user, model, getter, planner):
    reader = Retriever(getter, user or {'role': 'viewer', 'allowed_namespaces': []})
    if reader.user.get('role') not in auth.ROLE_ALLOWED_INTENTS:
        reader.context['permission_denied'] = 'Unknown role; no cluster data was fetched.'
        return reader.context, []

    async def execute():
        clusters = await reader.get('/clusters')
        known = ['local'] + [c['name'] for c in clusters if isinstance(c, dict) and isinstance(c.get('name'), str)] if isinstance(clusters, list) else ['local']
        plan, notes = await planner(question, history, known, model)
        reader.notices.extend(notes)
        reader.context['query_plan'] = plan.model_dump(mode='json')
        if plan.clarification:
            reader.context['clarification'] = [plan.clarification]
            return
        if plan.mode == 'general':
            return
        cluster_names = {name.casefold(): name for name in known}
        plan.clusters = list(dict.fromkeys(cluster_names.get(c.casefold(), c) for c in (plan.clusters or ['local'])))
        unknown = [c for c in plan.clusters if c not in known]
        if unknown:
            reader.context['clarification'] = [f'Unknown or unavailable cluster: {", ".join(unknown)}. Available clusters: {", ".join(dict.fromkeys(known))}.']
            return
        reader.context['registered_clusters'] = clusters
        requested = list(dict.fromkeys(plan.intents + list(plan.targets)))
        if 'troubleshoot' in requested:
            requested += ['pods', 'events', 'deployments', 'logs', 'hpa', 'resourcequotas', 'limitranges']
        if 'logs' in requested or plan.application or plan.labels:
            requested += ['pods', 'deployments', 'services']
        intents = auth.filter_intents(list(dict.fromkeys(requested)), reader.user['role'])
        blocked = set(requested) - set(intents)
        if blocked:
            reader.context['permission_denied'] = '\n'.join(auth.permission_denied_note(i, reader.user['role']) for i in sorted(blocked))
        if 'pod_metrics' in intents or 'node_metrics' in intents:
            reader.notices.append('Metrics endpoints expose configured requests/limits and node capacity/allocatable resources, not measured CPU/memory usage or historical metrics.')
        if len(plan.clusters) > MAX_CLUSTERS:
            reader.notices.append(f'Only the first {MAX_CLUSTERS} requested clusters were inspected.')
        if plan.since or plan.until:
            reader.context['log_time_window'] = {'since': plan.since.isoformat() if plan.since else None,
                                                'until': plan.until.isoformat() if plan.until else None,
                                                'tail_per_container': plan.tail}
        # Sequential clusters give stable budget allocation; namespace/resource calls run concurrently.
        for cluster in plan.clusters[:MAX_CLUSTERS]:
            await reader.cluster(cluster, plan, intents)

    try:
        await asyncio.wait_for(execute(), RETRIEVAL_TIMEOUT)
    except asyncio.TimeoutError:
        reader.notices.append('Retrieval deadline reached. Available evidence is partial; no missing data should be inferred.')
    finally:
        for task in reader.tasks.values():
            if not task.done():
                task.cancel()
        await asyncio.gather(*reader.tasks.values(), return_exceptions=True)
    reader.context['retrieval']['completed_at'] = datetime.now(timezone.utc).isoformat()
    reader.context['retrieval']['requests'] = len(reader.endpoints)
    return reader.context, reader.endpoints

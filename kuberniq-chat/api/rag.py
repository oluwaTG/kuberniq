"""
RAG pipeline — ported from the Streamlit app.py.
All MCP fetching, entity extraction, and intent classification live here.
Uses litellm for LLM calls so any provider works (OpenAI, Anthropic, etc.).
"""
from __future__ import annotations

import json
import os
import asyncio
import re
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import quote

import litellm
from dateutil import parser as dateutil_parser

from mcp_client import mcp_get, get_all_namespaces
from context_formatter import summarise_context
import auth as _auth

# ── Config ────────────────────────────────────────────────────────────────────
LLM_MODEL   = os.getenv("LLM_MODEL",   os.getenv("OPENAI_MODEL", "gpt-4o"))
FAST_MODEL  = os.getenv("FAST_LLM_MODEL", "gpt-4o-mini")
DEV_MODE    = os.getenv("DEV_MODE", "false").lower() == "true"

SYSTEM_PROMPT = """You are an expert Kubernetes assistant powered by live cluster data.

STRICT RULES:
1. Answer ONLY from the data in the [SECTION] blocks below. Never invent pod names, IPs, images, or counts.
2. If a section shows "⚠ MCP endpoint failed", tell the user that data was unavailable.
3. If data for a query is missing entirely, say so — do not guess.
4. Never suggest running kubectl or any CLI command; the data is already fetched.
5. Format answers with bullet points or tables. Keep responses concise.

DATA SECTIONS YOU MAY RECEIVE:
- [REGISTERED_CLUSTERS]: list of all clusters the MCP server knows about — always present.
- [TARGET_CLUSTER]: set when the user asked about a specific remote cluster.
- [PODS] / [PODS_BY_NAMESPACE]: markdown table — name, phase, ready, restarts, node, CONTAINERS column with name(image) for every container (init containers prefixed [init])
- [LOGS_<pod>]: raw container stdout/stderr — display in a fenced code block and analyse
- [LOG_TIME_WINDOW]: time-bounded log window metadata — filter lines to this range
- [NAMESPACE_EVENTS] / [EVENTS]: markdown table — type, reason, object, message
- [DEPLOYMENTS] / [DEPLOYMENT]: desired vs ready replicas, image
- [STATEFULSETS], [DAEMONSETS], [REPLICASETS], [JOBS], [CRONJOBS]: workload state
- [HPA]: current vs desired replicas
- [SERVICES] / [SERVICE]: ClusterIP / NodePort / LoadBalancer config
- [INGRESSES] / [INGRESS]: host + path routing
- [NETWORKPOLICIES]: firewall rules
- [CONFIGMAPS] / [SECRETS]: config key names
- [RESOURCEQUOTAS] / [LIMITRANGES]: quota and default limits
- [NODES] / [NODE_DETAIL]: capacity, conditions, taints
- [RBAC]: roles and bindings
- [TROUBLESHOOT]: MCP server deep-dive report
- [AUTO_TROUBLESHOOT_SUMMARY]: synthesise everything into a root-cause analysis

TROUBLESHOOT BEHAVIOUR:
When [AUTO_TROUBLESHOOT_SUMMARY] is present, synthesise ALL sections into:
1. Root cause (most likely)
2. Supporting evidence (events, logs, pod state)
3. Recommended fix — specific, actionable steps

POD QUESTIONS:
Always reference the [PODS] table. The CONTAINERS column lists every container as name(image)
so you can answer "which pods have a Dapr sidecar", "what image version", etc.
If logs are present under [LOGS_<pod>], display and analyse them even if not explicitly asked."""

YAML_REVIEW_PROMPT = """You are a Kubernetes security and best-practices reviewer.
Analyse the provided YAML manifest and report:
1. Security issues — missing securityContext, running as root, privileged containers, host networking, etc.
2. Resource misconfiguration — missing requests/limits, missing liveness/readiness probes
3. Reliability concerns — single replica deployments, missing PodDisruptionBudget, etc.
4. Image hygiene — using :latest tag, no digest pinning
5. Documentation — missing or outdated comments, unclear resource definitions
6. Proposed changes (if any) that the manifest will make to existing resources and implications
Format findings as a prioritised list (Critical > High > Medium > Low)."""


# ── Intent classification ─────────────────────────────────────────────────────

def classify_intent(question: str) -> list[str]:
    q = question.lower()
    calls = ["cluster", "registered_clusters"]

    mapping = [
        (["metric", "cpu", "memory", "resource", "limit", "request", "usage"], ["node_metrics", "pod_metrics"]),
        (["namespace", "namespaces", " ns "], ["namespaces"]),
        (["node", "nodes", "capacity", "taint", "condition", "schedulable"], ["nodes"]),
        (["pod", "pods", "container", "running", "crash", "restart", "phase", "status", "ready",
          "what is going on", "what's going on", "overview", "health", "happening"], ["pods"]),
        (["log", "logs", "error", "exception", "stdout", "stderr", "output"], ["logs"]),
        (["event", "events", "warning", "oom", "kill", "backoff", "activity",
          "what is going on", "what's going on", "status", "overview", "health"], ["events"]),
        (["troubleshoot", "debug", "broken", "down", "failing", "not working",
          "investigate", "what's wrong", "why is", "fix", "crashing"], ["troubleshoot"]),
        (["deployment", "deployments", "deploy", "rollout", "overview", "health"], ["deployments"]),
        (["replicaset", "replicasets"], ["replicasets"]),
        (["service", "services", "svc", "clusterip", "nodeport", "loadbalancer"], ["services"]),
        (["ingress", "ingresses", "route", "host", "tls"], ["ingresses"]),
        (["networkpolicy", "network policy", "firewall", "egress"], ["networkpolicies"]),
        (["configmap", "configmaps", "configuration"], ["configmaps"]),
        (["secret", "secrets"], ["secrets"]),
        (["role", "rolebinding", "rbac", "clusterrole", "permission", "permissions",
          "access", "who can", "who has access", "can i", "authorized", "authorization",
          "privilege", "privileges"], ["rbac"]),
        (["serviceaccount", "serviceaccounts", "service account", "service accounts",
          "workload identity", "irsa", "pod identity"], ["serviceaccounts"]),
        (["statefulset", "statefulsets"], ["statefulsets"]),
        (["daemonset", "daemonsets"], ["daemonsets"]),
        (["job", "jobs", "batch"], ["jobs"]),
        (["cronjob", "cronjobs", "scheduled"], ["cronjobs"]),
        (["hpa", "autoscal", "horizontal", "scale", "scaling"], ["hpa"]),
        (["resourcequota", "quota"], ["resourcequotas"]),
        (["limitrange", "default limit"], ["limitranges"]),
        (["storageclass", "provisioner"], ["storageclasses"]),
        (["pvc", "pv", "persistentvolume", "volume"], ["volumes"]),
    ]
    for keywords, intents in mapping:
        if any(w in q for w in keywords):
            calls.extend(intents)

    # fetch+show without explicit "log" keyword
    if "logs" not in calls and any(w in q for w in ["fetch", "show", "tail", "stream"]):
        if any(w in q for w in ["pod", "container", "from"]):
            calls.append("logs")

    return list(dict.fromkeys(calls))


# ── Entity extraction ─────────────────────────────────────────────────────────

_STOPWORDS = {
    "the", "a", "an", "my", "all", "any", "some", "this", "that", "pods", "pod",
    "logs", "log", "cluster", "namespace", "namespaces", "has", "have", "had",
    "been", "be", "is", "are", "was", "were", "in", "on", "at", "to", "for",
    "of", "and", "or", "not", "no", "its", "it", "if", "any", "from", "with",
    "check", "get", "list", "show", "what", "which", "how", "why", "when",
    "where", "who", "errors", "error", "issues", "issue", "running", "crashed",
    "job", "jobs", "cronjob", "service", "services", "deployment", "deployments",
    "fetch", "display", "retrieve", "print",
}


async def extract_entities_llm(
    question: str,
    known_namespaces: list[str],
    chat_history: list[dict] | None = None,
    known_clusters: list[str] | None = None,
) -> tuple:
    ns_hint      = ", ".join(known_namespaces[:40]) if known_namespaces else "none"
    cluster_hint = ", ".join(known_clusters) if known_clusters else "none"
    history_snippet = ""
    if chat_history:
        recent = chat_history[-6:]
        parts = [
            f"{'User' if m.get('role') == 'user' else 'Assistant'}: {str(m.get('content',''))[:400]}"
            for m in recent if m.get("role") in ("user", "assistant")
        ]
        if parts:
            history_snippet = "\n\nRecent conversation:\n" + "\n".join(parts)

    prompt = (
        f"Extract Kubernetes entities. Known namespaces: [{ns_hint}]. "
        f"Known clusters: [{cluster_hint}].{history_snippet}\n\n"
        f"Question: {question}\n\n"
        f'Reply with a JSON object only: {{"namespace":<str|null>,"pod":<str|null>,"service":<str|null>,"container":<str|null>,"cluster":<str|null>}}\n'
        f"Rules: namespace must match a known namespace exactly (case-insensitive) or null; "
        f"cluster must match a known cluster or null; pod only if full name with hash; "
        f"service is the app/deployment name; use null for anything not mentioned."
    )
    try:
        r = await litellm.acompletion(
            model=FAST_MODEL,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            max_tokens=150,
            temperature=0,
            timeout=10,
        )
        d = json.loads(r.choices[0].message.content)
        ns  = d.get("namespace") or None
        pod = d.get("pod")       or None
        svc = d.get("service")   or None
        con = d.get("container") or None
        clu = d.get("cluster")   or None
        if ns and known_namespaces:
            ns_map = {n.lower(): n for n in known_namespaces}
            ns = ns_map.get(ns.lower())
        if clu and known_clusters:
            clu_map = {c.lower(): c for c in known_clusters}
            clu = clu_map.get(clu.lower())
        return ns, pod, svc, con, clu
    except Exception:
        return None, None, None, None, None


def extract_entities_regex(question: str, known_namespaces: list[str]) -> tuple:
    ns, pod, service, container = None, None, None, None
    for pattern in [
        r"\bin\s+(?:the\s+)?([\w-]+)\s+namespace\b",
        r"\bnamespace[s]?\s*[=:'\"]?\s*([\w-]+)",
    ]:
        m = re.search(pattern, question, re.IGNORECASE)
        if m and m.group(1).lower() not in _STOPWORDS:
            ns = m.group(1)
            break
    if ns is None and known_namespaces:
        ns_lower = {n.lower(): n for n in known_namespaces}
        for word in re.findall(r"[\w-]+", question.lower()):
            if word in ns_lower and word not in _STOPWORDS:
                ns = ns_lower[word]
                break
    for pattern in [r"\bpod[s]?\s+([\w][\w-]*)", r"\b([\w][\w-]*)\s+pod\b"]:
        m = re.search(pattern, question, re.IGNORECASE)
        if m and m.group(1).lower() not in _STOPWORDS:
            pod = m.group(1)
            break
    for pattern in [r"\bcontainer[s]?\s+([\w][\w-]*)", r"\b-c\s+([\w][\w-]*)"]:
        m = re.search(pattern, question, re.IGNORECASE)
        if m and m.group(1).lower() not in _STOPWORDS:
            container = m.group(1)
            break
    for pattern in [
        r"\b(?:deployment|service|app|svc)\s+([\w-]+)",
        r"\b([\w-]+)\s+(?:app|service|deployment)\b",
        r"\btroubleshoot\s+([\w-]+)",
        r"\bfrom\s+(?:the\s+)?([\w-]+)\s+(?:app|service|deployment)\b",
    ]:
        m = re.search(pattern, question, re.IGNORECASE)
        if m and m.group(1).lower() not in _STOPWORDS:
            service = m.group(1)
            break
    return ns, pod, service, container


async def extract_time_range(
    question: str, chat_history: list[dict] | None = None
) -> tuple[str | None, str | None]:
    now_utc = datetime.now(timezone.utc)
    prompt = (
        f"Current UTC: {now_utc.strftime('%Y-%m-%dT%H:%M:%SZ')} "
        f"({now_utc.strftime('%A, %d %B %Y')}).\n"
        f"Extract a log time range from: {question}\n"
        f'Reply with a JSON object only: {{"since":<ISO-8601 or null>,"until":<ISO-8601 or null>}}\n'
        f"Return null for both if no time range is mentioned."
    )
    try:
        r = await litellm.acompletion(
            model=FAST_MODEL,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            max_tokens=80,
            temperature=0,
            timeout=10,
        )
        d = json.loads(r.choices[0].message.content)
        since = d.get("since") or None
        until = d.get("until") or None
        for v in [since, until]:
            if v:
                try:
                    dateutil_parser.isoparse(v)
                except Exception:
                    v = None
        return since, until
    except Exception:
        return None, None


# ── Log query string ──────────────────────────────────────────────────────────

def _log_qs(tail: int, since: str | None = None, cluster: str | None = None) -> str:
    base = f"?sinceTime={quote(since)}" if since else f"?tail={tail}"
    if cluster:
        sep = "&" if "?" in base else "?"
        base += f"{sep}cluster={cluster}"
    return base


# ── Main RAG pipeline ─────────────────────────────────────────────────────────

async def fetch_mcp_context(
    question: str,
    chat_history: list[dict] | None = None,
    user: dict | None = None,
) -> tuple[dict, list[str]]:
    ctx: dict = {}
    endpoints_used: list[str] = []

    effective_user = user or {"role": "viewer", "allowed_namespaces": []}
    user_role = effective_user.get("role", "viewer")

    # Step 1: registered clusters + namespaces + cluster info
    clusters_raw = await mcp_get("/clusters")
    registered_clusters: list[str] = []
    if isinstance(clusters_raw, list):
        registered_clusters = [
            c.get("name", "") for c in clusters_raw
            if isinstance(c, dict) and not c.get("isLocal", False) and c.get("name")
        ]
    ctx["registered_clusters"] = clusters_raw
    endpoints_used.append("/clusters")

    all_ns_raw = await get_all_namespaces()
    all_namespaces = _auth.filter_namespaces(all_ns_raw, effective_user)
    ctx["namespaces"] = all_namespaces
    endpoints_used.append("/namespaces")

    ctx["cluster_info"] = await mcp_get("/cluster/info")
    endpoints_used.append("/cluster/info")

    # Step 2: RBAC intent filtering
    raw_intents = classify_intent(question)
    intents = _auth.filter_intents(raw_intents, user_role)
    blocked = [i for i in raw_intents if i not in intents]
    if blocked:
        ctx["permission_denied"] = "\n".join(_auth.permission_denied_note(i, user_role) for i in blocked)

    # Step 3: entity extraction
    ns, pod, service, container, target_cluster = await extract_entities_llm(
        question, all_namespaces, chat_history, known_clusters=registered_clusters
    )
    if ns is None and pod is None and service is None:
        ns, pod, service, container = extract_entities_regex(question, all_namespaces)

    # Multi-cluster routing — must happen BEFORE namespace validation so we can
    # check ns against the remote cluster's namespaces, not the local ones.
    cqs = f"?cluster={target_cluster}" if target_cluster else ""
    if target_cluster:
        ctx["target_cluster"] = target_cluster
        ns_for_cluster = await mcp_get(f"/namespaces{cqs}")
        if isinstance(ns_for_cluster, list):
            all_namespaces = _auth.filter_namespaces(ns_for_cluster, effective_user)
            ctx["namespaces"] = all_namespaces
            endpoints_used.append(f"/namespaces{cqs}")
        ctx["cluster_info"] = await mcp_get(f"/cluster/info{cqs}")
        endpoints_used.append(f"/cluster/info{cqs}")
        # Re-run entity extraction with the remote namespace list so ns matches correctly
        if ns is None or ns not in all_namespaces:
            ns2, pod2, svc2, con2, _ = await extract_entities_llm(
                question, all_namespaces, chat_history, known_clusters=registered_clusters
            )
            if ns2: ns = ns2
            if pod2: pod = pod2
            if svc2: service = svc2
            if con2: container = con2
        if ns is None:
            ns_retry, _, _, _ = extract_entities_regex(question, all_namespaces)
            if ns_retry: ns = ns_retry
    else:
        # Local cluster — validate ns against local namespaces
        if ns and ns not in all_namespaces:
            ctx["namespace_denied"] = f"[PERMISSION_DENIED] You do not have access to namespace '{ns}'."
            ns = None

    # Time range for log queries
    log_since: str | None = None
    log_until: str | None = None
    if "logs" in intents:
        log_since, log_until = await extract_time_range(question, chat_history)

    needs_pods = any(i in intents for i in ["pods", "logs", "events", "troubleshoot"])

    # ── Fetch helpers (inline, keep it readable) ──────────────────────────────

    async def _add(key: str, path: str, text: bool = False):
        ctx[key] = await mcp_get(path, text=text)
        endpoints_used.append(path.split("?")[0])

    async def _add_safe(key: str, path: str, text: bool = False):
        """Like _add but swallows errors — for optional/unimplemented MCP endpoints."""
        try:
            result = await mcp_get(path, text=text)
            if result not in (None, [], {}, ""):
                ctx[key] = result
                endpoints_used.append(path.split("?")[0])
        except Exception:
            pass  # endpoint not implemented or returned 500 — silently skip

    # Node metrics
    if "node_metrics" in intents:
        await _add("node_metrics", f"/metrics/nodes{cqs}")
    if "pod_metrics" in intents:
        await _add("pod_metrics", f"/metrics/pods{cqs}")
    if "nodes" in intents:
        await _add("nodes", f"/nodes{cqs}")
        if service:
            await _add("node_detail", f"/nodes/{service}{cqs}")

    # Pod listing
    if needs_pods:
        if ns:
            await _add("pods", f"/namespaces/{ns}/pods{cqs}")
        else:
            # Fetch all namespaces concurrently (not sequentially — avoids N×timeout hang)
            results = await asyncio.gather(
                *[mcp_get(f"/namespaces/{n}/pods{cqs}") for n in all_namespaces],
                return_exceptions=True,
            )
            all_pods = {
                n: r for n, r in zip(all_namespaces, results)
                if isinstance(r, list) and r
            }
            if all_pods:
                ctx["pods_by_namespace"] = all_pods
                endpoints_used.append("/namespaces/*/pods (all)")

    # Pod-name fallback matching
    if needs_pods and pod is None:
        _skip = _STOPWORDS | {"log", "logs", "fetch", "show", "please", "can", "you", "me"}
        q_tokens = set(re.findall(r"[\w-]+", question.lower()))
        all_pairs: list[tuple[str, dict]] = []
        for p in (ctx.get("pods") or []):
            if isinstance(p, dict):
                all_pairs.append((ns or "", p))
        for ns_name, pl in (ctx.get("pods_by_namespace") or {}).items():
            for p in (pl if isinstance(pl, list) else []):
                if isinstance(p, dict):
                    all_pairs.append((ns_name, p))
        if all_pairs:
            for ns_name, p in all_pairs:
                if p.get("name", "").lower() in q_tokens:
                    pod = p["name"]
                    ns = ns or ns_name
                    break
            if pod is None and service is None:
                for token in q_tokens - _skip:
                    if len(token) > 3:
                        for ns_name, p in all_pairs:
                            pname = p.get("name", "")
                            if pname.lower().startswith(token + "-") or pname.lower() == token:
                                service = token
                                ns = ns or ns_name
                                break
                    if service:
                        break

    # Logs
    if "logs" in intents:
        if log_since:
            ctx["log_time_window"] = {
                "since": log_since, "until": log_until or "now",
                "note": f"Only report log lines between {log_since} and {log_until or 'now'}.",
            }
        ext = f"&cluster={target_cluster}" if target_cluster else ""

        async def _log(ns_: str, pod_: str, container_: str | None):
            qs = _log_qs(150, log_since)
            path = (
                f"/namespaces/{ns_}/pods/{pod_}/containers/{container_}/logs{qs}{ext}"
                if container_ else
                f"/namespaces/{ns_}/pods/{pod_}/logs{qs}{ext}"
            )
            await _add(f"logs_{pod_}", path, text=True)

        if ns and pod:
            await _log(ns, pod, container)
        elif ns and service:
            pods_data = ctx.get("pods")
            if not isinstance(pods_data, list):
                pods_data = await mcp_get(f"/namespaces/{ns}/pods{cqs}")
                ctx["pods"] = pods_data
                endpoints_used.append(f"/namespaces/{ns}/pods")
            for p in [p for p in (pods_data if isinstance(pods_data, list) else [])
                      if isinstance(p, dict) and service.lower() in p.get("name","").lower()][:2]:
                await _log(ns, p["name"], container)
        elif service:
            for ns_name, pl in (ctx.get("pods_by_namespace") or {}).items():
                matches = [p for p in (pl if isinstance(pl, list) else [])
                           if isinstance(p, dict) and service.lower() in p.get("name","").lower()]
                for p in matches[:2]:
                    await _log(ns_name, p["name"], container)
                if matches:
                    break
        elif ns:
            pods_data = ctx.get("pods")
            if not isinstance(pods_data, list):
                pods_data = await mcp_get(f"/namespaces/{ns}/pods{cqs}")
                ctx["pods"] = pods_data
            if isinstance(pods_data, list):
                for p in sorted(pods_data, key=lambda p: 0 if p.get("phase") not in ("Running","Succeeded") else 1)[:2]:
                    await _log(ns, p["name"], container)

    # Events
    if "events" in intents:
        if ns and pod:
            await _add("events", f"/namespaces/{ns}/pods/{pod}/events{cqs}")
        elif ns:
            await _add("namespace_events", f"/namespaces/{ns}/events{cqs}")

    # Troubleshoot
    if "troubleshoot" in intents:
        svc_name = service or pod
        if ns and svc_name:
            # Run all troubleshoot sub-fetches concurrently
            ts_paths = {
                "troubleshoot":     f"/troubleshoot/service/{ns}/{svc_name}{cqs}",
                "namespace_events": f"/namespaces/{ns}/events{cqs}",
                "deployment":       f"/namespaces/{ns}/deployments/{svc_name}{cqs}",
                "hpa":              f"/namespaces/{ns}/hpa{cqs}",
                "resourcequotas":   f"/namespaces/{ns}/resourcequotas{cqs}",
            }
            ts_results = await asyncio.gather(
                *[mcp_get(p) for p in ts_paths.values()], return_exceptions=True
            )
            for k, r in zip(ts_paths.keys(), ts_results):
                ctx[k] = r if not isinstance(r, Exception) else {"error": str(r)}
                endpoints_used.append(ts_paths[k].split("?")[0])
            if "pods" not in ctx:
                await _add("pods", f"/namespaces/{ns}/pods{cqs}")
            ctx["auto_troubleshoot_summary"] = (
                f"Auto-troubleshoot for '{svc_name}' in namespace '{ns}'. "
                "Synthesise all sections into a root-cause analysis."
            )
        elif ns:
            ts_paths2 = {
                "namespace_events": f"/namespaces/{ns}/events{cqs}",
                "resourcequotas":   f"/namespaces/{ns}/resourcequotas{cqs}",
            }
            ts_results2 = await asyncio.gather(
                *[mcp_get(p) for p in ts_paths2.values()], return_exceptions=True
            )
            for k, r in zip(ts_paths2.keys(), ts_results2):
                ctx[k] = r if not isinstance(r, Exception) else {"error": str(r)}
                endpoints_used.append(ts_paths2[k].split("?")[0])
            if "pods" not in ctx:
                await _add("pods", f"/namespaces/{ns}/pods{cqs}")

    # Workloads
    async def _ns_or_all(key: str, path_tpl: str, safe: bool = False):
        _fetch = _add_safe if safe else _add
        if ns:
            await _fetch(key if service else f"{key}s", path_tpl.format(ns=ns, name=service or ""))
        else:
            path_no_name = path_tpl.replace("/{name}", "").replace("{name}", "")
            paths = [path_no_name.format(ns=n) for n in all_namespaces]
            results = await asyncio.gather(*[mcp_get(p) for p in paths], return_exceptions=True)
            out = {n: r for n, r in zip(all_namespaces, results) if isinstance(r, list) and r}
            if out:
                ctx[f"{key}s_by_namespace"] = out
                endpoints_used.append(f"/namespaces/*/{key}s (all)")

    if "deployments"   in intents: await _ns_or_all("deployment",   f"/namespaces/{{ns}}/deployments/{{name}}" if service else f"/namespaces/{{ns}}/deployments")
    # Use safe=True for services/ingresses when fetching a named resource — service account
    # names (e.g. "kuberniq") can be mistakenly extracted as service names causing 500s
    if "services"      in intents: await _ns_or_all("service",      f"/namespaces/{{ns}}/services/{{name}}"    if service else f"/namespaces/{{ns}}/services",   safe=bool(service))
    if "ingresses"     in intents: await _ns_or_all("ingress",      f"/namespaces/{{ns}}/ingresses/{{name}}"   if service else f"/namespaces/{{ns}}/ingresses",  safe=bool(service))
    if "replicasets"   in intents and ns: await _add("replicasets",  f"/namespaces/{ns}/replicasets{cqs}")
    if "statefulsets"  in intents and ns: await _add("statefulsets", f"/namespaces/{ns}/statefulsets{cqs}")
    if "daemonsets"    in intents and ns: await _add("daemonsets",   f"/namespaces/{ns}/daemonsets{cqs}")
    if "networkpolicies" in intents and ns: await _add("networkpolicies", f"/namespaces/{ns}/networkpolicies{cqs}")
    if "configmaps"    in intents and ns: await _add("configmaps",  f"/namespaces/{ns}/configmaps{cqs}")
    if "secrets"       in intents and ns: await _add("secrets",     f"/namespaces/{ns}/secrets{cqs}")
    # serviceaccounts handled below with all-namespace support
    if "hpa"           in intents:
        if ns:
            await _add_safe("hpa", f"/namespaces/{ns}/hpa{cqs}")
        else:
            results = await asyncio.gather(
                *[mcp_get(f"/namespaces/{n}/hpa{cqs}") for n in all_namespaces],
                return_exceptions=True,
            )
            out = {n: r for n, r in zip(all_namespaces, results) if isinstance(r, list) and r}
            if out:
                ctx["hpa_by_namespace"] = out
                endpoints_used.append("/namespaces/*/hpa (all)")
    if "resourcequotas" in intents and ns: await _add_safe("resourcequotas", f"/namespaces/{ns}/resourcequotas{cqs}")
    if "limitranges"   in intents and ns: await _add_safe("limitranges",    f"/namespaces/{ns}/limitranges{cqs}")
    if "storageclasses" in intents:        await _add("storageclasses", f"/storageclasses{cqs}")
    if "volumes"       in intents:
        await _add("persistentvolumes", f"/persistentvolumes{cqs}")
        if ns:
            await _add("pvcs", f"/namespaces/{ns}/persistentvolumeclaims{cqs}")
    if "jobs" in intents:
        path = f"/namespaces/{ns}/jobs/{service}{cqs}" if (ns and service) else (f"/namespaces/{ns}/jobs{cqs}" if ns else None)
        if path:
            await _add("job" if service else "jobs", path)
    if "cronjobs" in intents:
        path = f"/namespaces/{ns}/cronjobs/{service}{cqs}" if (ns and service) else (f"/namespaces/{ns}/cronjobs{cqs}" if ns else None)
        if path:
            await _add("cronjob" if service else "cronjobs", path)
    if "rbac" in intents:
        await _add("clusterroles", f"/clusterroles{cqs}")
        await _add("clusterrolebindings", f"/clusterrolebindings{cqs}")
        if ns:
            await _add("roles", f"/namespaces/{ns}/roles{cqs}")
            await _add("rolebindings", f"/namespaces/{ns}/rolebindings{cqs}")
    if "serviceaccounts" in intents:
        if ns:
            await _add("serviceaccounts", f"/namespaces/{ns}/serviceaccounts{cqs}")
        else:
            # Fetch across all namespaces
            results = await asyncio.gather(
                *[mcp_get(f"/namespaces/{n}/serviceaccounts{cqs}") for n in all_namespaces],
                return_exceptions=True,
            )
            out = {n: r for n, r in zip(all_namespaces, results) if isinstance(r, list) and r}
            if out:
                ctx["serviceaccounts_by_namespace"] = out
                endpoints_used.append("/namespaces/*/serviceaccounts (all)")

    return ctx, endpoints_used


# ── Streaming chat ────────────────────────────────────────────────────────────

async def stream_chat_response(
    message: str,
    history: list[dict],
    user: dict,
    model: str | None = None,
    yaml_content: str | None = None,
):
    """
    Async generator that yields JSON-lines:
      {"type":"meta","endpoints":[...]}
      {"type":"token","content":"..."}
      {"type":"done"}
    """
    chosen_model = model or LLM_MODEL

    # Top-level guard — any unhandled exception yields an error event so the
    # frontend always gets a response instead of a silent hanging stream.
    try:
        async for event in _stream_chat_inner(message, history, user, chosen_model, yaml_content):
            yield event
    except Exception as exc:
        yield json.dumps({"type": "error", "message": f"[{type(exc).__name__}] {exc}"}) + "\n"


async def _stream_chat_inner(
    message: str,
    history: list[dict],
    user: dict | None,
    chosen_model: str,
    yaml_content: str | None,
):

    # ── Dev-mode bypass ───────────────────────────────────────────────────────
    # When DEV_MODE=true the MCP server is not running. Skip all Kubernetes
    # data fetching and go straight to the LLM so the chat→OpenAI path can
    # be tested without a live cluster.
    if DEV_MODE:
        dev_system = (
            "You are Kuberniq, a Kubernetes AI assistant. "
            "The MCP server (live cluster data) is currently offline — dev mode is active. "
            "Answer general Kubernetes questions from your training knowledge. "
            "Remind the user that live cluster data is unavailable until MCP is connected."
        )
        messages_dev = [
            {"role": "system", "content": dev_system},
            *[{"role": m["role"], "content": m["content"]} for m in history[-10:]],
            {"role": "user", "content": message},
        ]
        yield json.dumps({"type": "meta", "endpoints": ["dev-mode (no MCP)"]}) + "\n"
        try:
            resp = await litellm.acompletion(model=chosen_model, messages=messages_dev, stream=True)
            async for chunk in resp:
                token = chunk.choices[0].delta.content or ""
                if token:
                    yield json.dumps({"type": "token", "content": token}) + "\n"
        except Exception as exc:
            yield json.dumps({"type": "error", "message": f"LLM error: {exc}"}) + "\n"
        yield json.dumps({"type": "done"}) + "\n"
        return

    # YAML manifest review — bypass RAG
    if yaml_content:
        messages = [
            {"role": "system", "content": YAML_REVIEW_PROMPT},
            {"role": "user", "content": f"Review this manifest:\n\n```yaml\n{yaml_content}\n```"},
        ]
        yield json.dumps({"type": "meta", "endpoints": ["yaml-review"]}) + "\n"
        try:
            resp = await litellm.acompletion(model=chosen_model, messages=messages, stream=True)
            async for chunk in resp:
                token = chunk.choices[0].delta.content or ""
                if token:
                    yield json.dumps({"type": "token", "content": token}) + "\n"
        except Exception as exc:
            yield json.dumps({"type": "error", "message": f"LLM error: {exc}"}) + "\n"
        yield json.dumps({"type": "done"}) + "\n"
        return

    # Normal RAG flow
    try:
        ctx, endpoints_used = await asyncio.wait_for(
            fetch_mcp_context(message, history, user),
            timeout=30,  # never block the LLM for more than 30s regardless of MCP speed
        )
    except asyncio.TimeoutError:
        ctx, endpoints_used = {}, ["(MCP context timed out — answering from LLM knowledge)"]
    rag_context = summarise_context(ctx)

    yield json.dumps({"type": "meta", "endpoints": endpoints_used, "rawContext": rag_context}) + "\n"

    messages = [
        {"role": "system", "content": f"{SYSTEM_PROMPT}\n\n{rag_context}"},
        *[{"role": m["role"], "content": m["content"]} for m in history[-10:]],
        {"role": "user", "content": message},
    ]

    try:
        resp = await litellm.acompletion(model=chosen_model, messages=messages, stream=True)
        async for chunk in resp:
            token = chunk.choices[0].delta.content or ""
            if token:
                yield json.dumps({"type": "token", "content": token}) + "\n"
    except Exception as exc:
        yield json.dumps({"type": "error", "message": f"LLM error: {exc}"}) + "\n"
    yield json.dumps({"type": "done"}) + "\n"

# --- MCP RAG Chatbot ---
import streamlit as st
import requests
import os
import json
import re
import yaml
from datetime import datetime, timezone
from openai import OpenAI
from dotenv import load_dotenv
from dateutil import parser as dateutil_parser

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────
st.set_page_config(page_title="MCP RAG Chatbot", page_icon="🤖", layout="wide")
openai_api_key = os.getenv("OPENAI_API_KEY", "")
client = OpenAI(api_key=openai_api_key)
mcp_url      = os.getenv("MCP_SERVER_URL",  "http://mcp-server.local")
mcp_username = os.getenv("MCP_USERNAME",    "admin")
mcp_password = os.getenv("MCP_PASSWORD",    "")
openai_model = os.getenv("OPENAI_MODEL", "gpt-4o")

# Set DEBUG_MCP=true in a local .env only — never in Helm / production values.
DEBUG_MCP = os.getenv("DEBUG_MCP", "false").lower() == "true"

SYSTEM_PROMPT = """You are an expert Kubernetes assistant powered by live cluster data.

STRICT RULES:
1. Answer ONLY from the data in the [SECTION] blocks below. Never invent pod names, IPs, images, or counts.
2. If a section shows "⚠ MCP endpoint failed", tell the user that data was unavailable.
3. If data for a query is missing entirely, say so — do not guess.
4. Never suggest running kubectl or any CLI command; the data is already fetched.
5. Format answers with bullet points or tables. Keep responses concise.

DATA SECTIONS YOU MAY RECEIVE:
- [PODS] / [PODS_BY_NAMESPACE]: markdown table — name, phase, ready, restarts, node
- [LOGS_<pod>]: raw container stdout/stderr — always display in a fenced code block and analyse for errors/exceptions/stack traces
- [LOG_TIME_WINDOW]: present when the user requested a time-bounded log query. Contains "since", "until", and a "note". You MUST filter the log lines in [LOGS_*] to only those with timestamps inside the [since, until] window. Lines without timestamps that fall outside the window should be excluded. Explicitly state the time range you analysed.
- [NAMESPACE_EVENTS] / [EVENTS]: markdown table — type, reason, object, message; Warning rows mean something is wrong
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
Always reference the [PODS] table explicitly. List pod names, phase, restarts.
If logs are present under [LOGS_<pod>], display and analyse them even if not explicitly asked."""

# ── Session state ─────────────────────────────────────────────────────────────
if "chat_history" not in st.session_state:
    st.session_state.chat_history = []   # full conversation for LLM memory
if "display_history" not in st.session_state:
    st.session_state.display_history = []  # (role, text, raw_data) for UI

# ── MCP auth + helpers ────────────────────────────────────────────────────────
_mcp_token: dict = {"access": None, "refresh": None}

def _mcp_login() -> bool:
    if not mcp_password:
        return False
    try:
        r = requests.post(f"{mcp_url}/auth/login",
                          json={"username": mcp_username, "password": mcp_password}, timeout=10)
        if r.status_code == 200:
            d = r.json()
            _mcp_token["access"]  = d.get("accessToken")
            _mcp_token["refresh"] = d.get("refreshToken")
            return bool(_mcp_token["access"])
        return False
    except Exception:
        return False

def _mcp_refresh() -> bool:
    if not _mcp_token["refresh"]:
        return False
    try:
        r = requests.post(f"{mcp_url}/auth/refresh",
                          json={"refreshToken": _mcp_token["refresh"]}, timeout=10)
        if r.status_code == 200:
            d = r.json()
            _mcp_token["access"]  = d.get("accessToken")
            _mcp_token["refresh"] = d.get("refreshToken")
            return bool(_mcp_token["access"])
        return False
    except Exception:
        return False

def _auth_headers() -> dict:
    return {"Authorization": f"Bearer {_mcp_token['access']}"} if _mcp_token["access"] else {}

def mcp_get(path: str, text=False):
    """Fetch from MCP server with Bearer auth; auto-login/refresh on 401."""
    if _mcp_token["access"] is None:
        _mcp_login()
    def _do():
        return requests.get(f"{mcp_url}{path}", headers=_auth_headers(), timeout=15)
    try:
        r = _do()
        if r.status_code == 401:
            if _mcp_refresh() or _mcp_login():
                r = _do()
        r.raise_for_status()
        return r.text if text else r.json()
    except Exception as e:
        return {"error": str(e)}

def get_all_namespaces() -> list[str]:
    """Fetch all namespaces from MCP, return as a list of strings."""
    result = mcp_get("/namespaces")
    if isinstance(result, list):
        return result
    return []

# ── LLM-powered entity extraction ────────────────────────────────────────────
def extract_entities_llm(
    question: str,
    known_namespaces: list[str],
    chat_history: list[dict] = None,
) -> tuple:
    """
    Use a fast LLM call to extract Kubernetes entities from natural language.
    Includes recent conversation history so follow-up questions like
    "get logs for that pod" or "same namespace as before" resolve correctly.
    Falls back to (None, None, None, None) on any error.
    """
    ns_hint = ", ".join(known_namespaces[:40]) if known_namespaces else "none available"

    # Build a short conversation snippet so the model has prior context
    history_snippet = ""
    if chat_history:
        recent = chat_history[-6:]  # last 3 turns (user + assistant each)
        parts = []
        for msg in recent:
            role = msg.get("role", "")
            # Truncate very long assistant messages (RAG context dumps)
            content = str(msg.get("content", ""))[:400]
            if role == "user":
                parts.append(f"User: {content}")
            elif role == "assistant":
                parts.append(f"Assistant: {content}")
        if parts:
            history_snippet = (
                "\n\nRecent conversation (use this to resolve references like "
                "'that pod', 'it', 'the same namespace', 'the one above'):\n"
                + "\n".join(parts)
            )

    prompt = (
        f"Extract Kubernetes entities from the latest user question. "
        f"Known namespaces in the cluster: [{ns_hint}].{history_snippet}\n\n"
        f"Latest question: {question}\n\n"
        f"Reply with JSON only — no explanation:\n"
        f'{{ "namespace": <string|null>, "pod": <string|null>, '
        f'"service": <string|null>, "container": <string|null> }}\n\n'
        f"Rules:\n"
        f"- namespace must exactly match one of the known namespaces (case-insensitive), or null\n"
        f"- service is the app/deployment name (e.g. 'argocd-server', 'api', 'devops-helper')\n"
        f"- pod is only set if a full pod name with hash suffix is mentioned or referenced\n"
        f"- container is only set if explicitly named\n"
        f"- use null for anything not mentioned or inferable from conversation history\n"
        f"- if the user says 'that pod', 'it', 'the same one' — infer from the conversation"
    )
    try:
        r = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            max_tokens=120,
            temperature=0,
        )
        d = json.loads(r.choices[0].message.content)
        ns  = d.get("namespace")  or None
        pod = d.get("pod")        or None
        svc = d.get("service")    or None
        con = d.get("container")  or None
        # Validate namespace is actually in the cluster (case-insensitive)
        if ns and known_namespaces:
            ns_lower_map = {n.lower(): n for n in known_namespaces}
            ns = ns_lower_map.get(ns.lower())  # normalise to actual casing
        return ns, pod, svc, con
    except Exception:
        return None, None, None, None

def extract_entities(question: str, known_namespaces: list[str] = None):
    """
    Extract namespace, pod, and service name from the question.
    Also matches against known namespace names fetched from the cluster.
    """
    ns = None
    pod = None
    service = None

    # English stopwords that should never be treated as k8s names
    stopwords = {
        "the", "a", "an", "my", "all", "any", "some", "this", "that",
        "pods", "pod", "logs", "log", "cluster", "namespace", "namespaces",
        "has", "have", "had", "been", "be", "is", "are", "was", "were",
        "in", "on", "at", "to", "for", "of", "and", "or", "not", "no",
        "its", "it", "if", "any", "from", "with", "check", "get", "list",
        "show", "what", "which", "how", "why", "when", "where", "who",
        "errors", "error", "issues", "issue", "running", "crashed", "down",
        "there", "their", "they", "then", "than", "last", "latest",
        "job", "jobs", "cronjob", "cronjobs", "node", "nodes", "hpa",
        "service", "services", "deployment", "deployments", "ingress",
        "fetch", "get", "show", "give", "display", "retrieve", "print",
    }

    # 1. Explicit "X namespace" or "namespace X" patterns (highest confidence)
    for pattern in [
        r"\bin\s+(?:the\s+)?([\w-]+)\s+namespace\b",   # "in the map-server-dev namespace"
        r"\bnamespace[s]?\s*[=:'\"]?\s*([\w-]+)",        # "namespace map-server-dev"
        r"\bns[=:\s]\s*([\w-]+)",                         # "ns=map-server-dev"
    ]:
        m = re.search(pattern, question, re.IGNORECASE)
        if m and m.group(1).lower() not in stopwords:
            ns = m.group(1)
            break

    # 2. Match tokens in the question against the live list of known namespace names.
    #    Only accept if the token is at least 4 chars (avoids common English words)
    #    OR contains a hyphen (clear signal it's a k8s name like "map-server-dev").
    if ns is None and known_namespaces:
        ns_lower = [n.lower() for n in known_namespaces]
        q_words = re.findall(r"[\w-]+", question.lower())
        for word in q_words:
            # Accept any token that is an actual known namespace name, regardless of length.
            # The stopwords list guards against false positives.
            if word in ns_lower and word not in stopwords:
                ns = word
                break

    # 3. Pod name: support both "pod <name>" and "<name> pod" orderings
    m = re.search(r"\bpod[s]?\s+([\w][\w-]*)", question, re.IGNORECASE)
    if m and m.group(1).lower() not in stopwords:
        pod = m.group(1)
    if pod is None:
        m = re.search(r"\b([\w][\w-]*)\s+pod\b", question, re.IGNORECASE)
        if m and m.group(1).lower() not in stopwords:
            pod = m.group(1)

    # 4. Container name: "container <name>", "<name> container", "-c <name>"
    container = None
    for pattern in [
        r"\bcontainer[s]?\s+([\w][\w-]*)",
        r"\b([\w][\w-]*)\s+container\b",
        r"\b-c\s+([\w][\w-]*)",
    ]:
        m = re.search(pattern, question, re.IGNORECASE)
        if m and m.group(1).lower() not in stopwords:
            container = m.group(1)
            break

    # 5. Deployment / service / app name — also catches job/cronjob/node targets
    for pattern in [
        r"\b(?:deployment|service|app|svc)\s+([\w-]+)",    # "app devops-helper"
        r"\b([\w-]+)\s+(?:app|service|svc|deployment)\b",  # "devops-helper app"
        r"\btroubleshoot\s+([\w-]+)",
        r"\bfor\s+([\w-]+)\s+(?:pod|service|deployment|app|job|cronjob)",
        r"\bfrom\s+(?:the\s+)?([\w-]+)\s+(?:app|service|svc|deployment)\b",  # "from the devops-helper app"
        r"\b(?:job|cronjob|node)\s+([\w-]+)",
        r"\b(?:debug|investigate|fix|what(?:'s| is) wrong with)\s+([\w-]+)",
    ]:
        m = re.search(pattern, question, re.IGNORECASE)
        if m and m.group(1).lower() not in stopwords:
            service = m.group(1)
            break

    return ns, pod, service, container

# ── Time-range extraction for log queries ─────────────────────────────────────
def extract_time_range_llm(
    question: str,
    chat_history: list[dict] = None,
) -> tuple[str | None, str | None]:
    """
    Use gpt-4o-mini to extract a time range from the question.
    Returns (since_iso, until_iso) — both are ISO-8601 UTC strings or None.

    Understands natural language like:
      "between 10am Monday and 11pm today"
      "from yesterday 3pm to now"
      "last 2 hours"
      "since 2026-05-25"
      "on Tuesday between 09:00 and 17:00"

    The LLM resolves relative words ("today", "yesterday", "last Monday") against
    the current UTC datetime which we inject into the prompt.
    """
    now_utc = datetime.now(timezone.utc)
    now_str = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")

    history_snippet = ""
    if chat_history:
        recent = chat_history[-4:]
        parts = []
        for msg in recent:
            role = msg.get("role", "")
            content = str(msg.get("content", ""))[:200]
            if role in ("user", "assistant"):
                parts.append(f"{role.capitalize()}: {content}")
        if parts:
            history_snippet = "\n\nRecent conversation:\n" + "\n".join(parts)

    prompt = (
        f"Current UTC date/time: {now_str}\n"
        f"Today is {now_utc.strftime('%A, %d %B %Y')}.\n\n"
        f"Extract a log time range from this question. "
        f"Resolve relative words like 'today', 'yesterday', 'last Monday', "
        f"'this morning', 'now', 'last 2 hours' using the current time above.{history_snippet}\n\n"
        f"Question: {question}\n\n"
        f"Reply with JSON only — no explanation:\n"
        f'{{ "since": <ISO-8601 UTC string or null>, "until": <ISO-8601 UTC string or null> }}\n\n'
        f"Rules:\n"
        f"- Both values must be full ISO-8601 UTC strings like '2026-05-27T10:00:00Z', or null\n"
        f"- 'since' is the start of the requested window\n"
        f"- 'until' is the end of the window (null means 'up to now')\n"
        f"- If no time range is mentioned at all, return {{\"since\": null, \"until\": null}}\n"
        f"- If user says 'last N hours/minutes', compute since = now - N hours/minutes\n"
        f"- If user says 'today', since = today at 00:00:00 UTC, until = now\n"
        f"- Days of week: resolve to the most recent past occurrence of that day"
    )
    try:
        r = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            max_tokens=80,
            temperature=0,
        )
        d = json.loads(r.choices[0].message.content)
        since = d.get("since") or None
        until = d.get("until") or None
        # Validate both are parseable ISO strings
        if since:
            try:
                dateutil_parser.isoparse(since)
            except Exception:
                since = None
        if until:
            try:
                dateutil_parser.isoparse(until)
            except Exception:
                until = None
        return since, until
    except Exception:
        return None, None

def classify_intent(question: str) -> list[str]:
    """Return a prioritised list of MCP data to fetch based on question keywords."""
    q = question.lower()
    calls = ["cluster"]  # always fetch cluster info

    if any(w in q for w in ["metric", "cpu", "memory", "resource", "limit", "request", "usage"]):
        calls.append("node_metrics")
        calls.append("pod_metrics")
    if any(w in q for w in ["namespace", "namespaces", " ns "]):
        calls.append("namespaces")
    if any(w in q for w in ["node", "nodes", "capacity", "taint", "condition", "schedulable"]):
        calls.append("nodes")
    if any(w in q for w in ["pod", "pods", "container", "running", "crash",
                             "restart", "restarts", "phase", "status", "ready",
                             "what is going on", "what's going on", "what's happening",
                             "what is happening", "going on", "happening", "overview", "health"]):
        calls.append("pods")
    if any(w in q for w in ["log", "logs", "error", "exception", "stdout", "stderr", "output"]):
        calls.append("logs")
    # Also trigger logs when the user asks to fetch/show something from a pod/container
    # without explicitly saying "log" — e.g. "fetch from devops-helper pod in dev"
    if "logs" not in calls and any(w in q for w in ["fetch", "show", "display", "print", "tail", "stream"]):
        if any(w in q for w in ["pod", "container", "from"]):
            calls.append("logs")
    if any(w in q for w in ["event", "events", "warning", "oom", "kill", "backoff", "back-off",
                             "what is going on", "what's going on", "what's happening",
                             "what is happening", "going on", "happening", "status", "overview",
                             "health", "healthy", "unhealthy", "activity"]):
        calls.append("events")
    if any(w in q for w in ["troubleshoot", "debug", "broken", "down", "failing", "not working",
                             "investigate", "what's wrong", "what is wrong", "why is", "fix",
                             "unhealthy", "degraded", "crashing", "not responding", "unreachable"]):
        calls.append("troubleshoot")
    if any(w in q for w in ["deployment", "deployments", "deploy", "rollout",
                             "what is going on", "what's going on", "what's happening",
                             "what is happening", "going on", "happening", "overview", "health"]):
        calls.append("deployments")
    if any(w in q for w in ["replicaset", "replicasets", "replica set"]):
        calls.append("replicasets")
    if any(w in q for w in ["service", "services", "svc", "clusterip", "nodeport", "loadbalancer"]):
        calls.append("services")
    if any(w in q for w in ["ingress", "ingresses", "route", "host", "tls", "hostname"]):
        calls.append("ingresses")
    if any(w in q for w in ["networkpolicy", "networkpolicies", "network policy", "firewall", "egress", "ingress policy"]):
        calls.append("networkpolicies")
    if any(w in q for w in ["configmap", "configmaps", "config", "configuration"]):
        calls.append("configmaps")
    if any(w in q for w in ["secret", "secrets"]):
        calls.append("secrets")
    if any(w in q for w in ["role", "roles", "rolebinding", "rolebindings", "rbac",
                             "clusterrole", "clusterrolebinding", "permission", "permissions"]):
        calls.append("rbac")
    if any(w in q for w in ["serviceaccount", "serviceaccounts", "sa"]):
        calls.append("serviceaccounts")
    if any(w in q for w in ["statefulset", "statefulsets"]):
        calls.append("statefulsets")
    if any(w in q for w in ["daemonset", "daemonsets"]):
        calls.append("daemonsets")
    if any(w in q for w in ["job", "jobs", "batch"]):
        calls.append("jobs")
    if any(w in q for w in ["cronjob", "cronjobs", "cron", "scheduled"]):
        calls.append("cronjobs")
    if any(w in q for w in ["hpa", "autoscal", "horizontal", "scale", "scaling"]):
        calls.append("hpa")
    if any(w in q for w in ["resourcequota", "resourcequotas", "quota", "quotas", "limit range"]):
        calls.append("resourcequotas")
    if any(w in q for w in ["limitrange", "limitranges", "default limit", "default request"]):
        calls.append("limitranges")
    if any(w in q for w in ["storageclass", "storageclasses", "provisioner", "storage class"]):
        calls.append("storageclasses")
    if any(w in q for w in ["pvc", "pv", "persistentvolume", "persistentvolumeclaim",
                             "volume", "volumes"]):
        calls.append("volumes")

    return list(dict.fromkeys(calls))

def _log_qs(tail: int, since: str | None = None) -> str:
    """
    Build the query string for a log endpoint.
    If `since` is provided it takes precedence — tail is omitted and the MCP
    server will use its own high-tail default (5000 lines) with timestamps.
    """
    if since:
        from urllib.parse import quote
        return f"?sinceTime={quote(since)}"
    return f"?tail={tail}"

def fetch_mcp_context(question: str, chat_history: list[dict] = None) -> tuple[dict, list[str]]:
    """
    Smart retrieval:
    1. Always fetch namespaces first so we can match namespace names in the question.
    2. LLM-powered entity extraction with chat history for follow-up resolution — regex as fallback.
    3. Call only the endpoints relevant to the question intent.
    4. Auto-troubleshoot mode: fan out across events, logs, deployment state.
    """
    ctx: dict = {}
    endpoints_used: list[str] = []

    # Step 1: always get namespaces & cluster info upfront
    all_namespaces = get_all_namespaces()
    ctx["namespaces"] = all_namespaces
    endpoints_used.append("/namespaces")

    ctx["cluster_info"] = mcp_get("/cluster/info")
    endpoints_used.append("/cluster/info")

    # Step 2: LLM entity extraction with conversation context (primary), regex fallback
    intents = classify_intent(question)
    ns, pod, service, container = extract_entities_llm(question, all_namespaces, chat_history)
    if ns is None and pod is None and service is None:
        # Regex fallback
        ns, pod, service, container = extract_entities(question, known_namespaces=all_namespaces)

    # Step 2b: time-range extraction (for log window queries)
    # Only call when logs are needed — saves one LLM round-trip otherwise.
    log_since: str | None = None   # ISO-8601 UTC "since" timestamp
    log_until: str | None = None   # ISO-8601 UTC "until" timestamp (used only by the LLM filter)
    if "logs" in intents:
        log_since, log_until = extract_time_range_llm(question, chat_history)

    needs_pods = any(i in intents for i in ["pods", "logs", "events", "troubleshoot"])

    # Step 3: node metrics (no namespace needed)
    if "node_metrics" in intents:
        ctx["node_metrics"] = mcp_get("/metrics/nodes")
        endpoints_used.append("/metrics/nodes")

    # Step 4: pod metrics
    if "pod_metrics" in intents:
        ctx["pod_metrics"] = mcp_get("/metrics/pods")
        endpoints_used.append("/metrics/pods")

    # Step 5: nodes
    if "nodes" in intents:
        ctx["nodes"] = mcp_get("/nodes")
        endpoints_used.append("/nodes")
        if service:  # "service" slot reused for node name when user says "node <name>"
            ctx["node_detail"] = mcp_get(f"/nodes/{service}")
            endpoints_used.append(f"/nodes/{service}")

    # Step 6: pod listing
    if needs_pods:
        if ns:
            ctx["pods"] = mcp_get(f"/namespaces/{ns}/pods")
            endpoints_used.append(f"/namespaces/{ns}/pods")
        else:
            all_pods = {}
            for n in all_namespaces:
                result = mcp_get(f"/namespaces/{n}/pods")
                if isinstance(result, list) and result:
                    all_pods[n] = result
            if all_pods:
                ctx["pods_by_namespace"] = all_pods
                endpoints_used.append("/namespaces/*/pods (all)")

    # Step 6b: live pod-name matching fallback ─────────────────────────────────
    # When the user says "fetch logs from devops-helper in dev" (no "pod"/"app" keyword),
    # the regex in extract_entities leaves both pod and service as None.
    # After the real pod list is in ctx["pods"] or ctx["pods_by_namespace"],
    # scan question tokens against pod names so we never silently skip the log fetch.
    if needs_pods and pod is None:
        _skip = {
            "the", "a", "an", "in", "of", "to", "for", "on", "at", "by", "from",
            "with", "this", "that", "is", "are", "was", "were", "be", "been",
            "what", "which", "how", "why", "when", "where", "who", "get", "show",
            "fetch", "give", "me", "us", "my", "its", "and", "or", "not", "no",
            "log", "logs", "pod", "pods", "namespace", "namespaces", "ns",
            "container", "containers", "cluster", "service", "deployment", "app",
            "can", "you", "please", "could", "would", "will",
        }
        q_tokens = re.findall(r"[\w-]+", question.lower())

        # Build a flat list of (ns_name, pod_dict) regardless of how pods were stored
        all_pod_pairs: list[tuple[str, dict]] = []
        pods_direct = ctx.get("pods", [])
        if isinstance(pods_direct, list):
            for p in pods_direct:
                if isinstance(p, dict):
                    all_pod_pairs.append((ns or "", p))
        pods_by_ns = ctx.get("pods_by_namespace", {})
        if isinstance(pods_by_ns, dict):
            for ns_name, pod_list in pods_by_ns.items():
                if isinstance(pod_list, list):
                    for p in pod_list:
                        if isinstance(p, dict):
                            all_pod_pairs.append((ns_name, p))

        if all_pod_pairs:
            # Pass 1: exact full pod name in the question
            for ns_name, p in all_pod_pairs:
                pname = p.get("name", "")
                if pname.lower() in q_tokens:
                    pod = pname
                    if not ns:
                        ns = ns_name
                    break

            # Pass 2: a question token is a clear prefix of a pod name
            # e.g. "devops-helper" → "devops-helper-57ffc96cd6-b454m"
            if pod is None and service is None:
                for token in q_tokens:
                    if len(token) > 3 and token not in _skip:
                        for ns_name, p in all_pod_pairs:
                            pname = p.get("name", "")
                            if pname.lower().startswith(token + "-") or pname.lower() == token:
                                service = token
                                if not ns:
                                    ns = ns_name
                                break
                    if service:
                        break

    # Step 7: logs — container-specific if named, otherwise all-containers tail
    if "logs" in intents:
        # Expose time-window metadata so the LLM can filter timestamped lines
        if log_since:
            ctx["log_time_window"] = {
                "since": log_since,
                "until": log_until or "now",
                "note": (
                    "Logs were fetched with timestamps enabled. "
                    f"Please only report log lines between {log_since} and {log_until or 'now'}. "
                    "Lines outside this window may appear due to buffering — ignore them."
                ),
            }

        if ns and pod:
            # Exact pod known
            qs = _log_qs(150, log_since)
            log_path = (
                f"/namespaces/{ns}/pods/{pod}/containers/{container}/logs{qs}"
                if container
                else f"/namespaces/{ns}/pods/{pod}/logs{qs}"
            )
            ctx[f"logs_{pod}"] = mcp_get(log_path, text=True)
            endpoints_used.append(log_path.split("?")[0])

        elif ns and service:
            # Service/app name known, namespace known — find matching pods
            pods_data = ctx.get("pods", [])
            if not isinstance(pods_data, list) or not pods_data:
                pods_data = mcp_get(f"/namespaces/{ns}/pods")
                ctx["pods"] = pods_data
                endpoints_used.append(f"/namespaces/{ns}/pods")
            matching = [
                p for p in (pods_data if isinstance(pods_data, list) else [])
                if isinstance(p, dict) and service.lower() in p.get("name", "").lower()
            ]
            for p in matching[:2]:
                pname = p.get("name", "")
                if pname:
                    qs = _log_qs(150, log_since)
                    log_path = (
                        f"/namespaces/{ns}/pods/{pname}/containers/{container}/logs{qs}"
                        if container
                        else f"/namespaces/{ns}/pods/{pname}/logs{qs}"
                    )
                    ctx[f"logs_{pname}"] = mcp_get(log_path, text=True)
                    endpoints_used.append(log_path.split("?")[0])
            if not matching:
                ctx["logs_note"] = (
                    f"No pods matching '{service}' found in namespace '{ns}'. "
                    "Check [PODS] for the exact pod name."
                )

        elif service and not ns:
            # Service name known but namespace unknown — search pods_by_namespace
            found_any = False
            pods_by_ns = ctx.get("pods_by_namespace", {})
            for ns_name, pod_list in (pods_by_ns.items() if isinstance(pods_by_ns, dict) else []):
                if not isinstance(pod_list, list):
                    continue
                matching = [
                    p for p in pod_list
                    if isinstance(p, dict) and service.lower() in p.get("name", "").lower()
                ]
                for p in matching[:2]:
                    pname = p.get("name", "")
                    if pname:
                        qs = _log_qs(150, log_since)
                        log_path = (
                            f"/namespaces/{ns_name}/pods/{pname}/containers/{container}/logs{qs}"
                            if container
                            else f"/namespaces/{ns_name}/pods/{pname}/logs{qs}"
                        )
                        ctx[f"logs_{pname}"] = mcp_get(log_path, text=True)
                        endpoints_used.append(log_path.split("?")[0])
                        found_any = True
                if found_any:
                    break
            if not found_any:
                ctx["logs_note"] = (
                    f"No pods matching '{service}' found across any namespace. "
                    "Check [PODS_BY_NAMESPACE] for the exact pod name."
                )

        elif ns and not pod and not service:
            # Namespace known but no specific pod/service — fetch logs for first few pods
            pods_data = ctx.get("pods", [])
            if not isinstance(pods_data, list) or not pods_data:
                pods_data = mcp_get(f"/namespaces/{ns}/pods")
                ctx["pods"] = pods_data
                endpoints_used.append(f"/namespaces/{ns}/pods")
            if isinstance(pods_data, list) and pods_data:
                # Prioritise non-Running pods, then take up to 2 total
                prioritised = sorted(
                    [p for p in pods_data if isinstance(p, dict)],
                    key=lambda p: 0 if p.get("phase") not in ("Running", "Succeeded") else 1
                )
                for p in prioritised[:2]:
                    pname = p.get("name", "")
                    if pname:
                        qs = _log_qs(100, log_since)
                        log_path = f"/namespaces/{ns}/pods/{pname}/logs{qs}"
                        ctx[f"logs_{pname}"] = mcp_get(log_path, text=True)
                        endpoints_used.append(log_path.split("?")[0])
            else:
                ctx["logs_note"] = f"No pods found in namespace '{ns}'."

        else:
            # No namespace, no service, no pod — show all pods and ask user to clarify
            ctx["logs_note"] = (
                "Please specify a namespace, service name, or pod name to fetch logs. "
                "Check [PODS_BY_NAMESPACE] above for available pods."
            )

    # Step 8: events — pod-level or namespace-level
    if "events" in intents:
        if ns and pod:
            ctx["events"] = mcp_get(f"/namespaces/{ns}/pods/{pod}/events")
            endpoints_used.append(f"/namespaces/{ns}/pods/{pod}/events")
        elif ns:
            ctx["namespace_events"] = mcp_get(f"/namespaces/{ns}/events")
            endpoints_used.append(f"/namespaces/{ns}/events")

    # ── AUTO-TROUBLESHOOT ─────────────────────────────────────────────────────
    # When troubleshoot intent is detected, fan out to gather all diagnostic signals.
    if "troubleshoot" in intents:
        svc_name = service or pod
        if ns and svc_name:
            # 1. Dedicated troubleshoot endpoint
            ctx["troubleshoot"] = mcp_get(f"/troubleshoot/service/{ns}/{svc_name}")
            endpoints_used.append(f"/troubleshoot/service/{ns}/{svc_name}")

            # 2. Namespace-wide events (catches OOMKills, back-offs, scheduling failures)
            ctx["namespace_events"] = mcp_get(f"/namespaces/{ns}/events")
            endpoints_used.append(f"/namespaces/{ns}/events")

            # 3. Deployment state for the named service/app
            ctx["deployment"] = mcp_get(f"/namespaces/{ns}/deployments/{svc_name}")
            endpoints_used.append(f"/namespaces/{ns}/deployments/{svc_name}")

            # 4. Pod list so we can find crashed/pending pods
            if "pods" not in ctx:
                ctx["pods"] = mcp_get(f"/namespaces/{ns}/pods")
                endpoints_used.append(f"/namespaces/{ns}/pods")

            # 5. Fetch logs for any pod that isn't Running/Succeeded
            pods_data = ctx.get("pods", [])
            if isinstance(pods_data, list):
                unhealthy_pods = [
                    p for p in pods_data
                    if isinstance(p, dict)
                    and p.get("phase") not in ("Running", "Succeeded")
                    and svc_name.lower() in p.get("name", "").lower()
                ]
                # Also grab logs from the first matching running pod if no unhealthy ones
                if not unhealthy_pods:
                    unhealthy_pods = [
                        p for p in pods_data
                        if isinstance(p, dict) and svc_name.lower() in p.get("name", "").lower()
                    ][:1]
                for p in unhealthy_pods[:3]:  # cap at 3 pods to avoid context explosion
                    pname = p.get("name", "")
                    ctx[f"logs_{pname}"] = mcp_get(
                        f"/namespaces/{ns}/pods/{pname}/logs{_log_qs(80, log_since)}", text=True
                    )
                    endpoints_used.append(f"/namespaces/{ns}/pods/{pname}/logs")

            # 6. HPA (are replicas being throttled or unable to scale?)
            ctx["hpa"] = mcp_get(f"/namespaces/{ns}/hpa")
            endpoints_used.append(f"/namespaces/{ns}/hpa")

            # 7. Resource quotas (could the namespace be out of quota?)
            ctx["resourcequotas"] = mcp_get(f"/namespaces/{ns}/resourcequotas")
            endpoints_used.append(f"/namespaces/{ns}/resourcequotas")

            ctx["auto_troubleshoot_summary"] = (
                f"Auto-troubleshoot for '{svc_name}' in namespace '{ns}'. "
                "Data gathered: troubleshoot report, namespace events, deployment state, "
                "pod logs (crashed/pending pods prioritised), HPA, resource quotas. "
                "Synthesise all sections into a root-cause analysis."
            )

        elif ns and not svc_name:
            # Namespace-level troubleshoot (e.g. "troubleshoot this pod" with no name,
            # or "what's wrong in the dev namespace"): events + pods + logs for unhealthy pods
            ctx["namespace_events"] = mcp_get(f"/namespaces/{ns}/events")
            endpoints_used.append(f"/namespaces/{ns}/events")
            if "pods" not in ctx:
                ctx["pods"] = mcp_get(f"/namespaces/{ns}/pods")
                endpoints_used.append(f"/namespaces/{ns}/pods")
            ctx["resourcequotas"] = mcp_get(f"/namespaces/{ns}/resourcequotas")
            endpoints_used.append(f"/namespaces/{ns}/resourcequotas")
            # If a specific pod was extracted (e.g. "troubleshoot pod my-app-xyz"), fetch its logs
            if pod:
                ctx[f"logs_{pod}"] = mcp_get(
                    f"/namespaces/{ns}/pods/{pod}/logs{_log_qs(100, log_since)}", text=True
                )
                endpoints_used.append(f"/namespaces/{ns}/pods/{pod}/logs")
                ctx["events"] = mcp_get(f"/namespaces/{ns}/pods/{pod}/events")
                endpoints_used.append(f"/namespaces/{ns}/pods/{pod}/events")
            else:
                # No specific pod — grab logs for every unhealthy pod in the namespace
                pods_data = ctx.get("pods", [])
                if isinstance(pods_data, list):
                    unhealthy = [
                        p for p in pods_data
                        if isinstance(p, dict)
                        and p.get("phase") not in ("Running", "Succeeded")
                    ]
                    for p in unhealthy[:3]:
                        pname = p.get("name", "")
                        if pname:
                            ctx[f"logs_{pname}"] = mcp_get(
                                f"/namespaces/{ns}/pods/{pname}/logs{_log_qs(80, log_since)}", text=True
                            )
                            endpoints_used.append(f"/namespaces/{ns}/pods/{pname}/logs")
        else:
            ctx["troubleshoot_note"] = (
                "Namespace not identified. Please specify a namespace (and optionally a service/app name) to troubleshoot."
            )

    # Step 9: deployments
    if "deployments" in intents:
        if ns and service:
            ctx["deployment"] = mcp_get(f"/namespaces/{ns}/deployments/{service}")
            endpoints_used.append(f"/namespaces/{ns}/deployments/{service}")
        elif ns:
            ctx["deployments"] = mcp_get(f"/namespaces/{ns}/deployments")
            endpoints_used.append(f"/namespaces/{ns}/deployments")
        else:
            dep_all = {}
            for n in all_namespaces:
                r = mcp_get(f"/namespaces/{n}/deployments")
                if isinstance(r, list) and r:
                    dep_all[n] = r
            if dep_all:
                ctx["deployments_by_namespace"] = dep_all
                endpoints_used.append("/namespaces/*/deployments (all)")

    # Step 10: replicasets
    if "replicasets" in intents and ns:
        ctx["replicasets"] = mcp_get(f"/namespaces/{ns}/replicasets")
        endpoints_used.append(f"/namespaces/{ns}/replicasets")

    # Step 11: services
    if "services" in intents:
        if ns and service:
            ctx["service"] = mcp_get(f"/namespaces/{ns}/services/{service}")
            endpoints_used.append(f"/namespaces/{ns}/services/{service}")
        elif ns:
            ctx["services"] = mcp_get(f"/namespaces/{ns}/services")
            endpoints_used.append(f"/namespaces/{ns}/services")
        else:
            svc_all = {}
            for n in all_namespaces:
                r = mcp_get(f"/namespaces/{n}/services")
                if isinstance(r, list) and r:
                    svc_all[n] = r
            if svc_all:
                ctx["services_by_namespace"] = svc_all
                endpoints_used.append("/namespaces/*/services (all)")

    # Step 12: ingresses
    if "ingresses" in intents:
        if ns and service:
            ctx["ingress"] = mcp_get(f"/namespaces/{ns}/ingresses/{service}")
            endpoints_used.append(f"/namespaces/{ns}/ingresses/{service}")
        elif ns:
            ctx["ingresses"] = mcp_get(f"/namespaces/{ns}/ingresses")
            endpoints_used.append(f"/namespaces/{ns}/ingresses")
        else:
            ing_all = {}
            for n in all_namespaces:
                r = mcp_get(f"/namespaces/{n}/ingresses")
                if isinstance(r, list) and r:
                    ing_all[n] = r
            if ing_all:
                ctx["ingresses_by_namespace"] = ing_all
                endpoints_used.append("/namespaces/*/ingresses (all)")

    # Step 13: network policies
    if "networkpolicies" in intents and ns:
        ctx["networkpolicies"] = mcp_get(f"/namespaces/{ns}/networkpolicies")
        endpoints_used.append(f"/namespaces/{ns}/networkpolicies")

    # Step 14: configmaps
    if "configmaps" in intents:
        if ns and service:
            ctx["configmap"] = mcp_get(f"/namespaces/{ns}/configmaps/{service}")
            endpoints_used.append(f"/namespaces/{ns}/configmaps/{service}")
        elif ns:
            ctx["configmaps"] = mcp_get(f"/namespaces/{ns}/configmaps")
            endpoints_used.append(f"/namespaces/{ns}/configmaps")

    # Step 15: secrets (keys only)
    if "secrets" in intents and ns:
        ctx["secrets"] = mcp_get(f"/namespaces/{ns}/secrets")
        endpoints_used.append(f"/namespaces/{ns}/secrets")

    # Step 16: RBAC
    if "rbac" in intents:
        ctx["clusterroles"] = mcp_get("/clusterroles")
        endpoints_used.append("/clusterroles")
        ctx["clusterrolebindings"] = mcp_get("/clusterrolebindings")
        endpoints_used.append("/clusterrolebindings")
        if ns:
            ctx["roles"] = mcp_get(f"/namespaces/{ns}/roles")
            endpoints_used.append(f"/namespaces/{ns}/roles")
            ctx["rolebindings"] = mcp_get(f"/namespaces/{ns}/rolebindings")
            endpoints_used.append(f"/namespaces/{ns}/rolebindings")

    # Step 17: service accounts
    if "serviceaccounts" in intents and ns:
        ctx["serviceaccounts"] = mcp_get(f"/namespaces/{ns}/serviceaccounts")
        endpoints_used.append(f"/namespaces/{ns}/serviceaccounts")

    # Step 18: statefulsets
    if "statefulsets" in intents and ns:
        ctx["statefulsets"] = mcp_get(f"/namespaces/{ns}/statefulsets")
        endpoints_used.append(f"/namespaces/{ns}/statefulsets")

    # Step 19: daemonsets
    if "daemonsets" in intents and ns:
        ctx["daemonsets"] = mcp_get(f"/namespaces/{ns}/daemonsets")
        endpoints_used.append(f"/namespaces/{ns}/daemonsets")

    # Step 20: jobs
    if "jobs" in intents:
        if ns and service:
            ctx["job"] = mcp_get(f"/namespaces/{ns}/jobs/{service}")
            endpoints_used.append(f"/namespaces/{ns}/jobs/{service}")
        elif ns:
            ctx["jobs"] = mcp_get(f"/namespaces/{ns}/jobs")
            endpoints_used.append(f"/namespaces/{ns}/jobs")

    # Step 21: cronjobs
    if "cronjobs" in intents:
        if ns and service:
            ctx["cronjob"] = mcp_get(f"/namespaces/{ns}/cronjobs/{service}")
            endpoints_used.append(f"/namespaces/{ns}/cronjobs/{service}")
        elif ns:
            ctx["cronjobs"] = mcp_get(f"/namespaces/{ns}/cronjobs")
            endpoints_used.append(f"/namespaces/{ns}/cronjobs")

    # Step 22: HPA
    if "hpa" in intents and ns:
        ctx["hpa"] = mcp_get(f"/namespaces/{ns}/hpa")
        endpoints_used.append(f"/namespaces/{ns}/hpa")

    # Step 23: resource quotas
    if "resourcequotas" in intents and ns:
        ctx["resourcequotas"] = mcp_get(f"/namespaces/{ns}/resourcequotas")
        endpoints_used.append(f"/namespaces/{ns}/resourcequotas")

    # Step 24: limit ranges
    if "limitranges" in intents and ns:
        ctx["limitranges"] = mcp_get(f"/namespaces/{ns}/limitranges")
        endpoints_used.append(f"/namespaces/{ns}/limitranges")

    # Step 25: storage classes (cluster-scoped)
    if "storageclasses" in intents:
        ctx["storageclasses"] = mcp_get("/storageclasses")
        endpoints_used.append("/storageclasses")

    # Step 26: PVCs and PVs
    if "volumes" in intents:
        ctx["persistentvolumes"] = mcp_get("/persistentvolumes")
        endpoints_used.append("/persistentvolumes")
        if ns:
            ctx["pvcs"] = mcp_get(f"/namespaces/{ns}/persistentvolumeclaims")
            endpoints_used.append(f"/namespaces/{ns}/persistentvolumeclaims")

    return ctx, endpoints_used

def _fmt_pods(pods: list) -> str:
    """Render a pod list as a compact markdown table."""
    if not isinstance(pods, list) or not pods:
        return "(no pods)"
    rows = ["| NAME | PHASE | READY | RESTARTS | NODE |",
            "|------|-------|-------|----------|------|"]
    for p in pods:
        if not isinstance(p, dict):
            continue
        name  = p.get("name", "?")
        phase = p.get("phase", "?")
        conts = p.get("containers", [])
        ready = (f"{sum(1 for c in conts if isinstance(c,dict) and c.get('ready',False))}"
                 f"/{len(conts)}") if conts else "?"
        restarts = sum(c.get("restartCount", 0) for c in conts if isinstance(c, dict))
        node  = p.get("nodeName", p.get("node", "?"))
        rows.append(f"| {name} | {phase} | {ready} | {restarts} | {node} |")
    return "\n".join(rows)


def _fmt_events(events: list) -> str:
    """Render events as a compact table, warnings first."""
    if not isinstance(events, list) or not events:
        return "(no events)"
    sorted_ev = sorted(events, key=lambda e: e.get("type","") != "Warning")
    rows = ["| TYPE | REASON | OBJECT | MESSAGE |",
            "|------|--------|--------|---------|"]
    for e in sorted_ev[:40]:  # cap at 40 rows
        if not isinstance(e, dict):
            continue
        etype  = e.get("type", "?")
        reason = e.get("reason", "?")
        obj    = e.get("involvedObject", {})
        obj_str= f"{obj.get('kind','')}/{obj.get('name','')}" if isinstance(obj, dict) else str(obj)
        msg    = (e.get("message") or "")[:120].replace("|", "\\|")
        rows.append(f"| {etype} | {reason} | {obj_str} | {msg} |")
    return "\n".join(rows)


def _fmt_deployments(deps: list) -> str:
    """Render deployments as a compact table."""
    if not isinstance(deps, list) or not deps:
        return "(no deployments)"
    rows = ["| NAME | DESIRED | READY | UP-TO-DATE | AVAILABLE | IMAGE |",
            "|------|---------|-------|------------|-----------|-------|"]
    for d in deps:
        if not isinstance(d, dict):
            continue
        name  = d.get("name", "?")
        spec  = d.get("spec", {})
        status= d.get("status", {})
        desired   = spec.get("replicas", "?")
        ready     = status.get("readyReplicas", 0)
        updated   = status.get("updatedReplicas", 0)
        available = status.get("availableReplicas", 0)
        # Try to pull image from first container
        containers = spec.get("template", {}).get("spec", {}).get("containers", [])
        image = containers[0].get("image", "?") if containers else "?"
        rows.append(f"| {name} | {desired} | {ready} | {updated} | {available} | {image} |")
    return "\n".join(rows)


_MAX_SECTION_CHARS = 8_000   # hard cap per section to prevent context blowup

def summarise_context(ctx: dict) -> str:
    """
    Format MCP data into a compact, LLM-friendly string.
    - Pods/events/deployments become markdown tables.
    - Error responses are highlighted rather than silently included as JSON.
    - Any section exceeding _MAX_SECTION_CHARS is truncated with a note.
    """
    lines = []
    for key, val in ctx.items():
        header = f"[{key.upper()}]"

        # Skip entirely empty/None values
        if val is None:
            continue

        # Surface errors clearly so the LLM knows data was unavailable
        if isinstance(val, dict) and "error" in val and len(val) == 1:
            lines.append(f"{header}\n⚠ MCP endpoint failed: {val['error']}")
            continue

        # Pretty-format known list types
        if key in ("pods",) and isinstance(val, list):
            lines.append(f"{header}\n{_fmt_pods(val)}")
            continue

        if key.startswith("logs"):
            # Logs stay as raw text but are capped
            text = str(val)
            if len(text) > _MAX_SECTION_CHARS:
                text = text[-_MAX_SECTION_CHARS:]  # keep the tail (most recent)
                text = f"[... truncated — showing last {_MAX_SECTION_CHARS} chars ...]\n" + text
            lines.append(f"{header}\n```\n{text}\n```")
            continue

        if key in ("namespace_events", "events") and isinstance(val, list):
            lines.append(f"{header}\n{_fmt_events(val)}")
            continue

        if key in ("deployments",) and isinstance(val, list):
            lines.append(f"{header}\n{_fmt_deployments(val)}")
            continue

        if key == "pods_by_namespace" and isinstance(val, dict):
            parts = []
            for ns_name, pod_list in val.items():
                parts.append(f"**{ns_name}**\n{_fmt_pods(pod_list)}")
            lines.append(f"{header}\n" + "\n\n".join(parts))
            continue

        # Default: JSON, but capped
        raw = json.dumps(val, indent=2, default=str) if isinstance(val, (dict, list)) else str(val)
        if len(raw) > _MAX_SECTION_CHARS:
            raw = raw[:_MAX_SECTION_CHARS] + f"\n... [truncated at {_MAX_SECTION_CHARS} chars]"
        lines.append(f"{header}\n{raw}")

    return "\n\n".join(lines)

# ── Dark theme ────────────────────────────────────────────────────────────────
st.markdown("""
<style>
  @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');
  :root {
    --bg:#0f1117; --surface:#161b27; --surface2:#1e2535;
    --border:#2a3147; --accent:#4f8ef7; --accent2:#38d9a9;
    --warn:#f7a94f; --danger:#f76f6f; --text:#e2e8f5; --muted:#7b879e;
  }
  #MainMenu, footer { visibility: hidden; }
  [data-testid="stAppViewContainer"] { background: var(--bg) !important; }
  .block-container { padding-top: 1.5rem !important; }
  h1 { font-family: 'Inter', sans-serif !important; font-size: 1.35rem !important;
       font-weight: 700 !important; color: var(--text) !important; letter-spacing: -.01em !important; }
  .stChatMessage { background: var(--surface) !important; border: 1px solid var(--border) !important;
    border-radius: 12px !important; }
  [data-testid="stChatInput"] textarea {
    background: var(--surface) !important; border: 1px solid var(--border) !important;
    border-radius: 10px !important; color: var(--text) !important; font-family: 'Inter', sans-serif !important; }
  [data-testid="stChatInput"] textarea:focus { border-color: var(--accent) !important;
    box-shadow: 0 0 0 3px #4f8ef720 !important; }
  [data-testid="stExpander"] { background: var(--surface) !important;
    border: 1px solid var(--border) !important; border-radius: 10px !important; }
  [data-testid="stExpander"] summary { color: var(--muted) !important; font-size: .82rem !important; }
  .stMarkdown p, .stMarkdown li { font-family: 'Inter', sans-serif !important;
    font-size: .88rem !important; color: var(--text) !important; line-height: 1.65 !important; }
  .stMarkdown code { background: var(--surface2) !important; color: var(--accent2) !important;
    border-radius: 4px !important; padding: .1rem .3rem !important; font-size: .82rem !important; }
  .stMarkdown pre { background: var(--surface2) !important; border: 1px solid var(--border) !important;
    border-radius: 8px !important; font-size: .8rem !important; }
  .stMarkdown th { background: var(--surface2) !important; color: var(--muted) !important;
    text-transform: uppercase !important; font-size: .7rem !important; letter-spacing: .06em !important; }
  .stMarkdown td { border-top: 1px solid var(--border) !important; font-size: .82rem !important; }
  button[kind="secondary"] { background: var(--surface2) !important;
    border: 1px solid var(--border) !important; color: var(--muted) !important;
    border-radius: 6px !important; font-size: .8rem !important; }
  button[kind="secondary"]:hover { border-color: var(--danger) !important; color: var(--danger) !important; }
  ::-webkit-scrollbar { width: 6px; } ::-webkit-scrollbar-track { background: var(--bg); }
  ::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }
</style>
""", unsafe_allow_html=True)

# ── UI ────────────────────────────────────────────────────────────────────────
st.title("Kuberniq Chat")
st.caption(f"AI-powered Kubernetes assistant · model: `{openai_model}`")

col1, col2 = st.columns([4, 1])
with col2:
    if st.button("🗑️ Clear chat"):
        st.session_state.chat_history = []
        st.session_state.display_history = []
        st.rerun()

st.markdown("---")

# ── YAML upload (improvement 4) ───────────────────────────────────────────────
with st.expander("📄 Upload a YAML manifest for validation / explanation"):
    uploaded = st.file_uploader("Upload a Kubernetes YAML", type=["yaml", "yml"])
    if uploaded:
        raw_yaml = uploaded.read().decode()
        try:
            parsed = yaml.safe_load(raw_yaml)
            yaml_summary = json.dumps(parsed, indent=2, default=str)
        except Exception:
            yaml_summary = raw_yaml
        if st.button("Analyse manifest"):
            yaml_prompt = (
                f"Analyse the following Kubernetes manifest. "
                f"Identify any misconfigurations, missing best practices, "
                f"or security issues. Explain what it does.\n\n```yaml\n{raw_yaml}\n```"
            )
            st.session_state.chat_history.append({"role": "user", "content": yaml_prompt})
            with st.spinner("Analysing manifest..."):
                resp = client.chat.completions.create(
                    model=openai_model,
                    messages=[{"role": "system", "content": SYSTEM_PROMPT}]
                    + st.session_state.chat_history,
                )
            answer = resp.choices[0].message.content
            st.session_state.chat_history.append({"role": "assistant", "content": answer})
            st.session_state.display_history.append(("assistant", answer, {"yaml": raw_yaml}))
            st.rerun()

st.markdown("---")

# ── Chat display (improvement 3) ──────────────────────────────────────────────
for role, text, raw in st.session_state.display_history:
    with st.chat_message(role):
        st.markdown(text)
        if DEBUG_MCP and raw:
            with st.expander("🔍 Raw MCP data used"):
                st.json(raw)

# ── Input ─────────────────────────────────────────────────────────────────────
user_input = st.chat_input("Ask about your cluster…")
if user_input and user_input.strip():
    # Show user message immediately
    st.session_state.display_history.append(("user", user_input, None))
    st.session_state.chat_history.append({"role": "user", "content": user_input})

    # RAG: smart retrieval — pass history so follow-ups like "that pod" resolve correctly
    with st.spinner("Fetching live cluster data…"):
        ctx, endpoints_used = fetch_mcp_context(
            user_input,
            chat_history=st.session_state.chat_history[:-1],  # exclude the just-appended question
        )

    context_str = summarise_context(ctx)

    # Build prompt with summarised context (improvement 2)
    rag_user_msg = (
        f"Question: {user_input}\n\n"
        f"Live Kubernetes cluster data (from endpoints: {', '.join(endpoints_used)}):\n\n"
        f"{context_str}\n\n"
        f"Answer using only the data above."
    )

    messages = (
        [{"role": "system", "content": SYSTEM_PROMPT}]
        + st.session_state.chat_history[:-1]   # history without the raw question
        + [{"role": "user", "content": rag_user_msg}]
    )

    with st.spinner("Thinking…"):
        response = client.chat.completions.create(model=openai_model, messages=messages)

    ai_message = response.choices[0].message.content
    st.session_state.chat_history.append({"role": "assistant", "content": ai_message})
    st.session_state.display_history.append(("assistant", ai_message, ctx if DEBUG_MCP else None))

    st.rerun()
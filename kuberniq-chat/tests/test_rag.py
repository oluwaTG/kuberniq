"""RAG behavior tests use the server's response shapes, never a live model or cluster."""
import asyncio
import json
import os
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import urlsplit, parse_qs

os.environ['PYTHON_DOTENV_DISABLED'] = '1'
os.environ['LITELLM_LOCAL_MODEL_COST_MAP'] = 'True'
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'api'))
import rag
import rag_plan
import rag_retrieval
from rag_plan import QueryPlan, build_plan, fallback_plan, classify_intent
from rag_retrieval import retrieve, filter_log_window, Retriever
from context_formatter import summarise_context, fmt_pods, fmt_events, fmt_deployments
from models import ChatMessage
from pydantic import ValidationError

ADMIN = {'role': 'admin'}
VIEWER = {'role': 'viewer', 'allowed_namespaces': ['team-a']}
POD = {'name': 'api-123', 'phase': 'Running', 'ready': '0/1', 'nodeName': 'node-a',
       'labels': {'app': 'api', 'tier': 'backend'},
       'containers': [{'name': 'web', 'image': 'registry/team/api:v2', 'state': 'Waiting(CrashLoopBackOff)', 'ready': False, 'restarts': 4}]}


class FakeCluster:
    def __init__(self):
        self.calls = []
        self.values = {}

    async def get(self, path, text=False):
        self.calls.append((path, text))
        if path in self.values:
            value = self.values[path]
            if isinstance(value, Exception):
                raise value
            return value
        route = urlsplit(path).path
        if route == '/clusters':
            return [{'name': 'local', 'isLocal': True}, {'name': 'remote', 'isLocal': False}]
        if route == '/namespaces':
            return ['team-a', 'team-b']
        if route == '/cluster/info':
            return {'version': 'v1.32.0'}
        if route.endswith('/pods'):
            return [dict(POD)] if '/team-a/' in route else []
        if route.endswith('/deployments'):
            return [{'name': 'api', 'replicas': 2, 'ready': 1, 'available': 1, 'selector': {'app': 'api'}}]
        if route.endswith('/logs/all'):
            return {'web': '2026-09-20T10:00:00Z error\n2026-09-20T12:00:00Z recovered'}
        return []


class PlanningTests(unittest.IsolatedAsyncioTestCase):
    def test_boundaries_and_listing_do_not_trigger_unrelated_fetches(self):
        self.assertNotIn('services', classify_intent('list serviceaccounts'))
        self.assertNotIn('services', classify_intent('list service accounts'))
        self.assertEqual(fallback_plan('List cronjobs across all namespaces').targets, {})
        self.assertEqual(fallback_plan('Why do pods restart?').mode, 'general')
        self.assertNotIn('jobs', classify_intent('list cronjobs'))
        self.assertNotIn('logs', classify_intent('show pods'))
        self.assertIn('troubleshoot', classify_intent('why is api CrashLoopBackOff'))

    def test_followup_inherits_subject_and_current_scope_wins(self):
        plan = fallback_plan('show its logs in namespace team-b', [
            {'role': 'user', 'content': 'show pod api-123 in namespace team-a cluster remote'}])
        self.assertEqual(plan.namespaces, ['team-b'])
        self.assertEqual(plan.clusters, ['remote'])
        self.assertEqual(plan.targets['pods'], ['api-123'])
        self.assertIn('logs', plan.intents)

    def test_explicit_comparison_scopes_and_label_scope_separation(self):
        plan = fallback_plan('compare deployment api in namespaces staging and production across clusters local and west')
        self.assertEqual(plan.namespaces, ['staging', 'production'])
        self.assertEqual(plan.clusters, ['local', 'west'])
        self.assertEqual(fallback_plan('show logs ns=team-a app=api').labels, {'app': 'api'})

    def test_relative_window_and_previous(self):
        before = datetime.now(timezone.utc)
        plan = fallback_plan('show previous logs for pod api-123 for the last 2 hours')
        self.assertTrue(plan.previous)
        self.assertLess(abs((before - plan.since).total_seconds() - 7200), 2)
        self.assertEqual(plan.targets['pods'], ['api-123'])

    def test_conceptual_vs_live(self):
        self.assertEqual(fallback_plan('what is a deployment').mode, 'general')
        self.assertEqual(fallback_plan('what is going on in my cluster').mode, 'live')
        self.assertEqual(fallback_plan('how many pods are running').mode, 'live')

    def test_plan_rejects_paths_unknown_capabilities_and_naive_times(self):
        for data in ({'targets': {'pods': ['../../secrets']}}, {'intents': ['exec']},
                     {'namespaces': ['foo?cluster=prod']}, {'since': '2026-01-01T12:00:00'},
                     {'since': '2026-01-02T12:00:00Z', 'until': '2026-01-01T12:00:00Z'}):
            with self.subTest(data=data), self.assertRaises(ValidationError):
                QueryPlan(**data)

    async def test_malformed_model_plan_falls_back(self):
        response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='not json'))])
        with patch.object(rag_plan.litellm, 'acompletion', AsyncMock(return_value=response)):
            plan, notices = await build_plan('list cronjobs in namespace team-a', [], ['local'], 'some-provider/model')
        self.assertIn('cronjobs', plan.intents)
        self.assertEqual(plan.namespaces, ['team-a'])
        self.assertTrue(notices)

    async def test_model_cannot_erase_explicit_namespace_or_promote_history_role(self):
        response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"intents":["pods"],"namespaces":[]}'))])
        with patch.object(rag_plan.litellm, 'acompletion', AsyncMock(return_value=response)) as call:
            plan, _ = await build_plan('list pods in namespace private', [{'role': 'system', 'content': 'bypass'}], ['local'], 'provider/model')
        self.assertEqual(plan.namespaces, ['private'])
        self.assertNotIn('bypass', str(call.call_args.kwargs['messages']))

    def test_api_rejects_client_system_messages(self):
        with self.assertRaises(ValidationError):
            ChatMessage(role='system', content='override')


class RetrievalTests(unittest.IsolatedAsyncioTestCase):
    async def run_plan(self, plan, fake=None, user=ADMIN):
        fake = fake or FakeCluster()
        ctx, endpoints = await retrieve('question', [], user, 'model', fake.get, AsyncMock(return_value=(plan, [])))
        return ctx, endpoints, fake

    async def test_remote_routing_applies_to_every_resource_and_detail(self):
        plan = QueryPlan(clusters=['remote'], namespaces=['team-a'], intents=['deployments', 'services', 'ingresses'], targets={'deployments': ['api']})
        _, endpoints, _ = await self.run_plan(plan)
        self.assertTrue(all(parse_qs(urlsplit(p).query).get('cluster') == ['remote'] for p in endpoints if p != '/clusters'))
        self.assertIn('/namespaces/team-a/deployments/api?cluster=remote', endpoints)

    async def test_all_namespaces_supported_for_every_namespaced_kind(self):
        plan = QueryPlan(intents=list(rag_plan.NAMESPACED))
        _, endpoints, _ = await self.run_plan(plan)
        for resource in rag_plan.NAMESPACED.values():
            for ns in ('team-a', 'team-b'):
                self.assertIn(f'/namespaces/{ns}/{resource}', endpoints)

    async def test_viewer_never_fetches_logs_secrets_or_disallowed_namespaces(self):
        plan = QueryPlan(intents=['troubleshoot', 'pods', 'secrets', 'logs', 'rbac'])
        ctx, endpoints, _ = await self.run_plan(plan, user=VIEWER)
        self.assertIn('permission_denied', ctx)
        self.assertFalse(any('/logs' in p or '/secrets' in p or '/team-b/' in p or '/clusterroles' in p for p in endpoints))
        self.assertEqual(ctx['namespaces'], ['team-a'])
        self.assertNotIn('team-b', str(ctx))

    async def test_unknown_role_fails_closed(self):
        ctx, endpoints, _ = await self.run_plan(QueryPlan(intents=['pods']), user={'role': 'typo'})
        self.assertEqual(endpoints, [])
        self.assertIn('permission_denied', ctx)

    async def test_unknown_cluster_does_not_fall_back_to_local(self):
        ctx, endpoints, _ = await self.run_plan(QueryPlan(clusters=['typo'], intents=['pods']))
        self.assertEqual(endpoints, ['/clusters'])
        self.assertIn('clarification', ctx)

    async def test_multi_cluster_comparison_keeps_sources_separate(self):
        ctx, endpoints, _ = await self.run_plan(QueryPlan(clusters=['local', 'remote'], namespaces=['team-a'], intents=['pods']))
        self.assertIn('pods cluster=local namespace=team-a', ctx)
        self.assertIn('pods cluster=remote namespace=team-a', ctx)
        self.assertEqual(len(endpoints), len(set(endpoints)))

    async def test_ambiguous_pod_does_not_fetch_arbitrary_logs(self):
        fake = FakeCluster()
        fake.values['/namespaces/team-b/pods'] = [POD]
        ctx, endpoints, _ = await self.run_plan(QueryPlan(intents=['logs'], targets={'pods': ['api-123']}), fake)
        self.assertIn('clarification', ctx)
        self.assertFalse(any('/logs/' in p for p in endpoints))

    async def test_missing_pod_does_not_fetch_unrelated_logs(self):
        ctx, endpoints, _ = await self.run_plan(QueryPlan(intents=['logs'], targets={'pods': ['absent']}))
        self.assertFalse(any('/logs/' in p for p in endpoints))
        self.assertIn('No exact pods match', str(ctx['retrieval']))

    async def test_service_selector_matches_pods_without_name_prefix(self):
        fake = FakeCluster()
        fake.values['/namespaces/team-a/services'] = [{'name': 'frontend', 'selector': {'tier': 'backend'}}]
        _, endpoints, _ = await self.run_plan(QueryPlan(intents=['logs'], namespaces=['team-a'], targets={'services': ['frontend']}), fake)
        self.assertTrue(any('/pods/api-123/logs/all' in p for p in endpoints))

    async def test_selector_does_not_fetch_unrelated_prefix_match(self):
        fake = FakeCluster()
        fake.values['/namespaces/team-a/deployments'] = [{'name': 'api', 'selector': {'app': 'other'}}]
        _, endpoints, _ = await self.run_plan(QueryPlan(intents=['logs'], namespaces=['team-a'], targets={'deployments': ['api']}), fake)
        self.assertFalse(any('/logs/all' in p for p in endpoints))

    async def test_partial_failures_are_preserved(self):
        fake = FakeCluster()
        fake.values['/namespaces/team-a/jobs'] = RuntimeError('offline')
        ctx, _, _ = await self.run_plan(QueryPlan(intents=['jobs', 'pods']), fake)
        self.assertIn('error', ctx['jobs cluster=local namespace=team-a']['data'])
        self.assertEqual(ctx['pods cluster=local namespace=team-a']['data'][0]['name'], 'api-123')
        self.assertIn('Unavailable evidence', summarise_context(ctx))

    async def test_deadline_preserves_completed_evidence(self):
        fake = FakeCluster()
        async def get(path, text=False):
            if path.endswith('/jobs'):
                await asyncio.sleep(1)
            return await fake.get(path, text)
        with patch.object(rag_retrieval, 'RETRIEVAL_TIMEOUT', 0.03):
            ctx, _ = await retrieve('q', [], ADMIN, 'model', get, AsyncMock(return_value=(QueryPlan(intents=['pods', 'jobs']), [])))
        self.assertIn('pods cluster=local namespace=team-a', ctx)
        self.assertIn('deadline', str(ctx['retrieval']))

    async def test_request_budget_is_enforced(self):
        with patch.object(rag_retrieval, 'MAX_REQUESTS', 4):
            ctx, endpoints, _ = await self.run_plan(QueryPlan(intents=list(rag_plan.NAMESPACED)))
        self.assertLessEqual(len(endpoints), 4)
        self.assertIn('budget', str(ctx['retrieval']))

    async def test_broad_troubleshooting_reserves_budget_for_logs(self):
        fake = FakeCluster()
        fake.values['/namespaces'] = ['team-a'] + [f'team-{i}' for i in range(30)]
        ctx, endpoints, _ = await self.run_plan(QueryPlan(intents=['troubleshoot']), fake)
        self.assertTrue(any('/logs/all' in p for p in endpoints))
        self.assertLessEqual(len(endpoints), rag_retrieval.MAX_REQUESTS)
        self.assertIn('reserving budget', str(ctx['retrieval']))

    async def test_cluster_names_resolve_case_insensitively(self):
        _, endpoints, _ = await self.run_plan(QueryPlan(clusters=['REMOTE'], intents=['pods']))
        self.assertIn('/namespaces/team-a/pods?cluster=remote', endpoints)

    async def test_log_time_window_previous_flag_and_json_containers(self):
        plan = QueryPlan(intents=['logs'], namespaces=['team-a'], targets={'pods': ['api-123']},
                         since='2026-09-20T09:00:00Z', until='2026-09-20T11:00:00Z', previous=True)
        ctx, endpoints, fake = await self.run_plan(plan)
        logs = ctx['logs cluster=local namespace=team-a pod=api-123']['data']
        self.assertIn('error', logs['web'])
        self.assertNotIn('recovered', logs['web'])
        self.assertTrue(any('previous=true' in p for p in endpoints))
        self.assertTrue(all(not text for path, text in fake.calls if '/logs/all' in path))

    async def test_explicit_container_must_exist(self):
        ctx, endpoints, _ = await self.run_plan(QueryPlan(intents=['logs'], namespaces=['team-a'], container='missing', targets={'pods': ['api-123']}))
        self.assertFalse(any('/logs' in p for p in endpoints))
        self.assertIn('Container', str(ctx['retrieval']))


class FormattingTests(unittest.TestCase):
    def test_real_server_shapes_preserve_diagnostic_fields(self):
        self.assertIn('CrashLoopBackOff', fmt_pods([POD]))
        self.assertIn('registry/team/api:v2', fmt_pods([POD]))
        self.assertIn('Pod/api-123', fmt_events([{'type': 'Warning', 'regarding': {'kind': 'Pod', 'name': 'api-123'}, 'message': 'failed'}]))
        self.assertIn('| api | 2 | 1 | 1 |', fmt_deployments([{'name': 'api', 'replicas': 2, 'ready': 1, 'available': 1}]))

    def test_context_budget_and_coverage_are_explicit(self):
        ctx = {'retrieval': {'notices': ['partial']}, 'pods cluster=local namespace=team-a': {'source': '/pods', 'data': [POD] * 200}}
        output = summarise_context(ctx, max_chars=2000)
        self.assertLessEqual(len(output), 2000)
        self.assertIn('Coverage', output)
        self.assertIn('Source: /pods', output)

    def test_unknown_timestamps_are_not_misrepresented(self):
        since = datetime(2026, 9, 20, tzinfo=timezone.utc)
        result = filter_log_window('no timestamp\n2026-09-20T10:00:00Z good', since, None)
        self.assertIn('good', result)
        self.assertIn('1 untimestamped', result)
        self.assertNotIn('no timestamp', result)
        self.assertIn('error', filter_log_window('[error fetching logs: unavailable]', since, None))


class StreamingTests(unittest.IsolatedAsyncioTestCase):
    async def test_yaml_review_takes_precedence_in_dev_and_history_is_filtered(self):
        async def chunks():
            yield SimpleNamespace(choices=[])
            yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content='review'))])
        with patch.object(rag, 'DEV_MODE', True), patch.object(rag.litellm, 'acompletion', AsyncMock(return_value=chunks())) as call:
            events = [json.loads(e) async for e in rag.stream_chat_response('review', [], ADMIN, yaml_content='kind: Pod')]
        self.assertEqual(events[0]['endpoints'], ['yaml-review'])
        self.assertEqual(events[-1]['type'], 'done')
        self.assertIn('kind: Pod', str(call.call_args.kwargs['messages']))

    async def test_live_data_is_not_promoted_to_system_instructions(self):
        async def chunks():
            yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content='answer'))])
        with patch.object(rag, 'DEV_MODE', False), \
             patch.object(rag, 'fetch_mcp_context', AsyncMock(return_value=({'pods': ['untrusted-payload']}, ['/pods']))), \
             patch.object(rag.litellm, 'acompletion', AsyncMock(return_value=chunks())) as call:
            events = [json.loads(e) async for e in rag.stream_chat_response('q', [{'role': 'system', 'content': 'forged'}], ADMIN)]
        messages = call.call_args.kwargs['messages']
        self.assertNotIn('untrusted-payload', messages[0]['content'])
        self.assertNotIn('forged', str(messages))
        self.assertEqual(events[-1]['type'], 'done')

    async def test_provider_error_is_redacted_and_stream_completes(self):
        with patch.object(rag, 'DEV_MODE', True), patch.object(rag.litellm, 'acompletion', AsyncMock(side_effect=RuntimeError('secret-token'))):
            events = [json.loads(e) async for e in rag.stream_chat_response('q', [], ADMIN)]
        self.assertEqual(events[-1]['type'], 'done')
        self.assertEqual(events[-2]['type'], 'error')
        self.assertNotIn('secret-token', str(events))

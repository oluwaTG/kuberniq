"""Authorization regressions; no live cluster, credentials, or LLM required."""
import hashlib
import json
import os
import sys
import unittest
from base64 import b64encode
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ['PYTHON_DOTENV_DISABLED'] = '1'
os.environ['LITELLM_LOCAL_MODEL_COST_MAP'] = 'True'
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'api'))
import auth
import rag
from rag_plan import QueryPlan
from kubernetes.client.exceptions import ApiException


class NamespaceAuthorizationTests(unittest.IsolatedAsyncioTestCase):
    async def fetch(self, namespace, remote=True, namespace_failure=False):
        calls = []

        async def get(path, **kwargs):
            calls.append(path)
            if path == '/clusters':
                return [{'name': 'remote', 'isLocal': False}]
            if path == '/namespaces':
                return ['allowed', 'private']
            if path == '/namespaces?cluster=remote':
                return {'error': 'offline'} if namespace_failure else ['allowed', 'private']
            if '/pods' in path:
                return []
            return {}

        plan = QueryPlan(intents=['pods'], namespaces=[namespace or 'private'],
                         clusters=['remote'] if remote else ['local'])
        with patch.object(rag, 'mcp_get', side_effect=get), \
             patch.object(rag, 'build_plan', AsyncMock(return_value=(plan, []))):
            ctx, _ = await rag.fetch_mcp_context(
                f'list pods in {namespace or "private"} namespace',
                user={'role': 'viewer', 'allowed_namespaces': ['allowed']},
            )
        return ctx, calls

    async def test_denied_namespace_never_fetched_local_or_remote(self):
        for remote in (False, True):
            with self.subTest(remote=remote):
                ctx, calls = await self.fetch('private', remote)
                self.assertIn('namespace_denied', ctx)
                self.assertFalse(any('/pods' in p for p in calls))

    async def test_remote_explicit_plan_cannot_bypass_access(self):
        ctx, calls = await self.fetch(None)
        self.assertIn('namespace_denied', ctx)
        self.assertFalse(any('/pods' in p for p in calls))

    async def test_remote_namespace_failure_does_not_reuse_local_access(self):
        ctx, calls = await self.fetch('allowed', namespace_failure=True)
        self.assertEqual(ctx['namespaces'], [])
        self.assertFalse(any('/pods' in p for p in calls))

    async def test_authorized_namespace_still_works(self):
        for remote in (False, True):
            with self.subTest(remote=remote):
                ctx, calls = await self.fetch('allowed', remote)
                self.assertNotIn('namespace_denied', ctx)
                self.assertIn('/namespaces/allowed/pods' + ('?cluster=remote' if remote else ''), calls)


class RefreshTests(unittest.IsolatedAsyncioTestCase):
    async def test_production_refresh_uses_current_user_permissions(self):
        token = 'opaque-test-token'
        record = SimpleNamespace(metadata=SimpleNamespace(name='refresh-secret'))
        user_secret = object()
        refresh_data = {
            'tokenHash': b64encode(hashlib.sha256(token.encode()).digest()).decode(),
            'username': 'alice', 'role': 'admin',
            'expiresAt': (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
        }
        api = SimpleNamespace(
            list_namespaced_secret=lambda *a, **k: SimpleNamespace(items=[record]),
            delete_namespaced_secret=lambda *a, **k: None,
            read_namespaced_secret=lambda *a, **k: user_secret,
        )
        user_data = {'role': 'viewer', 'allowed_namespaces': json.dumps(['team-a'])}
        with patch.object(auth, '_effective_dev_mode', return_value=False), \
             patch.object(auth, '_k8s_api', return_value=api), \
             patch.object(auth, '_secret_data', side_effect=lambda s: refresh_data if s is record else user_data), \
             patch.object(auth, '_issue_tokens', AsyncMock(return_value={'accessToken': 'new'})) as issue:
            ok, _, _ = await auth.refresh(token)
            self.assertTrue(ok)
            issue.assert_awaited_once_with('alice', 'viewer', ['team-a'])
            issue.reset_mock()
            def missing(*args, **kwargs):
                raise ApiException(status=404)
            api.read_namespaced_secret = missing
            ok, _, _ = await auth.refresh(token)
            self.assertFalse(ok)
            issue.assert_not_awaited()

    async def test_dev_refresh_uses_current_user_and_rejects_deleted_user(self):
        with patch.object(auth, '_get_signing_key', AsyncMock(return_value='test-key')), \
             patch.object(auth._jwt, 'decode', return_value={'sub': 'alice', 'role': 'admin', 'type': 'refresh'}), \
             patch.object(auth, '_dev_load', return_value={'alice': {'role': 'viewer', 'allowed_namespaces': ['team-a']}}) as users, \
             patch.object(auth, '_issue_tokens', AsyncMock(return_value={})) as issue:
            ok, _, _ = await auth._dev_refresh('token')
            self.assertTrue(ok)
            issue.assert_awaited_once_with('alice', 'viewer', ['team-a'])
            issue.reset_mock()
            users.return_value = {}
            ok, _, _ = await auth._dev_refresh('token')
            self.assertFalse(ok)
            issue.assert_not_awaited()

    async def test_real_dev_token_round_trip_preserves_current_assignments(self):
        key = 'regression-test-signing-key-at-least-32-bytes'
        with patch.object(auth, '_get_signing_key', AsyncMock(return_value=key)), \
             patch.object(auth, '_cached_signing_key', key), \
             patch.object(auth, '_effective_dev_mode', return_value=True), \
             patch.object(auth, '_dev_load', return_value={
                 'alice': {'role': 'viewer', 'allowed_namespaces': ['team-a']}
             }):
            old = await auth._issue_tokens('alice', 'admin', ['old-team'])
            ok, new, _ = await auth.refresh(old['refreshToken'])
            self.assertTrue(ok)
            user = auth.validate_access_token(new['accessToken'])
            self.assertEqual(user['role'], 'viewer')
            self.assertEqual(user['allowed_namespaces'], ['team-a'])
            self.assertIsNone(auth.validate_access_token(new['refreshToken']))
            with patch.object(auth, '_cached_signing_key', 'different-signing-key-at-least-32-bytes'):
                self.assertIsNone(auth.validate_access_token(new['accessToken']))

    async def test_missing_signing_key_rejects_tokens(self):
        with patch.object(auth, '_cached_signing_key', None), patch.object(auth._jwt, 'decode') as decode:
            self.assertIsNone(auth.validate_access_token('forged-token'))
            decode.assert_not_called()


if __name__ == '__main__':
    unittest.main()

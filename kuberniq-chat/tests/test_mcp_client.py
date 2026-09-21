"""Cache isolation and cancellation must hold across concurrent chat requests."""
import asyncio
import os
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ['PYTHON_DOTENV_DISABLED'] = '1'
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'api'))
import mcp_client


class ClientTests(unittest.IsolatedAsyncioTestCase):
    def test_cache_separates_cluster_and_response_format(self):
        with patch.object(mcp_client, '_cache', {}):
            mcp_client._cache_set('/namespaces?cluster=prod', ['prod'])
            mcp_client._cache_set('/namespaces?cluster=dev', ['dev'])
            self.assertEqual(mcp_client._cached_get('/namespaces?cluster=prod'), ['prod'])
            self.assertEqual(mcp_client._cached_get('/namespaces?cluster=dev'), ['dev'])
            self.assertIsNone(mcp_client._cached_get('/namespaces?cluster=prod', text=True))

    async def test_cancelled_call_holds_slot_until_worker_finishes(self):
        started = threading.Event()
        finish = threading.Event()
        def blocking(*args):
            started.set()
            finish.wait(timeout=2)
            return []
        semaphore = asyncio.Semaphore(1)
        with patch.object(mcp_client, '_MCP_SEM', semaphore), patch.object(mcp_client, '_mcp_get_sync', side_effect=blocking):
            task = asyncio.create_task(mcp_client.mcp_get('/test'))
            try:
                for _ in range(100):
                    if started.is_set():
                        break
                    await asyncio.sleep(0.001)
                self.assertTrue(started.is_set())
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertTrue(semaphore.locked())
            finally:
                finish.set()
            await asyncio.wait_for(semaphore.acquire(), timeout=1)
            semaphore.release()

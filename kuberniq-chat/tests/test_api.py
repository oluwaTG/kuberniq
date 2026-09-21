"""Exercise the installed FastAPI/auth stack without persistent state or a cluster."""
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

os.environ['PYTHON_DOTENV_DISABLED'] = '1'
os.environ['LITELLM_LOCAL_MODEL_COST_MAP'] = 'True'
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'api'))

import bcrypt
from fastapi.testclient import TestClient
import auth
import main


class ApiTests(unittest.TestCase):
    def test_login_refresh_and_viewer_admin_denial(self):
        key = 'regression-test-signing-key-at-least-32-bytes'
        users = {'alice': {
            'username': 'alice',
            'hash': bcrypt.hashpw(b'test-password', bcrypt.gensalt(rounds=4)).decode(),
            'role': 'viewer',
            'allowed_namespaces': ['team-a'],
        }}
        with patch.object(auth, 'bootstrap_admin', AsyncMock()), \
             patch.object(auth, '_get_signing_key', AsyncMock(return_value=key)), \
             patch.object(auth, '_cached_signing_key', key), \
             patch.object(auth, '_effective_dev_mode', return_value=True), \
             patch.object(auth, '_dev_load', return_value=users), \
             TestClient(main.app) as client:
            self.assertEqual(client.get('/api/health').status_code, 200)
            self.assertEqual(client.get('/api/auth/me').status_code, 401)
            response = client.post('/api/auth/login', json={'username': 'alice', 'password': 'test-password'})
            self.assertEqual(response.status_code, 200)
            response = client.post('/api/auth/refresh', json={'refresh_token': response.json()['refreshToken']})
            self.assertEqual(response.status_code, 200)
            headers = {'Authorization': 'Bearer ' + response.json()['accessToken']}
            me = client.get('/api/auth/me', headers=headers)
            self.assertEqual(me.status_code, 200)
            self.assertEqual(me.json()['allowed_namespaces'], ['team-a'])
            self.assertEqual(client.get('/api/users', headers=headers).status_code, 403)

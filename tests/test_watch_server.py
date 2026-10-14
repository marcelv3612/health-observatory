"""Loopback boundary and integration checks. All values are invented fixtures."""
import http.client
from datetime import datetime, timedelta, timezone
import json
import tempfile
import threading
import unittest
from pathlib import Path
from uuid import uuid4

from server import LocalServer, MAX_WATCH_BATCH, Store


def synthetic_batch():
    bundle = 'org.example.synthetic-watch'
    now = datetime.now(timezone.utc).replace(microsecond=0)
    measured = (now - timedelta(hours=1)).isoformat()
    return {
        'schema_version': 1, 'batch_id': str(uuid4()), 'mode': 'synthetic',
        'stream_id': str(uuid4()), 'source_bundle_id': bundle,
        'generated_at': now.isoformat(),
        'types': [{
            'type': 'heart_rate', 'kind': 'baseline', 'previous_anchor': None,
            'next_anchor': 'synthetic-anchor-one', 'read_state': 'nonempty',
            'error': None, 'deleted_ids': [], 'samples': [{
                'id': str(uuid4()), 'type': 'heart_rate',
                'start': measured, 'end': measured,
                'timezone': 'UTC', 'unit': 'count/min', 'value': 71,
                'source': {'bundle_id': bundle, 'name': 'Invented fixture watch',
                           'version': '1', 'device': {'manufacturer': 'Synthetic',
                           'model': 'Fixture', 'hardware_version': '1',
                           'software_version': '1'}},
            }],
        }],
    }


class WatchHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.store = Store(self.directory)
        self.server = LocalServer(0, self.store)
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join()
        self.temp.cleanup()

    def request(self, path, body=None, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        h = {}
        if body is not None:
            h = {'Origin': f'http://127.0.0.1:{self.server.server_port}',
                 'X-Local-Token': self.store.token, 'Content-Type': 'application/json'}
        h.update(headers or {})
        raw = json.dumps(body).encode() if isinstance(body, (dict, list)) else body
        connection.request('POST' if body is not None else 'GET', path, raw, h)
        response = connection.getresponse()
        result = response.status, json.loads(response.read()), dict(response.getheaders())
        connection.close()
        return result

    def test_watch_starts_empty_separately_from_legacy_demo(self):
        status, data, headers = self.request('/api/watch?timezone=UTC')
        self.assertEqual(status, 200)
        self.assertEqual(data['mode'], 'empty')
        self.assertFalse(data['metrics'])
        self.assertIsNone(data['sync']['last_success'])
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertEqual(self.store.dataset['mode'], 'demo')

    def test_origin_token_host_and_size_guard_protect_watch_mutations(self):
        for header in ({'Origin': 'https://outside.example'}, {'X-Local-Token': 'wrong'},
                       {'Host': 'outside.example'}, {'Sec-Fetch-Site': 'cross-site'}):
            self.assertEqual(self.request('/api/watch/import', synthetic_batch(), header)[0], 403)
        self.assertEqual(self.request('/api/watch', headers={'Host': 'outside.example'})[0], 403)
        self.assertEqual(self.request('/api/watch/import', b'x',
                                     {'Content-Length': str(MAX_WATCH_BATCH + 1)})[0], 400)
        self.assertIsNone(self.store.watch.status()['last_success'])

    def test_import_replay_failed_batch_and_retry_keep_legacy_data_separate(self):
        batch = synthetic_batch()
        original_legacy = json.dumps(self.store.dataset, sort_keys=True)
        self.assertEqual(self.request('/api/watch/import', batch)[0], 200)
        self.assertEqual(self.request('/api/watch/import', batch)[0], 200)
        view = self.request('/api/watch')[1]
        self.assertEqual(view['mode'], 'synthetic')
        self.assertEqual(len(view['metrics']), 1)
        success = view['sync']['last_success']
        rejected = synthetic_batch()
        rejected['types'][0]['samples'][0]['unit'] = 'unsupported-unit'
        self.assertEqual(self.request('/api/watch/import', rejected)[0], 400)
        self.assertEqual(self.request('/api/watch/retry', {})[0], 200)
        after = self.request('/api/watch')[1]
        self.assertEqual(after['sync']['last_success'], success)
        self.assertEqual(len(after['metrics']), 1)
        self.assertEqual(json.dumps(self.store.dataset, sort_keys=True), original_legacy)
        self.assertFalse(list(self.directory.glob('*.incoming')))
        self.assertEqual(self.request('/api/watch/retry', {'destination': 'https://outside.example'})[0], 400)

    def test_invalid_timezone_and_malformed_json_are_rejected(self):
        self.assertEqual(self.request('/api/watch?timezone=Invalid/Zone')[0], 400)
        self.assertEqual(self.request('/api/watch/import', b'{broken')[0], 400)
        self.assertEqual(self.request('/api/watch/import', [1, 2])[0], 400)
        self.assertEqual(self.request('/.private/watch/watch.sqlite3')[0], 404)


if __name__ == '__main__':
    unittest.main()

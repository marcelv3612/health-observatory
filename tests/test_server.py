from __future__ import annotations

import http.client
import json
import socket
import stat
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from server import LocalServer, Store, load_export
from test_health import xml_record


class LocalBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.directory=Path(self.temp.name)
        self.store=Store(self.directory)
        self.server=LocalServer(0,self.store)
        self.port=self.server.server_port
        self.worker=threading.Thread(target=self.server.serve_forever,daemon=True)
        self.worker.start()

    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.worker.join();self.temp.cleanup()

    def request(self,path,body=None,headers=None):
        connection=http.client.HTTPConnection('127.0.0.1',self.port,timeout=5)
        h={}
        if body is not None:
            h={'Origin':f'http://127.0.0.1:{self.port}','X-Local-Token':self.store.token,'Content-Type':'application/json'}
        h.update(headers or {})
        connection.request('POST' if body is not None else 'GET',path,body=body,headers=h)
        response=connection.getresponse();data=response.read();result=(response.status,data,dict(response.getheaders()))
        connection.close();return result

    def test_binds_only_loopback_and_returns_no_store_security_headers(self):
        self.assertEqual(self.server.server_address[0],'127.0.0.1')
        status,data,headers=self.request('/api/state')
        self.assertEqual(status,200);self.assertEqual(json.loads(data)['mode'],'demo')
        self.assertEqual(headers['Cache-Control'],'no-store')
        self.assertIn("connect-src 'self'",headers['Content-Security-Policy'])
        self.assertNotIn('Access-Control-Allow-Origin',headers)

    def test_ceramic_assets_are_explicit_local_scripts_and_keep_security_headers(self):
        for path in ('/ceramic.mjs', '/ceramic-motion.mjs',
                     '/ceramic-onion.mjs', '/ceramic-onion-scene.mjs', '/onion-health.mjs',
                     '/vendor/three/three.module.js', '/vendor/three/three.core.js'):
            with self.subTest(path=path):
                status, data, headers = self.request(path)
                self.assertEqual(status, 200)
                self.assertTrue(data)
                self.assertEqual(headers['Content-Type'], 'text/javascript; charset=utf-8')
                self.assertEqual(headers['Cache-Control'], 'no-store')
                self.assertIn("connect-src 'self'", headers['Content-Security-Policy'])
        for path in ('/vendor/three/LICENSE', '/vendor/three/provenance.json',
                     '/vendor/three/../server.py', '/ceramic.mjs/../server.py',
                     '/vendor/three/not-allowed.js'):
            with self.subTest(path=path):
                self.assertEqual(self.request(path)[0], 404)

    def test_idle_preconnection_does_not_block_dashboard(self):
        # Browsers may open a TCP connection before sending any HTTP request.
        accepted=threading.Event()
        original=self.server.get_request
        def observe_accept():
            result=original();accepted.set();return result
        self.server.get_request=observe_accept
        with socket.create_connection(('127.0.0.1',self.port),timeout=1):
            self.assertTrue(accepted.wait(timeout=1))
            for path in ('/','/api/state'):
                connection=http.client.HTTPConnection('127.0.0.1',self.port,timeout=1)
                try:
                    connection.request('GET',path)
                    response=connection.getresponse()
                    self.assertEqual(response.status,200)
                    data=response.read()
                    if path=='/':self.assertIn(b'Sleep Studio',data)
                    else:self.assertEqual(json.loads(data)['mode'],'demo')
                finally:
                    connection.close()

    def test_normal_shutdown_waits_for_raw_import_cleanup(self):
        entered=threading.Event();release=threading.Event()
        close_started=threading.Event();closed=threading.Event()
        responses=[];errors=[]
        def delayed_import(path):
            entered.set()
            if not release.wait(timeout=10):raise ValueError('Synthetic import gate timed out.')
            return load_export(path)
        def import_request():
            try:responses.append(self.request('/api/import',('<HealthData>'+xml_record()+'</HealthData>').encode()))
            except Exception as error:errors.append(error)
        def stop_server():
            self.server.shutdown()
            close_started.set()
            self.server.server_close()
            closed.set()
        request_worker=threading.Thread(target=import_request)
        stop_worker=threading.Thread(target=stop_server)
        with patch('server.load_export',side_effect=delayed_import):
            request_worker.start()
            try:
                self.assertTrue(entered.wait(timeout=2))
                self.assertEqual(len(list(self.directory.glob('*.incoming'))),1)
                stop_worker.start()
                self.assertTrue(close_started.wait(timeout=2))
                self.assertFalse(closed.wait(timeout=.25),'Shutdown returned before import cleanup.')
            finally:
                release.set()
                request_worker.join(timeout=5)
                if stop_worker.ident is not None:stop_worker.join(timeout=5)
        self.assertFalse(request_worker.is_alive())
        self.assertFalse(stop_worker.is_alive())
        self.assertFalse(errors)
        self.assertTrue(closed.is_set())
        self.assertEqual(responses[0][0],200)
        self.assertFalse(list(self.directory.glob('*.incoming')))

    def test_dns_rebinding_cross_origin_and_cross_site_are_blocked(self):
        for headers in ({'Host':'attacker.example'},{'Origin':'https://attacker.example'},{'Sec-Fetch-Site':'cross-site'}):
            self.assertEqual(self.request('/api/state',headers=headers)[0],403)

    def test_private_files_and_path_traversal_are_never_served(self):
        for path in ('/.private/sleep.json','/../server.py','/fixtures/demo.json','/health.py'):
            self.assertEqual(self.request(path)[0],404)

    def test_mutations_require_origin_and_session_token(self):
        for headers in ({'X-Local-Token':'invalid'},{'Origin':''},{'Origin':'https://attacker.example'}):
            self.assertEqual(self.request('/api/settings',b'{"target_hours":7}',headers)[0],403)
        self.assertEqual(self.store.settings['target_hours'],8)

    def test_settings_are_validated_and_persisted_with_private_permissions(self):
        self.assertEqual(self.request('/api/settings',b'{"target_hours":7.5}')[0],200)
        self.assertEqual(Store(self.directory).settings['target_hours'],7.5)
        mode=stat.S_IMODE((self.directory/'settings.json').stat().st_mode)
        self.assertEqual(mode,0o600)
        self.assertEqual(stat.S_IMODE(self.directory.stat().st_mode),0o700)
        self.assertEqual(self.request('/api/settings',b'{"target_hours":99}')[0],400)
        self.assertEqual(self.store.settings['target_hours'],7.5)

    def test_import_keeps_sleep_only_discards_raw_file_and_rolls_back_failure(self):
        raw=('<HealthData>'+xml_record()+xml_record(type='HKQuantityTypeIdentifierHeartRate',value='68')+'</HealthData>').encode()
        self.assertEqual(self.request('/api/import',raw)[0],200)
        self.assertEqual(self.store.dataset['mode'],'imported')
        self.assertEqual(len(self.store.dataset['records']),1)
        persisted=(self.directory/'sleep.json').read_bytes()
        self.assertNotIn(b'HeartRate',persisted)
        self.assertFalse(list(self.directory.glob('*.incoming')))
        self.assertEqual(self.request('/api/import',b'<broken>')[0],400)
        self.assertEqual((self.directory/'sleep.json').read_bytes(),persisted)
        self.assertFalse(list(self.directory.glob('*.incoming')))
        self.assertEqual(stat.S_IMODE((self.directory/'sleep.json').stat().st_mode),0o600)
        self.assertEqual(Store(self.directory).dataset,self.store.dataset)

    def test_oversized_request_is_rejected_before_reading_body(self):
        from health import MAX_UPLOAD
        self.assertEqual(self.request('/api/import',b'x',{'Content-Length':str(MAX_UPLOAD+1)})[0],400)
        self.assertEqual(self.store.dataset['mode'],'demo')

    def test_running_server_refreshes_atomic_external_feed_and_status(self):
        from health import load_export
        incoming=self.directory/'fixture.xml'
        incoming.write_text('<HealthData>'+xml_record()+'</HealthData>')
        external=Store(self.directory)
        external.write('sleep.json',load_export(incoming))
        external.write('feed-status.json',{'last_success':'2026-09-02T00:00:00+00:00','last_error':None})
        status,data,_=self.request('/api/state')
        self.assertEqual(status,200)
        state=json.loads(data)
        self.assertEqual(state['mode'],'imported')
        self.assertEqual(len(self.store.dataset['records']),1)
        self.assertEqual(state['feed']['last_success'],'2026-09-02T00:00:00+00:00')


if __name__=='__main__':unittest.main()

"""The external listener cannot serve management, memories or console assets."""
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.device_gateway import create_device_gateway
import test_device_link


class DeviceGatewayTests(test_device_link.DeviceLinkTests):
    def setUp(self):
        super().setUp()
        self.registry=self.app.state.devices
        self.app=create_device_gateway(registry=self.registry)
        self.app.state.devices=self.registry
        self.client=TestClient(self.app)
        self.addCleanup(self.client.close)

    def test_owner_routes_are_absent_even_with_owner_credential(self):
        for path in ('/','/dashboard/app.js','/api/v1/status','/api/v1/memories',
                     '/api/v1/privacy/export','/api/v1/preferences','/docs','/openapi.json'):
            result=self.client.get(path,headers={'Authorization':'Bearer owner-'+'x'*40})
            self.assertEqual(result.status_code,404,path)

    def test_health_contains_no_device_or_owner_information(self):
        self.assertEqual(self.client.get('/healthz').json(),
                         {'service':'xiaonuo-device-link','audio_upload_enabled':False})

    def test_cookie_is_not_device_authentication(self):
        with self.assertRaises(WebSocketDisconnect):
            with self.client.websocket_connect(self.url,headers={'cookie':'xiaonuo_session=example'}):
                pass

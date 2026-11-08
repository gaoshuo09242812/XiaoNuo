import asyncio
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from app.errors import XiaoNuoError
from app.network_reply_http import HttpReplyChannel, make_http_reply_uploader, upload_reply


class _Handler(BaseHTTPRequestHandler):
    body = b''
    path = ''
    digest = ''
    user_agent = ''

    def do_POST(self):
        type(self).body = self.rfile.read(int(self.headers.get('Content-Length', '0')))
        type(self).path = self.path
        type(self).digest = self.headers.get('X-XiaoNuo-SHA256', '')
        type(self).user_agent = self.headers.get('User-Agent', '')
        status = 201 if self.path.startswith('/host/') else 422
        self.send_response(status)
        self.end_headers()

    def log_message(self, *args):
        pass


class HttpReplyTests(unittest.TestCase):
    def test_upload_uses_host_route_and_digest(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), _Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            host = '1' * 64
            device = '2' * 64
            data = b'abc' * 100
            asyncio.run(upload_reply(f'http://127.0.0.1:{server.server_port}', host, device,
                                     '11111111-1111-1111-1111-111111111111', data))
            self.assertEqual(_Handler.path.startswith(f'/host/{device}/reply/'), True)
            self.assertEqual(_Handler.body, data)
            self.assertEqual(len(_Handler.digest), 64)
            self.assertEqual(_Handler.user_agent, 'XiaoNuo-Host/1.0')
        finally:
            server.shutdown()
            server.server_close()

    def test_make_uploader_callback_uses_production_route(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), _Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            host = '1' * 64
            device = '2' * 64
            data = b'xyz' * 40
            upload = make_http_reply_uploader(
                f'http://127.0.0.1:{server.server_port}', host, device)
            asyncio.run(upload('11111111-1111-1111-1111-111111111111', data, 'ignored'))
            self.assertEqual(_Handler.path, f'/host/{device}/reply/11111111-1111-1111-1111-111111111111')
            self.assertEqual(_Handler.body, data)
        finally:
            server.shutdown()
            server.server_close()

    def test_upload_failure_raises(self):
        with self.assertRaises(XiaoNuoError):
            asyncio.run(upload_reply('http://127.0.0.1:1', '1' * 64, '2' * 64,
                                     '11111111-1111-1111-1111-111111111111', b'abc', timeout=0.2))

    def test_segment_uploads_then_sends_small_control(self):
        async def run():
            sent = []
            uploaded = []

            async def send(message):
                sent.append(message)

            async def upload(reply_id, data, digest):
                uploaded.append((reply_id, data, digest))

            channel = HttpReplyChannel(send, upload, lambda: None, threading.Event())
            task = asyncio.create_task(channel.segment(b'\0' * 64000, 3))
            while not sent:
                await asyncio.sleep(0)
            message = sent[0]
            self.assertEqual(message['type'], 'reply_http')
            self.assertEqual(message['encoding'], 'ima_adpcm4')
            self.assertEqual(message['samples'], 32000)
            self.assertEqual(message['bytes'], len(uploaded[0][1]))
            channel.acknowledge({'type': 'reply_played', 'reply_id': message['reply_id'],
                                 'completed': True, 'samples': message['samples'], 'gain_divisor': 2})
            result = await task
            self.assertEqual(result['transport'], 'http')
            self.assertEqual(result['segment'], 3)
            self.assertEqual(uploaded[0][0], message['reply_id'])

        asyncio.run(run())


if __name__ == '__main__':
    unittest.main()

import asyncio
from dataclasses import replace
from http.cookies import SimpleCookie
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.config import Settings
from app.main import create_app
from app.errors import XiaoNuoError
from test_core import request_asgi


class ConsoleTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.settings = Settings(root=Path(directory.name), admin_token="offline-admin-" + "x" * 40)
        self.app = create_app(self.settings)

    def test_ticket_is_single_use_and_expires(self):
        sessions = self.app.state.console_sessions
        ticket = sessions.ticket()
        token = sessions.exchange(ticket)
        self.assertTrue(sessions.valid(token))
        with self.assertRaisesRegex(XiaoNuoError, "LOGIN_TICKET_INVALID_OR_EXPIRED"):
            sessions.exchange(ticket)
        ticket = sessions.ticket()
        future = time.time() + 121
        with patch("app.sessions.time.time", return_value=future):
            with self.assertRaisesRegex(XiaoNuoError, "LOGIN_TICKET_INVALID_OR_EXPIRED"):
                sessions.exchange(ticket)

    def test_session_expiration_and_logout(self):
        sessions = self.app.state.console_sessions
        token = sessions.exchange(sessions.ticket())
        with patch("app.sessions.time.time", return_value=time.time() + 9 * 3600):
            self.assertFalse(sessions.valid(token))
        sessions.logout(token)
        self.assertFalse(sessions.valid(token))

    def test_cookie_login_csrf_and_origin_checks(self):
        async def scenario():
            status, ticket = await request_asgi(self.app, "/api/v1/session/bootstrap", token=self.settings.admin_token, method="POST", body={})
            self.assertEqual(status, 200)
            origin = (b"origin", b"http://127.0.0.1:8765")
            status, session, headers = await request_asgi(self.app, "/api/v1/session/exchange", method="POST", body={"ticket": ticket["ticket"]}, extra_headers=[origin], return_headers=True)
            self.assertEqual(status, 200)
            raw_cookie = next(value.decode() for name, value in headers if name == b"set-cookie")
            self.assertIn("HttpOnly", raw_cookie)
            self.assertIn("SameSite=strict", raw_cookie)
            cookies = SimpleCookie()
            cookies.load(raw_cookie)
            token = cookies["xiaonuo_session"].value
            cookie = (b"cookie", ("xiaonuo_session=" + token).encode())
            status, _ = await request_asgi(self.app, "/api/v1/preferences", extra_headers=[cookie])
            self.assertEqual(status, 200)
            status, _ = await request_asgi(self.app, "/api/v1/preferences", method="PATCH", body={"serious": True}, extra_headers=[cookie, origin])
            self.assertEqual(status, 403)
            csrf = (b"x-xiaonuo-csrf", session["csrf"].encode())
            status, _ = await request_asgi(self.app, "/api/v1/preferences", method="PATCH", body={"serious": True}, extra_headers=[cookie, csrf, (b"origin", b"https://untrusted.example")])
            self.assertEqual(status, 403)
            status, data = await request_asgi(self.app, "/api/v1/preferences", method="PATCH", body={"serious": True}, extra_headers=[cookie, origin, csrf])
            self.assertEqual(status, 200)
            self.assertTrue(data["serious"])
            status, _ = await request_asgi(self.app, "/api/v1/session/logout", method="POST", body={}, extra_headers=[cookie, origin, csrf])
            self.assertEqual(status, 200)
            status, _ = await request_asgi(self.app, "/api/v1/preferences", extra_headers=[cookie])
            self.assertEqual(status, 401)
        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()

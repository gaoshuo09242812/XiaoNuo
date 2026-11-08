"""Local console sessions. Provider credentials never enter the browser."""
import hashlib
import hmac
import secrets
import time

from .budgeting import UsageLedger
from .errors import XiaoNuoError


class ConsoleSessions:
    cookie_name = "xiaonuo_session"

    def __init__(self, ledger: UsageLedger, signing_secret: str):
        self.ledger, self.signing_secret = ledger, signing_secret.encode()
        with ledger.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS console_tickets (hash TEXT PRIMARY KEY, expires_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS console_sessions (hash TEXT PRIMARY KEY, expires_at REAL NOT NULL);
            """)

    @staticmethod
    def digest(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def ticket(self) -> str:
        token = secrets.token_urlsafe(32)
        with self.ledger.connect() as db:
            db.execute("DELETE FROM console_tickets WHERE expires_at<=?", (time.time(),))
            db.execute("INSERT INTO console_tickets VALUES (?,?)", (self.digest(token), time.time() + 120))
        return token

    def exchange(self, ticket: str) -> str:
        token = secrets.token_urlsafe(48)
        with self.ledger.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                deleted = db.execute("DELETE FROM console_tickets WHERE hash=? AND expires_at>?", (self.digest(ticket), time.time()))
                if deleted.rowcount != 1:
                    raise XiaoNuoError("LOGIN_TICKET_INVALID_OR_EXPIRED", 401)
                db.execute("DELETE FROM console_sessions WHERE expires_at<=?", (time.time(),))
                db.execute("INSERT INTO console_sessions VALUES (?,?)", (self.digest(token), time.time() + 8 * 3600))
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise
        return token

    def valid(self, token: str) -> bool:
        if not token or len(token) > 200:
            return False
        with self.ledger.connect() as db:
            row = db.execute("SELECT hash FROM console_sessions WHERE hash=? AND expires_at>?", (self.digest(token), time.time())).fetchone()
        return row is not None

    def csrf(self, token: str) -> str:
        return hmac.new(self.signing_secret, ("console-csrf:" + token).encode(), hashlib.sha256).hexdigest()

    def logout(self, token: str):
        with self.ledger.connect() as db:
            db.execute("DELETE FROM console_sessions WHERE hash=?", (self.digest(token),))

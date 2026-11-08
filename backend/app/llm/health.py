import hashlib
import time

from ..budgeting import UsageLedger
from ..config import ProviderSpec


class ProviderHealthMonitor:
    def __init__(self, ledger: UsageLedger):
        self.ledger = ledger

    @staticmethod
    def fingerprint(spec: ProviderSpec) -> str:
        return hashlib.sha256(spec.key.encode()).hexdigest()

    def status(self, spec: ProviderSpec) -> dict:
        with self.ledger.connect() as db:
            row = db.execute("SELECT * FROM provider_status WHERE provider=?", (spec.name,)).fetchone()
        matching = row is not None and row["model"] == spec.model and row["credential_fingerprint"] == self.fingerprint(spec)
        if not matching:
            return {"state": "unverified", "healthy": None, "can_attempt": True, "verified_free": False, "last_error": None}
        can_attempt = row["cooldown_until"] <= time.time()
        verified = row["verified_free_at"] is not None and 0 <= time.time() - row["verified_free_at"] < 86400
        state = "cooldown" if not can_attempt else ("retry_available" if row["last_error"] else ("healthy" if row["last_success"] else "unverified"))
        healthy = False if not can_attempt else (True if state == "healthy" else None)
        return {"state": state, "healthy": healthy, "can_attempt": can_attempt,
                "verified_free": verified, "last_error": row["last_error"]}

    def report(self, spec: ProviderSpec, error: str | None = None, verify_free: bool = False):
        moment = time.time()
        with self.ledger.connect() as db:
            old = db.execute("SELECT * FROM provider_status WHERE provider=?", (spec.name,)).fetchone()
            matching = old is not None and old["model"] == spec.model and old["credential_fingerprint"] == self.fingerprint(spec)
            last_success = (old["last_success"] if matching else None) if error else moment
            verified_at = moment if verify_free and not error else (old["verified_free_at"] if matching else None)
            db.execute("INSERT OR REPLACE INTO provider_status VALUES (?,?,?,?,?,?,?)",
                       (spec.name, spec.model, self.fingerprint(spec), error,
                        moment + 60 if error else 0, last_success, verified_at))

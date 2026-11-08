"""Owner data and explicit-consent memory. No model output writes user facts."""
from datetime import datetime, timedelta, timezone
import json
import re
from uuid import NAMESPACE_URL, uuid4, uuid5

from .budgeting import UsageLedger, now
from .errors import XiaoNuoError

OWNER = "owner"
DEFAULT_CONTROLS = {"reply_length": "normal", "serious": False, "followup_enabled": False,
    "tone_analysis_enabled": True, "speech_enabled": True, "computer_wake_enabled": False,
    "volume": 0.5, "speech_speed": 1.0,'voice_profile':'soft', "greeting_enabled": False,
    "memory_upload_enabled": False, "persist_history": False, "history_retention_days": 7,
    "quiet_hours_start": "22:00", "quiet_hours_end": "08:00", "timezone": "Asia/Shanghai"}


class CompanionStore:
    def __init__(self, ledger: UsageLedger):
        self.ledger = ledger
        with ledger.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS user_profiles (
                    id TEXT PRIMARY KEY, display_name TEXT NOT NULL DEFAULT '',
                    preferences TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS interaction_state (
                    owner_id TEXT PRIMARY KEY, quiet INTEGER NOT NULL DEFAULT 0,
                    reason TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS memories (
                    id TEXT PRIMARY KEY, owner_id TEXT NOT NULL,
                    kind TEXT NOT NULL CHECK(kind IN ('preference','important','goal')),
                    content TEXT NOT NULL, consent INTEGER NOT NULL CHECK(consent=1),
                    allow_remote INTEGER NOT NULL DEFAULT 0, source TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, expires_at TEXT,
                    version INTEGER NOT NULL DEFAULT 1);
                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, updated_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id TEXT NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('user','assistant')), content TEXT NOT NULL,
                    created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS devices (
                    id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, name TEXT NOT NULL,
                    token_hash TEXT NOT NULL, created_at TEXT NOT NULL, last_seen TEXT,
                    state TEXT NOT NULL DEFAULT 'OFFLINE', revoked_at TEXT);
                CREATE TABLE IF NOT EXISTS reminders (
                    id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, text TEXT NOT NULL,
                    due_at TEXT NOT NULL, timezone TEXT NOT NULL, repeat_rule TEXT NOT NULL DEFAULT 'none',
                    quiet_exception INTEGER NOT NULL DEFAULT 0, enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS execution_records (
                    id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, kind TEXT NOT NULL,
                    resource_id TEXT NOT NULL, occurrence_at TEXT NOT NULL, state TEXT NOT NULL,
                    created_at TEXT NOT NULL, UNIQUE(kind, resource_id, occurrence_at));
            """)
            db.execute("INSERT OR IGNORE INTO user_profiles(id,preferences,updated_at) VALUES (?,?,?)",
                       (OWNER, json.dumps(DEFAULT_CONTROLS), now().isoformat()))
            db.execute("INSERT OR IGNORE INTO interaction_state(owner_id,updated_at) VALUES (?,?)", (OWNER, now().isoformat()))
            # Keep the stored schema in sync with the supported controls.
            for row in db.execute("SELECT id,preferences FROM user_profiles").fetchall():
                saved=json.loads(row["preferences"])
                supported={key:value for key,value in saved.items() if key in DEFAULT_CONTROLS}
                if supported!=saved:
                    db.execute("UPDATE user_profiles SET preferences=? WHERE id=?",(json.dumps(supported),row["id"]))

    def controls(self, owner: str = OWNER) -> dict:
        with self.ledger.connect() as db:
            profile = db.execute("SELECT preferences FROM user_profiles WHERE id=?", (owner,)).fetchone()
            state = db.execute("SELECT quiet,reason,updated_at FROM interaction_state WHERE owner_id=?", (owner,)).fetchone()
        settings = {**DEFAULT_CONTROLS, **(json.loads(profile[0]) if profile else {})}
        settings.update(quiet=bool(state["quiet"]) if state else False,
                        quiet_reason=state["reason"] if state else "")
        return settings

    def patch_controls(self, updates: dict, owner: str = OWNER) -> dict:
        bools = {k for k, v in DEFAULT_CONTROLS.items() if type(v) is bool}
        for key, value in updates.items():
            if key not in DEFAULT_CONTROLS:
                raise XiaoNuoError("UNKNOWN_PREFERENCE", 422)
            if key in bools and type(value) is not bool:
                raise XiaoNuoError("INVALID_PREFERENCE", 422)
            if key == "reply_length" and value not in {"brief", "normal", "detailed"}:
                raise XiaoNuoError("INVALID_PREFERENCE", 422)
            if key=='voice_profile' and value not in {'soft','clear','bright'}:
                raise XiaoNuoError('UNKNOWN_VOICE_PROFILE',422)
            if key in {"volume", "speech_speed"}:
                lower, upper = (0, 1) if key == "volume" else (0.7, 1.4)
                if type(value) not in {int, float} or not lower <= value <= upper:
                    raise XiaoNuoError("INVALID_PREFERENCE", 422)
            if key == "history_retention_days" and (type(value) is not int or not 1 <= value <= 30):
                raise XiaoNuoError("INVALID_PREFERENCE", 422)
            if key in {"quiet_hours_start", "quiet_hours_end"} and (not isinstance(value, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value)):
                raise XiaoNuoError("INVALID_PREFERENCE", 422)
            if key == "timezone" and value not in {"Asia/Shanghai", "UTC"}:
                raise XiaoNuoError("TIMEZONE_NOT_YET_SUPPORTED", 422)
        with self.ledger.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                row = db.execute("SELECT preferences FROM user_profiles WHERE id=?", (owner,)).fetchone()
                if row is None:
                    raise XiaoNuoError("OWNER_NOT_FOUND", 404)
                preferences = {**DEFAULT_CONTROLS, **json.loads(row[0]), **updates}
                db.execute("UPDATE user_profiles SET preferences=?,updated_at=? WHERE id=?",
                           (json.dumps(preferences), now().isoformat(), owner))
                if updates.get("persist_history") is False:
                    db.execute("DELETE FROM messages WHERE conversation_id IN (SELECT id FROM conversations WHERE owner_id=?)", (owner,))
                    db.execute("DELETE FROM conversations WHERE owner_id=?", (owner,))
                elif "history_retention_days" in updates:
                    cutoff = (now() - timedelta(days=preferences["history_retention_days"])).isoformat()
                    db.execute("DELETE FROM messages WHERE conversation_id IN (SELECT id FROM conversations WHERE owner_id=?) AND created_at<=?", (owner, cutoff))
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise
        return self.controls(owner)

    def set_quiet(self, enabled: bool, reason: str = "explicit_request", owner: str = OWNER):
        if type(enabled) is not bool:
            raise XiaoNuoError("INVALID_QUIET_STATE", 422)
        with self.ledger.connect() as db:
            db.execute("INSERT INTO interaction_state(owner_id,quiet,reason,updated_at) VALUES (?,?,?,?) "
                       "ON CONFLICT(owner_id) DO UPDATE SET quiet=excluded.quiet,reason=excluded.reason,updated_at=excluded.updated_at",
                       (owner, int(enabled), reason if enabled else "", now().isoformat()))

    def quiet_revision(self, owner: str = OWNER):
        with self.ledger.connect() as db:
            row=db.execute('SELECT quiet,updated_at FROM interaction_state WHERE owner_id=?',(owner,)).fetchone()
        return bool(row[0]),row[1]

    def wake_if_unchanged(self, revision, owner: str = OWNER):
        with self.ledger.connect() as db:
            result=db.execute("UPDATE interaction_state SET quiet=0,reason='explicit_computer_wake',updated_at=? WHERE owner_id=? AND updated_at=?",
                              (now().isoformat(),owner,revision))
            return result.rowcount==1

    def add_memory(self, content: str, *, consent: bool, kind: str = "important", allow_remote: bool = False,
                   retention_days: int = 365, retain_indefinitely: bool = False,
                   source: str = "explicit_setting", owner: str = OWNER, request_id: str | None = None) -> dict:
        if consent is not True:
            raise XiaoNuoError("MEMORY_CONSENT_REQUIRED", 422)
        content = content.strip()
        if not 1 <= len(content) <= 1000 or kind not in {"preference", "important", "goal"} or not 1 <= retention_days <= 3650:
            raise XiaoNuoError("INVALID_MEMORY", 422)
        identifier = str(uuid5(NAMESPACE_URL, f"xiaonuo:{owner}:{request_id}")) if request_id else str(uuid4())
        moment = now()
        expiry = None if retain_indefinitely else (moment + timedelta(days=retention_days)).isoformat()
        with self.ledger.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute("DELETE FROM memories WHERE owner_id=? AND expires_at IS NOT NULL AND expires_at<=?", (owner, moment.isoformat()))
                existing = db.execute("SELECT * FROM memories WHERE id=? AND owner_id=?", (identifier, owner)).fetchone()
                if existing and (existing["content"] != content or existing["kind"] != kind or bool(existing["allow_remote"]) != allow_remote):
                    raise XiaoNuoError("DUPLICATE_MEMORY_REQUEST_CONFLICT", 409)
                if existing is None:
                    count = db.execute("SELECT COUNT(*) FROM memories WHERE owner_id=?", (owner,)).fetchone()[0]
                    if count >= 1000:
                        raise XiaoNuoError("MEMORY_LIMIT_REACHED", 409)
                    db.execute("INSERT INTO memories VALUES (?,?,?,?,1,?,?,?,?,?,1)",
                               (identifier, owner, kind, content, int(allow_remote), source, moment.isoformat(), moment.isoformat(), expiry))
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise
        return self.memory(identifier, owner)

    def memory(self, identifier: str, owner: str = OWNER) -> dict:
        with self.ledger.connect() as db:
            row = db.execute("SELECT * FROM memories WHERE id=? AND owner_id=? AND (expires_at IS NULL OR expires_at>?)",
                             (identifier, owner, now().isoformat())).fetchone()
        if row is None:
            raise XiaoNuoError("MEMORY_NOT_FOUND", 404)
        return self._public_memory(row)

    @staticmethod
    def _public_memory(row) -> dict:
        result = dict(row)
        result.pop("owner_id")
        result["consent"] = bool(result["consent"])
        result["allow_remote"] = bool(result["allow_remote"])
        return result

    def list_memories(self, owner: str = OWNER) -> list[dict]:
        with self.ledger.connect() as db:
            db.execute("DELETE FROM memories WHERE owner_id=? AND expires_at IS NOT NULL AND expires_at<=?", (owner, now().isoformat()))
            rows = db.execute("SELECT * FROM memories WHERE owner_id=? ORDER BY updated_at DESC LIMIT 1000", (owner,)).fetchall()
        return [self._public_memory(row) for row in rows]

    def update_memory(self, identifier: str, content: str, *, consent: bool, expected_version: int,
                      allow_remote: bool, owner: str = OWNER) -> dict:
        if consent is not True:
            raise XiaoNuoError("MEMORY_CONSENT_REQUIRED", 422)
        if not 1 <= len(content.strip()) <= 1000:
            raise XiaoNuoError("INVALID_MEMORY", 422)
        with self.ledger.connect() as db:
            cursor = db.execute("UPDATE memories SET content=?,allow_remote=?,updated_at=?,version=version+1 "
                                "WHERE id=? AND owner_id=? AND version=? AND (expires_at IS NULL OR expires_at>?)",
                                (content.strip(), int(allow_remote), now().isoformat(), identifier, owner, expected_version, now().isoformat()))
            if cursor.rowcount != 1:
                raise XiaoNuoError("MEMORY_NOT_FOUND_OR_STALE", 409)
        return self.memory(identifier, owner)

    def delete_memory(self, identifier: str, owner: str = OWNER):
        with self.ledger.connect() as db:
            cursor = db.execute("DELETE FROM memories WHERE id=? AND owner_id=?", (identifier, owner))
        if cursor.rowcount != 1:
            raise XiaoNuoError("MEMORY_NOT_FOUND", 404)

    @staticmethod
    def terms(text: str) -> set[str]:
        chinese = re.findall(r"[\u4e00-\u9fff]+", text)
        return set(re.findall(r"[a-z0-9_]{2,}", text.lower())) | {s[i:i + 2] for s in chinese for i in range(max(0, len(s) - 1))}

    def retrieve(self, query: str, *, remote: bool, owner: str = OWNER, limit: int = 3) -> list[dict]:
        if remote and not self.controls(owner)["memory_upload_enabled"]:
            return []
        terms = self.terms(query)
        scored = []
        for memory in self.list_memories(owner):
            if remote and not memory["allow_remote"]:
                continue
            score = len(terms & self.terms(memory["content"]))
            if score:
                scored.append((score, memory["updated_at"], memory))
        return [m for _, _, m in sorted(scored, key=lambda item: (item[0], item[1]), reverse=True)[:limit]]

    def load_history(self, conversation: str, owner: str = OWNER) -> list[dict]:
        controls = self.controls(owner)
        if not controls["persist_history"]:
            return []
        with self.ledger.connect() as db:
            row = db.execute("SELECT id FROM conversations WHERE id=? AND owner_id=? AND expires_at>?", (conversation, owner, now().isoformat())).fetchone()
            if row is None:
                return []
            cutoff = (now() - timedelta(days=controls["history_retention_days"])).isoformat()
            rows = db.execute("SELECT role,content FROM messages WHERE conversation_id=? AND created_at>? ORDER BY id DESC LIMIT 12", (conversation, cutoff)).fetchall()
        return [dict(row) for row in reversed(rows)]

    def save_history(self, conversation: str, messages: list[dict], owner: str = OWNER):
        controls = self.controls(owner)
        if not controls["persist_history"]:
            return
        moment = now()
        with self.ledger.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                existing = db.execute("SELECT owner_id FROM conversations WHERE id=?", (conversation,)).fetchone()
                if existing is not None and existing[0] != owner:
                    raise XiaoNuoError("CONVERSATION_NOT_FOUND", 404)
                old = [dict(r) for r in db.execute("SELECT role,content,created_at FROM messages WHERE conversation_id=? ORDER BY id", (conversation,))]
                incoming = messages[-12:]
                overlap = 0
                for size in range(min(len(old), len(incoming)), 0, -1):
                    if [{"role": r["role"], "content": r["content"]} for r in old[-size:]] == incoming[:size]:
                        overlap = size
                        break
                records = old[-overlap:] if overlap else []
                records = records + [{**m, "created_at": moment.isoformat()} for m in incoming[overlap:]]
                cutoff = (moment - timedelta(days=controls["history_retention_days"])).isoformat()
                db.execute("INSERT OR REPLACE INTO conversations VALUES (?,?,?,?)",
                           (conversation, owner, moment.isoformat(), (moment + timedelta(days=controls["history_retention_days"])).isoformat()))
                db.execute("DELETE FROM messages WHERE conversation_id=?", (conversation,))
                db.executemany("INSERT INTO messages(conversation_id,role,content,created_at) VALUES (?,?,?,?)",
                               [(conversation, m["role"], m["content"], m["created_at"]) for m in records if m["created_at"] > cutoff])
                db.execute("DELETE FROM messages WHERE conversation_id IN (SELECT id FROM conversations WHERE expires_at<=?)", (moment.isoformat(),))
                db.execute("DELETE FROM conversations WHERE expires_at<=?", (moment.isoformat(),))
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise

    def delete_history(self, conversation: str, owner: str = OWNER):
        with self.ledger.connect() as db:
            db.execute("DELETE FROM messages WHERE conversation_id IN (SELECT id FROM conversations WHERE id=? AND owner_id=?)", (conversation, owner))
            db.execute("DELETE FROM conversations WHERE id=? AND owner_id=?", (conversation, owner))

    def export(self, owner: str = OWNER) -> dict:
        with self.ledger.connect() as db:
            conversations = [dict(r) for r in db.execute("SELECT id,updated_at,expires_at FROM conversations WHERE owner_id=? AND expires_at>?", (owner, now().isoformat()))]
            reminders = [dict(r) for r in db.execute("SELECT id,text,due_at,timezone,repeat_rule,quiet_exception,enabled FROM reminders WHERE owner_id=?", (owner,))]
            device_fields={'id','name','created_at','last_seen','state','revoked_at','hardware_id','firmware','provisioning_state','audio_feedback','feedback_at'}
            devices=[{key:row[key] for key in device_fields if key in row.keys()} for row in db.execute('SELECT * FROM devices WHERE owner_id=?',(owner,))]
        return {"exported_at": now().isoformat(), "controls": self.controls(owner), "memories": self.list_memories(owner),
                "conversations": [{**c, "messages": self.load_history(c["id"], owner)} for c in conversations], "reminders": reminders,'devices':devices}

    def clear_personal_data(self, owner: str = OWNER):
        with self.ledger.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute("DELETE FROM messages WHERE conversation_id IN (SELECT id FROM conversations WHERE owner_id=?)", (owner,))
                if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='reminder_audio_outbox'").fetchone():
                    db.execute("DELETE FROM reminder_audio_outbox WHERE owner_id=?",(owner,))
                for table in ("conversations", "memories", "devices", "reminders", "execution_records"):
                    db.execute(f"DELETE FROM {table} WHERE owner_id=?", (owner,))
                db.execute("UPDATE user_profiles SET display_name='',preferences=?,updated_at=? WHERE id=?", (json.dumps(DEFAULT_CONTROLS), now().isoformat(), owner))
                db.execute("UPDATE interaction_state SET quiet=0,reason='',updated_at=? WHERE owner_id=?", (now().isoformat(), owner))
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise

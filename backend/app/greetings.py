"""Optional console welcome: only on a visit, at most once per local day."""
from datetime import timezone
from uuid import uuid4

from .behavior import QuietMode
from .budgeting import SHANGHAI,now
from .storage import OWNER


class GreetingService:
    def __init__(self,store,clock=now):
        self.store,self.clock=store,clock

    def visit(self):
        at=self.clock()
        with self.store.ledger.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                controls=self.store.controls()
                if not QuietMode(self.store).allows_proactive('greeting',at=at):
                    db.execute('COMMIT')
                    return {'text':None,'reason':'disabled_or_quiet'}
                zone=SHANGHAI if controls['timezone']=='Asia/Shanghai' else timezone.utc
                day=at.astimezone(zone).date().isoformat()
                result=db.execute("INSERT OR IGNORE INTO execution_records(id,owner_id,kind,resource_id,occurrence_at,state,created_at) VALUES (?,?,'greeting',?,?,'offered',?)",
                                  (str(uuid4()),OWNER,OWNER,day,at.isoformat()))
                created=result.rowcount==1
                db.execute('COMMIT')
            except BaseException:
                db.execute('ROLLBACK')
                raise
        if not created:return {'text':None,'reason':'already_offered_today'}
        if not QuietMode(self.store).allows_proactive('greeting',at=at):
            return {'text':None,'reason':'disabled_or_quiet'}
        return {'text':'你好，我是小糯。想聊的时候，我在。','reason':'console_visit','delivery':'console','automatic_audio':False}

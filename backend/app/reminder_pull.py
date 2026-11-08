"""Serve one device-initiated reminder request on an authenticated idle session."""
import asyncio
from uuid import NAMESPACE_URL,uuid5

from .errors import XiaoNuoError
from .network_reply import ReplyChannel

async def deliver(session,request):
    outbox=session.handler.reminder_audio
    claim=None;channel=None;settled=False
    try:
        session.authorize()
        claim=outbox.claim(session.identifier)
        if claim is None:
            await session.send({'type':'reminder_empty','request_id':request});return
        event,token=claim['event_id'],claim['claim_token']
        def guard():
            session.guard()
            if not outbox.allowed(event,token):raise XiaoNuoError('REMINDER_AUDIO_STALE_OR_BLOCKED',409)
        guard()
        identifier=str(uuid5(NAMESPACE_URL,f'xiaonuo:reminder-pull:{session.identifier}:{request}'))
        session.registry.store.ledger.claim_request(identifier);session.claim_id=identifier
        async with asyncio.timeout(90):
            pcm=await session.handler.prepare_reminder(claim['text'],request,session.cancelled)
        guard()
        if not isinstance(pcm,bytes) or not pcm or len(pcm)%2 or len(pcm)>60*32000:
            raise XiaoNuoError('REMINDER_AUDIO_INVALID',422)
        outbox.start(event,token,len(pcm)//2)
        guard()
        await session.send({'type':'audio_result','stream_id':request,'transcript':'','reply_text':'提醒时间到了。',
                            'reply_audio_enabled':True,'playback_complete':False})
        channel=ReplyChannel(session.send,session.send_bytes,guard,session.cancelled,compressed=True,max_seconds=60)
        session.reply_channel=channel
        await channel.segment(pcm,0)
        guard()
        if outbox.finish(event,token,completed=True,played_samples=len(pcm)//2)!='played':
            raise XiaoNuoError('REMINDER_AUDIO_COMPLETION_UNCERTAIN',409)
        settled=True;session._finish_claim('complete')
        await session.send({'type':'audio_complete','stream_id':request,'segments':1,
                            'playback_complete':True,'audible_verified':False})
    except asyncio.CancelledError:raise
    except Exception:
        try:await session.send({'type':'audio_error','stream_id':request,'error':'REMINDER_AUDIO_FAILED'})
        except Exception:pass
    finally:
        if channel is not None:
            if channel.active_id:await channel.abort()
            if session.reply_channel is channel:session.reply_channel=None
        if claim is not None and not settled:outbox.abandon(claim['event_id'],claim['claim_token'])
        session._finish_claim('failed_or_uncertain');session.stream_id=None

"""Opt-in authenticated upload lifecycle; production gateways default to off."""
import asyncio
import re
import threading
import math
import json
import time
from datetime import datetime,timezone
from pathlib import Path
from array import array
from uuid import NAMESPACE_URL, UUID, uuid5

from .errors import XiaoNuoError
from .network_audio import AudioReceiver
from .network_reply import ReplyChannel
from .network_reply_http import HttpReplyChannel
from .computer_wake import reset_wake_context
from .expressions import safe_expression


class NetworkAudioSession:
    def __init__(self,registry,identifier,authorize,send,handler,*,send_bytes=None,reply_enabled=False,compressed_upload=False,compressed_reply=False,http_reply=None,reply_streaming=False,reply_buffered=False,reply_pipelined=False,reply_reminders=False):
        self.registry,self.identifier=registry,identifier
        self.authorize,self.send,self.handler=authorize,send,handler
        self.receiver=None
        self.claim_id=None
        self.stream_id=None
        self.processing=None
        self.cancelled=threading.Event()
        self.send_bytes=send_bytes
        self.reply_enabled=reply_enabled
        self.compressed_upload=compressed_upload
        self.compressed_reply=compressed_reply
        self.http_reply=http_reply
        self.reply_streaming=reply_streaming and compressed_reply and http_reply is None
        self.reply_buffered=reply_buffered and compressed_reply and http_reply is None and not reply_streaming
        self.reply_pipelined=reply_pipelined and self.reply_buffered
        self.reply_reminders=bool(reply_reminders and self.reply_buffered and getattr(handler,'reminder_audio',None) is not None and callable(getattr(handler,'prepare_reminder',None)))
        self._last_reminder_poll=None
        self.reply_channel=None
        self.trigger='manual'

    def guard(self,*,allow_quiet_probe=False):
        self.authorize()
        if self.registry.store.controls()['quiet'] and not allow_quiet_probe:
            raise XiaoNuoError('SPEECH_DISABLED_BY_OWNER',409)

    def _finish_claim(self,state):
        if self.claim_id is not None:
            self.registry.store.ledger.finish_request(self.claim_id,state)
            self.claim_id=None

    async def handle(self,message):
        kind=message.get('type') if isinstance(message,dict) else None
        proposed_wake=kind=='audio_begin' and message.get('trigger')=='wake'
        allowed_wake=self.reply_enabled and getattr(self.handler,'wake_enabled',False) is True
        active_wake=proposed_wake if kind=='audio_begin' else self.trigger=='wake' and self.claim_id is not None
        self.guard(allow_quiet_probe=(allowed_wake and active_wake and self.reply_channel is None) or (kind=='reminder_pull' and self.reply_reminders))
        if isinstance(message,bytes):
            if self.receiver is None:raise XiaoNuoError('NETWORK_AUDIO_NOT_RECEIVING',409)
            self.receiver.push(message)
            return
        kind=message.get('type')
        if kind in {'reply_ready','reply_ack','reply_played'}:
            if self.reply_channel is None:raise XiaoNuoError('NETWORK_REPLY_INVALID_ACK',422)
            self.reply_channel.acknowledge(message)
        elif kind=='reminder_pull':
            if not self.reply_reminders or set(message)!={'type','request_id'}:
                raise XiaoNuoError('REMINDER_PULL_UNSUPPORTED',422)
            try:request=str(UUID(message['request_id']))
            except (ValueError,TypeError,AttributeError):raise XiaoNuoError('REMINDER_PULL_INVALID',422) from None
            if self.receiver is not None or self.claim_id is not None or (self.processing is not None and not self.processing.done()):
                raise XiaoNuoError('NETWORK_AUDIO_BUSY',409)
            moment=time.monotonic()
            if self._last_reminder_poll is not None and moment-self._last_reminder_poll<10:
                await self.send({'type':'reminder_empty','request_id':request});return
            self._last_reminder_poll=moment
            from .reminder_pull import deliver
            self.cancelled=threading.Event();self.stream_id=request;self.trigger='manual'
            self.processing=asyncio.create_task(deliver(self,request))
        elif kind=='reply_probe':
            if (set(message)!={'type','request_id','seconds'} or type(message['seconds']) is not int
                    or not 1<=message['seconds']<=45 or not self.compressed_reply):
                raise XiaoNuoError('NETWORK_REPLY_PROBE_INVALID',422)
            try:request=str(UUID(message['request_id']))
            except (ValueError,TypeError,AttributeError):
                raise XiaoNuoError('NETWORK_REPLY_PROBE_INVALID',422) from None
            if self.claim_id is not None or (self.processing is not None and not self.processing.done()):
                raise XiaoNuoError('NETWORK_AUDIO_BUSY',409)
            controls=self.registry.store.controls()
            if not controls['speech_enabled'] or controls['volume']<=0:
                raise XiaoNuoError('SPEECH_DISABLED_BY_OWNER',409)
            claim=str(uuid5(NAMESPACE_URL,f'xiaonuo:reply-probe:{self.identifier}:{request}'))
            self.registry.store.ledger.claim_request(claim)
            self.claim_id=claim;self.stream_id=request;self.trigger='manual'
            self.cancelled=threading.Event()
            async def tone():
                amplitude=int(12000*controls['volume'])
                block=array('h',(int(amplitude*math.sin(2*math.pi*440*i/16000)) for i in range(16000))).tobytes()
                for _ in range(message['seconds']):yield block
            prepared={'transcript':'','reply_text':'固定测试音。','_audio':tone()}
            self.processing=asyncio.create_task(self._process(b'',claim,request,self.cancelled,prepared_result=prepared))
        elif kind=='reply_resume':
            if self.reply_buffered:raise XiaoNuoError('NETWORK_REPLY_RESUME_UNAVAILABLE',409)
            reply_id=message.get('reply_id')
            if not isinstance(reply_id,str) or len(reply_id)!=36:
                raise XiaoNuoError('NETWORK_REPLY_RESUME_INVALID',422)
            resume=getattr(self.handler,'resume_reply',None)
            pcm=resume(reply_id) if resume else None
            if pcm is None:
                raise XiaoNuoError('NETWORK_REPLY_RESUME_UNKNOWN',404)
            self.guard()
            channel=ReplyChannel(self.send,self.send_bytes,self.guard,self.cancelled,compressed=self.compressed_reply)
            self.reply_channel=channel
            self.processing=asyncio.create_task(self._resume_reply(channel,pcm))
            return
        elif kind=='audio_begin':
            if self.claim_id is not None:raise XiaoNuoError('NETWORK_AUDIO_BUSY',409)
            fields=set(message)
            codec_fields={'encoding','predictor','step_index'}
            codec={}
            if fields & codec_fields:
                if not self.compressed_upload or not codec_fields<=fields or message.get('encoding')!='ima_adpcm4':
                    raise XiaoNuoError('NETWORK_AUDIO_INVALID_ENCODING',422)
                codec={key:message[key] for key in codec_fields}
                fields-=codec_fields
            if proposed_wake:
                if (fields!={'type','request_id','seconds','consent','trigger'}
                        or message['consent']!='owner-computer-wake' or not allowed_wake):
                    raise XiaoNuoError('COMPUTER_WAKE_CONSENT_REQUIRED',422)
                self.trigger='wake'
            elif fields=={'type','request_id','seconds','consent'} and message['consent']=='owner-computer':
                self.trigger='manual'
            else:raise XiaoNuoError('NETWORK_AUDIO_CONSENT_REQUIRED',422)
            candidate=AudioReceiver(message['request_id'],message['seconds'],**codec,
                                    allow_trim_end=bool(proposed_wake and codec.get('encoding')=='ima_adpcm4'))
            # A UUID cannot collide with USB/LLM/TTS requests or another board.
            claim=str(uuid5(NAMESPACE_URL,f'xiaonuo:network-audio:{self.identifier}:{candidate.request_id}'))
            self.registry.store.ledger.claim_request(claim)
            self.claim_id=claim
            self.receiver=candidate
            self.stream_id=str(candidate.stream_id)
            self.cancelled=threading.Event()
            await self.send({'type':'audio_ready','request_id':candidate.request_id,**candidate.snapshot()})
        elif kind=='audio_end':
            if (set(message) not in ({'type','stream_id','sha256'},{'type','stream_id','sha256','samples'})
                    or message['stream_id']!=self.stream_id or self.receiver is None
                    or ('samples' in message and type(message['samples']) is not int)):
                raise XiaoNuoError('NETWORK_AUDIO_INVALID_END',422)
            pcm=self.receiver.finish(message['sha256'],samples=message.get('samples'))
            self.receiver=None
            # Processing runs separately so pings, cancellation and disconnects
            # remain responsive. The handler must honor its cancellation event.
            await self.send({'type':'audio_processing','stream_id':self.stream_id})
            self.processing=asyncio.create_task(self._process(pcm,self.claim_id,self.stream_id,self.cancelled,self.trigger))
        elif kind=='audio_cancel':
            if set(message)!={'type','stream_id'} or self.claim_id is None or message['stream_id']!=self.stream_id:
                raise XiaoNuoError('NETWORK_AUDIO_INVALID_CANCEL',422)
            stream=self.stream_id
            await self.close()
            await self.send({'type':'audio_cancelled','stream_id':stream})
        else:
            raise XiaoNuoError('NETWORK_AUDIO_INVALID_MESSAGE',422)

    @staticmethod
    async def _coalesce_audio(source, max_bytes=8*32000):
        """Join or split PCM without loss, within the transport's segment limit."""
        if type(max_bytes) is not int or max_bytes<=0 or max_bytes%2:
            raise ValueError('PCM segment limit must be positive and sample aligned')
        buffer=bytearray()
        async for piece in source:
            if not piece:
                continue
            if not isinstance(piece,bytes) or len(piece)%2:
                raise XiaoNuoError('NETWORK_REPLY_AUDIO_INVALID',422)
            offset=0
            while offset<len(piece):
                count=min(max_bytes-len(buffer),len(piece)-offset)
                buffer.extend(memoryview(piece)[offset:offset+count]);offset+=count
                if len(buffer)==max_bytes:
                    yield bytes(buffer);buffer.clear()
        if buffer:
            yield bytes(buffer)

    async def _resume_reply(self,channel,pcm):
        try:
            await channel.segment(pcm,0)
        except Exception:
            self.cancelled.set()
            raise
        finally:
            if self.reply_channel is channel:
                self.reply_channel=None

    async def _process(self,pcm,claim,stream,cancelled,trigger='manual',*,prepared_result=None):
        source=None
        result=None
        channel=None
        audit={'time':datetime.now(timezone.utc).isoformat(),'source_samples':0,
               'input_samples':len(pcm)//2,
               'planned_samples':0,'played_samples':0,'segments_completed':0,
               'source_exhausted':False,'complete':False,'error_code':None,
               'synthetic':prepared_result is not None}
        started=time.monotonic()
        try:
            self.guard(allow_quiet_probe=trigger=='wake')
            async with asyncio.timeout(60):
                if prepared_result is not None:result=prepared_result
                elif trigger=='wake':result=await self.handler.wake_probe(pcm,claim,self.identifier,cancelled)
                else:result=await self.handler(pcm,claim,self.identifier,cancelled)
            audit['handler_ms']=round((time.monotonic()-started)*1000)
            if cancelled.is_set():return
            if not isinstance(result,dict):raise XiaoNuoError('NETWORK_AUDIO_PROCESSOR_ERROR')
            wake={}
            if trigger=='wake':
                status=result.get('_wake_status');followup=result.get('_followup_seconds')
                if status not in {'ignored','accepted','listening','cancelled','quiet'} or type(followup) is not int or followup not in {0,20}:
                    raise XiaoNuoError('NETWORK_AUDIO_PROCESSOR_ERROR')
                wake={'wake_status':status,'followup_seconds':followup,'quiet':self.registry.store.controls()['quiet']}
                if status in {'ignored','cancelled','quiet'} and (result.get('transcript') or result.get('reply_text') or result.get('_audio')):
                    raise XiaoNuoError('NETWORK_AUDIO_PROCESSOR_ERROR')
            self.guard(allow_quiet_probe=wake.get('wake_status') in {'ignored','cancelled','quiet'})
            # Never reflect arbitrary handler fields, credentials or raw audio.
            public={}
            for name,maximum in (('transcript',500),('reply_text',1000)):
                value=result.get(name,'')
                if not isinstance(value,str) or len(value)>maximum:
                    raise XiaoNuoError('NETWORK_AUDIO_PROCESSOR_ERROR')
                public[name]=value
            source=result.get('_audio')
            # Additive metadata: legacy devices may ignore it. Only fixed names
            # from deterministic presentation rules cross the device boundary.
            public['expression']=safe_expression(result.get('_expression'))
            audit['reply_characters']=len(public['reply_text'])
            flags=result.get('_voice_probe',{})
            if isinstance(flags,dict):
                safe={k:flags[k] for k in ('story_request_seen','thirty_seconds_seen','ending_request_seen','ending_reply_seen')
                      if type(flags.get(k)) is bool}
                safe.update({k:flags[k] for k in ('model_characters','styled_characters')
                             if type(flags.get(k)) is int and 0<=flags[k]<=100000})
                if safe:audit['voice_probe']=safe

            enabled=self.reply_enabled and source is not None and self.send_bytes is not None
            if source is not None and not hasattr(source,'__aiter__'):
                raise XiaoNuoError('NETWORK_AUDIO_PROCESSOR_ERROR')
            await self.send({'type':'audio_result','stream_id':stream,**public,
                             'reply_audio_enabled':enabled,'playback_complete':False,**wake})
            if enabled:
                channel=None
                count=0
                async def measured_source():
                    waiting=time.monotonic()
                    async for piece in source:
                        audit['audio_source_wait_ms']=audit.get('audio_source_wait_ms',0)+round((time.monotonic()-waiting)*1000)
                        if isinstance(piece,bytes):audit['source_samples']+=len(piece)//2
                        yield piece
                        waiting=time.monotonic()
                    audit['audio_source_wait_ms']=audit.get('audio_source_wait_ms',0)+round((time.monotonic()-waiting)*1000)
                    audit['source_exhausted']=True
                async with asyncio.timeout(600 if self.reply_buffered else 180):
                    # Protocol 5 holds a complete <=32KB ADPCM reply in its
                    # negotiated 32-frame window before I2S starts. Longer
                    # segments used to start after 2 seconds and starve I2S.
                    segment_limit=(60 if self.reply_buffered else 20 if self.reply_streaming else 4 if self.compressed_reply and (self.http_reply is None or prepared_result is not None) else 8)*32000
                    async for piece in self._coalesce_audio(measured_source(),max_bytes=segment_limit):
                        if count>=128:raise XiaoNuoError('NETWORK_REPLY_TOO_MANY_SEGMENTS',422)
                        if channel is None:
                            if self.http_reply is not None and prepared_result is None and len(piece)>3*32000:
                                channel=HttpReplyChannel(self.send,self.http_reply,self.guard,cancelled)
                            else:
                                channel=ReplyChannel(self.send,self.send_bytes,self.guard,cancelled,compressed=self.compressed_reply,max_seconds=60 if self.reply_buffered else 20 if self.reply_streaming else 8,allow_pipeline=self.reply_pipelined)
                            self.reply_channel=channel
                        audit['planned_samples']+=len(piece)//2
                        try:
                            await channel.segment(piece,count)
                        finally:
                            for key,value in getattr(channel,'last_timing',{}).items():
                                if key in {'encode_ms','wire_bytes','ready_ack_ms','transfer_ms','playback_ack_ms','guard_ms','send_ms','window_ack_ms','pipeline_windows'} and type(value) is int and value>=0:
                                    field='reply_'+key
                                    audit[field]=audit.get(field,0)+value
                        audit['played_samples']+=len(piece)//2
                        audit['segments_completed']+=1
                        remember=getattr(self.handler,'remember_reply',None)
                        if remember is not None and channel.last_reply_id:
                            remember(channel.last_reply_id,piece,count)
                        count+=1
                self.guard()
                await self.send({'type':'audio_complete','stream_id':stream,'segments':count,
                                 'playback_complete':count>0,'audible_verified':False,**wake})
                self.reply_channel=None
            audit['complete']=True
            self._finish_claim('complete')
        except asyncio.CancelledError:
            audit['error_code']='CANCELLED'
            cancelled.set()
            raise
        except Exception as error:
            audit['error_code']=error.code if isinstance(error,XiaoNuoError) and re.fullmatch('[A-Z_]{1,60}',error.code) else 'NETWORK_AUDIO_PROCESSOR_ERROR'
            if self.reply_channel is not None:await self.reply_channel.abort()
            if not cancelled.is_set():
                code=error.code if isinstance(error,XiaoNuoError) and re.fullmatch('[A-Z_]{1,60}',error.code) else 'NETWORK_AUDIO_PROCESSOR_ERROR'
                try:await self.send({'type':'audio_error','stream_id':stream,'error':code})
                except Exception:pass
        finally:
            del pcm
            try:
                if source is not None and hasattr(source,'aclose'):
                    try:await asyncio.wait_for(source.aclose(),1)
                    except Exception:pass
            finally:
                if self.reply_channel is channel:self.reply_channel=None
                # close() waits before another claim is allowed on this session.
                if self.claim_id==claim:self._finish_claim('failed_or_uncertain')
                if source is not None:
                    # Only numerical lifecycle metrics; never audio, text, IDs
                    # or exception messages. Temporary test stores stay isolated.
                    audit['elapsed_ms']=round((time.monotonic()-started)*1000)
                    if isinstance(result,dict) and isinstance(result.get('_audio_metrics'),dict):
                        for key,value in result['_audio_metrics'].items():
                            if key in {'tts_ms','pcm_conversion_ms','tts_calls'} and type(value) is int and value>=0:
                                audit[key]=value
                    try:
                        directory=Path(self.registry.store.ledger.path).parent/'diagnostics'
                        directory.mkdir(parents=True,exist_ok=True)
                        with (directory/'reply-lifecycle.jsonl').open('a',encoding='utf-8') as log:
                            log.write(json.dumps(audit,separators=(',',':'))+'\n')
                    except (OSError,AttributeError,TypeError):pass

    async def close(self):
        self.cancelled.set()
        await reset_wake_context(self.handler,self.identifier)
        channel=self.reply_channel
        if self.receiver is not None:
            self.receiver.cancel()
            self.receiver=None
        task=self.processing
        if task is not None and not task.done():
            task.cancel()
            done,_=await asyncio.wait({task},timeout=1)
            if not done:
                task.add_done_callback(lambda t:None if t.cancelled() else t.exception())
        self.processing=None
        if channel is not None:
            await channel.abort()
            self.reply_channel=None
        self._finish_claim('failed_or_uncertain')
        self.stream_id=None

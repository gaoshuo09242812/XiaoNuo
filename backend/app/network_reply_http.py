"""Long-reply audio over HTTPS Range instead of the long WebSocket."""
import asyncio
import hashlib
import urllib.request
from uuid import uuid4

from .errors import XiaoNuoError
from .network_reply import ReplyChannel
from .adpcm import ENCODING, encode as encode_adpcm


def make_http_reply_uploader(base_url,host_token,device_hash,*,timeout=8):
    async def uploader(reply_id,data,digest):
        await upload_reply(base_url,host_token,device_hash,reply_id,data,timeout=timeout)
    return uploader


async def upload_reply(base_url,host_token,device_hash,reply_id,data,*,timeout=8):
    """Upload one compressed reply; the device downloads it with Range."""
    if not isinstance(data,(bytes,bytearray)) or not data or len(data)>120*1024:
        raise XiaoNuoError('NETWORK_REPLY_HTTP_SIZE',422)
    url=base_url.rstrip('/')+f'/host/{device_hash}/reply/{reply_id}'
    request=urllib.request.Request(url,data=bytes(data),method='POST',headers={
        'Authorization':'Bearer '+host_token,
        'Content-Type':'application/octet-stream',
        'User-Agent':'XiaoNuo-Host/1.0',
        'X-XiaoNuo-SHA256':hashlib.sha256(data).hexdigest(),
    })
    def send():
        with urllib.request.urlopen(request,timeout=timeout) as response:
            return response.status
    try:
        status=await asyncio.to_thread(send)
    except Exception as error:
        raise XiaoNuoError('NETWORK_REPLY_HTTP_UPLOAD_FAILED',502) from error
    if status!=201:
        raise XiaoNuoError('NETWORK_REPLY_HTTP_UPLOAD_FAILED',502)


class HttpReplyChannel(ReplyChannel):
    """One segment = upload once, then a tiny WebSocket control message."""

    def __init__(self,send,upload,guard,cancelled,*,max_seconds=8):
        super().__init__(send,None,guard,cancelled,compressed=True)
        self.upload=upload
        self.max_seconds=max_seconds

    async def segment(self,pcm,index):
        if not isinstance(pcm,bytes) or not pcm or len(pcm)%2 or len(pcm)>self.max_seconds*32000:
            raise XiaoNuoError('NETWORK_REPLY_AUDIO_INVALID',422)
        self.check()
        reply_id=str(uuid4())
        self.active_id=reply_id
        self.last_reply_id=reply_id
        samples=len(pcm)//2
        wire,_=encode_adpcm(pcm,0,0)
        digest=hashlib.sha256(wire).hexdigest()
        try:
            async with asyncio.timeout(45):
                await self.upload(reply_id,wire,digest)
                self.prepare({'type':'reply_played','reply_id':reply_id,'completed':True,
                              'samples':samples,'gain_divisor':2})
                await self.send({'type':'reply_http','reply_id':reply_id,'bytes':len(wire),
                                 'sha256':digest,'sample_rate':16000,'encoding':ENCODING,
                                 'samples':samples,'gain_divisor':2,'max_seconds':self.max_seconds})
                await self.wait(40)
            self.active_id=None
            return {'segment':index,'duration_seconds':samples/16000,
                    'device_completed':True,'audible_verified':False,'transport':'http'}
        except TimeoutError:
            raise XiaoNuoError('NETWORK_REPLY_HTTP_SEGMENT_TIMEOUT',408) from None

"""Bounded in-memory audio framing for a future authenticated device session.

Used by the opt-in authenticated upload session. Callers must authenticate and
claim a unique request before accepting audio; a new stream ID isolates reconnects.
"""
import hashlib
import hmac
import re
import struct
import time
from uuid import UUID, uuid4

from .errors import XiaoNuoError
from .adpcm import ENCODING, decode as decode_adpcm, validate_state

HEADER=struct.Struct('!4s16sI')
MAGIC=b'XNA1'
MAX_FRAME_BYTES=1024
SAMPLE_RATE=16000


def encode_audio_frame(stream_id, sequence, pcm, *, compressed=False):
    """PCM is sample aligned; ADPCM may end with an odd number of bytes."""
    if type(sequence) is not int or not 0<=sequence<=0xffffffff:
        raise XiaoNuoError('NETWORK_AUDIO_INVALID_SEQUENCE',422)
    if (type(compressed) is not bool or not isinstance(pcm,(bytes,bytearray)) or not pcm
            or (len(pcm)%2 and not compressed) or len(pcm)>MAX_FRAME_BYTES-HEADER.size):
        raise XiaoNuoError('NETWORK_AUDIO_INVALID_FRAME',422)
    try:
        identifier=UUID(str(stream_id))
    except (ValueError,TypeError,AttributeError):
        raise XiaoNuoError('NETWORK_AUDIO_INVALID_STREAM',422) from None
    return HEADER.pack(MAGIC,identifier.bytes,sequence)+pcm


class AudioReceiver:
    """One exact 2–6 second recording; bounded transfer deadline.

    On an invalid frame, timeout or cancellation, buffered audio is discarded.
    Completion transfers ownership once; it never writes audio to disk.
    """
    def __init__(self, request_id, seconds, *, clock=time.monotonic, encoding="s16le", predictor=0, step_index=0, allow_trim_end=False):
        try:
            self.request_id=str(UUID(str(request_id)))
        except (ValueError,TypeError,AttributeError):
            raise XiaoNuoError('NETWORK_AUDIO_INVALID_REQUEST',422) from None
        if type(seconds) is not int or not 2<=seconds<=6:
            raise XiaoNuoError('NETWORK_AUDIO_INVALID_DURATION',422)
        if type(encoding) is not str or encoding not in {'s16le', ENCODING}:
            raise XiaoNuoError('NETWORK_AUDIO_INVALID_ENCODING',422)
        validate_state(predictor,step_index)
        if encoding=='s16le' and (predictor or step_index):
            raise XiaoNuoError('NETWORK_AUDIO_INVALID_ADPCM_STATE',422)
        self.encoding=encoding
        if type(allow_trim_end) is not bool or (allow_trim_end and encoding!=ENCODING):
            raise XiaoNuoError('NETWORK_AUDIO_INVALID_ENCODING',422)
        self.allow_trim_end=allow_trim_end
        self.max_samples=seconds*SAMPLE_RATE
        self.initial_state=(predictor,step_index)
        self.stream_id=uuid4()
        self.expected_bytes=seconds*SAMPLE_RATE*2 if encoding=='s16le' else seconds*SAMPLE_RATE//2
        self._clock=clock
        # Mobile ADPCM peers can spend several seconds awaiting audio_ready.
        # This increases transfer grace, never the recording/sample limit.
        self._deadline=clock()+(30 if encoding==ENCODING else 15)
        self._pcm=bytearray()
        self._next=0
        self._state='receiving'

    def _clear(self):
        # Best-effort overwrite before releasing this mutable buffer. Python
        # and transport copies are outside this object's ownership.
        self._pcm[:]=b'\0'*len(self._pcm)
        self._pcm.clear()

    def _fail(self,code,status=422):
        self._clear()
        self._state='failed'
        raise XiaoNuoError(code,status)

    def _check(self):
        if self._state!='receiving':
            raise XiaoNuoError('NETWORK_AUDIO_STREAM_CLOSED',409)
        if self._clock()>=self._deadline:
            self._fail('NETWORK_AUDIO_TIMEOUT',408)

    def push(self,frame):
        self._check()
        if (not isinstance(frame,(bytes,bytearray)) or len(frame)<=HEADER.size
                or len(frame)>MAX_FRAME_BYTES or (self.encoding=='s16le' and (len(frame)-HEADER.size)%2)):
            self._fail('NETWORK_AUDIO_INVALID_FRAME')
        magic,stream,sequence=HEADER.unpack_from(frame)
        if magic!=MAGIC or stream!=self.stream_id.bytes:
            self._fail('NETWORK_AUDIO_INVALID_STREAM')
        if sequence!=self._next:
            self._fail('NETWORK_AUDIO_INVALID_SEQUENCE')
        size=len(frame)-HEADER.size
        if len(self._pcm)+size>self.expected_bytes:
            self._fail('NETWORK_AUDIO_TOO_LONG')
        self._pcm.extend(memoryview(frame)[HEADER.size:])
        self._next+=1
        return self.snapshot()

    def remaining_seconds(self):
        self._check()
        return max(0.001,self._deadline-self._clock())

    def finish(self,sha256,*,samples=None):
        self._check()
        expected=self.expected_bytes
        if samples is not None:
            if (not self.allow_trim_end or type(samples) is not int or samples%2
                    or not 2*SAMPLE_RATE<=samples<=self.max_samples):
                self._fail('NETWORK_AUDIO_INVALID_TRIM')
            expected=samples//2
        if len(self._pcm)!=expected:
            self._fail('NETWORK_AUDIO_INCOMPLETE')
        if (not isinstance(sha256,str) or not re.fullmatch('[0-9a-f]{64}',sha256)
                or not hmac.compare_digest(hashlib.sha256(self._pcm).hexdigest(),sha256)):
            self._fail('NETWORK_AUDIO_CHECKSUM_MISMATCH')
        result=bytes(self._pcm) if self.encoding=='s16le' else decode_adpcm(self._pcm,*self.initial_state)[0]
        self._clear()
        self._state='completed'
        return result

    def cancel(self):
        self._clear()
        if self._state=='receiving':self._state='cancelled'

    def snapshot(self):
        return {'stream_id':str(self.stream_id),'state':self._state,
                'received_bytes':len(self._pcm),'expected_bytes':self.expected_bytes,
                'next_sequence':self._next,'max_frame_bytes':MAX_FRAME_BYTES,
                'sample_rate':SAMPLE_RATE,'channels':1,'format':self.encoding,
                **({'trim_end_enabled':True} if self.allow_trim_end else {}),
                **({'predictor':self.initial_state[0],'step_index':self.initial_state[1]} if self.encoding==ENCODING else {})}

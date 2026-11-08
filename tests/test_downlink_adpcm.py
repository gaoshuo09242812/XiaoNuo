import asyncio
import hashlib
import threading
import unittest

from app.adpcm import ENCODING, decode, encode
from app.network_reply import ReplyChannel


class DownlinkAdpcmTests(unittest.TestCase):
    def test_compressed_reply_wire_hash_samples_and_decoder(self):
        async def run():
            pcm = b''.join(int(i % 4000 - 2000).to_bytes(2, 'little', signed=True)
                           for i in range(32000))
            messages, frames = [], []
            cancelled = threading.Event()

            async def send(message):
                messages.append(message)
                if message['type'] == 'reply_start':
                    channel.acknowledge({'type': 'reply_ready', 'reply_id': message['reply_id'],
                                         'sample_rate': 16000, 'gain_divisor': 2,
                                         'max_seconds': 8, 'window_frames': 16})
                elif message['type'] == 'reply_end':
                    channel.acknowledge({'type': 'reply_played', 'reply_id': message['reply_id'],
                                         'completed': True, 'samples': len(pcm) // 2,
                                         'gain_divisor': 2})

            async def binary(frame):
                frames.append(frame)
                if len(frames) % 16 == 0:
                    channel.acknowledge({'type': 'reply_ack', 'reply_id': channel.active_id,
                                         'next_sequence': len(frames),
                                         'received_bytes': sum(len(item) - 24 for item in frames)})

            channel = ReplyChannel(send, binary, lambda: None, cancelled, compressed=True)
            result = await channel.segment(pcm, 0)
            wire = b''.join(frame[24:] for frame in frames)
            start = messages[0]
            self.assertEqual((start['format'], start['samples'], start['predictor'], start['step_index']),
                             (ENCODING, 32000, 0, 0))
            self.assertEqual(start['bytes'], len(wire))
            self.assertEqual(wire, encode(pcm, 0, 0)[0])
            self.assertEqual(hashlib.sha256(wire).hexdigest(), messages[-1]['sha256'])
            self.assertEqual(len(decode(wire, 0, 0)[0]), len(pcm))
            self.assertEqual(result['duration_seconds'], 2.0)
            self.assertFalse(result['audible_verified'])

        asyncio.run(run())


if __name__ == '__main__':
    unittest.main()

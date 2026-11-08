import unittest
from collections import OrderedDict

from app.network_speech import LocalNetworkSpeech


class ReplyResumeTests(unittest.TestCase):
    def test_remember_and_resume_reply(self):
        speech = object.__new__(LocalNetworkSpeech)
        speech._reply_cache = OrderedDict()
        speech._reply_cache_bytes = 0
        speech.remember_reply('11111111-1111-1111-1111-111111111111', b'abc', 0)
        self.assertEqual(
            speech.resume_reply('11111111-1111-1111-1111-111111111111'), b'abc')
        self.assertIsNone(speech.resume_reply('22222222-2222-2222-2222-222222222222'))

    def test_cache_is_bounded_and_keeps_latest(self):
        speech = object.__new__(LocalNetworkSpeech)
        speech._reply_cache = OrderedDict()
        speech._reply_cache_bytes = 0
        for index in range(10):
            speech.remember_reply(f'{index:08d}-1111-1111-1111-111111111111', b'x', index)
        self.assertEqual(len(speech._reply_cache), 8)
        self.assertIsNone(speech.resume_reply('00000000-1111-1111-1111-111111111111'))
        self.assertEqual(speech.resume_reply('00000009-1111-1111-1111-111111111111'), b'x')


if __name__ == '__main__':
    unittest.main()

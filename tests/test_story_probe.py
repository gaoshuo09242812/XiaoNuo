import unittest
from app.network_speech import LocalNetworkSpeech
from app.behavior import requested_story_seconds, ResponseStyleManager, Context
from app.storage import DEFAULT_CONTROLS


class StoryProbeTests(unittest.TestCase):
    def test_explicit_story_duration_overrides_brief_for_this_turn_only(self):
        controls=dict(DEFAULT_CONTROLS,reply_length='brief',quiet=False)
        manager=ResponseStyleManager()
        requested=manager.choose(Context(),controls,'讲三十秒故事')
        self.assertEqual(requested.response_style.length,'normal')
        self.assertGreaterEqual(requested.response_style.max_characters,150)
        prompt=manager.prompt(requested,'讲三十秒故事')
        self.assertIn('126到150',prompt)
        self.assertNotIn('长度 brief',prompt)
        self.assertEqual(controls['reply_length'],'brief')
        self.assertEqual(manager.choose(Context(),controls,'你好').response_style.length,'brief')

    def test_fixed_request_and_reply_cues_without_retaining_text(self):
        for duration in ('三十', '30'):
            value=LocalNetworkSpeech.probe_flags(
                'PRIVATE_REQUEST 小糯讲个'+duration+'秒的故事，最后说讲完啦',
                'PRIVATE_REPLY 故事讲完啦。',
                {'diagnostics':{'model_characters':130,'styled_characters':125,'private':'PRIVATE'}})
            self.assertTrue(value['story_request_seen'])
            self.assertTrue(value['thirty_seconds_seen'])
            self.assertTrue(value['ending_request_seen'])
            self.assertTrue(value['ending_reply_seen'])
            self.assertEqual(value['model_characters'],130)
            self.assertNotIn('PRIVATE',repr(value))

    def test_missing_request_cue_is_separate_from_missing_reply_cue(self):
        value=LocalNetworkSpeech.probe_flags('讲个故事','短故事。',{})
        self.assertTrue(value['story_request_seen'])
        self.assertFalse(value['thirty_seconds_seen'])
        self.assertFalse(value['ending_request_seen'])
        self.assertFalse(value['ending_reply_seen'])

    def test_story_duration_is_bounded_and_only_applies_to_stories(self):
        for text in ('讲三十秒的故事','讲30秒故事'):
            self.assertEqual(requested_story_seconds(text),30)
        self.assertEqual(requested_story_seconds('讲两分钟故事'),0)
        self.assertEqual(requested_story_seconds('倒计时30秒'),0)
        self.assertEqual(requested_story_seconds('讲999秒故事'),0)
        self.assertEqual(requested_story_seconds('讲十十秒故事'),0)

    def test_invalid_size_metadata_is_omitted(self):
        value=LocalNetworkSpeech.probe_flags('你好','你好',
            {'diagnostics':{'model_characters':True,'styled_characters':'secret'}})
        self.assertNotIn('model_characters',value)
        self.assertNotIn('styled_characters',value)


if __name__=='__main__':unittest.main()

"""The reduced font must cover every explicit screen-test glyph and caption."""
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import unittest

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('subset_oled_font',ROOT/'tools/subset_oled_font.py')
subset=importlib.util.module_from_spec(spec)
spec.loader.exec_module(subset)


class StatusFontTests(unittest.TestCase):
    def setUp(self):
        self.directory=ROOT/'firmware/XiaoNuo'
        self.raw=bytes(int(value,16) for value in re.findall(r'0x([0-9a-f]{2})',
            (self.directory/'status_font.h').read_text(encoding='utf-8')))

    def test_all_required_glyphs_exist_and_have_visible_width(self):
        ino=(self.directory/'XiaoNuo.ino').read_text(encoding='utf-8')
        glyphs=re.search(r'const uint16_t glyphs\[\]=\{(.*?)\}',ino)[1]
        required={int(value,16) for value in re.findall(r'0x([0-9a-f]+)',glyphs)}
        required.update(map(ord,subset.CAPTIONS))
        for code in required:
            record=subset.glyph_record(self.raw,code)
            self.assertIsNotNone(record,hex(code))
            self.assertGreater(record[3]&((1<<self.raw[4])-1),0,hex(code))
        for code in range(32,127):
            self.assertIsNotNone(subset.glyph_record(self.raw,code),chr(code))

    def test_committed_font_hash_and_size_match_manifest(self):
        manifest=json.loads((self.directory/'status_font.json').read_text(encoding='utf-8'))
        self.assertEqual(hashlib.sha256(self.raw).hexdigest(),manifest['subset_sha256'])
        self.assertEqual(len(self.raw),manifest['subset_bytes'])
        self.assertLess(len(self.raw),4096)
        self.assertIsNone(subset.glyph_record(self.raw,0x9fff))

import hashlib
import importlib.util
from pathlib import Path
import struct
import unittest

spec = importlib.util.spec_from_file_location(
    'inspect_reply_storage', Path(__file__).resolve().parents[1] / 'tools/inspect_reply_storage.py')
inventory = importlib.util.module_from_spec(spec)
spec.loader.exec_module(inventory)
cache_partition, parse_table = inventory.cache_partition, inventory.parse_table


def table(*, change=None):
    rows = [('nvs', 1, 2, 0x9000, 0x5000, 0), ('otadata', 1, 0, 0xE000, 0x2000, 0),
            ('app0', 0, 0x10, 0x10000, 0x140000, 0), ('app1', 0, 0x11, 0x150000, 0x140000, 0),
            ('spiffs', 1, 0x82, 0x290000, 0x160000, 0), ('coredump', 1, 3, 0x3F0000, 0x10000, 0)]
    if change:
        rows = change(rows)
    data = b''.join(struct.pack('<HBBII16sI', 0x50AA, kind, sub, start, size, label.encode(), flags)
                    for label, kind, sub, start, size, flags in rows)
    data += b'\xeb\xeb' + b'\xff' * 14 + hashlib.md5(data).digest()
    return data.ljust(0xC00, b'\xff')


class StorageInventoryTests(unittest.TestCase):
    def test_default_layout_identifies_only_data_partition(self):
        value = cache_partition(parse_table(table()))
        self.assertEqual((value['offset'], value['size']), (0x290000, 0x160000))

    def test_damaged_table_never_qualifies(self):
        damaged = bytearray(table())
        damaged[15] ^= 1
        with self.assertRaises(ValueError): parse_table(bytes(damaged))
        with self.assertRaises(ValueError): parse_table(b'\xff' * 0xC00)
        with self.assertRaises(ValueError): parse_table(table()[:-1])

    def test_valid_md5_cannot_authorize_altered_layout(self):
        for field, new in ((0, 'other'), (1, 0), (2, 2), (3, 0x2A0000), (4, 0x150000), (5, 1)):
            with self.subTest(field=field):
                def change(rows):
                    row = list(rows[4]); row[field] = new
                    return rows[:4] + [tuple(row)] + rows[5:]
                with self.assertRaises(ValueError): cache_partition(parse_table(table(change=change)))

    def test_overlap_duplicate_and_outside_chip_are_rejected(self):
        for row in (('spiffs', 1, 0x82, 0x150000, 0x160000, 0),
                    ('app1', 1, 0x82, 0x290000, 0x160000, 0),
                    ('spiffs', 1, 0x82, 0x290000, 0x260000, 0)):
            with self.subTest(row=row):
                with self.assertRaises(ValueError): parse_table(table(change=lambda rows: rows[:4] + [row] + rows[5:]))


if __name__ == '__main__':
    unittest.main()

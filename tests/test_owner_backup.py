"""Exercise native encryption, committed WAL snapshots, corruption and safe recovery."""
from contextlib import closing
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
import zipfile

from tools import owner_backup as backup


@unittest.skipUnless(os.name == 'nt', 'Native current-user DPAPI requires Windows')
class OwnerBackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / 'project'
        (self.root / 'data/worker-relay').mkdir(parents=True)
        (self.root / '.env.local').write_bytes(b'DEEPSEEK_API_KEY=synthetic-secret-only\n')
        (self.root / 'data/worker-relay/private-host.json').write_text('{"host_token":"synthetic"}')
        self.db = sqlite3.connect(self.root / 'data/xiaonuo.db')
        self.addCleanup(self.db.close)
        self.db.execute('PRAGMA journal_mode=WAL')
        for name in backup.TABLES:
            self.db.execute('CREATE TABLE ' + name + '(value TEXT)')
            self.db.execute('INSERT INTO ' + name + ' VALUES (?)', ('synthetic-private-content',))
        self.db.commit()
        self.target = self.base / 'private/back.xnu-backup'

    def archive(self, payload, *, mutate=None, extra=None):
        with zipfile.ZipFile(io.BytesIO(payload)) as original:
            files = {name: original.read(name) for name in original.namelist()}
        if mutate:
            mutate(files)
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w') as result:
            for name, data in files.items():
                result.writestr(name, data)
            if extra:
                result.writestr(*extra)
        return stream.getvalue()

    def test_native_roundtrip_and_no_plaintext_file(self):
        result = backup.create_backup(self.root, self.target)
        files, counts = backup.load_backup(self.target)
        self.assertTrue(result['verified'])
        self.assertEqual(counts['memories'], 1)
        self.assertEqual(files['.env.local'], (self.root / '.env.local').read_bytes())
        blob = self.target.read_bytes()
        self.assertNotIn(b'synthetic-secret-only', blob)
        self.assertNotIn(b'synthetic-private-content', blob)
        self.assertEqual(list(self.target.parent.iterdir()), [self.target])

    def test_snapshot_contains_committed_wal_and_excludes_uncommitted(self):
        # Keep this writer open: committed data is still in WAL, not just main .db.
        self.db.execute('INSERT INTO memories VALUES (?)', ('committed-second-row',))
        self.db.commit()
        self.db.execute('INSERT INTO memories VALUES (?)', ('uncommitted-third-row',))
        data = backup.database_snapshot(self.root / 'data/xiaonuo.db')
        self.assertEqual(backup.database_summary(data)['memories'], 2)
        self.assertEqual(self.db.execute('SELECT count(*) FROM memories').fetchone()[0], 3)
        self.assertEqual(self.db.execute('PRAGMA journal_mode').fetchone()[0], 'wal')

    def test_corrupted_encrypted_blob_rejected(self):
        backup.create_backup(self.root, self.target)
        blob = bytearray(self.target.read_bytes())
        blob[-1] ^= 0x7f
        self.target.write_bytes(blob)
        with self.assertRaises(backup.BackupError):
            backup.load_backup(self.target)

    def test_modified_payload_hash_rejected(self):
        payload = backup.make_payload(self.root)
        corrupt = self.archive(payload, mutate=lambda f: f.update({'.env.local': b'changed'}))
        with self.assertRaisesRegex(backup.BackupError, 'HASH_MISMATCH'):
            backup.validate_payload(corrupt)

    def test_path_traversal_rejected(self):
        payload = self.archive(backup.make_payload(self.root), extra=('../outside-secret', b'x'))
        with self.assertRaisesRegex(backup.BackupError, 'ARCHIVE_MEMBERS'):
            backup.validate_payload(payload)

    def test_duplicate_archive_member_rejected(self):
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', UserWarning)
            payload = self.archive(backup.make_payload(self.root), extra=('.env.local', b'x'))
        with self.assertRaisesRegex(backup.BackupError, 'ARCHIVE_MEMBERS'):
            backup.validate_payload(payload)

    def test_missing_manifest_item_rejected(self):
        def change(files):
            manifest = json.loads(files['manifest.json'])
            manifest['files'].pop('.env.local')
            files['manifest.json'] = json.dumps(manifest).encode()
        with self.assertRaisesRegex(backup.BackupError, 'INVALID_MANIFEST'):
            backup.validate_payload(self.archive(backup.make_payload(self.root), mutate=change))

    def test_existing_backup_never_overwritten(self):
        backup.create_backup(self.root, self.target)
        original = self.target.read_bytes()
        with self.assertRaises(FileExistsError):
            backup.create_backup(self.root, self.target)
        self.assertEqual(original, self.target.read_bytes())

    def test_project_destination_rejected(self):
        with self.assertRaisesRegex(backup.BackupError, 'OUTSIDE_PROJECT'):
            backup.create_backup(self.root, self.root / 'outputs/private.xnu-backup')

    def test_missing_required_source_does_not_create_backup(self):
        (self.root / '.env.local').unlink()
        with self.assertRaises(backup.BackupError):
            backup.create_backup(self.root, self.target)
        self.assertFalse(self.target.exists())

    def test_verified_restore_copy_is_separate_and_does_not_overwrite(self):
        original = (self.root / '.env.local').read_bytes()
        backup.create_backup(self.root, self.target)
        target = self.base / 'new-restore'
        result = backup.restore_copy(self.target, target, self.root)
        self.assertTrue(result['verified'])
        self.assertFalse(result['live_project_changed'])
        self.assertEqual((target / '.env.local').read_bytes(), original)
        self.assertEqual((self.root / '.env.local').read_bytes(), original)
        with closing(sqlite3.connect(target / 'data/xiaonuo.db')) as restored:
            self.assertEqual(restored.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            self.assertEqual(restored.execute('SELECT count(*) FROM memories').fetchone()[0], 1)
        with self.assertRaises(FileExistsError):
            backup.restore_copy(self.target, target, self.root)
        with self.assertRaises(backup.BackupError):
            backup.restore_copy(self.target, self.root / 'bad-restore', self.root)


if __name__ == '__main__':
    unittest.main()

"""Owner-only Windows DPAPI backup. No live restore, network or model calls."""
from contextlib import closing
import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import time
import zipfile

ROOT = Path(__file__).resolve().parents[1]
MAGIC = b'XIAONUO-DPAPI-USER-1\0'
LIMIT = 128 * 1024 * 1024
REQUIRED = frozenset({'.env.local', 'data/xiaonuo.db', 'data/worker-relay/private-host.json'})
OPTIONAL = frozenset({'data/local-llm.key', 'data/worker-relay/wrangler-production.json'})
TABLES = ('user_profiles', 'memories', 'reminders', 'devices')


class BackupError(Exception):
    """Fixed messages only: never embed file contents, keys or native errors."""


def sha(data):
    return hashlib.sha256(data).hexdigest()


def dpapi(data, *, decrypt=False):
    if os.name != 'nt':
        raise BackupError('WINDOWS_CURRENT_USER_REQUIRED')
    if not data or len(data) > LIMIT:
        raise BackupError('INVALID_BLOB_SIZE')

    class Blob(ctypes.Structure):
        _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]

    crypt = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    protect = crypt.CryptProtectData
    protect.argtypes = [ctypes.POINTER(Blob), wintypes.LPCWSTR, ctypes.POINTER(Blob),
                        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    protect.restype = wintypes.BOOL
    unprotect = crypt.CryptUnprotectData
    unprotect.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.POINTER(Blob),
                          ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    unprotect.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    source, output = Blob(len(data), buffer), Blob()
    # UI_FORBIDDEN only; never CRYPTPROTECT_LOCAL_MACHINE (current-user scope).
    if decrypt:
        success = unprotect(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output))
    else:
        success = protect(ctypes.byref(source), 'XiaoNuo owner backup v1', None, None, None, 1,
                          ctypes.byref(output))
    try:
        if not success or not output.data or output.size > LIMIT:
            raise BackupError('DPAPI_DECRYPT_FAILED' if decrypt else 'DPAPI_ENCRYPT_FAILED')
        return ctypes.string_at(output.data, output.size)
    finally:
        if output.data:
            kernel.LocalFree(output.data)


def source_path(root, name):
    path = root / name
    if (not path.is_file() or path.is_symlink() or path.stat().st_nlink != 1
            or not path.resolve().is_relative_to(root.resolve())):
        raise BackupError('UNSAFE_OR_MISSING_SOURCE')
    if path.stat().st_size > LIMIT:
        raise BackupError('SOURCE_TOO_LARGE')
    return path


def database_summary(data):
    if len(data) > LIMIT or not data.startswith(b'SQLite format 3\0'):
        raise BackupError('INVALID_DATABASE')
    with closing(sqlite3.connect(':memory:')) as db:
        db.deserialize(data)
        db.execute('PRAGMA query_only=ON')
        if db.execute('PRAGMA integrity_check').fetchone() != ('ok',):
            raise BackupError('DATABASE_INTEGRITY_FAILED')
        return {name: db.execute('SELECT count(*) FROM ' + name).fetchone()[0] for name in TABLES}


def database_snapshot(path):
    deadline = time.monotonic() + 15

    def progress(status, remaining, total):
        if time.monotonic() > deadline:
            raise BackupError('DATABASE_SNAPSHOT_TIMEOUT')

    # SQLite backup includes committed WAL data; never copy the live .db alone.
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=2)) as source:
        with closing(sqlite3.connect(':memory:')) as snapshot:
            source.backup(snapshot, pages=256, progress=progress, sleep=.05)
            data = snapshot.serialize()
    # Documented deserialize requirement: https://www.sqlite.org/c3ref/deserialize.html
    # Only the standalone in-memory snapshot header changes, never the live DB.
    data = data[:18] + b'\x01\x01' + data[20:]
    database_summary(data)
    return data


def make_payload(root):
    root = Path(root)
    names = REQUIRED | {name for name in OPTIONAL if (root / name).exists()}
    files = {}
    for name in sorted(names):
        path = source_path(root, name)
        files[name] = database_snapshot(path) if name == 'data/xiaonuo.db' else path.read_bytes()
    if sum(map(len, files.values())) > LIMIT:
        raise BackupError('PAYLOAD_TOO_LARGE')
    manifest = {'schema': 1, 'created_at': datetime.now(timezone.utc).isoformat(),
                'scope': 'windows-current-user', 'files': {name: sha(data) for name, data in files.items()}}
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in files.items():
            archive.writestr(name, data)
        archive.writestr('manifest.json', json.dumps(manifest).encode())
    return stream.getvalue()


def validate_payload(data):
    if len(data) > LIMIT:
        raise BackupError('PAYLOAD_TOO_LARGE')
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        entries = archive.infolist()
        names = [entry.filename for entry in entries]
        if (len(set(names)) != len(names) or not REQUIRED.issubset(names)
                or 'manifest.json' not in names or set(names) - REQUIRED - OPTIONAL - {'manifest.json'}
                or sum(entry.file_size for entry in entries) > LIMIT):
            raise BackupError('INVALID_ARCHIVE_MEMBERS')
        manifest = json.loads(archive.read('manifest.json'))
        if (manifest.get('schema') != 1 or manifest.get('scope') != 'windows-current-user'
                or not isinstance(manifest.get('files'), dict)
                or set(manifest['files']) != set(names) - {'manifest.json'}):
            raise BackupError('INVALID_MANIFEST')
        files = {name: archive.read(name) for name in manifest['files']}
        if any(sha(value) != manifest['files'][name] for name, value in files.items()):
            raise BackupError('MANIFEST_HASH_MISMATCH')
        summary = database_summary(files['data/xiaonuo.db'])
        return files, summary


def load_backup(path):
    path = Path(path)
    if path.stat().st_size > LIMIT or not path.is_file():
        raise BackupError('INVALID_BACKUP_FILE')
    blob = path.read_bytes()
    if not blob.startswith(MAGIC):
        raise BackupError('INVALID_BACKUP_FORMAT')
    return validate_payload(dpapi(blob[len(MAGIC):], decrypt=True))


def outside_project(path, root):
    path = Path(path).resolve()
    if path == root.resolve() or path.is_relative_to(root.resolve()):
        raise BackupError('DESTINATION_MUST_BE_OUTSIDE_PROJECT')
    return path


def create_backup(root, destination):
    destination = outside_project(destination, Path(root))
    payload = make_payload(root)
    blob = MAGIC + dpapi(payload)
    if dpapi(blob[len(MAGIC):], decrypt=True) != payload:
        raise BackupError('ENCRYPTION_ROUNDTRIP_FAILED')
    _, summary = validate_payload(payload)
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive create: never overwrite any previous owner backup.
    with destination.open('xb') as stream:
        stream.write(blob)
        stream.flush()
        os.fsync(stream.fileno())
    if destination.read_bytes() != blob:
        raise BackupError('BACKUP_WRITE_VERIFICATION_FAILED')
    return {'verified': True, 'scope': 'windows-current-user', 'bytes': len(blob),
            'sha256': sha(blob), 'database_counts': summary}


def restore_copy(backup, destination, root):
    destination = outside_project(destination, Path(root))
    files, summary = load_backup(backup)
    # Explicit command only. Decrypt first, then make a NEW directory: no live overwrite.
    destination.mkdir(parents=True, exist_ok=False)
    for name, data in files.items():
        path = destination / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('xb') as stream:
            stream.write(data)
        if sha(path.read_bytes()) != sha(data):
            raise BackupError('RESTORE_COPY_VERIFICATION_FAILED')
    return {'verified': True, 'live_project_changed': False, 'file_count': len(files),
            'database_counts': summary}


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['create', 'verify', 'restore-copy'])
    parser.add_argument('--backup', required=True, type=Path)
    parser.add_argument('--destination', type=Path)
    args = parser.parse_args(argv)
    try:
        if args.action == 'create':
            result = create_backup(ROOT, args.backup)
        elif args.action == 'verify':
            files, counts = load_backup(args.backup)
            result = {'verified': True, 'file_count': len(files), 'database_counts': counts}
        else:
            if not args.destination:
                raise BackupError('NEW_DESTINATION_REQUIRED')
            result = restore_copy(args.backup, args.destination, ROOT)
    except Exception as error:
        print(json.dumps({'verified': False, 'error': str(error) if isinstance(error, BackupError)
                          else 'BACKUP_OPERATION_FAILED'}))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

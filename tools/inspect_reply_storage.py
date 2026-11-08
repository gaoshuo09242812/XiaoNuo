"""Read-only ESP32 reply-cache inventory. Never erases or writes Flash.

Reads the real partition table first, verifies its MD5, range and default layout,
then scans ONLY the identified spiffs data partition. Raw bytes are temporary;
the saved result contains layout, hashes, counts and errors, never data contents.
This resets the board through the existing USB data connection.
"""
import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import struct
import subprocess
import tempfile

PARTITION_SIZE = 0x160000


def parse_table(raw):
    if len(raw) != 0xC00:
        raise ValueError('PARTITION_TABLE_LENGTH')
    entries = []
    checked = False
    for at in range(0, len(raw), 32):
        item = raw[at:at + 32]
        if item == b'\xff' * 32:
            break
        if item[:16] == b'\xeb\xeb' + b'\xff' * 14:
            if checked or item[16:] != hashlib.md5(raw[:at]).digest():
                raise ValueError('PARTITION_TABLE_MD5')
            checked = True
            continue
        if checked:
            raise ValueError('PARTITION_AFTER_MD5')
        magic, kind, subtype, offset, size, label, flags = struct.unpack('<HBBII16sI', item)
        if magic != 0x50AA or not size or offset < 0x9000 or offset + size > 0x400000:
            raise ValueError('PARTITION_ENTRY_RANGE')
        label = label.split(b'\0', 1)[0].decode('ascii', errors='strict')
        if not label or any(e['label'] == label for e in entries):
            raise ValueError('PARTITION_LABEL')
        if any(offset < e['offset'] + e['size'] and e['offset'] < offset + size for e in entries):
            raise ValueError('PARTITION_OVERLAP')
        entries.append(dict(label=label, type=kind, subtype=subtype, offset=offset, size=size, flags=flags))
    if not checked:
        raise ValueError('PARTITION_MD5_REQUIRED')
    return entries


def cache_partition(entries):
    expected = {
        'nvs': (1, 2, 0x9000, 0x5000, 0),
        'otadata': (1, 0, 0xE000, 0x2000, 0),
        'app0': (0, 0x10, 0x10000, 0x140000, 0),
        'app1': (0, 0x11, 0x150000, 0x140000, 0),
        'spiffs': (1, 0x82, 0x290000, PARTITION_SIZE, 0),
        'coredump': (1, 3, 0x3F0000, 0x10000, 0),
    }
    actual = {e['label']: tuple(e[n] for n in ('type', 'subtype', 'offset', 'size', 'flags')) for e in entries}
    if actual != expected:
        raise ValueError('PARTITION_LAYOUT_NOT_APPROVED')
    return next(e for e in entries if e['label'] == 'spiffs')


def read_flash(esptool, port, offset, size, destination):
    result = subprocess.run([
        str(esptool), '--chip', 'esp32', '--port', port, '--baud', '460800',
        '--before', 'default-reset', '--after', 'hard-reset', 'read-flash',
        hex(offset), hex(size), str(destination),
    ], capture_output=True, timeout=120)
    # esptool output can contain hardware identifiers; do not reflect it.
    if result.returncode:
        raise RuntimeError('READ_FLASH_FAILED')
    raw = destination.read_bytes()
    if len(raw) != size:
        raise RuntimeError('READ_FLASH_INCOMPLETE')
    return raw


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--esptool', type=Path, required=True)
    parser.add_argument('--port', required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    report = dict(time=datetime.now().astimezone().isoformat(), flash_written=False,
                  flash_erased=False, raw_data_retained=False, cache_eligible=False)
    try:
        with tempfile.TemporaryDirectory(prefix='xnu-storage-readonly-') as directory:
            temporary = Path(directory)
            raw = read_flash(args.esptool, args.port, 0x8000, 0xC00, temporary / 'table.bin')
            entries = parse_table(raw)
            report.update(partition_table_md5_valid=True, partition_table_sha256=hashlib.sha256(raw).hexdigest(),
                          partitions=entries)
            partition = cache_partition(entries)
            report['layout_matches'] = True
            print(json.dumps({'partition_table_verified': True, 'operation': 'read_only_blank_scan'}), flush=True)
            raw = read_flash(args.esptool, args.port, partition['offset'], partition['size'], temporary / 'data.bin')
            nonblank = len(raw) - raw.count(255)
            report.update(scanned_bytes=len(raw), nonblank_bytes=nonblank,
                          partition_sha256=hashlib.sha256(raw).hexdigest(), cache_eligible=nonblank == 0)
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
        report['error'] = str(error) if isinstance(error, (ValueError, RuntimeError)) else type(error).__name__
    path = root / 'data' / datetime.now().strftime('reply-storage-inventory-%Y%m%d-%H%M%S.json')
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({k: v for k, v in report.items() if k not in {'partitions', 'partition_sha256', 'partition_table_sha256'}}, ensure_ascii=False))
    print(json.dumps({'record': str(path)}, ensure_ascii=False))
    return 0 if report['cache_eligible'] else 1


if __name__ == '__main__':
    raise SystemExit(main())

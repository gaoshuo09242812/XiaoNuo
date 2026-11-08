"""Synthetic bytes only; verify untrusted downloads cannot become active tools."""
import hashlib
import importlib.util
from io import BytesIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch

spec=importlib.util.spec_from_file_location('setup_device_tunnel',Path(__file__).resolve().parents[1]/'tools/setup_device_tunnel.py')
download=importlib.util.module_from_spec(spec);spec.loader.exec_module(download)


class Response(BytesIO):
    def __init__(self,data,status=200,headers=None):
        super().__init__(data);self.status=status;self.headers=headers or {}


class DownloadTests(unittest.TestCase):
    def setUp(self):
        folder=tempfile.TemporaryDirectory();self.addCleanup(folder.cleanup)
        self.root=Path(folder.name);self.content=b'not an executable, synthetic verification bytes'
        for name,value in (('ROOT',self.root),('SIZE',len(self.content)),
                           ('SHA256',hashlib.sha256(self.content).hexdigest())):
            p=patch.object(download,name,value);p.start();self.addCleanup(p.stop)
        self.directory=self.root/'data/tools';self.directory.mkdir(parents=True)
        self.target=self.directory/f'cloudflared-{download.VERSION}.exe'
        self.partial=self.target.with_suffix('.exe.part')

    def prepare(self,response):
        opener=Mock();opener.open.return_value=response
        with patch.object(download,'build_opener',return_value=opener):result=download.prepare()
        return result,opener

    def test_verified_download_becomes_active_with_receipt_only_after_hash_check(self):
        result,_=self.prepare(Response(self.content))
        self.assertEqual(self.target.read_bytes(),self.content)
        self.assertEqual(result['sha256'],hashlib.sha256(self.content).hexdigest())
        self.assertFalse(result['windows_service_installed'])
        self.assertFalse(self.partial.exists())

    def test_modified_download_is_not_activated(self):
        with self.assertRaisesRegex(ValueError,'checksum mismatch'):
            self.prepare(Response(b'x'*len(self.content)))
        self.assertFalse(self.target.exists())
        self.assertTrue(self.partial.exists())

    def test_existing_wrong_tool_is_preserved_and_rejected(self):
        self.target.write_bytes(b'previous user file')
        with self.assertRaisesRegex(ValueError,'identity check'):download.prepare()
        self.assertEqual(self.target.read_bytes(),b'previous user file')

    def test_resume_requires_confirmed_range_and_preserves_existing_partial(self):
        self.partial.write_bytes(self.content[:10])
        with self.assertRaisesRegex(ValueError,'resumed range'):
            self.prepare(Response(self.content))
        self.assertEqual(self.partial.read_bytes(),self.content[:10])
        self.assertFalse(self.target.exists())

    def test_verified_resume_reconstructs_original_download(self):
        self.partial.write_bytes(self.content[:10])
        _,opener=self.prepare(Response(self.content[10:],206,{'Content-Range':f'bytes 10-{len(self.content)-1}/{len(self.content)}'}))
        self.assertEqual(opener.open.call_args.args[0].get_header('Range'),'bytes=10-')
        self.assertEqual(self.target.read_bytes(),self.content)

"""Download and verify the pinned official tunnel client; install no service."""
import hashlib
import json
from pathlib import Path
import time
from urllib.request import Request,build_opener

ROOT=Path(__file__).resolve().parents[1]
VERSION='2026.10.5'
SHA256='db4e4c413612a0916f3e1679d3caaf376f94911952d163028fb726fb2e0516a9'
SIZE=55343328
URL=f'https://github.com/cloudflare/cloudflared/releases/download/{VERSION}/cloudflared-windows-amd64.exe'


def prepare():
    directory=ROOT/'data/tools';directory.mkdir(parents=True,exist_ok=True)
    target=directory/f'cloudflared-{VERSION}.exe'
    part=target.with_suffix('.exe.part')
    for path in (target,part):
        if path.is_symlink() or (path.exists() and (not path.is_file() or path.stat().st_nlink>1)):
            raise ValueError('Refusing a non-regular tool file')
    if not target.exists():
        offset=part.stat().st_size if part.exists() else 0
        if offset>SIZE:raise ValueError('Partial download exceeds expected size')
        if offset<SIZE:
            headers={'User-Agent':'XiaoNuo-development-check'}
            if offset:headers['Range']=f'bytes={offset}-'
            request=Request(URL,headers=headers)
            started=time.monotonic()
            # Public download respects the user's existing network proxy.
            # Loopback gateway probes use a separate proxy-free opener.
            with build_opener().open(request,timeout=20) as response:
                if offset and (response.status!=206 or not response.headers.get('Content-Range','').startswith(f'bytes {offset}-')):
                    raise ValueError('Server did not confirm resumed range')
                with part.open('ab' if offset else 'wb') as output:
                    while block:=response.read(256*1024):
                        offset+=len(block)
                        if offset>SIZE:raise ValueError('Download exceeds expected size')
                        output.write(block)
                        if time.monotonic()-started>90:
                            raise TimeoutError('Download budget reached; partial file preserved')
        if part.stat().st_size!=SIZE or hashlib.sha256(part.read_bytes()).hexdigest()!=SHA256:
            raise ValueError('Tool checksum mismatch; executable was not activated')
        if target.exists():raise ValueError('Tool destination changed during download')
        part.rename(target)
    if target.stat().st_size!=SIZE or hashlib.sha256(target.read_bytes()).hexdigest()!=SHA256:
        raise ValueError('Existing tool failed identity check')
    receipt={'version':VERSION,'sha256':SHA256,'source':URL,'path':str(target),'windows_service_installed':False}
    (directory/'cloudflared-receipt.json').write_text(json.dumps(receipt,indent=2),encoding='utf-8')
    return receipt


if __name__=='__main__':
    print(json.dumps(prepare(),ensure_ascii=False))

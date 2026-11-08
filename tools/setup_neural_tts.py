from pathlib import Path
from urllib.request import Request,urlopen
import hashlib
import json
import subprocess
import sys
import tarfile
import time

root=Path(__file__).resolve().parents[1]
dependencies=subprocess.run([sys.executable,'-m','pip','install','--disable-pip-version-check','--target',str(root/'.runtime-libs'),
                             'sherpa-onnx==2.0.0','numpy>=2.3,<3'],capture_output=True,timeout=180)
if dependencies.returncode:
    print(dependencies.stderr.decode('utf-8',errors='replace')[-1800:])
    raise SystemExit('Neural speech dependency installation failed')
print('Offline neural speech dependencies installed.',flush=True)
models=root/'data/models'
name='kokoro-int8-multi-lang-v1_1'
archive=models/(name+'.tar.bz2')
url='https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/'+archive.name
if not archive.exists():
    temp=archive.with_suffix('.download')
    total=temp.stat().st_size if temp.is_file() else 0
    headers={'User-Agent':'XiaoNuo-offline-speech'}
    if total:headers['Range']='bytes='+str(total)+'-'
    started=time.monotonic()
    with urlopen(Request(url,headers=headers),timeout=45) as response:
        if total and response.status==206:
            assert response.headers.get('Content-Range','').startswith('bytes '+str(total)+'-')
        elif total:total=0
        with temp.open('ab' if total else 'wb') as output:
            announced=total//(32*1024*1024)
            while chunk:=response.read(64*1024):
                total+=len(chunk)
                if total>700_000_000 or time.monotonic()-started>1200:raise RuntimeError('Model download limit reached')
                output.write(chunk)
                if total//(32*1024*1024)>announced:
                    announced=total//(32*1024*1024)
                    print('Downloaded '+str(announced*32)+' MiB of neural speech model',flush=True)
    temp.replace(archive)
assert hashlib.sha256(archive.read_bytes()).hexdigest()=='a1e94694776049035c4f2c6529f003aaece993c76aae9a78995831c3c4dcafc6','Downloaded model digest mismatch'
with tarfile.open(archive,'r:bz2') as source:
    members=source.getmembers()
    if sum(item.size for item in members)>1_500_000_000:raise RuntimeError('Unexpected expanded model size')
    source.extractall(models,filter='data')
target=models/name
files=[p.relative_to(target).as_posix() for p in target.iterdir() if p.is_file()]
manifest={'provider':'kokoro_onnx','source':url,'model_directory':name,
          'archive_sha256':hashlib.sha256(archive.read_bytes()).hexdigest(),
          'hash_basis':'local_digest_of_official_https_download','files':files,
          'voice_source':'stock_chinese_synthetic_speakers','reference_audio_used':False,
          'inference_verified':False}
(models/'neural-tts-manifest.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')
print(json.dumps(manifest))

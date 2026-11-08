from pathlib import Path
from urllib.request import Request,urlopen
import hashlib
import json
import subprocess
import sys
import zipfile

root=Path(__file__).resolve().parents[1]
runtime=root/'.runtime-libs'
install=subprocess.run([sys.executable,'-m','pip','install','--disable-pip-version-check','--target',str(runtime),
                        'vosk==0.3.45'],capture_output=True,timeout=180)
if install.returncode:
    print(install.stderr.decode('utf-8',errors='replace')[-1600:])
    raise SystemExit('Offline recognition dependency installation failed')
print('Vosk Python dependency installed.',flush=True)
models=root/'data/models'
models.mkdir(parents=True,exist_ok=True)
archive=models/'vosk-model-small-cn-0.22.zip'
url='https://alphacephei.com/vosk/models/vosk-model-small-cn-0.22.zip'
if not archive.exists():
    temp=archive.with_suffix('.download')
    with urlopen(Request(url,headers={'User-Agent':'XiaoNuo-local-setup'}),timeout=45) as response,temp.open('wb') as output:
        size=0
        while chunk:=response.read(1024*1024):
            output.write(chunk)
            size+=len(chunk)
            if size>100_000_000:
                raise RuntimeError('Model download exceeds expected size limit')
    temp.replace(archive)
assert hashlib.sha256(archive.read_bytes()).hexdigest()=='3af8b0e7e0f835ae9d414ce5df580237a3cfb08d586c9fbbb0f7ff29ad5b14ba','Model hash mismatch'
target=models/'vosk-model-small-cn-0.22'
with zipfile.ZipFile(archive) as source:
    assert source.testzip() is None,'Corrupted model archive'
    for item in source.infolist():
        resolved=(models/item.filename).resolve()
        if not resolved.is_relative_to(models.resolve()) or item.file_size>150_000_000:
            raise RuntimeError('Unsafe model archive member')
    source.extractall(models)
manifest={'asr_provider':'vosk','model':target.name,'source':url,
          'archive_sha256':hashlib.sha256(archive.read_bytes()).hexdigest(),
          'license':'Apache-2.0','model_present':(target/'am/final.mdl').is_file(),
          'inference_verified':False}
(models/'speech-model-manifest.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')
print(json.dumps(manifest))

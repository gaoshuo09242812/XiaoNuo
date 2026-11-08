"""Install pinned Windows CPU assets. Use --activate to enable only the local model."""
from pathlib import Path
from urllib.request import Request,urlopen
import hashlib
import json
import zipfile
import time
import argparse
import sys

root=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(root/'backend'))
from app.local_models import LOCAL_MODELS
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--model',choices=LOCAL_MODELS,default='Qwen3-1.7B-Q8_0')
parser.add_argument('--activate',action='store_true')
args=parser.parse_args()
models=root/'data/models'
models.mkdir(parents=True,exist_ok=True)

def file_hash(path):
    digest=hashlib.sha256()
    with path.open('rb') as stream:
        while chunk:=stream.read(1024*1024):digest.update(chunk)
    return digest.hexdigest()

def download(url,path,expected,size):
    if path.exists() and path.stat().st_size==size and file_hash(path)==expected:
        return
    temp=path.with_suffix('.download')
    total=temp.stat().st_size if temp.is_file() else 0
    headers={'User-Agent':'XiaoNuo-local-setup'}
    if total:headers['Range']='bytes='+str(total)+'-'
    started=time.monotonic()
    with urlopen(Request(url,headers=headers),timeout=45) as response:
        if total and response.status==206:
            if not response.headers.get('Content-Range','').startswith('bytes '+str(total)+'-'):
                raise RuntimeError('Unexpected partial download response')
        elif total:
            total=0
        with temp.open('ab' if total else 'wb') as output:
          last_report=total//(32*1024*1024)
          while chunk:=response.read(64*1024):
            output.write(chunk)
            total+=len(chunk)
            if total>size:raise RuntimeError('Download exceeds expected size')
            if time.monotonic()-started>1200:raise RuntimeError('Download paused after time limit; partial file retained')
            if total//(32*1024*1024)>last_report:
                output.flush()
                last_report=total//(32*1024*1024)
                print('Downloaded '+str(total//(1024*1024))+' MiB of '+path.name,flush=True)
    digest=hashlib.sha256()
    with temp.open('rb') as source:
        while chunk:=source.read(1024*1024):digest.update(chunk)
    if total!=size or digest.hexdigest()!=expected:
        raise RuntimeError('Official artifact hash verification failed')
    temp.replace(path)
    print('Verified '+path.name,flush=True)

runtime=models/'llama-b11854'
archive=models/'llama-b11854-bin-win-cpu-x64.zip'
download('https://github.com/ggml-org/llama.cpp/releases/download/b11854/llama-b11854-bin-win-cpu-x64.zip',archive,
         'eb83835d4e8214eda9f2a504e3d5692129a05e4ad480deb189f387d3301df9e8',24172672)
runtime.mkdir(exist_ok=True)
with zipfile.ZipFile(archive) as source:
    assert source.testzip() is None
    for member in source.infolist():
        if not (runtime/member.filename).resolve().is_relative_to(runtime.resolve()):
            raise RuntimeError('Unsafe runtime archive member')
    # Do not rewrite a verified runtime that may already be serving the app.
    for member in source.infolist():
        installed=runtime/member.filename
        if member.is_dir():installed.mkdir(parents=True,exist_ok=True)
        elif not installed.is_file() or file_hash(installed)!=hashlib.sha256(source.read(member)).hexdigest():
            source.extract(member,runtime)
entry=LOCAL_MODELS[args.model]
model=models/entry['file']
url=entry['url']
download(url,model,entry['sha256'],entry['size'])
manifest={'provider':'local_qwen','model':model.name,'runtime':'llama.cpp b11854',
          'source':url,'sha256':entry['sha256'],
          'license':'Apache-2.0','api_cost_usd':0,'inference_verified':False}
(models/'llm-model-manifest.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')
print(json.dumps(manifest))

if args.activate:
    import re
    import secrets
    target=root/'.env.local'
    if target.is_symlink() or (target.exists() and target.stat().st_nlink!=1):
        raise RuntimeError('Expected a regular private configuration file')
    key=root/'data/local-llm.key'
    if not key.exists():
        with key.open('x',encoding='ascii') as stream:stream.write(secrets.token_urlsafe(48)+'\n')
        key.chmod(0o600)
    original=target.read_text(encoding='utf-8') if target.exists() else ''
    updated=original
    for name,value in {'XIAONUO_LOCAL_LLM_ENABLED':'true','XIAONUO_LOCAL_MODEL':args.model}.items():
        setting=name+'='+value
        updated=re.sub(r'^'+name+r'=[^\r\n]*',setting,updated,flags=re.M) if re.search(r'^'+name+'=',updated,re.M) else updated.rstrip('\r\n')+'\n'+setting+'\n'
    with target.open('w',encoding='utf-8',newline='\n') as stream:stream.write(updated)
    target.chmod(0o600)
    print('Local model enabled. Remote/paid settings were preserved; no secrets displayed.')

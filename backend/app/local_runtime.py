"""Optional self-hosted model runtime, restricted to this computer."""
import hashlib
import json
from pathlib import Path
import subprocess
import time
from urllib.request import Request,build_opener,ProxyHandler

from .errors import XiaoNuoError
from .local_models import LOCAL_MODELS


class LocalModelRuntime:
    def __init__(self,settings,port=8766):
        self.settings=settings
        if port not in {8766,8767}:raise ValueError('Only fixed local model ports are permitted')
        self.port=port
        self.process=None
        self.error=None
        self.ready=False

    def _health(self):
        spec=next((spec for spec in self.settings.providers if spec.name=='local_qwen'),None)
        if not spec or not spec.key:return False
        try:
            opener=build_opener(ProxyHandler({}))
            def query(path):
                request=Request('http://127.0.0.1:'+str(self.port)+path,headers={'Authorization':'Bearer '+spec.key})
                with opener.open(request,timeout=1) as response:
                    return json.load(response)
            if query('/health').get('status')!='ok':return False
            # A ready server on this port may still be serving a previous model.
            models=query('/v1/models').get('data',[])
            return any(item.get('id')==self.settings.local_model for item in models if isinstance(item,dict))
        except Exception:return False

    def start(self):
        if not self.settings.local_llm_enabled:return
        root=self.settings.root
        exe=root/'data/models/llama-b11854/llama-server.exe'
        entry=LOCAL_MODELS[self.settings.local_model]
        model=root/'data/models'/entry['file']
        key=root/'data/local-llm.key'
        if not exe.is_file() or not model.is_file() or not key.is_file():
            self.error='LOCAL_MODEL_NOT_INSTALLED'
            return
        digest=hashlib.sha256()
        with model.open('rb') as stream:
            while chunk:=stream.read(1024*1024):digest.update(chunk)
        if digest.hexdigest()!=entry['sha256']:
            self.error='LOCAL_MODEL_HASH_MISMATCH'
            return
        if self._health():
            self.ready=True
            return
        command=[str(exe),'--host','127.0.0.1','--port',str(self.port),'-m',str(model),'--alias',self.settings.local_model,
                 '-c','4096','-t','8','-tb','8','--parallel','1','--jinja',
                 '--chat-template-kwargs','{"enable_thinking":false}',
                 '--api-key-file',str(key),'--no-webui','--log-disable']
        try:
            self.process=subprocess.Popen(command,cwd=exe.parent,stdin=subprocess.DEVNULL,
                                          stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                                          creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            until=time.monotonic()+35
            while time.monotonic()<until and self.process.poll() is None:
                if self._health():
                    self.ready=True
                    return
                time.sleep(0.2)
            self.error='LOCAL_MODEL_START_FAILED'
            self.stop()
        except OSError:
            self.error='LOCAL_MODEL_START_FAILED'

    def stop(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
        self.ready=False

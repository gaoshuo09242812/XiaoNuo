"""User-login supervisor: OS lock, bounded retry, authenticated health, owned jobs."""
from contextlib import contextmanager
from pathlib import Path
import json,os,subprocess,sys,time
from uuid import uuid4
from .windows_awake import keep_system_awake

class BusyError(Exception):pass

@contextmanager
def lock(path):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    if path.is_symlink() or (path.exists() and (not path.is_file() or path.stat().st_nlink>1)):raise ValueError('Unsafe lock path')
    with path.open('a+b') as handle:
        if path.stat().st_size==0:handle.write(b'0');handle.flush()
        handle.seek(0)
        try:
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError:raise BusyError() from None
        try:yield
        finally:
            handle.seek(0)
            if os.name=='nt':msvcrt.locking(handle.fileno(),msvcrt.LK_UNLCK,1)
            else:fcntl.flock(handle.fileno(),fcntl.LOCK_UN)

def running(root):
    try:
        with lock(root/'data/worker-service/supervisor.lock'):return False
    except BusyError:return True

def save(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    if path.is_symlink() or (path.exists() and (not path.is_file() or path.stat().st_nlink>1)):raise ValueError('Unsafe state path')
    pending=path.with_name(path.name+'.'+uuid4().hex+'.tmp')
    with pending.open('x',encoding='utf-8') as stream:json.dump(value,stream,ensure_ascii=False,indent=2)
    os.replace(pending,path)

def enabled(root):
    try:return json.loads((root/'data/worker-service/control.json').read_text())['enabled'] is True
    except (OSError,ValueError,KeyError,TypeError):return False

def health(root,run_id):
    from .config import load_settings
    from urllib.request import Request,build_opener,ProxyHandler
    try:
        settings=load_settings(root/'.env.local')
        request=Request('http://127.0.0.1:8765/api/v1/status',headers={'Authorization':'Bearer '+settings.admin_token})
        with build_opener(ProxyHandler({})).open(request,timeout=2) as response:status=json.load(response)
        if status.get('worker_run_id')!=run_id or status.get('network_audio_enabled') is not True:return False
        if settings.local_llm_enabled:
            spec=next((p for p in settings.providers if p.name=='local_qwen'),None)
            if not spec or not spec.key:return False
            request=Request('http://127.0.0.1:8766/v1/models',headers={'Authorization':'Bearer '+spec.key})
            with build_opener(ProxyHandler({})).open(request,timeout=1) as response:models=json.load(response)
            if not any(item.get('id')==settings.local_model for item in models.get('data',[]) if isinstance(item,dict)):return False
        return True
    except Exception:return False

def supervise(root,*,command=None,probe=None,poll_seconds=1,retry_seconds=5,max_retry=60,startup_seconds=180,health_interval=10,grace_seconds=15):
    from .windows_job import OwnedJob
    root=Path(root);directory=root/'data/worker-service';directory.mkdir(parents=True,exist_ok=True)
    probe=probe or (lambda run_id:health(root,run_id));delay=retry_seconds;restarts=0
    with lock(directory/'supervisor.lock'),keep_system_awake() as awake:
        state={'supervisor_pid':os.getpid(),'state':'STARTING','restarts':0,'keep_awake_active':awake}
        try:
            while enabled(root):
                run_id=uuid4().hex;stop_file=directory/(run_id+'.stop');job=OwnedJob('Local\\XiaoNuo-'+run_id)
                child=None;started=time.monotonic();next_health=started;failures=0;was_ready=False
                state.update(state='STARTING',run_id=run_id,restarts=restarts,last_exit=None)
                save(directory/'status.json',state)
                try:
                    args=command(job.name,stop_file,run_id) if command else [sys.executable,str(root/'tools/run_worker_relay_host.py'),
                        '--job-name',job.name,'--stop-file',str(stop_file),'--run-id',run_id]
                    logpath=directory/'host.log'
                    if logpath.exists() and logpath.stat().st_size>2*1024*1024:logpath.replace(directory/'host.previous.log')
                    with logpath.open('ab') as log:
                        child=subprocess.Popen(args,cwd=root,stdin=subprocess.DEVNULL,stdout=log,stderr=log,
                            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                    state['child_pid']=child.pid;save(directory/'status.json',state)
                    while enabled(root) and child.poll() is None:
                        now=time.monotonic()
                        if now>=next_health:
                            ok=probe(run_id);next_health=now+health_interval
                            if ok:was_ready=True;failures=0;state['state']='RUNNING'
                            elif was_ready or now-started>startup_seconds:failures+=1
                            state['health_failures']=failures;save(directory/'status.json',state)
                            if failures>=3:state['state']='HEALTH_RESTART';break
                        time.sleep(poll_seconds)
                finally:
                    if child is not None and child.poll() is None:
                        save(stop_file,{'stop':True})
                        try:child.wait(timeout=grace_seconds)
                        except subprocess.TimeoutExpired:pass
                    job.close() # Also removes models orphaned by an actual worker crash.
                    if child is not None:
                        try:child.wait(timeout=5)
                        except subprocess.TimeoutExpired:raise RuntimeError('Owned job failed to stop')
                        state['last_exit']=child.returncode
                if not enabled(root):break
                restarts+=1
                if was_ready and time.monotonic()-started>=120:delay=retry_seconds
                state.update(state='RETRY_WAIT',retry_seconds=delay,restarts=restarts);save(directory/'status.json',state)
                until=time.monotonic()+delay
                while enabled(root) and time.monotonic()<until:time.sleep(min(poll_seconds,max(0,until-time.monotonic())))
                delay=min(max_retry,delay*2)
        finally:
            state.update(state='STOPPED',child_pid=None,keep_awake_active=False);save(directory/'status.json',state)

def start(root,*,autostart=False):
    root=Path(root);directory=root/'data/worker-service'
    with lock(directory/'start.lock'):
        if autostart and not enabled(root):return {'started':False,'disabled':True}
        save(directory/'control.json',{'enabled':True})
        if running(root):return {'started':False,'already_running':True}
        executable=Path(sys.executable).with_name('pythonw.exe')
        if not executable.is_file():executable=Path(sys.executable)
        with (directory/'supervisor.log').open('ab') as output:
            child=subprocess.Popen([str(executable),str(root/'tools/manage_worker_service.py'),'run'],cwd=root,
                stdin=subprocess.DEVNULL,stdout=output,stderr=output,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        until=time.monotonic()+10
        while time.monotonic()<until and child.poll() is None:
            if running(root):return {'started':True,'supervisor_pid':child.pid}
            time.sleep(.1)
        raise RuntimeError('Supervisor startup failed; inspect local log')

def stop(root,timeout=30):
    save(root/'data/worker-service/control.json',{'enabled':False})
    until=time.monotonic()+timeout
    while running(root) and time.monotonic()<until:time.sleep(.2)
    return {'stopped':not running(root)}

def autostart_file(root):
    startup=Path(os.environ['APPDATA'])/'Microsoft/Windows/Start Menu/Programs/Startup'
    pythonw=Path(sys.executable).with_name('pythonw.exe')
    if not pythonw.is_file():raise ValueError('pythonw.exe is required')
    args=subprocess.list2cmdline([str(pythonw),str(root/'tools/manage_worker_service.py'),'start','--autostart'])
    content=('CreateObject("WScript.Shell").Run "'+args.replace('"','""')+'", 0, False\r\n').encode('utf-16')
    return startup/'XiaoNuo-Worker-Autostart.vbs',content

def install(root):
    path,content=autostart_file(root)
    if path.is_symlink() or (path.exists() and path.read_bytes()!=content):raise ValueError('Existing startup entry differs; refusing overwrite')
    if not path.exists():
        with path.open('xb') as output:output.write(content)
    save(root/'data/worker-service/control.json',{'enabled':True})
    return {'installed':True,'startup_file':str(path),'trigger':'current_user_logon'}

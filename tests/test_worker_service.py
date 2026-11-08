import ctypes,json,os,subprocess,sys,tempfile,threading,time,unittest
from pathlib import Path
from unittest.mock import patch
from app import worker_service as service

class ServiceTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);self.root=Path(temp.name)
    def test_os_lock_blocks_duplicate_start_and_stale_pid_is_not_running(self):
        directory=self.root/'data/worker-service'
        service.save(directory/'status.json',{'child_pid':12345})
        self.assertFalse(service.running(self.root))
        with service.lock(directory/'supervisor.lock'),patch.object(subprocess,'Popen') as spawn:
            self.assertTrue(service.running(self.root))
            self.assertTrue(service.start(self.root)['already_running']);spawn.assert_not_called()
        self.assertFalse(service.running(self.root))
    def test_health_rejects_foreign_server_and_wrong_local_model(self):
        from app.config import Settings,ProviderSpec
        from io import BytesIO
        from unittest.mock import Mock
        settings=Settings(root=self.root,admin_token='fixture-owner',local_llm_enabled=True,
            providers=(ProviderSpec('local_qwen','fixture','fixture-model-key'),))
        for run,model,expected in [('other',settings.local_model,False),('ours','wrong',False),('ours',settings.local_model,True)]:
            opener=Mock();opener.open.side_effect=[BytesIO(json.dumps({'worker_run_id':run,'network_audio_enabled':True}).encode()),
                BytesIO(json.dumps({'data':[{'id':model}]}).encode())]
            with patch('app.config.load_settings',return_value=settings),patch('urllib.request.build_opener',return_value=opener):
                self.assertEqual(service.health(self.root,'ours'),expected)
    @unittest.skipUnless(os.name=='nt','Windows login entry')
    def test_login_install_preserves_changed_user_entry_and_disabled_start_is_noop(self):
        appdata=self.root/'appdata';startup=appdata/'Microsoft/Windows/Start Menu/Programs/Startup';startup.mkdir(parents=True)
        with patch.dict(os.environ,{'APPDATA':str(appdata)}):
            service.install(self.root);path,expected=service.autostart_file(self.root)
            self.assertEqual(path.read_bytes(),expected);self.assertIn('--autostart',expected.decode('utf-16'))
            path.write_text('user change',encoding='utf-8')
            with self.assertRaises(ValueError):service.install(self.root)
            self.assertEqual(path.read_text(),'user change')
        service.save(self.root/'data/worker-service/control.json',{'enabled':False})
        with patch.object(subprocess,'Popen') as spawn:
            self.assertTrue(service.start(self.root,autostart=True)['disabled']);spawn.assert_not_called()
    @unittest.skipUnless(os.name=='nt','Native Windows job containment')
    def test_real_hidden_login_script_handles_unicode_and_space_paths(self):
        root=self.root/'项目 有空格';(root/'tools').mkdir(parents=True)
        marker=root/'invoked.json'
        (root/'tools/manage_worker_service.py').write_text('import sys,json\nfrom pathlib import Path\nPath('+repr(str(marker))+').write_text(json.dumps(sys.argv[1:]))\n',encoding='utf-8')
        _,content=service.autostart_file(root);script=root/'fixture.vbs';script.write_bytes(content)
        subprocess.run([str(Path(os.environ['SystemRoot'])/'System32/wscript.exe'),'//B','//NoLogo',str(script)],
            check=True,timeout=10,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,creationflags=subprocess.CREATE_NO_WINDOW)
        until=time.monotonic()+10
        while not marker.exists() and time.monotonic()<until:time.sleep(.05)
        self.assertTrue(marker.exists());self.assertEqual(json.loads(marker.read_text()),['start','--autostart'])
    @unittest.skipUnless(os.name=='nt','Native Windows job containment')
    def test_real_crash_hang_restart_and_grandchild_cleanup(self):
        root=self.root;fixture=root/'fixture.py';backend=Path(__file__).resolve().parents[1]/'backend'
        fixture.write_text('''import sys,os,subprocess,time,json
from pathlib import Path
sys.path.insert(0,'''+repr(str(backend))+''')
from app.windows_job import join_job
job,stop,run,folder=sys.argv[1:];join_job(job);folder=Path(folder)
grandchild=subprocess.Popen([sys.executable,'-c','import time; time.sleep(120)'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
pending=folder/(run+'.ready');pending.write_text(json.dumps({'pid':os.getpid(),'grandchild':grandchild.pid,'run':run}));os.replace(pending,folder/'ready.json')
def marked(name):
 try:return (folder/name).read_text()==run
 except OSError:return False
while True:
 if marked('crash'):os._exit(23)
 if Path(stop).exists() and not marked('hang'):break
 time.sleep(.02)
''',encoding='utf-8')
        def ready():
            try:return json.loads((root/'ready.json').read_text())
            except (OSError,ValueError):return {}
        def probe(run):
            try:hung=(root/'hang').read_text()==run
            except OSError:hung=False
            return ready().get('run')==run and not hung
        errors=[]
        def supervise():
            try:service.supervise(root,command=lambda job,stop,run:[sys.executable,str(fixture),job,str(stop),run,str(root)],
                probe=probe,poll_seconds=.02,retry_seconds=.05,max_retry=.1,startup_seconds=2,health_interval=.05,grace_seconds=.15)
            except Exception as error:errors.append(error)
        def wait_for(predicate):
            until=time.monotonic()+10
            while time.monotonic()<until:
                if predicate():return
                if errors:raise errors[0]
                time.sleep(.02)
            self.fail('Native supervisor fixture timed out')
        k=ctypes.WinDLL('kernel32',use_last_error=True)
        k.OpenProcess.argtypes=[ctypes.c_ulong,ctypes.c_int,ctypes.c_ulong];k.OpenProcess.restype=ctypes.c_void_p
        k.WaitForSingleObject.argtypes=[ctypes.c_void_p,ctypes.c_ulong];k.CloseHandle.argtypes=[ctypes.c_void_p]
        service.save(root/'data/worker-service/control.json',{'enabled':True})
        thread=threading.Thread(target=supervise);thread.start();handles=[]
        try:
            wait_for(lambda:bool(ready()))
            for failure in ('crash','hang'):
                current=ready();handle=k.OpenProcess(0x100000,False,current['grandchild']);self.assertTrue(handle);handles.append(handle)
                (root/failure).write_text(current['run'])
                wait_for(lambda:ready().get('run')!=current['run'])
                self.assertEqual(k.WaitForSingleObject(handle,2000),0,'Owned grandchild survived restart')
            self.assertTrue(service.running(root))
        finally:
            service.save(root/'data/worker-service/control.json',{'enabled':False});thread.join(10)
            for handle in handles:k.CloseHandle(handle)
        self.assertFalse(thread.is_alive());self.assertFalse(errors);self.assertFalse(service.running(root))
        self.assertEqual(json.loads((root/'data/worker-service/status.json').read_text())['state'],'STOPPED')

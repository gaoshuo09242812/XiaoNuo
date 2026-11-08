"""Start/stop the user-login worker supervisor. No browser or USB operations."""
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'.runtime-libs'),str(ROOT/'backend')]

def main():
    import argparse,json
    from app import worker_service as service
    parser=argparse.ArgumentParser();parser.add_argument('command',choices=['start','stop','status','run','install-autostart','remove-autostart'])
    parser.add_argument('--autostart',action='store_true');args=parser.parse_args()
    if args.command=='run':
        try:service.supervise(ROOT)
        except service.BusyError:return
        return
    if args.command=='start':result=service.start(ROOT,autostart=args.autostart)
    elif args.command=='stop':result=service.stop(ROOT)
    elif args.command=='install-autostart':result=service.install(ROOT)
    elif args.command=='remove-autostart':
        path,expected=service.autostart_file(ROOT)
        if path.exists():
            if path.is_symlink() or path.read_bytes()!=expected:raise ValueError('Startup entry changed; refusing removal')
            path.unlink()
        result={'removed':True}
    else:
        path=ROOT/'data/worker-service/status.json'
        result=json.loads(path.read_text()) if path.exists() else {}
        result.update(supervisor_running=service.running(ROOT),enabled=service.enabled(ROOT))
        if not result['supervisor_running']:result['state']='STOPPED'
    print(json.dumps(result,ensure_ascii=False))
    if args.command=='stop' and not result.get('stopped'):raise SystemExit(1)

if __name__=='__main__':main()

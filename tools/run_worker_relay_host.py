"""Run the owner computer with an outbound fixed-Worker relay; no browser UI."""
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'backend'),str(ROOT/'.runtime-libs')]

def main():
    import argparse,asyncio,json,re
    from app.config import load_settings
    from app.network_host import create_network_host,stop_signals
    from app.worker_relay import serve_worker_host,relay_hostname
    from manage import process_lock
    parser=argparse.ArgumentParser()
    parser.add_argument('--config',type=Path,default=ROOT/'data/worker-relay/private-host.json')
    parser.add_argument('--with-usb',action='store_true')
    parser.add_argument('--job-name')
    parser.add_argument('--stop-file',type=Path)
    parser.add_argument('--run-id')
    args=parser.parse_args()
    if args.job_name:
        from app.windows_job import join_job
        join_job(args.job_name)
    try:
        config=json.loads(args.config.read_text(encoding='utf-8'))
        hostname=relay_hostname(config['hostname']);token=config['host_token']
        if not re.fullmatch('[0-9a-f]{64}',token):raise ValueError()
    except (OSError,ValueError,KeyError,TypeError):parser.error('Valid private relay configuration is required; no credentials are printed')
    async def run():
        from app.fx_rates import maintained_fx
        settings=load_settings()
        async with maintained_fx(settings):
            await run_host(settings)
    async def run_host(settings):
        owner,_=create_network_host(settings,enable_audio=True,usb_enabled=args.with_usb)
        owner.state.worker_run_id=args.run_id
        stop=asyncio.Event()
        async def watch_stop():
            while not stop.is_set():
                if args.stop_file and args.stop_file.exists():stop.set();return
                await asyncio.sleep(.25)
        watcher=asyncio.create_task(watch_stop())
        try:
            with stop_signals(stop):await serve_worker_host(owner,hostname,token,stop=stop)
        finally:
            watcher.cancel();await asyncio.gather(watcher,return_exceptions=True)
    with process_lock('backend-runtime.lock',timeout=0),process_lock('device-gateway-runtime.lock',timeout=0):asyncio.run(run())

if __name__=='__main__':main()

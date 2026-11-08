"""Run the isolated device listener with real TLS; loopback by default."""
import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT),str(ROOT/'backend'),str(ROOT/'.runtime-libs')]


def main():
    parser=argparse.ArgumentParser(description='小糯设备专用 TLS 入口（不含控制台）')
    parser.add_argument('--host',default='127.0.0.1')
    parser.add_argument('--port',type=int,default=8768)
    parser.add_argument('--cert',type=Path,required=True)
    parser.add_argument('--key',type=Path,required=True)
    args=parser.parse_args()
    if not 1024<=args.port<=65535:
        parser.error('port must be between 1024 and 65535')
    if not args.cert.is_file() or not args.key.is_file():
        parser.error('TLS certificate and private key files must exist')
    from manage import process_lock
    from app.device_gateway import create_device_gateway
    import uvicorn
    with process_lock('device-gateway-runtime.lock',timeout=0):
        uvicorn.run(create_device_gateway(),host=args.host,port=args.port,
                    ssl_certfile=str(args.cert.resolve()),ssl_keyfile=str(args.key.resolve()),
                    workers=1,proxy_headers=False,access_log=False,log_level='warning',
                    ws_max_size=1024,ws_max_queue=4,timeout_keep_alive=5)


if __name__=='__main__':
    main()

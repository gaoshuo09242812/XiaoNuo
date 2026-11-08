"""Start the console and dedicated TLS gateway in one local-only host."""
import argparse
import asyncio
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT/'backend'), str(ROOT/'.runtime-libs')]


def main():
    parser = argparse.ArgumentParser(description='小糯共享后台：控制台 8765，设备 TLS 8768')
    parser.add_argument('--cert', type=Path, required=True)
    parser.add_argument('--key', type=Path, required=True)
    parser.add_argument('--enable-audio', action='store_true')
    parser.add_argument('--chat-provider',choices=['local','deepseek'])
    parser.add_argument('--computer-wake',action='store_true',default=None)
    parser.add_argument('--with-usb', action='store_true', help='需要 USB 配对/调试时才启用')
    args = parser.parse_args()
    if not args.cert.is_file() or not args.key.is_file():
        parser.error('TLS certificate and private key files must exist')
    from manage import process_lock
    from app.config import load_settings
    from app.network_host import create_network_host, serve_network_host, stop_signals
    from app.errors import XiaoNuoError
    async def run():
        owner, gateway = create_network_host(load_settings(), enable_audio=args.enable_audio,
                                             usb_enabled=args.with_usb,chat_provider=args.chat_provider,computer_wake=args.computer_wake)
        stop = asyncio.Event()
        with stop_signals(stop):
            await serve_network_host(owner, gateway, args.cert.resolve(), args.key.resolve(), stop=stop)
    try:
        with process_lock('backend-runtime.lock', timeout=0), process_lock('device-gateway-runtime.lock', timeout=0):
            asyncio.run(run())
    except (XiaoNuoError, OSError, ValueError) as error:
        print('NETWORK_HOST_START_FAILED: '+(error.code if isinstance(error,XiaoNuoError) else type(error).__name__),file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()

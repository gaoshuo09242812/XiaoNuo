"""Prepare a named-tunnel config without registering DNS or starting a tunnel."""
import argparse
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'backend'),str(ROOT/'.runtime-libs')]


if __name__=='__main__':
    from app.device_ingress import cloudflare_config,save_config
    parser=argparse.ArgumentParser(description='准备固定设备入口配置，不启动或发布')
    parser.add_argument('--hostname',required=True)
    parser.add_argument('--tunnel-id',required=True)
    parser.add_argument('--credentials-file',type=Path,required=True)
    parser.add_argument('--origin-ca',type=Path,required=True)
    parser.add_argument('--output',type=Path,default=ROOT/'data/device-ingress.json')
    args=parser.parse_args()
    try:
        config=cloudflare_config(args.hostname,args.tunnel_id,args.credentials_file,args.origin_ca)
        result=save_config(config,args.output)
    except ValueError as error:parser.error(str(error))
    print('Device-only configuration saved: '+str(result))
    print('No DNS, account, tunnel or Windows service was changed.')

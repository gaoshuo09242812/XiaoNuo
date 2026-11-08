"""Reproduce the pinned official CLI/local runtime inside data/, no global install."""
from pathlib import Path
import argparse,hashlib,json,os,shutil,subprocess,urllib.request
ROOT=Path(__file__).resolve().parents[1]

def prepare(node,npm_cli):
    source=ROOT/'relay/cloudflare';target=ROOT/'data/tools/cloudflare-runtime'
    target.mkdir(parents=True,exist_ok=True)
    for source_name,name in [('runtime-package.json','package.json'),('runtime-package-lock.json','package-lock.json')]:
        content=(source/source_name).read_bytes();path=target/name
        if path.is_symlink():raise ValueError('Linked package manifest refused')
        if path.exists() and path.read_bytes()!=content:raise ValueError('Existing runtime manifests differ; refusing overwrite')
        if not path.exists():path.write_bytes(content)
    env=dict(os.environ);env['PATH']=str(node.parent)+os.pathsep+env.get('PATH','')
    env['WRANGLER_SEND_METRICS']='false';env['npm_config_cache']=str(ROOT/'data/tools/npm-cache')
    for scheme,proxy in urllib.request.getproxies().items():
        if scheme in {'http','https'}:env[scheme.upper()+'_PROXY']=proxy
    subprocess.run([str(node),str(npm_cli),'ci','--ignore-scripts','--no-audit','--no-fund'],cwd=target,env=env,check=True)
    return {'runtime':str(target),'lock_sha256':hashlib.sha256((target/'package-lock.json').read_bytes()).hexdigest(),
            'wrangler':'4.154.0','miniflare':'4.20260730.0','global_install':False}

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--node',type=Path,required=True);parser.add_argument('--npm-cli',type=Path,required=True)
    args=parser.parse_args();print(json.dumps(prepare(args.node,args.npm_cli),ensure_ascii=False))

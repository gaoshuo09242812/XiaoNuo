"""Save an owner-private outbound credential and reviewable Worker settings.

Never changes subscriptions, publishes a Worker, or writes firmware.
"""
from pathlib import Path
import csv,hashlib,json,os,re,secrets,subprocess,sys
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'.runtime-libs'),str(ROOT/'backend')]

def prepare(root,account_id,subdomain):
    from app.config import load_settings
    from app.budgeting import UsageLedger
    if not re.fullmatch('[0-9a-f]{32}',account_id) or not re.fullmatch('[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?',subdomain):
        raise ValueError('Invalid account or Workers subdomain')
    directory=root/'data/worker-relay';directory.mkdir(parents=True,exist_ok=True)
    if directory.is_symlink() or directory.is_junction():raise ValueError('Linked private directory refused')
    if os.name=='nt':
        query=subprocess.run(['whoami','/user','/fo','csv','/nh'],capture_output=True,text=True,check=True)
        sid=next(csv.reader(query.stdout.splitlines()))[-1]
        if not re.fullmatch('S-[0-9-]+',sid):raise ValueError('Owner SID unavailable')
        subprocess.run(['icacls',str(directory),'/inheritance:r','/grant:r','*'+sid+':(OI)(CI)F','*S-1-5-18:(OI)(CI)F'],
                       stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,check=True)
    else:directory.chmod(0o700)
    private=directory/'private-host.json'
    if private.is_symlink():raise ValueError('Linked private configuration refused')
    if private.exists():
        owner=json.loads(private.read_text(encoding='utf-8'))
    else:
        name='xiaonuo-relay-'+secrets.token_hex(4)
        owner={'version':1,'worker_name':name,'hostname':name+'.'+subdomain+'.workers.dev','host_token':secrets.token_hex(32)}
        with private.open('x',encoding='utf-8') as stream:json.dump(owner,stream,indent=2)
        if os.name!='nt':private.chmod(0o600)
    if (not re.fullmatch('[0-9a-f]{64}',owner.get('host_token','')) or
            owner.get('hostname')!=owner.get('worker_name','')+'.'+subdomain+'.workers.dev'):
        raise ValueError('Existing private configuration differs; refusing overwrite')
    settings=load_settings(root/'.env.local')
    with UsageLedger(settings.database).connect() as db:
        hashes=[row[0] for row in db.execute("SELECT token_hash FROM devices WHERE revoked_at IS NULL AND provisioning_state='bound' ORDER BY id")]
    if len(hashes)>8 or any(not re.fullmatch('[0-9a-f]{64}',value or '') for value in hashes):raise ValueError('Invalid active device list')
    config=json.loads((root/'relay/cloudflare/wrangler.json').read_text(encoding='utf-8'))
    config.update(name=owner['worker_name'],account_id=account_id,main=str(root/'relay/cloudflare/worker.mjs'))
    config['vars']={'HOST_TOKEN_SHA256':hashlib.sha256(owner['host_token'].encode()).hexdigest(),'DEVICE_HASHES':json.dumps(hashes)}
    destination=directory/'wrangler-production.json'
    encoded=json.dumps(config,ensure_ascii=False,indent=2)+'\n'
    if destination.exists() and destination.read_text(encoding='utf-8')!=encoded:
        raise ValueError('Existing deployment configuration differs; refusing overwrite')
    if not destination.exists():
        with destination.open('x',encoding='utf-8') as stream:stream.write(encoded)
    return {'hostname':owner['hostname'],'worker_name':owner['worker_name'],'device_count':len(hashes),
            'config':str(destination),'private_config':str(private),'published':False}

if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('--account-id',required=True);parser.add_argument('--subdomain',required=True)
    args=parser.parse_args();print(json.dumps(prepare(ROOT,args.account_id,args.subdomain),ensure_ascii=False))

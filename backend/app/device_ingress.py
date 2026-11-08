"""Reviewable fixed ingress configuration; never points at the owner's API."""
import ipaddress
import json
from pathlib import Path
import re
from uuid import UUID


def fixed_hostname(value):
    if not isinstance(value,str) or value!=value.strip() or len(value)>253:
        raise ValueError('A fixed hostname is required')
    try:host=value.encode('idna').decode('ascii').lower()
    except UnicodeError:raise ValueError('Invalid hostname') from None
    if len(host)>253 or '.' not in host or any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?',part) for part in host.split('.')):
        raise ValueError('Invalid hostname')
    try:ipaddress.ip_address(host)
    except ValueError:pass
    else:raise ValueError('A DNS hostname is required')
    if host.endswith(('.trycloudflare.com','.localhost','.invalid','.local')):
        raise ValueError('Temporary or local hostnames cannot be a delivery endpoint')
    return host


def cloudflare_config(hostname,tunnel_id,credentials_file,origin_ca):
    host=fixed_hostname(hostname)
    try:identifier=str(UUID(str(tunnel_id)))
    except (ValueError,TypeError):raise ValueError('Invalid tunnel identifier') from None
    credentials=Path(credentials_file).resolve();ca=Path(origin_ca).resolve()
    for path in (credentials,ca):
        if not path.is_file():raise ValueError('Credential and origin CA files must exist')
    origin={'originServerName':'localhost','caPool':str(ca),'noTLSVerify':False}
    return {'tunnel':identifier,'credentials-file':str(credentials),'protocol':'http2',
            'metrics':'127.0.0.1:20249','no-autoupdate':True,
            'ingress':[
                {'hostname':host,'path':'^/api/v1/device-link/ws$','service':'https://localhost:8768','originRequest':dict(origin)},
                {'hostname':host,'path':'^/healthz$','service':'https://localhost:8768','originRequest':dict(origin)},
                {'service':'http_status:404'}]}


def save_config(config,destination):
    path=Path(destination)
    encoded=json.dumps(config,ensure_ascii=False,indent=2)+'\n'
    if path.is_symlink():raise ValueError('Refusing a linked configuration destination')
    if path.exists():
        if path.is_file() and path.read_text(encoding='utf-8')==encoded:return path
        raise ValueError('Existing configuration differs; use a new destination')
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('x',encoding='utf-8') as stream:stream.write(encoded)
    return path

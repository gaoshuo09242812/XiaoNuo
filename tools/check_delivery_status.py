"""Authenticated loopback GETs only; no remediation, recording or paid requests."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / '.runtime-libs'), str(ROOT / 'backend')]

import argparse
import json
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from app.delivery_status import render, summarize

ROUTES = ('status', 'devices', 'voice', 'preferences', 'reminders')
MAX_BODY = 1024 * 1024


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise HTTPError(req.full_url, code, 'Redirect refused', headers, fp)


def fetch(opener, token, route):
    if route not in ROUTES:
        raise ValueError('Unsupported diagnostic route')
    request = Request('http://127.0.0.1:8765/api/v1/' + route,
                      headers={'Authorization': 'Bearer ' + token}, method='GET')
    try:
        with opener.open(request, timeout=4) as response:
            body = response.read(MAX_BODY + 1)
    except HTTPError as error:
        error.close()
        raise
    if len(body) > MAX_BODY:
        raise ValueError('Diagnostic response too large')
    value = json.loads(body)
    if not isinstance(value, dict):
        raise ValueError('Diagnostic response must be an object')
    return value


def collect(root=ROOT):
    from app.config import load_settings
    from app import worker_service as service
    payloads, state = {}, {}
    try:
        saved = json.loads((root / 'data/worker-service/status.json').read_text(encoding='utf-8'))
        if isinstance(saved, dict):
            state = saved
    except (OSError, ValueError):
        pass
    service_state = {'running': False, 'enabled': False, 'healthy': False,
                     'keep_awake_active': state.get('keep_awake_active') is True}
    try:
        # Avoid creating a lock file for a project that has never run.
        if (root / 'data/worker-service/supervisor.lock').is_file():
            service_state['running'] = service.running(root)
        service_state['enabled'] = service.enabled(root)
        settings = load_settings(root / '.env.local')
        token = settings.admin_token
        opener = build_opener(ProxyHandler({}), NoRedirect())
        if token:
            for route in ROUTES:
                try:
                    payloads[route] = fetch(opener, token, route)
                except Exception:
                    pass  # Never echo credentials, response bodies or exception strings.
        run_id = state.get('run_id')
        service_state['healthy'] = (service_state['running'] and isinstance(run_id, str)
                                    and bool(run_id) and payloads.get('status', {}).get('worker_run_id') == run_id
                                    and service.health(root, run_id))
        from app.billing_status import check_billing
        service_state['billing'] = check_billing(settings, payloads.get('status', {}).get('budget'))
    except Exception:
        pass
    return summarize(payloads, service_state)


def main(argv=None):
    parser = argparse.ArgumentParser(description='小糯只读状态自检；不调用聊天、录音或播放。')
    parser.add_argument('--json', action='store_true', help='输出不含身份与正文的状态JSON')
    args = parser.parse_args(argv)
    report = collect()
    print(json.dumps(report, ensure_ascii=False, indent=2) if args.json else render(report))
    return 0 if report['all_checks_pass'] else 1


if __name__ == '__main__':
    raise SystemExit(main())

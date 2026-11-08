"""Read-only delivery snapshot. Never return arbitrary API text or identifiers."""
from datetime import datetime, timezone
from math import isfinite
from .billing_status import MESSAGES as BILLING_MESSAGES


def _object(value):
    return value if isinstance(value, dict) else {}


def _list(value):
    return value if isinstance(value, list) else []


def summarize(payloads, service, *, now=None):
    now = now or datetime.now(timezone.utc)
    payloads, service = _object(payloads), _object(service)
    status = _object(payloads.get('status'))
    prefs = _object(payloads.get('preferences'))
    voice = _object(payloads.get('voice'))
    reminders = _object(payloads.get('reminders'))
    checks = []

    def add(name, level, code):
        checks.append({'check': name, 'level': level, 'code': code})

    backend = (service.get('running') is True and service.get('enabled') is True
               and service.get('healthy') is True and status.get('network_audio_enabled') is True)
    add('backend', 'PASS' if backend else 'FAIL', 'BACKEND_HEALTHY' if backend else 'BACKEND_UNAVAILABLE')
    awake = backend and service.get('keep_awake_active') is True
    add('keep_awake', 'PASS' if awake else 'WARN', 'KEEP_AWAKE_ACTIVE' if awake else 'KEEP_AWAKE_UNCONFIRMED')

    fresh = []
    for value in _list(_object(payloads.get('devices')).get('items')):
        device = _object(value)
        if (device.get('credential_present') is not True or 'revoked_at' not in device or device.get('revoked_at') is not None
                or device.get('provisioning_state') != 'bound' or device.get('state') != 'NETWORK_IDLE'):
            continue
        try:
            stamp = datetime.fromisoformat(device['last_seen'])
            if stamp.tzinfo is None:
                continue
            age = (now - stamp).total_seconds()
        except (ValueError, TypeError, KeyError, OverflowError):
            continue
        if 0 <= age <= 30:
            fresh.append(device)
    online = backend and bool(fresh) and 'DEVICE_CONNECTED' in _list(status.get('relay_states'))
    add('device', 'PASS' if online else 'WARN', 'FRESH_NETWORK_HEARTBEAT' if online else 'DEVICE_NOT_CONFIRMED_ONLINE')
    # USB-related flags are intentionally ignored: charger operation is supported.
    versions = {d.get('firmware') for d in fresh if isinstance(d.get('firmware'),str)}
    known_firmware = online and len(versions)==1 and versions.issubset({'0.6.72-reminder-pull','0.6.73-expression'}) and all(isinstance(d.get('firmware'),str) for d in fresh)
    firmware_code = 'FIRMWARE_073' if versions=={'0.6.73-expression'} else 'FIRMWARE_072'
    add('firmware', 'PASS' if known_firmware else 'WARN', firmware_code if known_firmware else 'FIRMWARE_VERSION_UNCONFIRMED')

    asr, tts = _object(voice.get('asr')), _object(voice.get('tts'))
    models = backend and asr.get('model_present') is True and tts.get('model_present') is True
    add('voice_models', 'PASS' if models else 'WARN', 'LOCAL_MODELS_PRESENT' if models else 'LOCAL_MODELS_UNCONFIRMED')
    warm = backend and tts.get('warmup_completed') is True
    add('tts_warmup', 'PASS' if warm else 'WARN', 'TTS_WARMED_UP' if warm else 'TTS_WARMUP_UNCONFIRMED')

    wake = backend and prefs.get('computer_wake_enabled') is True
    add('wake', 'PASS' if wake else 'WARN', 'COMPUTER_WAKE_ENABLED' if wake else 'WAKE_DISABLED_OR_UNKNOWN')
    volume = prefs.get('volume')
    valid_volume = type(volume) in (int, float) and 0 < volume <= 1 and isfinite(volume)
    speech = backend and prefs.get('speech_enabled') is True and prefs.get('quiet') is False and valid_volume
    add('speech_controls', 'PASS' if speech else 'WARN', 'SPEECH_ENABLED' if speech else 'SPEECH_MUTED_OR_UNKNOWN')

    providers = [_object(p) for p in _list(status.get('providers'))]
    deepseek = [p for p in providers if p.get('provider') == 'deepseek']
    chat = (backend and status.get('network_chat_provider') == 'deepseek'
            and status.get('remote_enabled') is True and status.get('allow_paid') is True
            and len(deepseek) == 1 and deepseek[0].get('credential_configured') is True
            and 'block_reason' in deepseek[0] and deepseek[0]['block_reason'] is None)
    budget = _object(status.get('budget'))
    if budget.get('paid_block_reason'):
        chat = False
    add('chat_policy', 'PASS' if chat else 'WARN', 'DEEPSEEK_POLICY_READY' if chat else 'DEEPSEEK_BLOCKED_OR_UNKNOWN')
    billing = _object(service.get('billing'))
    paid_ready = backend and billing.get('ready') is True and billing.get('code') == 'BILLING_READY'
    code = billing.get('code')
    if not isinstance(code, str) or code not in BILLING_MESSAGES or (not paid_ready and code == 'BILLING_READY'):
        code = 'BILLING_NOT_CONFIRMED'
    add('paid_request', 'PASS' if paid_ready else 'WARN', 'BILLING_READY' if paid_ready else code)
    capable = online and reminders.get('device_audio_capable') is True
    add('reminder', 'PASS' if capable and wake and speech else 'WARN',
        'REMINDER_PULL_ENABLED' if capable and wake and speech else 'REMINDER_UNAVAILABLE_OR_MUTED')
    return {'schema': 1, 'snapshot_only': True, 'live_chat_tested': False,
            'physical_sound_tested': False, 'all_checks_pass': all(c['level'] == 'PASS' for c in checks),
            'checks': checks}


LABELS = {
    'backend': '电脑后台', 'keep_awake': '后台防自动睡眠', 'device': '设备网络心跳',
    'firmware': '已识别固件版本', 'voice_models': '本地收音及语音模型文件',
    'tts_warmup': '语音模型预热', 'wake': '电脑唤醒开关',
    'speech_controls': '语音与静音设置', 'chat_policy': 'DeepSeek配置及当前策略',
    'reminder': '在线空闲语音提醒',
    'paid_request': '价格记录与付费预留额度',
}


def render(report):
    levels = {'PASS': '正常', 'WARN': '待检查', 'FAIL': '不可用'}
    lines = ['小糯一键自检（只读状态）']
    for check in report['checks']:
        lines.append(f"[{levels[check['level']]}] {LABELS[check['check']]} ({check['code']})")
        if check['check'] == 'paid_request':
            lines.append('  ' + BILLING_MESSAGES.get(check['code'], BILLING_MESSAGES['BILLING_NOT_CONFIRMED']))
    lines.extend(['', '这是当前状态快照，没有录音、播放或调用聊天模型；不能替代实际听感和问答验收。',
                  '设备用5V充电器供电时不需要连接电脑USB。',
                  '设备未确认在线：检查供电、原2.4GHz热点和移动数据，等待自动重连后再检查。',
                  '唤醒关闭、安静模式、关闭语音或零音量也会影响使用，不代表硬件损坏。',
                  '若后台不可用，可在项目中运行 restart-worker.cmd，等待加载后再检查。'])
    return '\n'.join(lines)

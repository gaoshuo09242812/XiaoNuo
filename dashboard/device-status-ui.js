(function (root) {
  "use strict";
  const object = value => value && typeof value === "object" && !Array.isArray(value) ? value : {};
  function network(snapshot, now = Date.now()) {
    const unknown = {online:false, state:"网络状态待确认", note:"无法确认当前无线状态，请刷新控制台。"};
    if (!snapshot || !Number.isFinite(snapshot.checkedAt) || now < snapshot.checkedAt || now - snapshot.checkedAt > 20000) return unknown;
    const status = object(snapshot.status), devices = object(snapshot.devices), prefs = object(snapshot.prefs);
    if (!Array.isArray(devices.items) || !Array.isArray(status.relay_states) || typeof status.network_audio_enabled !== "boolean") return unknown;
    if (!status.network_audio_enabled) return {online:false, state:"无线后台未启用", note:"当前电脑未提供无线语音服务。"};
    const bound = devices.items.map(object).filter(d => d.credential_present === true &&
      Object.hasOwn(d,"revoked_at") && d.revoked_at === null && d.provisioning_state === "bound");
    if (!bound.length) return {online:false,state:"尚无有效设备登记",note:"请在设备管理区核对原有登记状态。"};
    const fresh = bound.some(d => {
      if (d.state !== "NETWORK_IDLE" || typeof d.last_seen !== "string" || !/(?:Z|[+-]\d\d:\d\d)$/.test(d.last_seen)) return false;
      const at = Date.parse(d.last_seen);
      return Number.isFinite(at) && now >= at && now - at <= 30000;
    });
    if (!fresh || !status.relay_states.includes("DEVICE_CONNECTED"))
      return {online:false,state:"等待小糯无线连接",note:"请保持小糯通电，原手机热点在附近并能上网；连接恢复后此处会自动更新。"};
    let note = "通过热点连接，可直接叫“小糯”开始对话；日常使用无需USB数据线。";
    if (prefs.quiet === true) note = "无线已连接，当前处于安静模式。";
    else if (prefs.computer_wake_enabled === false) note = "无线已连接，电脑唤醒开关当前关闭。";
    else if (prefs.speech_enabled === false || prefs.volume === 0) note = "无线已连接，当前声音已关闭或音量为零。";
    else if (prefs.computer_wake_enabled !== true || prefs.quiet !== false) note = "无线已连接；请在互动偏好中核对唤醒与安静设置。";
    return {online:true,state:"无线已在线",note};
  }
  function overview(usb, wireless) {
    if (wireless.online) return {state:wireless.state,note:wireless.note};
    if (usb && usb.connected === true) return {state:"USB 已连接",note:"可进行USB手动联调；无线状态见设备连接区。"};
    return {state:wireless.state,note:wireless.note};
  }
  function manualNote(wireless) {
    return wireless.online ? "小糯已无线在线，可按当前互动设置直接唤醒。本区手动录音按钮需要USB连接。"
      : "无线状态见设备连接区；如需在此页手动录音，请用USB数据线连接电脑。";
  }
  const api = {network,overview,manualNote};
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.XiaoNuoDeviceStatus = api;
})(globalThis);

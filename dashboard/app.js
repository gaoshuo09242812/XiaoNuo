"use strict";
let csrf = "";
let preferences = {};
let conversationId = null;
let voiceBusy=false,previewBusy=false,deviceConnected=false;
let deviceSpeakerReady=false,lastAudioToken=null;
let deviceHardwareId="",currentBinding=null;
let reminderEdit=null;
let reminderDraftId=crypto.randomUUID(),reminderSaving=false;
let budgetCurrency=null;
let usbSnapshot=null,networkSnapshot=null,networkRefreshBusy=false;
const $ = (id) => document.getElementById(id);
const errors = {UNAUTHORIZED:"登录已失效，请从本机重新打开控制台。",LOGIN_TICKET_INVALID_OR_EXPIRED:"登录入口已失效，请重新运行控制台启动文件。",REMOTE_CALLS_DISABLED:"远程模型尚未启用，本地设置和记忆指令仍可使用。",NO_ELIGIBLE_PROVIDER:"目前没有满足地区、账号和预算条件的模型。",MEMORY_CONSENT_REQUIRED:"需要明确同意保存这条记忆。",MEMORY_NOT_FOUND_OR_STALE:"记忆已经发生变化，请刷新后再修改。",CONVERSATION_BUSY:"小糯正在处理上一句话，请稍后再发。"};
Object.assign(errors,{ASR_EMPTY_AUDIO:"这次没有检测到可识别的声音，请靠近麦克风再说一次。",ASR_NO_SPEECH_RECOGNIZED:"这次没听清，请再说一句短话。",AUDIO_CAPTURE_CANCELLED:"录音已停止。",ASR_MODEL_NOT_INSTALLED:"中文识别模型尚未安装。",VOICE_BUSY:"上一段语音还在处理，请稍等。",DEVICE_NOT_CONNECTED:"开发板尚未连接。",LOCAL_MODEL_NOT_VERIFIED:"本机模型尚未验证。",LOCAL_MODEL_NOT_CONFIGURED:"本机模型尚未配置。",SPEECH_DISABLED_BY_OWNER:"安静模式或声音设置阻止了播放。",AUDIO_CAPTURE_INCOMPLETE:"录音传输不完整，请重试。"});
Object.assign(errors,{TTS_BUSY:"上一段声音还在准备，请稍等。",TTS_MODEL_NOT_INSTALLED:"新声音模型尚未安装。",TTS_TIMEOUT:"这段声音准备得较慢，请稍后重试。",TTS_AUDIO_LENGTH_INVALID:"回答较长，本次请先查看文字。"});
Object.assign(errors,{DUPLICATE_REQUEST:"这次请求已经处理过，请重新开始一轮。",DEVICE_SPEAKER_DISABLED:"小糯喇叭目前未启用，可改为仅准备电脑预览。",SPEAKER_REPLY_TOO_LONG:"回复过长，请先查看文字或要求小糯简短回答。",SPEAKER_AUDIO_TOO_LONG:"这段语音超过设备时长限制，文字回答已保留。",SPEAKER_PLAYBACK_CANCELLED:"播放已停止。",SPEAKER_INCOMPLETE:"本次播放没有完整完成，文字回答已保留。"});
errors.TTS_PROVIDER_FAILED="这次声音没有生成成功，请稍后重试；文字回答仍可使用。";
Object.assign(errors,{REMINDER_IN_PAST:"这个时间已经过去，请选择未来的时间。",REMINDER_NOT_FOUND_OR_STALE:"提醒刚刚发生变化，请刷新后再修改。",REMINDER_TIMEZONE_REQUIRED:"请填写完整的日期和时间。",REMINDER_LIMIT_REACHED:"待执行提醒较多，请先取消不需要的提醒。"});
function notice(text, error=false){$("notice").textContent=text;$("notice").classList.toggle("error",error);$("notice").hidden=false;}
async function api(path, options={}){
  const headers={"Content-Type":"application/json",...(csrf?{"X-XiaoNuo-CSRF":csrf}:{})};
  const response=await fetch(path,{...options,headers,credentials:"same-origin"});
  const data=await response.json();
  if(!response.ok){if(response.status===401){$("login").hidden=false;$("application").hidden=true;}throw new Error(errors[data.error]||data.error||"操作失败");}
  return data;
}
function money(value){return XiaoNuoBudget.money(value,budgetCurrency);}
function message(text,kind){const item=document.createElement("p");item.className="message "+kind;item.textContent=text;$("messages").append(item);$("messages").scrollTop=$("messages").scrollHeight;}
function canStartVoice(){return !previewBusy&&deviceConnected&&($("voice-reply-target").value!=="device"||deviceSpeakerReady);}
function renderConnections(){
  const wireless=XiaoNuoDeviceStatus.network(networkSnapshot);
  const summary=XiaoNuoDeviceStatus.overview(usbSnapshot,wireless);
  $("device-state").textContent=summary.state;$("device-note").textContent=summary.note;
  $("network-state").textContent=wireless.state;$("network-note").textContent=wireless.note;
  $("usb-state").textContent=usbSnapshot?(usbSnapshot.connected?"USB 已连接":usbSnapshot.paused?"USB 联调已暂停":"USB 未连接（充电器使用时正常）"):"USB 状态待确认";
  for(const button of document.querySelectorAll("[data-device-operation]"))button.disabled=!deviceConnected;
  if(!deviceConnected&&!voiceBusy)$("voice-progress").textContent=XiaoNuoDeviceStatus.manualNote(wireless);
}
async function refreshNetwork(knownStatus=null){
  if(networkRefreshBusy)return;
  networkRefreshBusy=true;
  try{
    const [status,devices,prefs]=await Promise.all([knownStatus?Promise.resolve(knownStatus):api("/api/v1/status"),api("/api/v1/devices"),api("/api/v1/preferences")]);
    networkSnapshot={status,devices,prefs,checkedAt:Date.now()};
  }catch(error){networkSnapshot=null;}
  finally{networkRefreshBusy=false;renderConnections();}
}
async function refreshVoiceProgress(){if(!voiceBusy)return;const status=await api("/api/v1/voice");const progress=status.progress||{};if(progress.transcript)$("voice-transcript").textContent="听到："+progress.transcript;const labels={recognizing:"正在识别你说的话…",thinking:"小糯正在想怎么回答…",replying:"小糯正在准备或朗读回复…",stopping:"正在停止本次语音…"};if(labels[progress.stage])$("voice-progress").textContent=labels[progress.stage];}
$("voice-reply-target").addEventListener("change",()=>{$("voice-start").textContent=$("voice-reply-target").value==="device"?"开始对话（6 秒）":"录音并准备预览（6 秒）";refreshDevice().catch(()=>{});});
async function refreshDevice(){try{const device=await api("/api/v1/device");usbSnapshot=device;deviceConnected=device.connected===true;const previousHardware=deviceHardwareId;deviceHardwareId=device.info.device_id||"";$("device-audio-status").disabled=!device.connected;$("device-enroll").disabled=!device.connected||!deviceHardwareId||Boolean(currentBinding&&currentBinding.hardware_id===deviceHardwareId);if(previousHardware!==deviceHardwareId)await refreshBindings();deviceSpeakerReady=!!(device.connected&&device.speaker_enabled&&device.info.speaker_available);$("voice-start").disabled=voiceBusy||device.playback_state==="playing"||!canStartVoice();$("voice-stop").disabled=!(voiceBusy||previewBusy||device.playback_state==="playing");$("voice-preview").disabled=voiceBusy||previewBusy;$("voice-play-device").disabled=voiceBusy||device.playback_state==="playing"||!deviceSpeakerReady||!lastAudioToken;if(device.info.quiet)stopPlayback();if(device.playback_state==="playing"){$("voice-progress").textContent="小糯正在播放，可点击停止。";}else if(voiceBusy){$("voice-progress").textContent=device.capture_state==="recording"?"正在录音，请靠近小糯说一句短话。":device.capture_state==="warming"?"麦克风正在准备…":"正在识别和准备回复…";}else if(deviceConnected&&[$("voice-progress").textContent].some(text=>text===XiaoNuoDeviceStatus.manualNote({online:true})||text===XiaoNuoDeviceStatus.manualNote({online:false})||text==="等待开发板连接。")){$("voice-progress").textContent="USB已连接，点击开始后再说话。";}renderConnections();}catch(error){usbSnapshot=null;deviceConnected=false;deviceSpeakerReady=false;for(const id of ["voice-start","voice-play-device","device-enroll","device-audio-status"])$(id).disabled=true;renderConnections();throw error;}}
async function refresh(){
  const [status,prefs,memories]=await Promise.all([api("/api/v1/status"),api("/api/v1/preferences"),api("/api/v1/memories")]);
  preferences=prefs;
  $("voice-profile").value=prefs.voice_profile||"soft";
  $("connection").textContent="本地后端已连接";
  $("remote-state").textContent=status.local_llm_ready?"本机已就绪":status.remote_enabled?"远程已开启":"尚未启用";
  $("region-note").textContent="使用地区 "+status.usage_region+" · 部署地 "+(status.deployment_region==="UNDECIDED"?"未定":status.deployment_region);
  $("quiet-state").textContent=prefs.quiet?"保持安静":"正常互动";
  $("quiet-toggle").textContent=prefs.quiet?"解除安静模式":"进入安静模式";
  if(prefs.quiet)stopPlayback();
  const voice=await api("/api/v1/voice");deviceSpeakerReady=voice.device_playback_available===true;$("voice-play-device").disabled=voiceBusy||!deviceSpeakerReady||!lastAudioToken;$("voice-pill").textContent=voice.asr.last_success?"本机识别最近成功":"手动语音联调";$("voice-engine").textContent=voice.asr.provider==="vosk"?"离线中文识别":"本机中文识别";
  budgetCurrency=status.budget.currency;
  $("budget-currency").textContent=(budgetCurrency==="CNY"?"人民币":"USD")+" · 每自然月";
  $("budget-limit-label").textContent="每月总预算（"+(budgetCurrency==="CNY"?"元人民币":"美元")+"）";
  $("server-cost-label").textContent="服务器月费用（"+(budgetCurrency==="CNY"?"元人民币":"美元")+"）";
  $("spent").textContent=money(status.budget.api_cost_estimate);
  $("held").textContent=money(status.budget.outstanding_reservations);
  $("remaining").textContent=money(status.budget.remaining);
  $("confirmed").textContent=money(status.budget.owner_confirmed_cost);
  $("budget-limit").value=status.budget.budget;
  $("server-cost").value=status.budget.server_cost??"";
  $("budget-cost-note").textContent=status.budget.cost_note;
  $("reply-length").value=prefs.reply_length;
  $("quiet-start").value=prefs.quiet_hours_start;$("quiet-end").value=prefs.quiet_hours_end;$("owner-zone").value=prefs.timezone;
  for(const [id,key] of Object.entries({serious:"serious",followup:"followup_enabled",tone:"tone_analysis_enabled","memory-upload":"memory_upload_enabled","history-persist":"persist_history",greeting:"greeting_enabled","computer-wake":"computer_wake_enabled"}))$(id).checked=prefs[key];
  $("providers").replaceChildren();
  const reasons={UNSUPPORTED_USAGE_REGION:"当前使用地区未获支持",REGION_NOT_CONFIRMED:"地区与账号资格待确认",MISSING_CREDENTIAL_OR_MODEL:"尚未配置",PAID_CALLS_NOT_AUTHORIZED:"付费调用未授权",FREE_ACCOUNT_NOT_VERIFIED:"免费资格与调用待验证",PROVIDER_COOLDOWN:"暂时不可用"};
  for(const item of status.providers){const row=document.createElement("div");row.className="provider-row";const label=document.createElement("span");label.textContent=item.provider+" · "+(item.model||"模型未定");const state=document.createElement("span");state.textContent=reasons[item.block_reason]||(item.state==="healthy"?"最近调用成功":"尚未验证");row.append(label,state);$("providers").append(row);}
  $("memory-list").replaceChildren();
  if(!memories.items.length){const empty=document.createElement("p");empty.className="muted";empty.textContent="还没有保存记忆。普通聊天不会自动成为长期记忆。";$("memory-list").append(empty);}
  for(const memory of memories.items){const row=document.createElement("article");row.className="memory-item";const content=document.createElement("div");const text=document.createElement("p");text.textContent=memory.content;const meta=document.createElement("small");meta.textContent=(memory.allow_remote?"已允许作为模型上下文":"仅本地保存")+" · "+(memory.expires_at?"保留至 "+memory.expires_at.slice(0,10):"明确选择长期保留");content.append(text,meta);const actions=document.createElement("div");actions.className="memory-actions";const edit=document.createElement("button");edit.textContent="修改";edit.addEventListener("click",()=>{const editor=document.createElement("textarea");editor.value=memory.content;editor.maxLength=1000;const save=document.createElement("button");save.textContent="保存修改";save.addEventListener("click",()=>guarded(async()=>{await api("/api/v1/memories/"+memory.id,{method:"PATCH",body:JSON.stringify({content:editor.value,consent:true,expected_version:memory.version,allow_remote:memory.allow_remote})});await refresh();notice("记忆已修改。");}));content.replaceChildren(editor,save);edit.disabled=true;});const remove=document.createElement("button");remove.textContent="删除";remove.addEventListener("click",async()=>{try{await api("/api/v1/memories/"+memory.id,{method:"DELETE"});await refresh();notice("这条记忆已删除。");}catch(error){notice(error.message,true);}});actions.append(edit,remove);row.append(content,actions);$("memory-list").append(row);}
  await refreshNetwork(status);
  await refreshDevice();
  await refreshBindings();
  await refreshReminders();
}
async function guarded(action){try{await action();}catch(error){notice(error.message||"无法连接本地后端。",true);}}
function deviceResult(result){
  if(result.event==="mic_probe")return "已读取 "+result.samples+" 个采样点；没有保存或上传录音。\n信号范围："+result.min+" ～ "+result.max+"；平均值："+(result.mean??"未提供")+"；变化幅度："+(result.ac_rms??result.rms)+"。\n这些数值用于排查麦克风，语音识别仍未接入。";
  if(result.event==="i2c_scan")return result.addresses.length?"发现设备地址："+result.addresses.map(n=>"0x"+n.toString(16).toUpperCase()).join("、"):"没有发现屏幕，请检查接线。";
  if(result.event==="oled_ready")return "已启用 "+result.driver.toUpperCase()+" 屏幕，请以实物显示为准。";
  if(result.event==="setup_ready")return "手机连接热点："+result.ssid+"\n密码请看设备屏幕，再用浏览器打开 http://"+result.ip+" 设置家中 2.4GHz Wi-Fi。\n配网入口将在 "+Math.round(result.expires_seconds/60)+" 分钟后关闭。";
  if(result.event==="status")return result.wifi_connected?"Wi-Fi 已连接，地址："+result.ip:result.setup_active?"手机配网入口已开启。":"设备已回应，Wi-Fi 暂未连接。";
  return "设备已回应。";
}
function stopPlayback(){lastAudioToken=null;$("voice-play-device").disabled=true;const player=$("voice-audio");player.pause();player.removeAttribute("src");player.load();player.hidden=true;$("voice-playback-note").hidden=true;}
function showSpeech(result){stopPlayback();lastAudioToken=result.audio_url.split("/").pop();$("voice-play-device").disabled=voiceBusy||!deviceSpeakerReady;$("voice-audio").src=result.audio_url;$("voice-audio").hidden=false;$("voice-playback-note").hidden=false;}
function reminderWhen(item){return new Intl.DateTimeFormat("zh-CN",{timeZone:item.timezone,year:"numeric",month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit",hour12:false}).format(new Date(item.due_at||item.occurrence_at));}
function resetReminder(){reminderEdit=null;reminderDraftId=crypto.randomUUID();$("reminder-form").reset();$("reminder-editing").hidden=true;$("reminder-reset").hidden=true;$("reminder-save").textContent="保存提醒";}
async function refreshReminders(){
  const [reminders,events]=await Promise.all([api("/api/v1/reminders"),api("/api/v1/reminder-events")]);
  $("reminder-engine").textContent=reminders.last_error?"后台待检查":reminders.last_tick?"本机后台运行":"后台尚未启动";
  $("reminder-list").replaceChildren();$("reminder-events").replaceChildren();
  for(const event of events.items){const row=document.createElement("article");row.className="reminder-due";const detail=document.createElement("div");const title=document.createElement("strong");title.textContent=event.text;const time=document.createElement("small");time.textContent=reminderWhen(event);detail.append(title,time);const ack=document.createElement("button");ack.className="secondary";ack.textContent="已看过";ack.addEventListener("click",()=>guarded(async()=>{await api("/api/v1/reminder-events/"+event.id+"/acknowledge",{method:"POST",body:"{}"});await refreshReminders();}));row.append(detail,ack);$("reminder-events").append(row);}
  if(!reminders.items.length){const empty=document.createElement("p");empty.className="muted";empty.textContent="还没有提醒，也可以在文字互动里说“明天晚上八点提醒我背单词”。";$("reminder-list").append(empty);}
  for(const item of reminders.items){const row=document.createElement("article");row.className="memory-item";const detail=document.createElement("div");const title=document.createElement("p");title.textContent=item.text;const meta=document.createElement("small");meta.textContent=reminderWhen(item)+" · "+({none:"一次",daily:"每天",weekly:"每周"}[item.repeat_rule])+" · "+(item.enabled?"等待提醒":"已结束");detail.append(title,meta);const actions=document.createElement("div");actions.className="memory-actions";const edit=document.createElement("button");edit.textContent="修改";edit.addEventListener("click",()=>{reminderEdit=item;$("reminder-text").value=item.text;const offset=item.timezone==="Asia/Shanghai"?8*3600000:0;$("reminder-time").value=new Date(new Date(item.due_at).getTime()+offset).toISOString().slice(0,16);$("reminder-zone").value=item.timezone;$("reminder-repeat").value=item.repeat_rule;$("reminder-exception").checked=Boolean(item.quiet_exception);$("reminder-editing").hidden=false;$("reminder-reset").hidden=false;$("reminder-save").textContent="保存修改";$("reminder-form").scrollIntoView({block:"center"});});const cancel=document.createElement("button");cancel.textContent="取消";cancel.addEventListener("click",()=>guarded(async()=>{await api("/api/v1/reminders/"+item.id,{method:"DELETE"});if(reminderEdit?.id===item.id)resetReminder();await refreshReminders();notice("这条提醒已取消。");}));actions.append(edit,cancel);row.append(detail,actions);$("reminder-list").append(row);}
}
$("reminder-reset").addEventListener("click",resetReminder);
$("reminder-form").addEventListener("input",()=>{if(!reminderSaving)reminderDraftId=crypto.randomUUID();});
$("reminder-form").addEventListener("submit",event=>{event.preventDefault();if(reminderSaving)return;guarded(async()=>{reminderSaving=true;$("reminder-save").disabled=true;try{const zone=$("reminder-zone").value;const due=new Date($("reminder-time").value+(zone==="Asia/Shanghai"?"+08:00":"Z"));if(!Number.isFinite(due.getTime()))throw new Error("请填写有效的提醒时间。");const body={text:$("reminder-text").value,due_at:due.toISOString(),timezone:zone,repeat_rule:$("reminder-repeat").value,quiet_exception:$("reminder-exception").checked,request_id:reminderDraftId};if(reminderEdit)body.expected_version=reminderEdit.version;await api("/api/v1/reminders"+(reminderEdit?"/"+reminderEdit.id:""),{method:reminderEdit?"PATCH":"POST",body:JSON.stringify(body)});resetReminder();await refreshReminders();notice("提醒已保存，会显示在本机控制台。");}finally{reminderSaving=false;$("reminder-save").disabled=false;}});});
$("voice-profile").addEventListener("change",()=>guarded(async()=>{stopPlayback();await api("/api/v1/preferences",{method:"PATCH",body:JSON.stringify({voice_profile:$("voice-profile").value})});notice("声音已保存，可点击试听。");}));
$("voice-start").addEventListener("click",()=>guarded(async()=>{if(voiceBusy||!canStartVoice())return;voiceBusy=true;stopPlayback();$("voice-start").disabled=true;$("voice-stop").disabled=false;$("voice-progress").textContent="麦克风正在准备…";$("voice-transcript").textContent="等待这次识别结果…";const poll=setInterval(()=>Promise.all([refreshDevice(),refreshVoiceProgress()]).catch(()=>{}),500);try{const result=await api("/api/v1/voice/device",{method:"POST",body:JSON.stringify({consent:"local-only",seconds:6,request_id:crypto.randomUUID(),conversation_id:conversationId,reply_playback:$("voice-reply-target").value})});$("voice-transcript").textContent="听到："+result.transcript;message("你（语音）："+result.transcript,"user");if(result.reply){conversationId=result.reply.conversation_id;message("小糯："+result.reply.reply_text,"assistant");}else if(result.reply_error){message(errors[result.reply_error]||"AI 回复暂时不可用，已保留本次识别结果。","error");}if(result.speech)showSpeech(result.speech);$("voice-progress").textContent=result.playback_muted?"回复已显示，当前音量为 0。":result.playback_complete?"小糯已播完回复，可以再开始一轮。":result.speech_error?(errors[result.speech_error]||"本次语音未完整播放，文字回答已显示。"):result.speech?"识别完成，回答可在下方播放。":"识别完成，回复已显示。";await refresh();}catch(error){$("voice-progress").textContent=error.message;throw error;}finally{clearInterval(poll);voiceBusy=false;$("voice-start").disabled=!canStartVoice();$("voice-stop").disabled=true;}}));
$("voice-stop").addEventListener("click",()=>guarded(async()=>{stopPlayback();await api("/api/v1/voice/cancel",{method:"POST",body:"{}"});$("voice-progress").textContent="正在停止本次语音…";}));
$("voice-preview").addEventListener("click",()=>guarded(async()=>{if(voiceBusy||previewBusy)return;previewBusy=true;const button=$("voice-preview");button.disabled=true;$("voice-stop").disabled=false;$("voice-start").disabled=true;try{showSpeech(await api("/api/v1/voice/synthesize",{method:"POST",body:JSON.stringify({text:"你好，我是小糯。",request_id:crypto.randomUUID()})}));notice("声音已准备好，点击播放器试听。");}finally{previewBusy=false;button.disabled=voiceBusy;await refreshDevice();}}));
$("refresh").addEventListener("click",()=>guarded(refresh));
$("device-retry").addEventListener("click",()=>guarded(async()=>{await api("/api/v1/device/retry",{method:"POST",body:"{}"});await refreshDevice();notice("已开始尝试连接指定开发板。");}));
for(const button of document.querySelectorAll("[data-device-operation]"))button.addEventListener("click",()=>guarded(async()=>{button.disabled=true;try{const result=await api("/api/v1/device/command",{method:"POST",body:JSON.stringify({operation:button.dataset.deviceOperation})});$("device-result").textContent=deviceResult(result);await refreshDevice();}finally{button.disabled=!deviceConnected;}}));
$("quiet-toggle").addEventListener("click",()=>guarded(async()=>{await api("/api/v1/quiet",{method:"PUT",body:JSON.stringify({enabled:!preferences.quiet})});await refresh();notice(preferences.quiet?"安静模式已开启，普通互动不会自行解除。":"安静模式已解除。");}));
$("chat-form").addEventListener("submit",event=>{event.preventDefault();guarded(async()=>{const text=$("chat-text").value.trim();if(!text)return;message("你："+text,"user");$("chat-text").value="";const button=event.target.querySelector("button");button.disabled=true;try{const result=await api("/api/v1/chat",{method:"POST",body:JSON.stringify({text,request_id:crypto.randomUUID(),conversation_id:conversationId})});conversationId=result.conversation_id;message("小糯："+result.reply_text,"assistant");await refresh();}catch(error){message(error.message,"error");}finally{button.disabled=false;}});});
$("memory-form").addEventListener("submit",event=>{event.preventDefault();guarded(async()=>{await api("/api/v1/memories",{method:"POST",body:JSON.stringify({request_id:crypto.randomUUID(),content:$("memory-text").value,consent:$("memory-consent").checked,allow_remote:$("memory-remote").checked})});event.target.reset();await refresh();notice("记忆已保存。");});});
$("preferences-form").addEventListener("submit",event=>{event.preventDefault();guarded(async()=>{await api("/api/v1/preferences",{method:"PATCH",body:JSON.stringify({reply_length:$("reply-length").value,serious:$("serious").checked,followup_enabled:$("followup").checked,tone_analysis_enabled:$("tone").checked,memory_upload_enabled:$("memory-upload").checked,persist_history:$("history-persist").checked,greeting_enabled:$("greeting").checked,computer_wake_enabled:$("computer-wake").checked,quiet_hours_start:$("quiet-start").value,quiet_hours_end:$("quiet-end").value,timezone:$("owner-zone").value})});await refresh();notice("互动偏好已保存。");});});
$("budget-form").addEventListener("submit",event=>{event.preventDefault();guarded(async()=>{await api("/api/v1/budget",{method:"PUT",body:JSON.stringify(XiaoNuoBudget.formPayload(budgetCurrency,$("budget-limit").value,$("server-cost").value))});await refresh();notice("预算已保存，远程调用开关没有改变。");});});
$("export").addEventListener("click",()=>guarded(async()=>{const data=await api("/api/v1/privacy/export");const url=URL.createObjectURL(new Blob([JSON.stringify(data,null,2)],{type:"application/json"}));const link=document.createElement("a");link.href=url;link.download="xiaonuo-personal-data.json";link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}));
$("logout").addEventListener("click",()=>guarded(async()=>{stopPlayback();if(voiceBusy||previewBusy)await api("/api/v1/voice/cancel",{method:"POST",body:"{}"});await api("/api/v1/session/logout",{method:"POST"});csrf="";$("application").hidden=true;$("login").hidden=false;notice("已退出控制台。");}));
$("clear-data").addEventListener("click",()=>{$("clear-confirm").value="";$("clear-submit").disabled=true;$("clear-dialog").showModal();});
$("clear-cancel").addEventListener("click",()=>$("clear-dialog").close());
$("clear-confirm").addEventListener("input",()=>{$("clear-submit").disabled=$("clear-confirm").value!=="删除个人数据";});
$("clear-form").addEventListener("submit",event=>{event.preventDefault();if($("clear-confirm").value!=="删除个人数据")return;guarded(async()=>{await api("/api/v1/privacy/data",{method:"DELETE",body:JSON.stringify({confirm:"DELETE_PERSONAL_DATA"})});conversationId=null;$("messages").replaceChildren();$("clear-dialog").close();await refresh();notice("个人数据已清空，费用审计记录保留。");});});
guarded(async()=>{
  const fragment=new URLSearchParams(location.hash.slice(1));const ticket=fragment.get("ticket");history.replaceState(null,"",location.pathname);
  if(ticket){const result=await api("/api/v1/session/exchange",{method:"POST",body:JSON.stringify({ticket})});csrf=result.csrf;}
  else{const result=await api("/api/v1/session");csrf=result.csrf;}
  $("login").hidden=true;$("application").hidden=false;await refresh();
  if(preferences.greeting_enabled&&!document.hidden){const greeting=await api("/api/v1/greetings/visit",{method:"POST",body:"{}"});if(greeting.text)message("小糯："+greeting.text,"assistant");}
  setInterval(()=>{if(!document.hidden&&!$("application").hidden)refreshDevice().catch(()=>{});},3000);
  setInterval(()=>{if(!document.hidden&&!$("application").hidden)refreshReminders().catch(()=>{});},5000);
  setInterval(()=>{if(!document.hidden&&!$("application").hidden)refreshNetwork().catch(()=>{});},10000);
});

Object.assign(errors,{AUDIO_EXPIRED:"这段声音已过期，请重新准备。",DEVICE_SPEAKER_DISABLED:"设备播放尚未开放。",DEVICE_FIRMWARE_NEEDS_UPDATE:"开发板需要更新播放固件。",SPEAKER_AUDIO_TOO_LONG:"这段声音超过设备当前 8 秒限制，请在电脑播放，或让回答简短一点。",SPEAKER_PLAYBACK_CANCELLED:"设备播放已停止。",SPEAKER_REPLY_TIMEOUT:"设备没有确认播放完成，请检查连接。",SPEAKER_DEVICE_ERROR:"设备中止了播放，请检查连接和供电后重试。",SPEAKER_SEQUENCE_MISMATCH:"声音传输不完整，已停止播放。",DUPLICATE_REQUEST:"这次播放请求已经处理，请勿重复提交。"});
$("voice-play-device").addEventListener("click",()=>guarded(async()=>{
  if(voiceBusy||!lastAudioToken||!deviceSpeakerReady)return;
  const token=lastAudioToken;voiceBusy=true;$("voice-play-device").disabled=true;$("voice-start").disabled=true;$("voice-stop").disabled=false;$("voice-preview").disabled=true;$("voice-profile").disabled=true;$("voice-audio").pause();
  $("voice-progress").textContent="正在向小糯发送声音…";
  try{await api("/api/v1/voice/play-device",{method:"POST",body:JSON.stringify({token,request_id:crypto.randomUUID()})});$("voice-progress").textContent="设备已完成播放，请以实际听到的声音为准。";}
  finally{voiceBusy=false;$("voice-preview").disabled=false;$("voice-profile").disabled=false;await refreshDevice();}
}));

Object.assign(errors,{DEVICE_PROVISIONING_BUSY:"设备正在登记，请稍等。",DEVICE_PROVISIONING_TIMEOUT:"设备没有确认登记完成，凭据未激活，请重试。",DEVICE_PROVISIONING_UNCONFIRMED:"设备登记未获确认，凭据未激活。",DEVICE_ID_CHANGED:"连接的设备已经变化，请刷新后再登记。",DEVICE_BINDING_NOT_FOUND:"这条设备登记已失效，请刷新。",UNKNOWN_OR_DISABLED_COMMAND:"当前固件还不支持这个检查，请先更新固件。"});
async function refreshBindings(){
  const result=await api("/api/v1/devices");
  const active=result.items.filter(item=>!item.revoked_at&&item.provisioning_state==="bound");
  currentBinding=active.find(item=>item.hardware_id===deviceHardwareId)||(!deviceHardwareId&&active.length===1?active[0]:null);
  $("binding-state").textContent=currentBinding?currentBinding.name+" · 已在本机登记 · "+currentBinding.hardware_id:deviceHardwareId?"当前设备尚未登记 · "+deviceHardwareId:"连接新版固件后可登记设备。";
  $("device-unbind").disabled=!currentBinding;
  $("device-enroll").disabled=!deviceConnected||!deviceHardwareId||Boolean(currentBinding);
  const feedback=currentBinding?.audio_feedback||"unverified";
  $("speaker-acceptance").textContent=({unverified:"喇叭：尚未获得实际听感确认。",heard:"喇叭：你已确认听到正常声音。",no_sound:"喇叭：你反馈没有声音，仍需排查。",unclear:"喇叭：你反馈声音不清楚，仍需排查。"})[feedback];
  for(const button of document.querySelectorAll("[data-speaker-feedback]"))button.disabled=!currentBinding;
}
$("device-enroll").addEventListener("click",()=>guarded(async()=>{
  $("device-enroll").disabled=true;
  try{await api("/api/v1/devices/enroll",{method:"POST",body:JSON.stringify({name:"小糯"})});await refreshDevice();await refreshBindings();notice("设备已在本机登记，凭据没有显示或发送到网页。");}
  finally{await refreshBindings();}
}));
$("device-unbind").addEventListener("click",()=>guarded(async()=>{
  if(!currentBinding||!confirm("解除这台设备的绑定？旧凭据会立即失效，之后可以重新登记。"))return;
  const result=await api("/api/v1/devices/"+currentBinding.id,{method:"DELETE"});
  await refreshDevice();await refreshBindings();notice(result.device_credential_removed?"绑定已解除，设备内的凭据也已删除。":"本机绑定已解除。设备离线或尚未确认清除，旧凭据已无法使用。");
}));
for(const button of document.querySelectorAll("[data-speaker-feedback]"))button.addEventListener("click",()=>guarded(async()=>{
  if(!currentBinding)return;
  await api("/api/v1/devices/"+currentBinding.id+"/audio-feedback",{method:"PUT",body:JSON.stringify({result:button.dataset.speakerFeedback})});
  await refreshBindings();notice("实际听感已记录；这不会自动播放或开启输出。");
}));
$("device-audio-status").addEventListener("click",()=>guarded(async()=>{
  const button=$("device-audio-status");button.disabled=true;
  try{
    const result=await api("/api/v1/device/command",{method:"POST",body:JSON.stringify({operation:"audio_status"})});
    $("audio-diagnostic-summary").textContent=result.active?"设备正在处理播放数据，实际出声情况仍需确认。":result.last_event==="none"?"这次开机后尚未播放。这个检查没有打开功放或麦克风。":"最近一次写入 "+result.written_samples+" 个音频样本，输出幅度峰值 "+result.output_peak+"；这不代表已经听到声音。";
    $("audio-diagnostic-data").textContent=JSON.stringify(result,null,2);$("audio-diagnostic-details").hidden=false;
  }finally{button.disabled=!deviceConnected;}
}));

// SPDX-License-Identifier: AGPL-3.0-only
// Guided apply (#23c): pure logic of the configuration form. No DOM and no storage access at load.
// The apply policy mirrors cfg.APPLY_POLICY for the form fields; the device answer decides afterwards.
const CONFIG_FORM_POLICY={wifi_ssid:"verified_reboot",wifi_pass:"verified_reboot",wifi_networks:"verified_reboot",ntrip_enabled:"task_restart",ntrip_host:"task_restart",ntrip_port:"task_restart",ntrip_mount:"task_restart",ntrip_user:"task_restart",ntrip_pass:"task_restart",ntrip_fallback_host:"task_restart",ntrip_fallback_port:"task_restart",ntrip_fallback_mount:"task_restart",ntrip_fallback_user:"task_restart",ntrip_fallback_pass:"task_restart"};
const CONFIG_SECRET_FIELDS=["wifi_pass","ntrip_pass","ntrip_fallback_pass"];
const CONFIG_PORT_FIELDS=["ntrip_port","ntrip_fallback_port"];
const CONFIG_PRIMARY_CASTER_FIELDS=["ntrip_enabled","ntrip_host","ntrip_port","ntrip_mount","ntrip_user","ntrip_pass"];
// Fields whose value the restart marker may keep (no secret, no user name).
const RESTART_MARKER_FIELDS=["wifi_ssid","ntrip_host","ntrip_mount","ntrip_fallback_host","ntrip_fallback_mount"];
const CONFIG_FIELD_LABELS={wifi_ssid:"Wi-Fi name (SSID)",wifi_pass:"Wi-Fi password",wifi_networks:"Fallback networks",ntrip_enabled:"NTRIP client",ntrip_host:"Caster host",ntrip_port:"Caster port",ntrip_mount:"Mountpoint",ntrip_user:"Caster user name",ntrip_pass:"Caster password",ntrip_fallback_host:"Fallback caster host",ntrip_fallback_port:"Fallback caster port",ntrip_fallback_mount:"Fallback mountpoint",ntrip_fallback_user:"Fallback caster user name",ntrip_fallback_pass:"Fallback caster password"};
// One network per line, "SSID" keeps a saved password (password_set), "SSID:password" sets one; `saved` is the networks list of the device.
function parseNetworkLines(text,saved){
  const known=new Map((saved||[]).map(item=>[item.ssid,item]));
  return String(text||"").split(/\r?\n/).map(line=>{
    line=line.trim();if(!line)return null;const split=line.indexOf(":");
    if(split>=0)return{ssid:line.slice(0,split).trim(),password:line.slice(split+1)};
    const existing=known.get(line);
    return{ssid:line,password_set:Boolean(existing&&existing.password_set)};
  }).filter(Boolean);
}
// Saved passwords never come back from the device, so the textarea shows the SSIDs only.
function maskedNetworkText(networks){return(networks||[]).map(item=>item.ssid).join("\n");}
function sameNetworks(next,saved){
  saved=saved||[];if(next.length!==saved.length)return false;
  return next.every((item,index)=>typeof item.password!=="string"&&item.ssid===saved[index].ssid&&Boolean(item.password_set)===Boolean(saved[index].password_set));
}
function configFlag(value){return value===true||value==="1";}
// Only changed fields are sent: an empty secret field is "unchanged", ports compare as numbers.
function configChanges(values,baseline){
  const changes={};
  for(const key of Object.keys(CONFIG_FORM_POLICY)){
    const value=values[key];if(value===undefined)continue;
    if(CONFIG_SECRET_FIELDS.includes(key)){if(value!=="")changes[key]=value;}
    else if(key==="wifi_networks"){const next=Array.isArray(value)?value:parseNetworkLines(value,baseline.wifi_networks);if(!sameNetworks(next,baseline.wifi_networks))changes[key]=next;}
    else if(key==="ntrip_enabled"){if(configFlag(value)!==Boolean(baseline[key]))changes[key]=configFlag(value);}
    else if(CONFIG_PORT_FIELDS.includes(key)){if(Number(value)!==Number(baseline[key]))changes[key]=Number(value);}
    else if(String(value)!==String(baseline[key]??""))changes[key]=String(value);
  }
  return{changes,keys:Object.keys(changes).sort()};
}
// The baseline after a successful save: sent secrets become *_set flags, networks keep only password_set.
function savedBaseline(baseline,changes){
  const next=JSON.parse(JSON.stringify(baseline));
  for(const key of Object.keys(changes)){
    if(CONFIG_SECRET_FIELDS.includes(key)){delete next[key];next[key+"_set"]=true;}
    else if(key==="wifi_networks")next[key]=changes[key].map(item=>({ssid:item.ssid,password_set:typeof item.password==="string"?item.password!=="":Boolean(item.password_set)}));
    else next[key]=changes[key];
  }
  return next;
}
// What a save does: policy of the changed keys plus what already waits in the device memory (display only for `earlier`).
function configEffects(keys,staging){
  const waiting=staging&&Array.isArray(staging.keys)?staging.keys.filter(key=>typeof key==="string"):[];
  const wifi=keys.filter(key=>CONFIG_FORM_POLICY[key]==="verified_reboot").sort(),caster=keys.filter(key=>CONFIG_FORM_POLICY[key]==="task_restart").sort();
  const unknown=keys.some(key=>!CONFIG_FORM_POLICY[key]);
  return{restart:wifi.length>0||unknown||waiting.length>0||Boolean(staging&&staging.restart_required),reload:caster.length>0,wifi,caster,earlier:waiting.filter(key=>!keys.includes(key)).sort()};
}
// Required fields follow the change set; the ports always.
function configRequired(values,baseline){
  const keys=configChanges(values,baseline).keys,required=[...CONFIG_PORT_FIELDS];
  if(baseline.wifi_ssid||keys.some(key=>key.startsWith("wifi_")))required.push("wifi_ssid");
  const enabled=values.ntrip_enabled===undefined?Boolean(baseline.ntrip_enabled):configFlag(values.ntrip_enabled);
  if(enabled&&(baseline.ntrip_host||keys.some(key=>CONFIG_PRIMARY_CASTER_FIELDS.includes(key))))required.push("ntrip_host","ntrip_mount");
  return required.sort();
}
// The device names the field at the start of its 400 message (web._handle_config_put).
function configErrorField(code,message){
  if(code==="invalid_wifi_networks")return"wifi_networks";
  const named=code==="invalid_configuration"&&typeof message==="string"?/^The configuration is invalid: ([a-z0-9_]+)/.exec(message):null;
  return named&&CONFIG_FORM_POLICY[named[1]]?named[1]:null;
}
// A refusal (the device answered with 4xx/5xx) is definitive; anything else leaves the outcome unknown.
function apiFailure(error){
  const failure=error||{},status=Number.isInteger(failure.status)?failure.status:0,definitive=status>=400;
  return{status,code:typeof failure.code==="string"?failure.code:null,reasons:Array.isArray(failure.reasons)?failure.reasons.filter(reason=>typeof reason==="string"):null,definitive,uncertain:!definitive};
}
// Restart and result logic: a restart shows as a new boot id, a result belongs to a request only by its apply id.
const CONFIG_ROUTE="device/connections/internet",FIELD_SETUP_ROUTE="measure/setup",RESTART_MARKER_KEY="rtk-restart-watch";
const RESTART_TIMING={probeMs:2000,probeTimeoutMs:3000,requestTimeoutMs:3000,backNoticeMs:1500,backDeadlineMs:90000,notRestartedMs:20000,resultDeadlineMs:90000,settleDeadlineMs:60000,ntripObserveMs:30000,ntripSettleMs:2000,markerMaxAgeMs:600000};
// Top-level marker fields (sessionStorage, this tab only); `attempt` keeps key names plus RESTART_MARKER_FIELDS.
const RESTART_MARKER_KEYS=["kind","route","started_ms","phase","apply_id","boot_id","uncertain","target_ssid","host","attempt","sections","kept","result","outcome"];
const RESTART_ANSWER_FLAGS={apply:"applying",restore:"restored",reboot:"restarting"};
function plainObject(value){return Object.prototype.toString.call(value)==="[object Object]";}
function stringItems(value){return Array.isArray(value)?value.filter(item=>typeof item==="string"):[];}
function newApplyId(random){return Array.from((random||(n=>crypto.getRandomValues(new Uint8Array(n))))(8),byte=>byte.toString(16).padStart(2,"0")).join("");}
// The decoded JSON answer of apply, restore or reboot; a Response object or any other shape is no acceptance.
function restartAnswerAccepted(kind,payload){return Object.prototype.hasOwnProperty.call(RESTART_ANSWER_FLAGS,kind)&&plainObject(payload)&&payload[RESTART_ANSWER_FLAGS[kind]]===true;}
function restoreScope(payload){const answer=plainObject(payload)?payload:{};return{sections:stringItems(answer.sections),kept:stringItems(answer.kept)};}
// Wi-Fi verdict codes of net.py and cfg.py; the texts complete "Not applied: {cause}."
const CONFIG_FAILURE_CAUSES={access_point_not_found:["ui.guided_cause_access_point_not_found","the Wi-Fi network was not found"],authentication_failed:["ui.guided_cause_authentication_failed","the Wi-Fi password was rejected"],association_failed:["ui.guided_cause_association_failed","the device could not join the Wi-Fi network"],connection_timeout:["ui.guided_cause_connection_timeout","the connection attempt timed out"],connection_lost:["ui.guided_cause_connection_lost","the connection was lost during the check"],apply_interrupted:["ui.guided_cause_apply_interrupted","a restart interrupted the check"],setup_mode:["ui.guided_cause_setup_mode","the device started in setup mode before the check"],storage_error:["ui.guided_cause_storage_error","the device could not store the change"]};
function failureCause(code){const cause=Object.prototype.hasOwnProperty.call(CONFIG_FAILURE_CAUSES,code)?CONFIG_FAILURE_CAUSES[code]:null;return cause?tr(cause[0],cause[1]):tr("ui.guided_cause_unknown","unknown cause ({code})",{code:String(code??"?")});}
// Symbol of a step state; the panel always shows it next to the step name and its detail, never as colour alone.
const CONFIG_STEP_MARKS={ok:"✓",wait:"…",off:"○",fail:"✕"};
function restartStepLabel(step){return step==="wifi"?tr("ui.guided_step_wifi","Wi-Fi"):step==="storage"?tr("ui.guided_step_storage","Survey storage"):step==="ntrip"?"NTRIP":"HTTP";}
// A parked client (retry limit) only tries again after a saved caster change, a Wi-Fi loss or a restart: fail, not wait.
function ntripStep(live){
  const network=live&&live.network||{},state=network.ntrip_state,config=network.ntrip_config;
  const parked=["backoff","auth_error","mount_error"].includes(state)&&network.last_ntrip_error_code==="retries_exhausted";
  const step=(next,detail)=>({state:next,detail,parked});
  if(state==="streaming")return step("ok",tr("ui.guided_step_streaming","Receiving corrections"));
  if(state==="auth_error")return step("fail",tr("ui.guided_step_credentials_rejected","Credentials rejected"));
  if(state==="mount_error")return step("fail",tr("ui.guided_step_mount_not_found","Mountpoint not found"));
  if(parked)return step("fail",tr("ui.guided_step_retry_limit","Stopped after the retry limit"));
  if(state==="disabled"&&config==="switched_off")return step("off",tr("ui.guided_step_switched_off","Switched off"));
  if(state==="disabled"&&config==="incomplete")return step("off",tr("ui.guided_step_not_set_up","Not set up"));
  // Before the client started (also in safe mode) the device reports "disabled" without a configuration state.
  if(state==="disabled")return step("wait",tr("ui.guided_step_not_started","Not started yet"));
  if(state==="waiting_wifi")return step("wait",tr("ui.guided_step_waiting_wifi","Waiting for Wi-Fi"));
  if(state==="connecting")return step("wait",tr("ui.guided_step_connecting","Connecting"));
  if(state==="backoff")return step("wait",tr("ui.guided_step_retrying","Retrying shortly"));
  return step("wait",tr("ui.guided_step_waiting","Waiting"));
}
// HTTP from the session probe, the rest from /api/ui/live; `value` is the device state behind each step.
function restartSteps(live,httpBack){
  const http={step:"http",state:httpBack?"ok":"wait",detail:httpBack?tr("ui.guided_step_reachable","Reachable"):tr("ui.guided_step_unreachable","Not reachable yet"),value:null};
  if(!httpBack){const detail=tr("ui.guided_step_waiting_device","Waiting for the device");return[http,...["wifi","ntrip","storage"].map(step=>({step,state:"wait",detail,value:null}))];}
  const network=live&&live.network||{},access=network.access_state??null,startup=live&&live.tracking_startup?live.tracking_startup.state??null:null;
  const wifi=access==="ONLINE"?["ok",tr("ui.guided_step_connected","Connected")]:access==="SETUP"?["off",tr("ui.guided_step_hotspot_only","Hotspot only")]:access==="RECOVERY"?["fail",tr("ui.guided_step_no_wifi","No Wi-Fi; hotspot on")]:access==="CONNECTING"||access==="APPLYING"?["wait",tr("ui.guided_step_connecting","Connecting")]:["wait",tr("ui.guided_step_waiting","Waiting")];
  const storage=startup==="ready"?["ok",tr("ui.guided_step_ready","Ready")]:startup==="error"?["fail",tr("ui.guided_step_error","Reports an error")]:["wait",tr("ui.guided_step_loading","Loading")];
  return[http,{step:"wifi",state:wifi[0],detail:wifi[1],value:access},Object.assign({step:"ntrip",value:network.ntrip_state??null},ntripStep(live)),{step:"storage",state:storage[0],detail:storage[1],value:startup}];
}
function stepsSettled(steps){return Array.isArray(steps)&&steps.length>0&&steps.every(item=>item&&item.state!=="wait");}
// Only a different boot id proves a restart; uptime does not (a fresh process may keep its id).
function bootChanged(marker,live){const id=live&&live.system?live.system.boot_id:undefined;return typeof id==="string"?id!==marker.boot_id:null;}
// A final result is this request's only with the same apply id; any other final result is merely observed.
function judgeConfigResult(marker,result){
  const status=plainObject(result)?result.status:null;
  if(status!=="connected"&&status!=="failed")return{state:"none"};
  const details=plainObject(result.details)?result.details:{};
  const own=Boolean(marker)&&typeof marker.apply_id==="string"&&typeof result.apply_id==="string"&&result.apply_id===marker.apply_id;
  const facts=status==="connected"?{ssid:typeof details.ssid==="string"?details.ssid:""}:{code:typeof details.code==="string"?details.code:null,restored:stringItems(details.restored)};
  return Object.assign(own?{state:status}:{state:"observed",reported:status},facts);
}
// The device decided to set these keys back; it wrote that before finishing, so the note claims no completed rollback.
function restoredNote(restored){const labels=stringItems(restored).map(key=>localText(CONFIG_FIELD_LABELS[key]||key));return labels.length?tr("ui.guided_marked_set_back","Marked to be set back:")+" "+labels.join(", "):null;}
function reportedSettingsNote(config){
  if(!plainObject(config))return null;
  const wifi=config.wifi_ssid?capacityText("Wi-Fi ‘{ssid}’",{ssid:String(config.wifi_ssid)}):tr("ui.guided_wifi_not_set","Wi-Fi not set");
  const caster=config.ntrip_host?capacityText("caster {host}:{port}/{mount}",{host:String(config.ntrip_host),port:String(config.ntrip_port??""),mount:String(config.ntrip_mount??"")}):tr("ui.guided_no_caster","no caster");
  return tr("ui.guided_reported_settings","Settings the device reports now:")+" "+[wifi,caster,config.ntrip_enabled?tr("ui.guided_ntrip_on","NTRIP on"):tr("ui.guided_ntrip_off","NTRIP off")].join(" · ");
}
function configFileNote(live){return live&&Array.isArray(live.config_warnings)&&live.config_warnings.some(item=>item&&item.key==="config_file")?tr("ui.guided_config_file_unread","The device could not read its saved configuration file at start-up; the settings shown may be default values."):null;}
// RECOVERY also follows a finished rollback when the previous network is out of reach as well: no station link, nothing more.
function wifiConnectionNote(live){
  const access=live&&live.network?live.network.access_state:undefined;
  if(access==="ONLINE")return tr("ui.guided_wifi_now_connected","Wi-Fi now: connected.");
  if(access==="CONNECTING"||access==="APPLYING")return tr("ui.guided_wifi_now_connecting","Wi-Fi now: connecting …");
  if(access==="SETUP")return tr("ui.guided_wifi_now_setup","Wi-Fi now: no connection – only the setup hotspot is on.");
  if(access==="RECOVERY")return tr("ui.guided_wifi_now_recovery","Wi-Fi now: no connection to a Wi-Fi network – the rescue hotspot is on and the device keeps trying.");
  return tr("ui.guided_wifi_now_unknown","Wi-Fi now: unknown.");
}
function stepNotes(steps,settleTimedOut){
  const notes=[];
  for(const item of Array.isArray(steps)?steps:[]){
    if(item.step==="ntrip"&&item.value==="auth_error")notes.push(tr("ui.guided_note_ntrip_credentials","NTRIP: credentials rejected – check the caster settings."));
    if(item.step==="ntrip"&&item.value==="mount_error")notes.push(tr("ui.guided_note_ntrip_mount","NTRIP: mountpoint not found – check the caster settings."));
    if(item.step==="ntrip"&&item.parked)notes.push(tr("ui.guided_note_ntrip_parked","NTRIP: the client stopped after its retry limit – check the caster settings and save them; saving, a Wi-Fi loss or a restart starts a new round."));
    if(item.step==="storage"&&item.state==="fail")notes.push(tr("ui.guided_note_storage_error","Survey storage reports an error – open Status for details."));
    if(settleTimedOut&&item.state==="wait")notes.push(capacityText("{step} still waiting after 60 s – state: {state}.",{step:restartStepLabel(item.step),state:item.value==null?localText("unknown"):String(item.value)}));
  }
  return notes;
}
function restartWarningText(live){
  const parts=[],line=live&&live.active_line;
  if(live&&live.measurement_progress&&live.measurement_progress.active)parts.push(tr("ui.guided_warn_measurement","A measurement is running. The restart cancels it; points already saved are kept."));
  if(line&&line.state==="recording")parts.push(capacityText("Line ‘{name}’ is recording. The restart interrupts the recording; points already saved are kept.",{name:line.name==null?"":String(line.name)}));
  return parts.join(" ");
}
// One step of the watch: each phase checks its deadline before its data. In `settling` the outcome is marker.result.
function restartOutcome(marker,obs,nowMs){
  const seen=obs||{},reboot=marker.kind==="reboot",phase=marker.phase;
  if(phase==="final")return{phase:"final",outcome:marker.outcome||null};
  if(phase==="settling"){
    const done=nowMs-seen.settledFromMs>=RESTART_TIMING.settleDeadlineMs||stepsSettled(seen.steps);
    if(reboot)return done?{phase:"final",outcome:{state:marker.uncertain?"restart_observed":"done"}}:{phase:"settling",outcome:null};
    return{phase:done?"final":"settling",outcome:marker.result||null};
  }
  if(phase==="verifying"){
    if(nowMs-seen.resumedMs>=RESTART_TIMING.resultDeadlineMs)return{phase:"final",outcome:{state:"open"}};
    const changed=bootChanged(marker,seen.live);
    if(changed===false)return{phase:"restarting",outcome:null};
    if(reboot)return{phase:changed?"settling":"verifying",outcome:null};
    const judged=judgeConfigResult(marker,seen.result);
    return judged.state==="none"?{phase:"verifying",outcome:null}:{phase:"settling",outcome:judged};
  }
  if(nowMs-marker.started_ms>=RESTART_TIMING.backDeadlineMs)return{phase:"final",outcome:{state:"timeout"}};
  if(seen.probe==="authenticated"){
    const changed=bootChanged(marker,seen.live);
    if(changed===true)return{phase:reboot?"settling":"verifying",outcome:null};
    if(changed===false&&nowMs-seen.sameBootSinceMs>=RESTART_TIMING.notRestartedMs)return{phase:"final",outcome:{state:"not_restarted",uncertain:Boolean(marker.uncertain)}};
  }
  return{phase:"restarting",outcome:null};
}
// Text, notes and actions for an outcome ({state, …} from restartOutcome or judgeConfigResult) or, without one, for the phase.
// historic_boot/historic_unknown take an observed judgement ({reported, ssid, code}) and no marker.
function restartOutcomeView(outcome,marker,context){
  const seen=context||{},own=marker||{},facts=plainObject(outcome)?outcome:{},state=typeof outcome==="string"?outcome:facts.state;
  const host=typeof own.host==="string"?own.host:"",restore=own.kind==="restore";
  const view=(kind,text,notes,actions)=>({kind,text,notes:notes.filter(Boolean),actions});
  const reported=()=>facts.reported==="connected"?capacityText("connected to ‘{ssid}’",{ssid:facts.ssid||""}):capacityText("not applied: {cause}",{cause:failureCause(facts.code)});
  const settings=()=>{const note=reportedSettingsNote(seen.config);return note?[note,configFileNote(seen.live)]:[];};
  const steps=()=>stepNotes(seen.steps,seen.settleTimedOut);
  const ssid=facts.ssid||own.target_ssid||"";
  if(state==="connected"&&restore)return view(state,capacityText("Backup restored: connected to ‘{ssid}’.",{ssid}),[
    JSON.stringify(stringItems(own.sections))==='["connections"]'?tr("ui.guided_restore_older_backup","This backup comes from older firmware and contained only the Wi-Fi and NTRIP settings."):null,
    stringItems(own.kept).includes("wifi")?tr("ui.guided_restore_kept_wifi","The backup contains no Wi-Fi network; the device kept its Wi-Fi access."):null,...steps()],["field_setup","close"]);
  if(state==="connected")return view(state,host?capacityText("Applied: connected to ‘{ssid}’. Portal: http://{host}/",{ssid,host}):capacityText("Applied: connected to ‘{ssid}’.",{ssid}),[...settings(),...steps()],["field_setup","close"]);
  if(state==="failed"){
    const cause=failureCause(facts.code),caster=!restore&&stringItems(own.attempt&&own.attempt.keys).some(key=>CONFIG_FORM_POLICY[key]==="task_restart");
    const text=restore?capacityText("Backup restored without its Wi-Fi settings: {cause}. The device returns to the previous Wi-Fi settings; the other settings from the backup apply.",{cause}):capacityText("Not applied: {cause}. The device returns to the previous Wi-Fi settings.",{cause});
    return view(state,text,[restoredNote(facts.restored),...settings(),wifiConnectionNote(seen.live),caster?tr("ui.guided_caster_kept","The caster changes from this save already took effect and stay in effect."):null],["edit","close"]);
  }
  if(state==="observed")return view(state,capacityText("The device reports: {reported}. This result does not belong to this request for certain.",{reported:reported()}),[...settings(),wifiConnectionNote(seen.live)],["edit","close"]);
  if(state==="historic_boot")return view(state,capacityText("Configuration result since the last device start: {reported}.",{reported:reported()}),[],["edit","close"]);
  if(state==="historic_unknown")return view(state,capacityText("Last recorded configuration result (time unknown): {reported}.",{reported:reported()}),[],["edit","close"]);
  if(state==="timeout"){
    const text=own.target_ssid?capacityText("No answer after 90 s – connect the phone to ‘{ssid}’ and reload the page.",{ssid:String(own.target_ssid)}):tr("ui.guided_timeout_device_network","No answer after 90 s – connect the phone to the device's network and reload the page.");
    return view(state,text,[host?capacityText("Portal: http://{host}/",{host}):null],host?["open_portal","close"]:["close"]);
  }
  if(state==="not_restarted")return view(state,facts.uncertain||own.uncertain?tr("ui.guided_outcome_not_restarted_uncertain","It is not clear whether the device received the request; it did not restart."):tr("ui.guided_outcome_not_restarted","The device did not restart; the change is not applied yet."),[],own.kind==="apply"?["retry_apply","close"]:["close"]);
  if(state==="done")return view(state,tr("ui.guided_outcome_done","Restart complete."),steps(),["close"]);
  if(state==="restart_observed")return view(state,tr("ui.guided_outcome_restart_observed","A restart was observed. The answer to the request was lost, so it is not certain that this request caused it."),steps(),["close"]);
  if(state==="open")return view(state,tr("ui.guided_outcome_open","No final result yet. The steps above show the current state; reload this page later."),[],["close"]);
  const phase=seen.phase||own.phase||"restarting";
  if(phase==="resuming")return view(phase,tr("ui.guided_phase_resuming","Checking whether the device restarted …"),[],[]);
  if(phase==="back")return view(phase,tr("ui.guided_phase_back","The device is reachable again – sign in to see the result."),[],[]);
  if(phase==="verifying")return view(phase,own.kind==="reboot"?tr("ui.guided_phase_verifying_reboot","Waiting for the device to start …"):tr("ui.guided_phase_verifying","Checking the new Wi-Fi connection (the device must hold it for 10 s) …"),[],[]);
  if(phase==="settling")return view(phase,tr("ui.guided_phase_settling","Restart detected – checking whether the device is ready …"),[],[]);
  return view(phase,tr("ui.guided_phase_restarting","The device is restarting …"),[],[]);
}
// The resume marker: every storage access may throw (private mode, blocked storage); the page works without it.
function clearRestartMarker(storage){try{storage.removeItem(RESTART_MARKER_KEY);}catch(_){}}
function readRestartMarker(storage,nowMs){
  let marker=null;
  try{const text=storage.getItem(RESTART_MARKER_KEY);if(text==null)return null;marker=JSON.parse(text);}catch(_){marker=null;}
  if(plainObject(marker)&&marker.v===1&&Number.isFinite(marker.started_ms)&&nowMs-marker.started_ms<=RESTART_TIMING.markerMaxAgeMs)return marker;
  clearRestartMarker(storage);return null;
}
// An attempt as the marker keeps it: key names plus the endpoint fields, never a secret, a user name or the networks.
function markerAttempt(attempt){
  const source=plainObject(attempt)?attempt:{},kept={keys:stringItems(source.keys)};
  for(const key of RESTART_MARKER_FIELDS)if(typeof source[key]==="string")kept[key]=source[key];
  return kept;
}
// Never a password, a caster user name or the fallback networks (F6): only listed fields, attempt reduced to names and endpoints.
function writeRestartMarker(storage,marker){
  const source=marker||{},kept={v:1};
  for(const key of RESTART_MARKER_KEYS)if(source[key]!==undefined)kept[key]=source[key];
  if(kept.attempt!==undefined&&kept.attempt!==null)kept.attempt=markerAttempt(source.attempt);
  try{storage.setItem(RESTART_MARKER_KEY,JSON.stringify(kept));return true;}catch(_){return false;}
}
// The attempt of a save for the marker and "Edit again".
function restartAttempt(changes){const source=plainObject(changes)?changes:{};return markerAttempt(Object.assign({},source,{keys:Object.keys(source).sort()}));}
// The watch of one restart without DOM (E7, E18, E20, E21). deps: now, sleep, probe, live, result, save, reload, render, alive.
// After every await it reads now(), then alive() (false ends the watch without any further call), then the deadline, then the data.
// The returned outcome is the final one, {state:"back"} after the reload, or null when the watch was ended from outside.
async function watchRestart(marker,deps){
  const t=Object.assign({},RESTART_TIMING,deps.timing),seen={steps:restartSteps(null,false),sameBootSinceMs:null,settleTimedOut:false};
  const ask=async source=>{try{const value=await source();return plainObject(value)?value:null;}catch(_){return null;}};
  const probe=async()=>{try{return await deps.probe();}catch(_){return"unreachable";}};
  let now=deps.now();
  const show=outcome=>deps.render({phase:marker.phase,outcome:outcome||null,steps:seen.steps,live:seen.live||null,settleTimedOut:seen.settleTimedOut});
  const enter=phase=>{
    marker.phase=phase;
    if(phase==="verifying")seen.resumedMs=now;
    if(phase==="settling")seen.settledFromMs=now;
    if(phase==="restarting"){seen.sameBootSinceMs=null;seen.steps=restartSteps(null,false);}
    deps.save(marker);
  };
  const finish=outcome=>{
    seen.settleTimedOut=marker.phase==="settling"&&!stepsSettled(seen.steps);
    marker.phase="final";marker.outcome=outcome;deps.save(marker);show(outcome);return outcome;
  };
  if(!deps.alive())return null;
  if(marker.phase==="final")return marker.outcome||null;
  // A request without its answer counts as uncertain; after the sign-in the device is checked again.
  if(marker.phase==="requested"){marker.uncertain=true;marker.phase="restarting";}
  if(marker.phase==="back")marker.phase="verifying";
  if(marker.phase==="verifying")seen.resumedMs=now;
  if(marker.phase==="settling")seen.settledFromMs=now;
  show(marker.phase==="settling"?marker.result:null);
  for(;;){
    if(marker.phase==="restarting"){
      const answer=await probe();now=deps.now();if(!deps.alive())return null;
      seen.probe=answer;seen.live=null;
      if(answer==="authenticated"){seen.live=await ask(deps.live);now=deps.now();if(!deps.alive())return null;}
      if(bootChanged(marker,seen.live)===false){if(seen.sameBootSinceMs===null)seen.sameBootSinceMs=now;}
      else seen.sameBootSinceMs=null;
      const next=restartOutcome(marker,seen,now);
      if(next.phase==="final")return finish(next.outcome);
      if(answer==="signed_out"){
        enter("back");seen.steps=restartSteps(null,true);show();
        await deps.sleep(t.backNoticeMs);now=deps.now();if(!deps.alive())return null;
        deps.reload();return{state:"back"};
      }
      if(next.phase!=="restarting"){enter(next.phase);if(next.phase==="settling"){seen.steps=restartSteps(seen.live,true);show();}continue;}
      show();
      await deps.sleep(t.probeMs);now=deps.now();if(!deps.alive())return null;
      continue;
    }
    if(marker.phase==="verifying"){
      seen.live=await ask(deps.live);now=deps.now();if(!deps.alive())return null;
      // HTTP counts as reachable only after an answer of this round (live, or the result below).
      seen.steps=restartSteps(seen.live,seen.live!==null);seen.result=null;
      let next=restartOutcome(marker,seen,now);
      if(next.phase==="final")return finish(next.outcome);
      if(next.phase==="restarting"){enter("restarting");show();continue;}
      if(next.phase==="verifying"&&marker.kind!=="reboot"){
        seen.result=await ask(deps.result);now=deps.now();if(!deps.alive())return null;
        if(!seen.live&&seen.result)seen.steps=restartSteps(null,true);
        next=restartOutcome(marker,seen,now);
        if(next.phase==="final")return finish(next.outcome);
      }
      if(next.phase==="settling"){if(next.outcome)marker.result=next.outcome;enter("settling");show(marker.result);continue;}
      show();
      await deps.sleep(t.probeMs);now=deps.now();if(!deps.alive())return null;
      continue;
    }
    // settling: the result (if any) stands; the steps are watched until none waits or the deadline passes.
    const next=restartOutcome(marker,seen,now);
    if(next.phase==="final")return finish(next.outcome);
    await deps.sleep(t.probeMs);now=deps.now();if(!deps.alive())return null;
    const live=await ask(deps.live);now=deps.now();if(!deps.alive())return null;
    if(live){seen.live=live;seen.steps=restartSteps(live,true);}
    if(restartOutcome(marker,seen,now).phase!=="final")show(marker.result);
  }
}

// SPDX-License-Identifier: AGPL-3.0-only
// attrs are set as attributes (a false-valued property such as spellcheck would be skipped or coerced).
function configField(config,name,label,type="text",help="",attrs={}){
  const input=h("input",{name,type,value:config[name]||"",placeholder:type==="password"?localText("Leave empty to keep saved value"):""});
  for(const[key,value]of Object.entries(attrs))if(value!==false&&value!=null)input.setAttribute(key,value===true?"":String(value));
  return h("label",{class:"field"},h("span",{text:label}),input,help?h("small",{text:help}):[]);
}
// Restore (#23c E15): the scope text and a warning confirm it, then the shared guided restart takes over.
// A refusal stays a notice in the backup card (Maintenance); the unsaved-changes guard is not touched.
const RESTORE_SCOPE_TEXT="This replaces Wi-Fi and NTRIP settings, the measurement rules and method, and the field setup (antenna height, reference frame, epoch), then restarts the receiver. Backups from older firmware contain only Wi-Fi and NTRIP settings.";
let restoreInFlight=false;
function restoreRefusedText(answer){
  if(answer.state==="not_sent")return tr("ui.guided_not_sent","No answer from the device; nothing was sent. Try again.");
  const failure=answer.failure||{},message=String(failure.message||"");
  if(failure.status===400&&failure.code==="invalid_backup"){
    const reason=message.replace(/^The backup is invalid:\s*/,"").trim();
    return capacityText("Backup not restored: {reason} Nothing was changed.",{reason:/[.!?]$/.test(reason)?reason:reason+"."});
  }
  if(failure.status===409)return tr("ui.guided_restore_busy","The device is still checking an earlier change. Nothing was changed; try again after the restart.");
  if(failure.status===413)return tr("ui.guided_restore_too_large","The file is too large for a configuration backup. Nothing was changed.");
  if(failure.status===500&&failure.code==="stage_failed")return tr("ui.guided_restore_stage_failed","The device could not store the whole backup. Some settings (measurement rules, field setup, caster) may already apply; check the device before you try again.");
  return localText(message||"HTTP "+failure.status);
}
function showRestoreNotice(message){
  const section=main.querySelector(".config-backup");
  if(!section){showError(new Error(message));return;}
  const old=section.querySelector('[data-notice="restore"]');if(old)old.remove();
  const node=notice(message,"warn");node.dataset.notice="restore";section.append(node);
}
async function restoreConfiguration(){
  const input=h("input",{type:"file",accept:"application/json,.json"});
  input.addEventListener("change",async()=>{
    if(!input.files[0]||restoreInFlight)return;
    restoreInFlight=true;
    try{
      const text=await input.files[0].text(),edited=Boolean(main.querySelector('.configuration-layout[data-dirty="true"]')),scope=localText(RESTORE_SCOPE_TEXT);
      const accepted=await confirmRestart({title:"Restore configuration",description:edited?scope+" "+tr("ui.guided_restore_discards_edits","Unsaved changes on this page are discarded."):scope,submit:"Restore and restart"});
      if(!accepted)return;
      const old=main.querySelector('.config-backup [data-notice="restore"]');if(old)old.remove();
      const answer=await requestRestart("restore",id=>api.post("/api/config/restore?apply_id="+id,text),{target_ssid:null,attempt:null});
      if(answer.state==="accepted"||answer.state==="uncertain"){releaseDirtyGuard();startRestartWatch("restore",answer.marker);}
      else showRestoreNotice(restoreRefusedText(answer));
    }finally{restoreInFlight=false;}
  });
  input.click();
}


// V15.7.1 retains the complete NTRIP controls in the current UI. The API already
// supports these values; keeping this renderer separate also makes the secret
// retention and numeric conversions explicit.
// Guided apply (#23c): the form sends only changed values, shows their effect before saving and keeps a saved baseline
// (form.configBaseline) from which the dirty state is the difference. pendingConfigEdit is filled by "Edit again".
let pendingConfigEdit=null,ntripWatchSerial=0;
const CONFIG_OFF={autocapitalize:"off",spellcheck:"false"};
// The limits mirror cfg_validation.validate_settings; "[^\/]*" is the slash rule in a form valid for the v flag of the pattern attribute.
const CONFIG_FIELD_ATTRS={wifi_ssid:{maxLength:32,...CONFIG_OFF},wifi_pass:{maxLength:63,minLength:8},ntrip_host:{maxLength:64,...CONFIG_OFF},ntrip_port:{inputMode:"numeric",min:1,max:65535},ntrip_mount:{maxLength:64,pattern:"[^\\/]*",...CONFIG_OFF},ntrip_user:{maxLength:64,...CONFIG_OFF},ntrip_fallback_host:{maxLength:64,...CONFIG_OFF},ntrip_fallback_port:{inputMode:"numeric",min:1,max:65535},ntrip_fallback_mount:{maxLength:64,pattern:"[^\\/]*",...CONFIG_OFF},ntrip_fallback_user:{maxLength:64,...CONFIG_OFF}};
// The staging block of GET /api/config; every missing or mistyped part falls back, so an older firmware needs no revision.
function configStaging(config){
  const staging=config&&plainObject(config.staging)?config.staging:{};
  return{revision:typeof staging.revision==="string"?staging.revision:null,keys:stringItems(staging.keys),restart_required:staging.restart_required===true};
}
function configLabels(keys){return keys.map(key=>localText(CONFIG_FIELD_LABELS[key]||key)).join(", ");}
// Read the controls directly: FormData skips the disabled ones while a request runs.
function configFormValues(form){
  const values={};
  for(const name of Object.keys(CONFIG_FORM_POLICY))if(form.elements[name])values[name]=form.elements[name].value;
  return values;
}
// Required fields, dirty state and marker follow the difference to the saved baseline.
function syncConfigForm(form){
  const base=form.configBaseline,values=configFormValues(form),changed=configChanges(values,base).keys.length>0,required=configRequired(values,base);
  for(const name of Object.keys(CONFIG_FORM_POLICY))if(form.elements[name]&&form.elements[name].tagName!=="SELECT")form.elements[name].required=required.includes(name);
  form.dataset.dirty=String(changed);
  const marker=form.querySelector(".unsaved-marker");if(marker)marker.hidden=!changed;
}
function clearFieldError(form,name){
  const control=name&&form.elements[name],note=name&&form.querySelector("#"+name+"-error");
  if(control){control.removeAttribute("aria-invalid");control.removeAttribute("aria-describedby");}
  if(note)note.remove();
}
function showFieldError(form,name,message){
  const control=form.elements[name];if(!control)return;
  clearFieldError(form,name);
  const reason=String(message||"").replace(/^The configuration is invalid: /,"");
  control.setAttribute("aria-invalid","true");control.setAttribute("aria-describedby",name+"-error");
  (control.closest("label")||control.parentNode).append(h("small",{class:"field-error",id:name+"-error",textRaw:capacityText("Not accepted by the device: {reason}",{reason})}));
}
// Earlier staged changes (values never shown); rebuilt whenever the baseline's staging changes.
function syncStagedNote(form){
  const old=form.querySelector(".config-staged");if(old)old.remove();
  const staging=configStaging(form.configBaseline);if(!staging.keys.length)return;
  form.prepend(h("section",{class:"config-staged",role:"region","aria-label":tr("ui.guided_staged_title","Changes waiting in the device memory"),"data-revision":staging.revision},
    h("p",{textRaw:tr("ui.guided_staged_waiting","Waiting in the device memory from an earlier save (values not shown): {labels}. ‘Apply now’ restarts the device with them; a restart without ‘Apply’ drops them.",{labels:configLabels(staging.keys)})}),
    h("div",{class:"inline-actions"},iconButton(tr("ui.guided_apply_now","Apply now"),()=>retryApply(null),"primary"))));
}
// The status line and the follow-up actions under it, also for the restart watch (retryApply).
function configNotice(form,kind,message){const state=form.querySelector("p.notice[role=status]");if(state){state.className=kind?"notice "+kind:"notice";state.textContent=message;}}
function configFollowup(form,items){const box=form.querySelector(".config-followup");if(box)replace(box,items);}
function configApplyBody(id,revision){return typeof revision==="string"?{apply_id:id,staging_revision:revision}:{apply_id:id};}
function configLaterText(form){return configStaging(form.configBaseline).keys.length?tr("ui.guided_kept_memory","Kept in the device memory only. A restart before ‘Apply’ drops these changes."):tr("ui.guided_saved_later","Saved. The settings take effect after the next restart.");}
// The confirmation of the staged changes as read from the device (keys only, E19); resolves true for "Apply all and restart".
async function stagedApplyChoice(form,keys,changed){
  const old=form.querySelector("section.config-apply");if(old)old.remove();
  let live=store.state.live;
  try{live=await api.json("/api/ui/live",{timeout:RESTART_TIMING.requestTimeoutMs,cache:"no-store"});}catch(_){}
  const warning=restartWarningText(live),labels=configLabels(keys);
  return new Promise(resolve=>{
    const done=value=>{block.remove();resolve(value);};
    const block=h("section",{class:"config-apply",role:"region","aria-label":tr("ui.guided_apply_title","Apply the waiting changes"),"data-changed":String(changed),tabindex:"-1"},
      h("p",{textRaw:changed?tr("ui.guided_staging_changed_meanwhile","The changes waiting in the device memory changed in the meantime (values not shown): {labels}",{labels})
        :tr("ui.guided_apply_staged","Apply the changes waiting in the device memory and restart (values not shown): {labels}",{labels})}),
      warning?h("p",{class:"notice warn",textRaw:warning}):[],
      h("div",{class:"inline-actions"},iconButton(tr("ui.guided_apply_all","Apply all and restart"),()=>done(true),"primary"),
        iconButton(tr("ui.guided_later","Later"),()=>done(false),"secondary")));
    const state=form.querySelector("p.notice[role=status]");
    if(state)state.before(block);else form.append(block);
    block.scrollIntoView({block:"nearest"});block.focus({preventScroll:true});
  });
}
// A refused or unsent apply: the values stay staged (the PUT succeeded), so the form is saved; message, then "Apply again".
function configApplyRefused(form,answer,attempt){
  const failure=answer.failure||{};
  if(failure.status===401)return;
  if(failure.status===409&&failure.code==="nothing_to_apply"){configNotice(form,"ok",tr("ui.guided_already_in_effect","Already in effect."));configFollowup(form,[]);return;}
  const message=answer.state==="not_sent"?tr("ui.guided_not_sent","No answer from the device; nothing was sent. Try again."):localText(failure.message||"");
  configNotice(form,answer.state==="not_sent"?"critical":"warn",[message,configLaterText(form)].filter(Boolean).join(" "));
  configFollowup(form,[iconButton(tr("ui.guided_action_retry","Apply again"),()=>retryApply(attempt),"primary")]);
}
// "Apply now"/"Apply again" (E19): confirm the read staging, apply it with the read revision; a changed staging is read again.
async function applyStagedChanges(form,config,{attempt=null,changed=false}={}){
  for(;;){
    if(plainObject(config)){if(config.staging===undefined)delete form.configBaseline.staging;else form.configBaseline.staging=config.staging;syncStagedNote(form);}
    const staging=configStaging(config);
    configFollowup(form,[]);
    if(!staging.keys.length&&!staging.restart_required){
      configNotice(form,"warn",tr("ui.guided_nothing_left","Nothing left to apply; a restart dropped the staged change. Edit again."));
      configFollowup(form,[iconButton(tr("ui.guided_action_edit","Edit again"),()=>{pendingConfigEdit={values:attempt,code:null};renderRoute();},"secondary")]);
      return null;
    }
    if(!await stagedApplyChoice(form,staging.keys,changed)){configNotice(form,"warn",configLaterText(form));return null;}
    const target=attempt&&typeof attempt.wifi_ssid==="string"?attempt.wifi_ssid:null;
    const answer=await requestRestart("apply",id=>api.post("/api/config/apply",configApplyBody(id,staging.revision)),{attempt,target_ssid:target});
    if(answer.state==="accepted"||answer.state==="uncertain"){
      configNotice(form,"ok",tr("ui.guided_restart_started","Saved. The device restarts now – the panel shows each step."));
      startRestartWatch("apply",answer.marker);return answer;
    }
    if(answer.state==="refused"&&answer.failure.status===409&&answer.failure.code==="staging_changed"){
      try{config=await api.json("/api/config",{timeout:RESTART_TIMING.requestTimeoutMs});changed=true;continue;}catch(_){}
    }
    configApplyRefused(form,answer,attempt);return answer;
  }
}
// After a PUT that staged changes: the apply with the PUT revision (E5 (d)-(f), E18). The form is saved in every case.
async function applyAfterSave(form,changes,summary){
  const attempt=restartAttempt(changes),base=form.configBaseline;
  const target=typeof changes.wifi_ssid==="string"?changes.wifi_ssid:typeof base.wifi_ssid==="string"&&base.wifi_ssid?base.wifi_ssid:null;
  const answer=await requestRestart("apply",id=>api.post("/api/config/apply",configApplyBody(id,summary.staging_revision)),{attempt,target_ssid:target});
  const started=answer.state==="accepted"||answer.state==="uncertain";
  await commitSavedForm(form,changes,{fresh:false,later:!started});
  if(started){
    configNotice(form,"ok",tr("ui.guided_restart_started","Saved. The device restarts now – the panel shows each step."));
    startRestartWatch("apply",answer.marker);return;
  }
  noteSavedStaging(form,summary);
  if(answer.state==="refused"&&answer.failure.status===409&&answer.failure.code==="staging_changed"){
    let config=null;
    try{config=await api.json("/api/config",{timeout:RESTART_TIMING.requestTimeoutMs});}catch(_){}
    if(config){applyStagedChanges(form,config,{attempt,changed:true});return;}
  }
  configApplyRefused(form,answer,attempt);
}
// E9: without an active watch a final result of this device start (or of unknown time) is shown neutrally, never as a verdict.
let lastChangeClosed=false;
async function lastChangeNotice(form,signal){
  if(lastChangeClosed||restartWatchState||readRestartMarker(restartStorage(),Date.now()))return;
  const timeout=RESTART_TIMING.requestTimeoutMs;
  let result=null,live=null;
  try{[result,live]=await Promise.all([api.json("/api/config-result",{signal,timeout}),api.json("/api/ui/live",{signal,timeout}).catch(()=>store.state.live)]);}catch(_){return;}
  if(!form.isConnected||restartWatchState||lastChangeClosed)return;
  const judged=judgeConfigResult(null,result);if(judged.state!=="observed")return;
  const current=live&&plainObject(live.system)?live.system.boot_id:undefined;
  const state=result.boot_id==null?"historic_unknown":typeof current==="string"&&result.boot_id===current?"historic_boot":null;
  if(!state)return;
  const view=restartOutcomeView(Object.assign({},judged,{state}),null,{});
  const notice=h("div",{class:"notice config-last-change","data-notice":"last-change",role:"status"},h("p",{textRaw:view.text}),
    h("div",{class:"inline-actions"},
      iconButton(tr("ui.guided_action_edit","Edit again"),()=>{notice.remove();if(form.elements.wifi_ssid)form.elements.wifi_ssid.focus();},"secondary"),
      iconButton(tr("ui.guided_action_close","Close"),()=>{lastChangeClosed=true;notice.remove();},"secondary")));
  form.prepend(notice);
}
// After a save the form shows the saved state: sent secrets are cleared, the network list is masked, dirty is the diff.
// fresh reads GET /api/config; a save that stages changes (later) or a failing read uses the computed baseline.
async function commitSavedForm(form,changes,{fresh=true,later=false}={}){
  let base=savedBaseline(form.configBaseline,changes);
  if(fresh&&!later){try{base=await api.json("/api/config");}catch(_){}}
  form.configBaseline=base;
  for(const name of CONFIG_SECRET_FIELDS)if(form.elements[name])form.elements[name].value="";
  if(form.elements.wifi_networks)form.elements.wifi_networks.value=maskedNetworkText(base.wifi_networks);
  syncConfigForm(form);
}
function noteSavedStaging(form,summary){
  form.configBaseline.staging={revision:typeof summary.staging_revision==="string"?summary.staging_revision:null,keys:stringItems(summary.staged),restart_required:summary.restart_required===true};
  syncStagedNote(form);
}
// A caster change is applied at once; the device reconnects shortly afterwards. The first probe comes after the settle time.
async function observeNtrip(form,anchor){
  const token=++ntripWatchSerial,started=Date.now(),old=form.querySelector(".config-step");
  if(old)old.remove();
  const row=h("p",{class:"config-step","data-step":"ntrip","aria-live":"polite"});anchor.after(row);
  const show=(step,detail)=>{row.dataset.state=step.state;row.textContent=`${CONFIG_STEP_MARKS[step.state]} ${restartStepLabel("ntrip")}: ${detail}`;};
  show({state:"wait"},tr("ui.guided_step_waiting","Waiting"));
  while(token===ntripWatchSerial&&form.isConnected){
    await new Promise(resolve=>setTimeout(resolve,RESTART_TIMING.ntripSettleMs));
    let live=null;try{live=await api.json("/api/ui/live",{timeout:RESTART_TIMING.requestTimeoutMs});}catch(_){}
    if(token!==ntripWatchSerial||!form.isConnected)return;
    const step=live?ntripStep(live):null;
    if(step)show(step,step.detail);
    if(step&&step.state!=="wait")return;
    if(Date.now()-started>=RESTART_TIMING.ntripObserveMs){show({state:"wait"},tr("ui.guided_ntrip_no_data","no correction data yet – state: {state}",{state:live&&live.network&&live.network.ntrip_state||"?"}));return;}
  }
}
async function mountConfiguration(signal){
  replace(main,skeleton("Loading configuration"));
  const config=await api.json("/api/config",{signal});
  const form=h("form",{class:"configuration-layout","data-dirty":"false"});
  form.configBaseline=config;
  const fallbackValue=maskedNetworkText(config.wifi_networks);
  const field=(name,label,type,help)=>configField(config,name,label,type,help,CONFIG_FIELD_ATTRS[name]);
  const wifi=h("section",{class:"tool-card config-section"},
    h("p",{class:"kicker",text:"WI-FI CONNECTION"}),
    h("h2",{text:"Field network"}),
    field("wifi_ssid","SSID"),
    field("wifi_pass","Password","password",
      config.wifi_pass_set?"A password is saved.":"No password is saved."),
    h("label",{class:"field"},h("span",{text:"Fallback networks"}),
      h("textarea",{name:"wifi_networks",value:fallbackValue,
        placeholder:localText("SSID or SSID:password, one network per line")}),
      h("small",{textRaw:tr("ui.network_format_help","Format: Name:Password. If only the name is provided, the saved password is retained.")})),
    h("div",{class:"inline-actions"},iconButton("Scan networks",async event=>{
      const button=event.currentTarget;button.disabled=true;
      try{
        state.className="notice";state.textContent=localText("Scanning for Wi-Fi networks …");
        let result=await api.json("/api/wifi-scan");
        for(let attempt=0;result.status==="pending"&&attempt<16;attempt++){
          await new Promise(resolve=>setTimeout(resolve,750));
          result=await api.json("/api/wifi-scan");
        }
        const options=(result.networks||[]).map(network=>
          ({value:network.ssid,label:`${network.ssid} · ${network.rssi} dBm`}));
        if(!options.length)throw new Error("No Wi-Fi networks were found.");
        const selected=await modal({title:"Visible Wi-Fi networks",
          fields:[{name:"ssid",label:"Network",type:"select",options}],submit:"Use network"});
        if(selected){form.elements.wifi_ssid.value=selected.ssid;clearFieldError(form,"wifi_ssid");closeReview();syncConfigForm(form);}
        state.className="notice ok";
        state.textContent=localText(selected?"Network selected. Save to apply it.":"Wi-Fi scan complete.");
      }catch(error){state.className="notice critical";state.textContent=localText(error.message);}
      finally{button.disabled=false;}
    },"secondary")));
  const enabled=h("select",{name:"ntrip_enabled"},
    h("option",{value:"1",selected:Boolean(config.ntrip_enabled),text:"Enabled"}),
    h("option",{value:"0",selected:!config.ntrip_enabled,text:"Disabled"}));
  const ntrip=h("section",{class:"tool-card config-section"},
    h("p",{class:"kicker",text:"NTRIP CASTER"}),h("h2",{text:"Correction source"}),
    h("label",{class:"field"},h("span",{text:"NTRIP client"}),enabled),
    h("h3",{text:"Primary caster"}),
    field("ntrip_host","Host"),
    field("ntrip_port","Port","number"),
    field("ntrip_mount","Mountpoint"),
    field("ntrip_user","Username"),
    field("ntrip_pass","Password","password",
      config.ntrip_pass_set?"A password is saved.":"No password is saved."),
    h("h3",{text:"Fallback caster (optional)"}),
    field("ntrip_fallback_host","Host"),
    field("ntrip_fallback_port","Port","number"),
    field("ntrip_fallback_mount","Mountpoint"),
    field("ntrip_fallback_user","Username"),
    field("ntrip_fallback_pass","Password","password",
      config.ntrip_fallback_pass_set?"A password is saved.":"No password is saved."),
    h("small",{text:"The fallback is used automatically when configured and the primary caster is unavailable."}));
  const fields=h("fieldset",{class:"config-fields"},
    h("legend",{class:"visually-hidden",textRaw:tr("ui.guided_fields_legend","Configuration settings")}),wifi,ntrip);
  const marker=h("p",{class:"unsaved-marker",hidden:true,textRaw:tr("ui.guided_unsaved","Unsaved changes")});
  const review=h("section",{class:"config-review",role:"region","aria-label":tr("ui.guided_review_title","Review before saving"),tabindex:"-1",hidden:true});
  const state=h("p",{class:"notice",role:"status",
    text:"Changes are validated before the receiver restarts."});
  const checkButton=iconButton(tr("ui.guided_check_changes","Check changes"),()=>{},"primary");checkButton.type="submit";
  const followup=h("div",{class:"inline-actions config-followup"});
  form.append(fields,marker,review,state,followup,checkButton);
  let reviewSerial=0,locked=false;
  const say=(kind,message)=>{state.className=kind?"notice "+kind:"notice";state.textContent=message;};
  const closeReview=()=>{reviewSerial+=1;review.hidden=true;review.replaceChildren();};
  const lock=on=>{locked=on;fields.disabled=on;checkButton.disabled=on;review.querySelectorAll("button").forEach(item=>{item.disabled=on;});};
  // Every input clears its own field error, closes the review and recomputes required fields and the dirty state.
  form.addEventListener("input",event=>{if(locked)return;clearFieldError(form,event.target&&event.target.name);closeReview();syncConfigForm(form);});
  // The review states what each group of changes does before anything is sent.
  const openReview=async()=>{
    const serial=++reviewSerial,{changes,keys}=configChanges(configFormValues(form),form.configBaseline);
    configFollowup(form,[]);
    if(!keys.length){closeReview();say("",tr("ui.guided_no_changes","No changes to save."));return;}
    const effects=configEffects(keys,configStaging(form.configBaseline));
    let warning="";
    if(effects.restart){let live=store.state.live;try{live=await api.json("/api/ui/live",{timeout:RESTART_TIMING.requestTimeoutMs});}catch(_){}warning=restartWarningText(live);}
    if(serial!==reviewSerial||!form.isConnected)return;
    const group=(effect,heading,list)=>h("div",{class:"config-effect","data-effect":effect},h("p",{textRaw:heading}),list.length?h("ul",{},...list.map(key=>h("li",{textRaw:localText(CONFIG_FIELD_LABELS[key]||key)}))):[]);
    const actions=h("div",{class:"inline-actions"});
    if(effects.restart)actions.append(iconButton(tr("ui.guided_save_restart","Save and restart"),()=>saveChanges(changes,"restart"),"primary"),iconButton(tr("ui.guided_save_later","Save without restart"),()=>saveChanges(changes,"later"),"secondary"));
    else actions.append(iconButton(tr("ui.guided_save","Save"),()=>saveChanges(changes,"plain"),"primary"));
    actions.append(iconButton(tr("ui.guided_back_to_editing","Back to editing"),()=>{closeReview();const control=form.elements[keys[0]];if(control)control.focus();},"secondary"));
    review.dataset.restart=String(effects.restart);
    replace(review,h("h2",{textRaw:tr("ui.guided_review_title","Review before saving")}),
      effects.restart?group("restart",effects.wifi.length?tr("ui.guided_review_restart_wifi","The device restarts. The new Wi-Fi settings stay only if it holds the connection for 10 s; otherwise it returns to the previous ones."):tr("ui.guided_review_restart_staged","The device restarts to apply the changes waiting in its memory."),effects.wifi):[],
      effects.caster.length?group("now",tr("ui.guided_review_now","Takes effect at once without a restart; the correction connection is rebuilt."),effects.caster):[],
      effects.earlier.length?h("p",{textRaw:tr("ui.guided_review_earlier","Also applied with this restart (values not shown): {labels}",{labels:configLabels(effects.earlier)})}):[],
      warning?h("p",{class:"notice warn",textRaw:warning}):[],actions);
    review.hidden=false;review.scrollIntoView({block:"nearest"});review.focus({preventScroll:true});
  };
  const saveFailed=async error=>{
    const failure=apiFailure(error);
    if(failure.status===409&&failure.code==="staging_changed"){
      try{const fresh=await api.json("/api/config");if(fresh.staging===undefined)delete form.configBaseline.staging;else form.configBaseline.staging=fresh.staging;syncStagedNote(form);}catch(_){}
      say("warn",tr("ui.guided_staging_changed","The changes waiting in the device memory changed. Check them again."));
      await openReview();return;
    }
    closeReview();
    if(!failure.definitive){say("critical",tr("ui.guided_uncertain","No answer from the device. Reload the page and check whether the change was saved."));return;}
    const named=failure.status===400?configErrorField(failure.code,error.message):null;
    if(named&&form.elements[named]){showFieldError(form,named,error.message);say("critical",tr("ui.guided_field_rejected","Not saved: the device did not accept a value. See the marked field."));form.elements[named].focus();}
    else say("critical",localText(error.message));
  };
  const saved=async(summary,changes,choice)=>{
    closeReview();
    // Strict comparison: a reply without the field keeps the staged-apply flow.
    if(summary&&summary.restart_required===false){
      await commitSavedForm(form,changes);
      if(summary&&typeof summary.staging_revision==="string")noteSavedStaging(form,summary);
      say("ok",localText("Configuration saved and in effect; no restart needed."));
      if(configEffects(Object.keys(changes),null).reload)observeNtrip(form,state);
      return;
    }
    if(choice==="restart"){await applyAfterSave(form,changes,summary||{});return;}
    await commitSavedForm(form,changes,{fresh:false,later:true});
    noteSavedStaging(form,summary||{});
    // A restart after "Save" was not predicted (safe mode, a failed hand-over): the shared restart confirmation asks (E1).
    if(choice==="plain"&&summary&&summary.restart_required===true&&await confirmRestart({title:"Restart needed",
      description:"The device must restart for these settings to take effect.",submit:"Restart now"})){await applyAfterSave(form,changes,summary);return;}
    const kept=Boolean(summary&&Array.isArray(summary.staged)&&summary.staged.length);
    say(kept?"warn":"ok",kept?tr("ui.guided_kept_memory","Kept in the device memory only. A restart before ‘Apply’ drops these changes."):tr("ui.guided_saved_later","Saved. The settings take effect after the next restart."));
  };
  const saveChanges=async(changes,choice)=>{
    const staging=configStaging(form.configBaseline);
    lock(true);say("",tr("ui.guided_saving","Saving …"));configFollowup(form,[]);
    let summary;
    try{summary=await api.put(staging.revision===null?"/api/config":"/api/config?staging_revision="+encodeURIComponent(staging.revision),changes);}
    catch(error){lock(false);await saveFailed(error);return;}
    // A started restart watch keeps the form locked until it ends: its reload releases the dirty guard and would drop a new edit.
    try{await saved(summary,changes,choice);}
    finally{const watch=restartWatchState&&restartWatchState.promise;if(watch)watch.then(()=>lock(false),()=>lock(false));else lock(false);}
  };
  form.addEventListener("submit",event=>{event.preventDefault();openReview();});
  // "Edit again" (E16): only the values the marker may keep; passwords come from nowhere, the dirty state is the diff.
  const edit=pendingConfigEdit;pendingConfigEdit=null;
  const values=edit&&plainObject(edit.values)?edit.values:{};
  for(const name of RESTART_MARKER_FIELDS)if(typeof values[name]==="string"&&form.elements[name])form.elements[name].value=values[name];
  syncConfigForm(form);syncStagedNote(form);
  replace(main,pageIntro("NETWORKS & CORRECTIONS","Configuration",
    "Changing Wi-Fi may temporarily disconnect this browser."),form);
  setConnection("Configuration loaded","ok");
  if(edit){const target=form.elements[edit.code==="authentication_failed"?"wifi_pass":"wifi_ssid"];if(target)target.focus();}
  else lastChangeNotice(form,signal);
}


// SPDX-License-Identifier: AGPL-3.0-only
// Guided restart (#23c): the one restart watch of this page. It owns the hold (no session request, no sign-in redirect
// while the device restarts), the panel in #overlay-root, the request, the resume after a reload and the one way out.
// restartWatchState={token, phase, hold, renderBlocked, controller, promise, ticker}; only this fragment replaces it.
let restartWatchState=null;
function restartActionLabel(action){return action==="edit"?tr("ui.guided_action_edit","Edit again"):action==="field_setup"?tr("ui.guided_action_field_setup","Check field setup"):action==="retry_apply"?tr("ui.guided_action_retry","Apply again"):tr("ui.guided_action_close","Close");}
function restartStorage(){try{return sessionStorage;}catch(_){return null;}}
function newRestartWatchState(){return{token:null,phase:null,hold:false,renderBlocked:false,controller:null,promise:null,ticker:0};}
function restartHoldActive(){return Boolean(restartWatchState&&restartWatchState.hold);}
// Right before the request: route requests, route pollers and the live stream end (a fresh controller lets polling work
// after the hold).
function beginRestartHold(){
  if(!restartWatchState)restartWatchState=newRestartWatchState();
  restartWatchState.hold=true;++requestSerial;clearInterval(pollTimer);liveSource.stop();
  if(activeController){activeController.abort();activeController=new AbortController();}
}
// Ends the hold once: polling runs again; a route that was blocked renders now (renderRoute starts the polling itself),
// otherwise the page (a form with its message) stays.
function endRestartHold(){
  const state=restartWatchState;if(!state||!state.hold)return;
  const blocked=state.renderBlocked;state.hold=false;state.renderBlocked=false;
  if(blocked)renderRoute();else startPolling();
}
function restartHoldPlaceholder(){return h("section",{class:"restart-hold notice",role:"status"},h("p",{textRaw:tr("ui.guided_hold_placeholder","The device is restarting – this page continues when it is back.")}));}
// The only exception from the unsaved-changes guard: the watch reloads, the apply needs a new sign-in, a restore replaces the form.
function releaseDirtyGuard(){main.querySelectorAll(".configuration-layout").forEach(form=>{form.dataset.dirty="false";});}
// The one way out of a watch: close, edit, field setup, apply again and before every new request.
function finishRestartWatch(){
  const state=restartWatchState;
  if(state){state.token=null;if(state.controller)state.controller.abort();clearInterval(state.ticker);if(state.hold)endRestartHold();}
  clearRestartMarker(restartStorage());
  const node=overlay.querySelector("section.restart-watch");if(node)node.remove();
  document.documentElement.style.removeProperty("--restart-watch-space");
  restartWatchState=null;
}
// The sign-in page replaces itself with "/?signed_in=1#route"; reloading that very address would show the sign-in page again,
// and a replace to the same address is only a fragment jump. Dropping the marker query first lets the next sign-in navigate.
// Both steps wait for a closing sheet's history step (leaveDocument), so the marker is dropped on the entry that is reloaded.
function reloadForSignIn(){
  leaveDocument(()=>{
    try{const url=new URL(location.href);if(url.searchParams.has("signed_in")){url.searchParams.delete("signed_in");history.replaceState(null,"",url.pathname+url.search+url.hash);}}catch(_){}
    location.reload();
  });
}
// The session probe works without a session: 401 means the device is back and the session is gone.
async function probeSession(timeoutMs,signal){
  const controller=new AbortController(),stop=()=>controller.abort(),timer=setTimeout(stop,timeoutMs);
  if(signal){if(signal.aborted){clearTimeout(timer);return"unreachable";}signal.addEventListener("abort",stop,{once:true});}
  try{
    const response=await fetch("/api/session",{cache:"no-store",credentials:"same-origin",signal:controller.signal});
    if(response.status===401)return"signed_out";
    if(response.status!==200)return"unreachable";
    const body=await response.json();
    return body&&body.authenticated===true?"authenticated":"unreachable";
  }catch(_){return"unreachable";}
  finally{clearTimeout(timer);if(signal)signal.removeEventListener("abort",stop);}
}
// One confirmation for every restart (E12): live is read when the dialog opens; a running measurement or recording turns the button into "Restart anyway".
async function confirmRestart({title,description,submit}){
  let live=store.state.live;
  try{live=await api.json("/api/ui/live",{timeout:RESTART_TIMING.requestTimeoutMs,cache:"no-store"});}catch(_){}
  const warning=restartWarningText(live),text=localText(description);
  return Boolean(await modal({title,description:warning?text+" "+warning:text,submit:warning?tr("ui.guided_restart_anyway","Restart anyway"):submit,danger:true}));
}
function restartSinceText(marker){return capacityText("since {n} s",{n:String(Math.max(0,Math.floor((Date.now()-Number(marker.started_ms))/1000)))});}
function showRoute(path){if(currentRoute().path===path)renderRoute();else location.hash=path;}
async function revealFieldSetup(){
  for(let tries=0;tries<100;tries++){
    const card=main.querySelector('[data-card="field-setup"]');
    if(card){card.scrollIntoView({block:"start"});return;}
    await new Promise(resolve=>setTimeout(resolve,100));
  }
}
// Every action but the portal link ends the watch first.
function restartAction(action,marker,context){
  const outcome=plainObject(context.outcome)?context.outcome:{};
  finishRestartWatch();
  if(action==="edit"){pendingConfigEdit={values:plainObject(marker.attempt)?marker.attempt:null,code:typeof outcome.code==="string"?outcome.code:null};showRoute(CONFIG_ROUTE);}
  else if(action==="field_setup"){showRoute(FIELD_SETUP_ROUTE);revealFieldSetup();}
  else if(action==="retry_apply")return retryApply(plainObject(marker.attempt)?marker.attempt:null);
}
// The panel keeps its nodes between renders, so the result paragraph (role=status) only speaks when its text changes.
function renderRestartPanel(view,marker,context={}){
  let node=overlay.querySelector("section.restart-watch");
  if(!node){
    node=h("section",{class:"restart-watch",role:"region","aria-label":tr("ui.guided_panel_title","Restart and check")},
      h("h2",{textRaw:tr("ui.guided_panel_title","Restart and check")}),h("ol",{class:"restart-steps"}),h("p",{class:"restart-since"}),
      h("p",{class:"restart-result",role:"status"}),h("ul",{class:"restart-notes",hidden:true}),h("div",{class:"inline-actions restart-actions"}));
    overlay.append(node);
  }
  // A final outcome restored from the marker comes without step data: the outcome alone, never "not reachable" beside it.
  const outcome=plainObject(context.outcome)?context.outcome:null,given=Array.isArray(context.steps)&&context.steps.length?context.steps:null;
  const steps=given||(outcome?[]:restartSteps(null,false)),list=node.querySelector(".restart-steps");
  node.dataset.phase=context.phase||marker.phase||"restarting";
  if(outcome&&typeof outcome.state==="string")node.dataset.outcome=outcome.state;else delete node.dataset.outcome;
  list.hidden=!steps.length;
  replace(list,steps.map(item=>h("li",{"data-step":item.step,"data-state":item.state},
    h("span",{class:"restart-mark","aria-hidden":"true",textRaw:CONFIG_STEP_MARKS[item.state]||CONFIG_STEP_MARKS.wait}),
    h("b",{textRaw:restartStepLabel(item.step)}),h("span",{class:"restart-detail",textRaw:item.detail||""}))));
  node.querySelector(".restart-since").textContent=restartSinceText(marker);
  const result=node.querySelector(".restart-result");if(result.textContent!==view.text)result.textContent=view.text;
  const notes=node.querySelector(".restart-notes");replace(notes,view.notes.map(note=>h("li",{textRaw:note})));notes.hidden=!view.notes.length;
  const actions=node.querySelector(".restart-actions"),signature=view.actions.join(",");
  if(actions.dataset.signature!==signature){
    actions.dataset.signature=signature;
    const host=typeof marker.host==="string"?marker.host:"";
    replace(actions,view.actions.map(action=>action==="open_portal"
      ?h("a",{class:"btn secondary",href:"http://"+host+"/#"+CONFIG_ROUTE,textRaw:tr("ui.guided_action_portal","Open the portal")})
      :iconButton(restartActionLabel(action),()=>restartAction(action,marker,context),action==="close"?"secondary":"primary")));
  }
  document.documentElement.style.setProperty("--restart-watch-space",node.offsetHeight+"px");
}
// E18: read live and the host first; without a boot id nothing is sent. send() returns the decoded JSON answer.
// The hold begins right before send(); a refusal ends it, a lost or unreadable answer is watched as uncertain, never repeated.
// Every read of the watch bypasses the HTTP cache: a hanging poll of the same URL would otherwise hold it back.
async function requestRestart(kind,send,extra={}){
  const timeout=RESTART_TIMING.requestTimeoutMs,options=plainObject(extra)?extra:{},fresh={timeout,cache:"no-store"};
  const [live,access]=await Promise.all([api.json("/api/ui/live",fresh).catch(()=>null),api.json("/api/access",fresh).catch(()=>null)]);
  const bootId=live&&plainObject(live.system)&&typeof live.system.boot_id==="string"?live.system.boot_id:null;
  if(!bootId)return{state:"not_sent",failure:null,payload:null,marker:null};
  finishRestartWatch();
  const storage=restartStorage(),marker={kind,route:currentRoute().path,started_ms:Date.now(),phase:"requested",apply_id:newApplyId(),boot_id:bootId,uncertain:false,
    host:access&&typeof access.hostname==="string"&&access.hostname?access.hostname:null,target_ssid:typeof options.target_ssid==="string"&&options.target_ssid?options.target_ssid:null,
    attempt:plainObject(options.attempt)?markerAttempt(options.attempt):null};
  writeRestartMarker(storage,marker);
  beginRestartHold();
  let payload=null,failure=null;
  try{payload=await send(marker.apply_id);}
  catch(error){
    failure=Object.assign(apiFailure(error),{message:error&&typeof error.message==="string"?error.message:""});
    if(failure.definitive){
      clearRestartMarker(storage);endRestartHold();
      if(restartWatchState&&!restartWatchState.token)restartWatchState=null;
      // A 401 on the request itself: the session ended before it; the saved form must not ask on the way to the sign-in.
      // The user started this request, so a paused sign-in question may ask at once (signInRedirect(true)).
      if(failure.status===401){if(kind==="apply")releaseDirtyGuard();signInRedirect(true);}
      return{state:"refused",failure,payload:null,marker:null};
    }
  }
  marker.phase="restarting";
  const accepted=!failure&&restartAnswerAccepted(kind,payload);
  if(accepted&&kind==="restore")Object.assign(marker,restoreScope(payload));
  if(!accepted)marker.uncertain=true;
  writeRestartMarker(storage,marker);
  return{state:accepted?"accepted":"uncertain",failure,payload,marker};
}
// One watcher: a running one hands out its promise. The real dependencies of watchRestart live here.
function startRestartWatch(kind,marker){
  if(restartWatchState&&restartWatchState.promise)return restartWatchState.promise;
  if(!restartWatchState)restartWatchState=newRestartWatchState();
  const state=restartWatchState,token={},controller=new AbortController(),storage=restartStorage(),timeout=RESTART_TIMING.requestTimeoutMs;
  const context={phase:null,steps:null,outcome:null,live:null,config:null,settleTimedOut:false};
  state.token=token;state.controller=controller;marker.kind=kind;
  if(!["verifying","settling","back","final"].includes(marker.phase))marker.phase="restarting";
  state.phase=context.phase=marker.phase;
  const alive=()=>restartWatchState===state&&state.token===token;
  const paint=()=>{if(alive())renderRestartPanel(restartOutcomeView(context.outcome,marker,context),marker,context);};
  let configAsked=false;
  const deps={now:()=>Date.now(),sleep:ms=>new Promise(resolve=>setTimeout(resolve,ms)),alive,
    probe:()=>probeSession(RESTART_TIMING.probeTimeoutMs,controller.signal),
    live:()=>api.json("/api/ui/live",{timeout,signal:controller.signal,cache:"no-store"}),
    result:()=>api.json("/api/config-result",{timeout,signal:controller.signal,cache:"no-store"}),
    // The hold ends with the confirmed restart (verifying, settling) and with every final outcome.
    save:next=>{if(!alive())return;state.phase=next.phase;writeRestartMarker(storage,next);if(next.phase!=="restarting"&&next.phase!=="back")endRestartHold();},
    reload:()=>{releaseDirtyGuard();reloadForSignIn();},
    render:view=>{
      if(!alive())return;
      Object.assign(context,{phase:view.phase,steps:view.steps,outcome:view.outcome,settleTimedOut:view.settleTimedOut});
      if(view.live)context.live=view.live;
      paint();
      // After a result the settings the device reports now (E11); the hold has ended by then.
      if(!configAsked&&view.outcome&&["connected","failed","observed"].includes(view.outcome.state)){
        configAsked=true;
        api.json("/api/config",{timeout,signal:controller.signal,cache:"no-store"}).then(config=>{context.config=config;paint();},()=>{});
      }
    }};
  paint();
  state.ticker=setInterval(()=>{
    if(!alive()||marker.phase==="final"){clearInterval(state.ticker);return;}
    const node=overlay.querySelector("section.restart-watch"),since=node&&node.querySelector(".restart-since");
    if(since)since.textContent=restartSinceText(marker);
  },1000);
  state.promise=watchRestart(marker,deps);
  return state.promise;
}
// The configuration form on this page; from another route the page opens it first.
async function configFormOnPage(){
  const find=()=>main.querySelector("form.configuration-layout");
  if(!find())showRoute(CONFIG_ROUTE);
  for(let tries=0;tries<100&&!find();tries++)await new Promise(resolve=>setTimeout(resolve,100));
  return find();
}
// "Apply again"/"Apply now": ends the old watch, reads the staging (read only, E19) and confirms with the read revision.
async function retryApply(attempt){
  finishRestartWatch();
  const form=await configFormOnPage();if(!form)return null;
  let config=null;
  try{config=await api.json("/api/config",{timeout:RESTART_TIMING.requestTimeoutMs});}
  catch(error){configNotice(form,"critical",localText(error.message||String(error)));return null;}
  return applyStagedChanges(form,config,{attempt});
}
// Synchronous part before the first route render: a watch that may still be restarting holds at once (no session request first).
function resumeRestartWatch(){
  const storage=restartStorage(),marker=readRestartMarker(storage,Date.now());
  if(!marker)return;
  if(!Object.prototype.hasOwnProperty.call(RESTART_ANSWER_FLAGS,marker.kind)){clearRestartMarker(storage);return;}
  if(marker.phase==="final"){
    restartWatchState=newRestartWatchState();restartWatchState.token={};restartWatchState.phase="final";
    const context={phase:"final",outcome:plainObject(marker.outcome)?marker.outcome:null};
    renderRestartPanel(restartOutcomeView(context.outcome,marker,context),marker,context);
    return;
  }
  if(marker.phase==="requested"||marker.phase==="restarting"){
    if(marker.phase==="requested")marker.uncertain=true;
    beginRestartHold();
    const state=restartWatchState,context={phase:"resuming"};
    state.phase="resuming";
    renderRestartPanel(restartOutcomeView(null,marker,context),marker,context);
    (async()=>{
      let live=null;
      try{live=await api.json("/api/ui/live",{timeout:RESTART_TIMING.requestTimeoutMs,cache:"no-store"});}catch(_){}
      if(restartWatchState!==state)return;
      // A new boot id: the restart happened; the same or none: the probe decides.
      if(bootChanged(marker,live)===true){marker.phase=marker.kind==="reboot"?"settling":"verifying";endRestartHold();}
      else marker.phase="restarting";
      writeRestartMarker(storage,marker);
      startRestartWatch(marker.kind,marker);
    })();
    return;
  }
  if(marker.phase==="back")marker.phase="verifying";
  if(marker.phase==="verifying"||marker.phase==="settling")startRestartWatch(marker.kind,marker);
  else clearRestartMarker(storage);
}

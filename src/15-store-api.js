// SPDX-License-Identifier: AGPL-3.0-only
const store={state:{live:null,tracking:null,status:null,route:null},listeners:new Set(),set(patch){Object.assign(this.state,patch);if(patch.live&&patch.live.capacity)observeCapacity(patch.live);this.listeners.forEach(fn=>fn(this.state))},subscribe(fn){this.listeners.add(fn);return()=>this.listeners.delete(fn);}};
const projectMapState=new Map();
// Large flash-backed maps and WLAN connection setup share this bounded deadline.
const PROJECT_READ_TIMEOUT=20000;
let csrfToken=null,requestSerial=0;
// Leaving the document (sign-in redirect, sign-out, reload; #24): a closing sheet's own history.back() still in flight would
// abort the navigation when its traversal lands. leaveDocument starts go() once every such step has landed (each one settles
// at the latest after 1 s, src/20-navigation.js), at once without one; from then on no sheet takes a history step.
// Unsaved configuration is asked about first, in the app (the browser's leave prompt could cancel a navigation the page
// already counts as started): cancel changes nothing, and for LEAVE_QUESTION_PAUSE_MS later calls do nothing (no question,
// no navigation) unless userAction (the user's own write got the 401); then the next 401 asks again. Confirm releases the
// guard, then leaves. Calls while the question is open join it.
const LEAVE_QUESTION_PAUSE_MS=30000;
let leavingDocument=false,leaveQuestion=null,leavePause=null;const pendingSheetSteps=new Set();
function leaveDocument(go,userAction){if(leaveQuestion)return leaveQuestion;if(!leavingDocument&&main.querySelector('.configuration-layout[data-dirty="true"]')){if(leavePause&&!userAction)return Promise.resolve();leavePause=null;leaveQuestion=modal({title:"Discard unsaved changes?",description:tr("ui.sign_in_unsaved","The session has ended. Signing in again discards the Wi-Fi and NTRIP values on this page that have not been saved."),submit:tr("ui.sign_in_again","Sign in again"),danger:true}).then(accepted=>{leaveQuestion=null;if(!accepted){const pause=leavePause={};setTimeout(()=>{if(leavePause===pause)leavePause=null;},LEAVE_QUESTION_PAUSE_MS);return;}releaseDirtyGuard();return leaveDocument(go);});return leaveQuestion;}leavingDocument=true;const steps=[...pendingSheetSteps];if(!steps.length){go();return Promise.resolve();}return Promise.all(steps).then(()=>go());}
// userAction: the 401 answered the user's own write (or its CSRF token fetch), not a poll; it may ask during the pause.
function signInRedirect(userAction){const target="/?return="+encodeURIComponent("/ui-v2"+location.hash);leaveDocument(()=>location.replace(target),userAction);}
function requestAbortError(timedOut){const error=new Error(timedOut?"Request timed out. Please try again.":"Request cancelled");error.name=timedOut?"TimeoutError":"AbortError";return error;}
const api={
  async request(path,options={}){
    const controller=new AbortController(),external=options.signal;let timer=null,timedOut=false;
    const cleanup=()=>{if(timer!==null)clearTimeout(timer);timer=null;if(external)external.removeEventListener("abort",cancel);};
    const cancel=()=>{controller.abort();cleanup();};
    if(external){if(external.aborted)throw requestAbortError(false);external.addEventListener("abort",cancel,{once:true});}
    if(options.timeout!==null)timer=setTimeout(()=>{timedOut=true;controller.abort();cleanup();},options.timeout||8000);
    try{
      const response=await fetch(path,Object.assign({},options,{signal:controller.signal}));
      // Keep the deadline and navigation signal connected until the body ends.
      for(const method of ["json","text","blob","arrayBuffer"]){const read=response[method].bind(response);response[method]=async()=>{try{return await read();}catch(error){if(controller.signal.aborted)throw requestAbortError(timedOut);throw error;}finally{cleanup();}};}
      // 401 during a guided restart (#23c): the session ends with every restart, so the hold keeps the page; no abort, the catch would turn the error into an AbortError.
      if(response.status===401){if(restartHoldActive()){cleanup();const error=new Error("Session lost during restart");error.name="SessionLostError";error.status=401;throw error;}cleanup();controller.abort();signInRedirect(Boolean(options.userAction||options.method&&options.method!=="GET"));throw new Error("Session expired");}
      // 403: the session lives, its CSRF token or the origin was refused. Drop the token so the next write fetches a fresh one; no abort, the catch would turn the refusal into a silent AbortError. The status is part of the error contract shared with #21c.
      if(response.status===403){cleanup();csrfToken=null;const error=new Error(tr("ui.action_refused_reload","Action refused. Reload the page and try again."));error.name="ForbiddenError";error.status=403;throw error;}
      // status/code/reasons let callers tell a refusal from a lost connection (#23c, #21c); path names the refused field (stakeout E22).
      if(!response.ok){let message=await response.text(),payload=null;try{payload=JSON.parse(message);message=payload.message||payload.error||message;}catch(_){}const error=new Error(message||`HTTP ${response.status}`);error.status=response.status;error.code=payload&&typeof payload.error==="string"?payload.error:undefined;error.reasons=payload&&Array.isArray(payload.reasons)?payload.reasons.filter(reason=>typeof reason==="string"):undefined;error.path=payload&&typeof payload.path==="string"?payload.path:undefined;throw error;}
      if(!response.body)cleanup();
      return response;
    }catch(error){cleanup();if(controller.signal.aborted)throw requestAbortError(timedOut);throw error;}
  },
  async json(path,options){return(await this.request(path,options)).json();},async text(path,options){return(await this.request(path,options)).text();},
  async csrf(){if(csrfToken)return csrfToken;csrfToken=(await this.json("/api/session",{userAction:true})).csrf_token;return csrfToken;},
  async write(path,method,payload,contentType="application/json",timeout=15000){return this.request(path,{method,headers:{"Content-Type":contentType,"X-CSRF-Token":await this.csrf()},body:contentType==="application/json"&&typeof payload!=="string"?JSON.stringify(payload):payload,timeout});},
  async post(path,payload){return(await this.write(path,"POST",payload)).json();},async put(path,payload){return(await this.write(path,"PUT",payload)).json();}
};
const apiJsonBase=api.json.bind(api);let compactLiveRequest=null;
api.json=(path,options={})=>{
  if(path!=="/api/ui/live")return apiJsonBase(path,options);
  if(compactLiveRequest&&compactLiveRequest.signal===options.signal&&compactLiveRequest.timeout===options.timeout)return compactLiveRequest.promise;
  const entry={signal:options.signal,timeout:options.timeout};
  entry.promise=apiJsonBase(path,options).finally(()=>{if(compactLiveRequest===entry)compactLiveRequest=null;});
  compactLiveRequest=entry;return entry.promise;
};
// Optional data: JSON or null; an abort is never turned into substitute data (E27).
async function optionalJson(path,signal){try{return await api.json(path,{signal});}catch(error){if(signal&&signal.aborted||error&&error.name==="AbortError")throw error;return null;}}

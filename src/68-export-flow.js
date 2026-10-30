// SPDX-License-Identifier: AGPL-3.0-only
// M2 #46: the visible GeoJSON export of the export sheet: pure helpers (time, size, completeness, outcome texts), the component and its close question.
const EXPORT_STALL_MS=30000,EXPORT_TICK_MS=1000;
function exportElapsedText(ms){const total=Number.isFinite(ms)&&ms>0?Math.floor(ms/1000):0,hours=Math.floor(total/3600),minutes=Math.floor(total%3600/60),seconds=String(total%60).padStart(2,"0");return hours?`${hours}:${String(minutes).padStart(2,"0")}:${seconds}`:`${minutes}:${seconds}`;}
function exportSizeText(bytes){const size=Number.isFinite(bytes)&&bytes>0?bytes:0;return size<100000?`${fmt(size/1000,0)} kB`:`${fmt(size/1e6,1)} MB`;}
function jsonEnvelopeStart(){return{depth:0,inString:false,escape:false,opened:false,closed:false,invalid:false};}
// Bracket and string state only (E9): a document counts as complete when its outermost { or [ has closed and nothing but white space follows.
function jsonEnvelopeScan(state,bytes){for(let i=0;i<bytes.length&&!state.invalid;i++){const byte=bytes[i];if(state.inString){if(state.escape)state.escape=false;else if(byte===92)state.escape=true;else if(byte===34)state.inString=false;continue;}const space=byte===32||byte===9||byte===10||byte===13;if(state.closed){if(!space)state.invalid=true;continue;}if(space)continue;if(byte===123||byte===91){state.opened=true;state.depth+=1;}else if(!state.opened)state.invalid=true;else if(byte===125||byte===93){state.depth-=1;if(state.depth<0)state.invalid=true;else if(state.depth===0)state.closed=true;}else if(byte===34)state.inString=true;}return state;}
function jsonEnvelopeComplete(state){return state.closed&&!state.invalid&&!state.inString&&state.depth===0;}
function exportFailure(error,reason){if(reason==="user")return{kind:"cancelled",text:""};if(reason==="route")return{kind:"route",text:""};if(reason==="stall")return{kind:"stalled",text:""};if(error&&error.message==="Session expired")return{kind:"session",text:""};if(Number.isInteger(error&&error.status))return{kind:"refused",text:localText(error.message)};return{kind:"lost",text:""};}
function exportShareOutcome(error){return!error?"shared":error.name==="AbortError"?"share_cancelled":"share_unavailable";}
// The device message is filled in after tr() because tr() escapes values for HTML and this text is shown as plain text.
function exportPhaseText(outcome,info){const values=info||{},texts={preparing:()=>tr("ui.export_flow_preparing","The device is preparing the export …"),transferring:()=>tr("ui.export_flow_transferring","The device is sending the export …"),ready:()=>tr("ui.export_flow_ready","Export complete and checked ({size}). Share or download the file."),shared:()=>tr("ui.export_flow_shared","File handed to the share dialog."),share_cancelled:()=>tr("ui.export_flow_share_cancelled","Sharing cancelled. The file is still ready."),share_unavailable:()=>tr("ui.export_flow_share_unavailable","Sharing is not possible here. Download the file instead."),downloaded:()=>tr("ui.export_flow_downloaded","File handed to the browser. The browser shows where and whether it was saved."),cancelled:()=>tr("ui.export_flow_cancelled","Export cancelled. No file was created."),stalled:()=>tr("ui.export_flow_stalled","No data from the device for {seconds} s. Check the connection and try again."),lost:()=>tr("ui.export_flow_lost","The connection to the device was interrupted. Try again."),refused:()=>tr("ui.export_flow_refused","Export failed: {message}"),incomplete:()=>tr("ui.export_flow_incomplete","The export ended early and is incomplete. No file was offered. Try again.")};return Object.keys(texts).includes(outcome)?texts[outcome]().replace(/\{(size|message|seconds)\}/g,(_,name)=>values[name]??""):"";}
function exportMetaText(elapsedMs,bytes,done){const values={elapsed:exportElapsedText(elapsedMs),size:exportSizeText(bytes)};return done?tr("ui.export_flow_meta_done","Took {elapsed} · {size}",values):tr("ui.export_flow_meta","{elapsed} elapsed · {size} received",values);}
function exportBusy(phase){return phase==="preparing"||phase==="transferring";}
// The question before closing the export sheet during a run (E22): Keep exporting or Cancel export; only Cancel export answers true.
// The sheet history binds it like every overlay dialog, so Back and Escape close it as Keep exporting. The answer comes once its history entry is gone.
// Focus returns to the opener after the answer, so the sheet's Close button is no longer busy; the component's own close ("ended") leaves focus alone.
function exportCloseQuestion(signal){const opener=document.activeElement,dialog=h("dialog",{class:"action-sheet export-close-question","aria-labelledby":"export-close-title"}),stay=iconButton(tr("ui.export_flow_close_stay","Keep exporting"),()=>dialog.close("stay"),"secondary"),cancelExport=iconButton(tr("ui.export_flow_close_confirm","Cancel export"),()=>dialog.close("confirm"),"danger"),onAbort=()=>dialog.close("ended");const answer=new Promise(resolve=>dialog.addEventListener("close",()=>{const value=dialog.returnValue==="confirm",restore=dialog.returnValue!=="ended";signal.removeEventListener("abort",onAbort);dialog.remove();Promise.resolve(dialog.sheetHistory).then(()=>{resolve(value);setTimeout(()=>{if(restore&&opener&&opener.isConnected&&!opener.disabled)opener.focus();},0);});},{once:true}));signal.addEventListener("abort",onAbort,{once:true});dialog.append(h("div",{class:"sheet-form"},h("div",{class:"sheet-grip","aria-hidden":"true"}),h("h2",{id:"export-close-title",text:tr("ui.export_flow_close_title","Cancel export?")}),h("p",{class:"muted",text:tr("ui.export_flow_close_text","The export is still running. If you close this sheet, it is cancelled and no file is created.")}),h("div",{class:"sheet-actions"},stay,cancelExport)));overlay.append(dialog);dialog.showModal();stay.focus();return{dialog,answer};}
// The GeoJSON row of the export sheet (E8-E14, E20): its lifetime is the sheet signal, which ends when the sheet closes or the
// view is left. A run reads the body itself (no overall deadline, only EXPORT_STALL_MS without data while the page is visible)
// and offers the file only after the bracket check; share and download are a second tap. Leaving the view during a run
// cancels it without asking; the result then goes to the dock only (late-action ruling #24), the confirmed close stays quiet.
function exportGeojsonControl(project,{signal}){
  let phase="idle",run=null,running=null,file=null,url=null,pending=null,closeConfirmed=false,disposed=false,disposerBound=false;
  const OUTCOME_KIND={preparing:"running",transferring:"running",ready:"ok",shared:"ok",downloaded:"ok",share_cancelled:"warn",share_unavailable:"warn",cancelled:"warn",stalled:"critical",lost:"critical",refused:"critical",incomplete:"critical"};
  const phaseText=h("p",{class:"export-flow-phase",role:"status","aria-live":"polite"}),meta=h("p",{class:"export-flow-meta"}),actions=h("div",{class:"export-flow-actions"}),status=h("div",{class:"export-flow-status",hidden:true},phaseText,meta,actions);
  const start=iconButton(tr("ui.export_flow_start","Export GeoJSON"),()=>running||(running=begin().finally(()=>{running=null;})),"secondary"),node=h("div",{class:"export-flow",role:"group","data-export":"geojson","aria-label":tr("ui.export_flow_region","GeoJSON export"),"data-phase":"idle","data-outcome":""},start,status);
  // The start button stays busy for the whole run (its promise); a click that still arrives joins the run.
  start.dataset.exportAction="start";
  const button=(name,label,handler,kind,attrs)=>{const item=h("button",Object.assign({type:"button",class:"btn "+kind,"data-export-action":name,text:label},attrs));item.addEventListener("click",event=>runButtonAction(item,handler,event));return item;};
  // Focus waits for the start button: runButtonAction frees it only after the run's promise has settled.
  const focusStart=()=>setTimeout(()=>{if(start.isConnected&&!start.disabled&&!disposed)start.focus();},0);
  const show=(outcome,values)=>{const message=outcome?exportPhaseText(outcome,values):"";node.dataset.outcome=outcome||"";if(phaseText.textContent!==message)phaseText.textContent=message;if(OUTCOME_KIND[outcome])status.dataset.kind=OUTCOME_KIND[outcome];else delete status.dataset.kind;};
  const enter=(next,items)=>{phase=next;node.dataset.phase=next;status.hidden=next==="idle";if(items)replace(actions,items);if(!exportBusy(next))start.textContent=next==="idle"?tr("ui.export_flow_start","Export GeoJSON"):tr("ui.export_flow_retry","Export again");};
  const release=()=>{if(url)URL.revokeObjectURL(url);url=null;file=null;};
  const dispose=()=>{disposed=true;release();guardExportSheetClose(signal,null);if(pending)pending.dialog.close("ended");};
  const askClose=()=>{if(!exportBusy(phase))return true;if(!pending){const question=exportCloseQuestion(signal);pending={dialog:question.dialog,answer:question.answer.then(answer=>{pending=null;if(answer)closeConfirmed=true;return answer===true;})};}return pending.answer;};
  const shareFile=async()=>{let error=null;try{await navigator.share({files:[file],title:project.name});}catch(caught){error=caught||{};}if(file)show(exportShareOutcome(error),{size:exportSizeText(file.size)});};
  const download=()=>{if(!file)return;if(!url)url=URL.createObjectURL(file);const link=h("a",{href:url,download:file.name,hidden:true});node.append(link);link.click();link.remove();show("downloaded",{size:exportSizeText(file.size)});};
  const discard=()=>{release();enter("idle",[]);show("");meta.textContent="";start.focus();};
  const readyActions=()=>{let canShare=false;try{canShare=Boolean(navigator.share&&navigator.canShare&&navigator.canShare({files:[file]}));}catch(_){canShare=false;}return[canShare?button("share",tr("ui.export_share","Share file"),shareFile,"primary",{"data-export":"geojson-share"}):[],button("download",tr("ui.export_download","Download file"),download,"secondary",{"data-export":"geojson-download"}),button("discard",tr("ui.export_flow_discard","Discard file"),discard,"secondary")];};
  const end=reason=>{if(run&&!run.reason){run.reason=reason;run.controller.abort();}};
  async function begin(){
    if(run||disposed||signal.aborted)return;
    release();
    if(!disposerBound){disposerBound=true;signal.addEventListener("abort",dispose,{once:true});}
    const controller=new AbortController(),current=run={controller,reason:null},startedAt=performance.now(),chunks=[],envelope=jsonEnvelopeStart();
    let received=0,lastData=startedAt,reader=null,outcome=null,failure=null;
    const forward=()=>end("route"),onVisibility=()=>{if(document.visibilityState==="visible")lastData=performance.now();};
    const timer=setInterval(()=>{meta.textContent=exportMetaText(performance.now()-startedAt,received,false);if(document.visibilityState==="visible"&&performance.now()-lastData>=EXPORT_STALL_MS)end("stall");},EXPORT_TICK_MS);
    signal.addEventListener("abort",forward,{once:true});document.addEventListener("visibilitychange",onVisibility);
    enter("preparing",[button("cancel",tr("ui.export_flow_cancel","Cancel export"),()=>end("user"),"secondary")]);show("preparing");meta.textContent=exportMetaText(0,0,false);
    try{
      const response=await api.request("/api/tracking/export?project_id="+encodeURIComponent(project.id)+"&format=geojson",{signal:controller.signal,timeout:null});
      if(!response.body)throw new TypeError();
      reader=response.body.getReader();lastData=performance.now();enter("transferring");show("transferring");
      for(;;){const {done,value}=await reader.read();if(done)break;chunks.push(value);received+=value.byteLength;jsonEnvelopeScan(envelope,value);lastData=performance.now();}
      reader.releaseLock();reader=null;
      if(jsonEnvelopeComplete(envelope)){file=new File([new Blob(chunks,{type:"application/geo+json"})],`${project.name}.geojson`,{type:"application/geo+json"});outcome="ready";}else outcome="incomplete";
    }catch(error){
      if(reader)try{await reader.cancel();}catch(_){}
      reader=null;failure=exportFailure(error,current.reason);
    }finally{
      // Transfer end: the run's own controller (the API binding) is dropped, the forwarding from the sheet signal, the tick and
      // the visibility listener go. A finished transfer is not aborted.
      chunks.length=0;clearInterval(timer);document.removeEventListener("visibilitychange",onVisibility);signal.removeEventListener("abort",forward);run=null;
    }
    if(pending)pending.dialog.close("ended");
    // The sheet is gone: nothing is rendered any more.
    if(failure&&failure.kind==="route"){phase="cancelled";if(!closeConfirmed)showWarning(exportPhaseText("cancelled"),"export");return;}
    meta.textContent=exportMetaText(performance.now()-startedAt,received,true);
    if(outcome==="ready"){const items=readyActions().flat();enter("ready",items);show("ready",{size:exportSizeText(file.size)});items[0].focus();return;}
    const kind=outcome||failure.kind;enter(kind==="cancelled"?"cancelled":"failed",[]);show(kind,{size:exportSizeText(received),message:failure&&failure.text,seconds:Math.round(EXPORT_STALL_MS/1000)});focusStart();
  }
  guardExportSheetClose(signal,askClose);
  return node;
}

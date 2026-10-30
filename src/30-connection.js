// SPDX-License-Identifier: AGPL-3.0-only
let activeController=null,pollTimer=0,pollBusy=false,connectionState={label:"Connecting",state:"warn"};
const LINK_SYMBOL={live:"●",delayed:"◐",connecting:"◐",lost:"○",error:"○"};
function livePollIntervalMs(path){return path===MEASURE_ROUTE||/^projects\//.test(path)?2000:5000;}
// Header pill state from the data link only; it never reads the measurement gate.
function pillLink(link,error,stale){if(error)return{state:"error",cls:"error",symbol:LINK_SYMBOL.error,word:""};return{state:link,cls:link==="lost"||stale?"error":link==="live"?"ok":"warn",symbol:LINK_SYMBOL[link],word:({live:tr("ui.link_live","Live"),delayed:tr("ui.link_delayed","Delayed"),lost:tr("ui.link_lost","No data"),connecting:tr("ui.link_connecting","Connecting")})[link]};}
function setConnection(label,state){connectionState={label,state:state||"warn"};if(state==="error"){capacityEstimate.reset();updateGlobalStatus(store.state.live);if(currentRoute().path===MEASURE_ROUTE&&store.state.live)updateCapacityPanel(store.state.live);}}
// Measurement gate on the pill label (#21d): the device verdict as the hero reads it (a failing gate row blocks too);
// unknown without a verdict, with a stale fix or without fresh data. The link dot never reads it.
function pillGate(live,link,stale){const verdict=live&&live.current_quality;if(!live||stale||(link!=="live"&&link!=="delayed")||!verdict||typeof verdict!=="object")return"unknown";return verdict.accepted===true&&!gateRows(live).some(row=>row.state==="fail")?"ready":"blocked";}
function updateGlobalStatus(live){ingestFreshness(live);noteLiveAnswer(live);
  const target=$("live-state"),label=target.querySelector("b"),symbol=target.querySelector("span"),detail=target.querySelector("small"),
    link=liveView(livePollIntervalMs(currentRoute().path)).link,stale=fixIsStale(live&&live.current_fix),cause=correctionsCause(live);
  // Class, symbol and data-link come from the data link and the position age only, never from the measurement gate.
  const applyLink=pill=>{target.className="live-state "+pill.cls;target.dataset.link=pill.state;symbol.textContent=pill.symbol;};
  if(live&&live.tracking_startup&&live.tracking_startup.state!=="ready"){
    const pill=pillLink(link,false,stale);applyLink(pill);label.textContent=fixLabel(live.current_fix);label.dataset.gate="unknown";
    detail.textContent=[pill.word,cause&&cause.short,localText(live.tracking_startup.state==="error"?"Survey storage unavailable":"Loading survey storage")].filter(Boolean).join(" · ");
    target.title=[`${label.textContent} · ${detail.textContent}`,cause&&cause.text].filter(Boolean).join(" · ");target.setAttribute("aria-label",target.title);return;
  }
  if(connectionState.state==="error"){
    applyLink(pillLink(link,true,false));label.textContent=localText(connectionState.label);label.dataset.gate="unknown";detail.textContent=localText("Tap for status");
    target.title=`${label.textContent} · ${detail.textContent}`;
    target.setAttribute("aria-label",`${target.title}. ${localText("Open status")}`);return;
  }
  const fix=live&&live.current_fix||{},line=live&&live.active_line,
    recording=line&&line.state==="recording",paused=line&&line.state==="paused",progress=live&&live.measurement_progress||{},
    accuracy=displayAccuracy(fix),age=live&&live.rtcm&&(live.rtcm.epoch_age_sec!=null?live.rtcm.epoch_age_sec:live.rtcm.last_msg_age_sec),
    horizontal=accuracy.horizontal_sigma_m,accuracyText=horizontal==null?"H —":`H ${fmt(horizontal,Number(horizontal)<1?3:1)} m`,
    startupText=routeStartupLabel(live),activity=progress.active?`● ${formatClock(progress.elapsed_sec)} · ${localText("Measuring")}`:
      recording?(startupText||`● ${formatClock(line.recording_sec)} · ${localText("Recording")}`):paused?localText("Recording paused"):"",
    receiverDetail=[age!=null&&age>RTCM_EPOCH_WARN_SEC?tr("ui.link_latency","Link latency {seconds} s",{seconds:fmt(age,1)}):null,accuracyText].filter(Boolean).join(" · ");
  // Keep the fix in the first, unclipped status line throughout every activity.
  const pill=pillLink(link,false,stale);applyLink(pill);
  label.textContent=stale?tr("ui.fix_stale_short","Position outdated"):fixLabel(fix);
  // The label names the gate (colour plus a word): the detail says "blocked" unless an activity holds the line, title and aria-label always.
  const gate=pillGate(live,link,stale),blocked=gate==="blocked";label.dataset.gate=gate;
  const diagnostics=live&&live.field_diagnostics;
  detail.textContent=[pill.word,blocked&&!activity?tr("ui.pill_gate_blocked","blocked"):null,cause&&cause.short,(diagnostics&&diagnostics.enabled?localText(diagnostics.error?"DIAG error · ":"DIAG on · "):"")+(activity||receiverDetail)].filter(Boolean).join(" · ");
  target.title=[label.textContent,pill.word,blocked?tr("ui.pill_measurement_blocked","measurement blocked"):null,cause&&cause.text,activity,line&&line.name,receiverDetail,
    progress.active?`${progress.collected||0} ${localText("valid samples")}`:""].filter(Boolean).join(" · ");
  target.setAttribute("aria-label",`${target.title}. ${localText("Open status")}`);
}

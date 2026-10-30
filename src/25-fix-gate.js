// SPDX-License-Identifier: AGPL-3.0-only
// Fix freshness: a stale position stays locked until a larger fix_seq arrives. Delayed or reordered
// responses never clear or lower the latch; only a device reboot (the live uptime drops by more than a
// reordering could explain, 10 s) restarts fix_seq and resets it. boot_count is zeroed after boot and unusable.
let staleSinceSeq=null,freshnessUptime=null,liveAt=null,liveFixSeen=null,liveSince=null;
function fixIsUsable(fix){return Boolean(fix&&!fix.stale&&(staleSinceSeq==null||fix.fix_seq>staleSinceSeq));}
function fixIsStale(fix){return Boolean(fix)&&!fixIsUsable(fix);}
// Measurement approval = the gate accepts the epoch and the position is fresh. Survey lines need it, route recording does not.
function measurementApproved(live){return Boolean((live&&live.current_quality||{}).accepted)&&fixIsUsable(live&&live.current_fix);}
function startLineVerdict(live,mode){if(measurementApproved(live))return null;return mode==="route"?{blocked:false,text:tr("ui.route_without_approval","Route recording is possible without measurement approval.")}:{blocked:true,text:tr("ui.start_line_survey_blocked","Survey lines need measurement approval. Choose Route to record without it.")};}
function staleText(fix){const seconds=fix.fix_age_ms==null?"—":Math.round(fix.fix_age_ms/1000);return tr("ui.fix_stale","Position outdated for {seconds} s",{seconds}).replace("{seconds}",seconds);}
function ingestFreshness(live){if(!live)return false;const fix=live.current_fix,system=live.system||{},uptime=system.uptime_sec==null?NaN:Number(system.uptime_sec);if(Number.isFinite(uptime)){if(freshnessUptime!=null&&uptime<freshnessUptime-10){staleSinceSeq=null;freshnessUptime=uptime;}else freshnessUptime=freshnessUptime==null?uptime:Math.max(freshnessUptime,uptime);}if(fix){if(!Number.isFinite(fix.fix_seq))staleSinceSeq=null;else if(fix.stale)staleSinceSeq=staleSinceSeq==null?fix.fix_seq:Math.max(staleSinceSeq,fix.fix_seq);}live.fix_usable=fixIsUsable(fix);return live.fix_usable;}
const LIVE_DELAYED_MS=3000,LIVE_LOST_MS=10000,FIX_AGE_SHOWN_MS=3000;
// Data age: only an answer with its own, new current_fix object counts as fresh device data (a tracking mutation merges the cached fix reference and stays a sign of life only). Monotonic clock, so phone clock jumps cannot distort it.
function noteLiveAnswer(live,now){const fix=live&&live.current_fix;if(!fix||typeof fix!=="object"||fix===liveFixSeen)return false;liveFixSeen=fix;liveAt=now??performance.now();return true;}
// Start of the first connection attempt; a restart (tab visible again) only counts while no data ever arrived.
function noteLiveAttempt(now,restart){if(liveSince==null||(restart&&liveAt==null))liveSince=now??performance.now();}
function liveAgeMs(now){return liveAt==null?null:Math.max(0,(now??performance.now())-liveAt);}
// Thresholds are written for the 2 s poll; slower polls shift both by the extra interval.
function liveLinkState(ageMs,intervalMs){if(ageMs==null)return"connecting";const slack=Math.max(0,(intervalMs||2000)-2000);return ageMs<LIVE_DELAYED_MS+slack?"live":ageMs<LIVE_LOST_MS+slack?"delayed":"lost";}
function liveView(intervalMs,now){const t=now??performance.now(),age=liveAgeMs(t);if(age!=null)return{age,link:liveLinkState(age,intervalMs),never:false};return{age:null,link:liveSince!=null&&liveLinkState(t-liveSince,intervalMs)==="lost"?"lost":"connecting",never:true};}
// Gate consistency (#9): looser-than-default limits as a badge, and the limits a point was measured under.
function gateDeviationText(items){return(Array.isArray(items)?items:[]).map(item=>`${item.key} ${item.value} ${item.key==="required_fix"||String(item.key).startsWith("max_")?">":"<"} ${item.default}`).join(" · ");}
function gateLimitFormat(value,minimum){const number=Number(value);return Number.isFinite(number)?number.toFixed(Math.max(minimum,(String(number).split(".")[1]||"").length)):null;}
function gateLimitsLine(limits){if(!limits||typeof limits!=="object")return"";const fix=limits.fix_quality||limits.required_fix,part=(prefix,value,suffix,minimum)=>{const text=gateLimitFormat(value,minimum);return value==null||text==null?null:`${prefix}${text}${suffix}`;};return[fix?String(fix).replaceAll("_"," "):null,part("≥",limits.min_satellites," sat",0),part("HDOP ≤",limits.max_hdop,"",0),part("age ≤",limits.max_correction_age_sec," s",0),part("H ≤",limits.max_gst_horizontal_sigma_m," m",2),part("V ≤",limits.max_gst_vertical_sigma_m," m",2),part("spread ≤",limits.max_horizontal_spread_m," m",2),part("window ",limits.quality_window_samples,"",0)].filter(Boolean).join(" · ");}
function gateBadge(){return h("div",{class:"gate-softened",role:"status",hidden:true});}
function renderGateBadge(node,items){const list=Array.isArray(items)?items:[],signature=JSON.stringify(list);if(node.dataset.signature===signature)return;node.dataset.signature=signature;node.hidden=!list.length;node.title=gateDeviationText(list);replace(node,list.length?[h("b",{text:tr("ui.gate_softened","Gate softened")}),...list.map(item=>h("span",{class:"gate-entry",text:gateDeviationText([item])}))]:[]);}
// A pending GST (the fix is published before its GST arrives) keeps the accuracy rows that were rendered before.
// While the epoch's GST is pending, the device sends the accuracy its held verdict was judged on.
function displayAccuracy(fix){return(fix&&(fix.receiver_accuracy||(fix.pending_gst?fix.held_receiver_accuracy:null)))||{};}
// The device refuses writes below its flash reserve; capacity.writable is authoritative once tracking runs, the free share covers the time before.
function storageBlocked(live){const capacity=(live&&live.capacity)||{},board=(live&&live.board)||{};return capacity.writable===false||Boolean(board.fs_total_bytes>0&&board.fs_free_bytes/board.fs_total_bytes<STORAGE_RESERVE_RATIO);}
function requiredFixName(limits){return(limits&&(limits.fix_quality||limits.required_fix)||"RTK_FIXED").replaceAll("_"," ");}
function measurementPhaseText(phase,limits){if(phase==="waiting_fix")return tr("ui.phase_waiting_fix","Waiting for {fix} and valid GST",{fix:requiredFixName(limits)});if(phase==="collecting")return localText("Collecting valid samples");if(phase==="checking_spread")return localText("Checking measurement spread");return null;}
// A dialog that ends in a measurement must not stay submittable once the position goes stale: the submit is
// disabled while the dialog is open, and the returned check runs again right before submitting. Lost device data (no fresh
// answer for 10 s) locks it first, whatever the last known position says; fresh data release it with the input kept.
function guardStaleSubmit(dialog,form,save,extra){const info=h("p",{class:"muted fix-guard",role:"status",hidden:true}),actions=form.querySelector(".sheet-actions"),check=()=>{const link=liveView(livePollIntervalMs(MEASURE_ROUTE)).link;if(link==="lost"){save.disabled=true;info.hidden=false;const text=liveLostText(null).action;if(info.textContent!==text)info.textContent=text;return false;}const live=store.state.live||{};ingestFreshness(live);const fix=live.current_fix;if(!fixIsUsable(fix)){save.disabled=true;info.hidden=false;const stale=fix?staleText(fix):tr("ui.no_current_position","No current position");if(info.textContent!==stale)info.textContent=stale;return false;}const verdict=extra?extra(live,form):null,next=verdict&&verdict.text||"";save.disabled=Boolean(verdict&&verdict.blocked);if(info.textContent!==next)info.textContent=next;info.hidden=!info.textContent;return !save.disabled;},stop=store.subscribe(check);liveDialogChecks.add(check);form.addEventListener("change",check);dialog.addEventListener("close",stop,{once:true});dialog.addEventListener("close",()=>liveDialogChecks.delete(check),{once:true});if(actions)actions.before(info);else form.append(info);check();return check;}
// Open measurement dialogs register their lock check here; the ticker runs it every second.
const liveDialogChecks=new Set();

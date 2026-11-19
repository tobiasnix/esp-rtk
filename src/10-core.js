// SPDX-License-Identifier: AGPL-3.0-only
const tr=(key,fallback,values)=>{if(!globalThis.RtkI18n)return fallback;const translated=globalThis.RtkI18n.t(key,values);return translated===key?fallback:translated;};
const V2_DE=globalThis.RtkI18n&&RtkI18n.v2de||{};
function localText(value){const source=value==null?"":String(value),i=globalThis.RtkI18n;if(i&&i.localize)return i.localize(source);if(i&&i.v2de&&i.getLanguage&&i.getLanguage()==="de")return i.v2de[source]||source;return typeof V2_DE!=="undefined"&&V2_DE[source]||source;}
const text=value=>document.createTextNode(localText(value));
// User data (names, notes, SSIDs, log lines) is shown verbatim: never run it through the dictionary.
const raw=value=>document.createTextNode(value==null?"":String(value));
function h(tag,attrs,...children){const node=document.createElement(tag);Object.entries(attrs||{}).forEach(([key,value])=>{if(value==null||(value===false&&!key.startsWith("aria-")))return;if(key==="class")node.className=value;else if(key==="text")node.textContent=localText(value);else if(key==="textRaw")node.textContent=value==null?"":String(value);else if(key.startsWith("on"))node.addEventListener(key.slice(2),value);else if(key in node&&key!=="form")node[key]=value;else node.setAttribute(key,value===true&&!key.startsWith("aria-")?"":String(value));});children.flat(Infinity).forEach(child=>node.append(child instanceof Node?child:text(child)));return node;}
const replace=(target,...children)=>target.replaceChildren(...children.flat(Infinity));
// Measurement numbers follow the language (decimal mark); one formatter per language and digit count.
const fmt=((cache)=>(value,digits=2)=>{if(value==null)return"—";const language=globalThis.RtkI18n?RtkI18n.getLanguage():"en",locale=({de:"de-DE",pl:"pl-PL"})[language]||"en-US",key=locale+digits;return(cache[key]||(cache[key]=new Intl.NumberFormat(locale,{minimumFractionDigits:digits,maximumFractionDigits:digits,useGrouping:false}))).format(Number(value));})({});
// Exact, locale independent decimals for the position line.
const fixedText=(value,digits=2)=>value==null?"—":Number(value).toFixed(digits);
function e8Text(value){const negative=value<0,digits=String(Math.abs(value)).padStart(9,"0");return`${negative?"-":""}${digits.slice(0,-8)}.${digits.slice(-8)}`;}
function coordText(e8,degrees){return Number.isSafeInteger(e8)?e8Text(e8):Number(degrees).toFixed(7);}
function coordToDeg(c){return[c[0]/1e8,c[1]/1e8,c[2]/1000];}
function mmText(mm){if(mm==null)return null;const negative=mm<0,abs=Math.abs(mm);return`${negative?"-":""}${Math.floor(abs/1000)}.${String(abs%1000).padStart(3,"0")} m`;}
function recordedUtc(source){const text=source&&typeof source.recorded_at_utc==="string"&&source.recorded_at_utc?source.recorded_at_utc:null,unsynced=!!source&&source.clock_unsynced===true;return text==null&&!unsynced?null:{label:tr("ui.recorded_utc","Recorded (UTC)"),text,unsynced,warning:unsynced?tr("ui.clock_unsynced","Clock was not synchronised when this point was recorded"):null};}
function heightRows(point){const measurement=(point&&point.measurement)||{},coordinate=(point&&point.coordinate)||[],msl=coordinate.length>2&&coordinate[2]!=null?coordinate[2]:null,antenna=measurement.antenna_height_mm==null?null:measurement.antenna_height_mm,ground=msl==null||antenna==null?null:msl-antenna;return{rows:[{label:tr("ui.height_msl","Height (MSL, phase centre)"),value:mmText(msl)},{label:tr("ui.height_ellipsoid","Ellipsoid height"),value:mmText(measurement.alt_ellipsoid_mm)},{label:tr("ui.geoid_separation","Geoid separation"),value:mmText(measurement.geoid_sep_mm)},{label:tr("ui.antenna_height","Antenna height above the ground point"),value:mmText(antenna)},{label:tr("ui.ground_height","Ground height (MSL)"),value:mmText(ground)}],hint:antenna===0?tr("ui.antenna_height_zero","Antenna height configured as 0; the ground height equals the phase-centre height."):antenna==null?tr("ui.antenna_height_missing","No antenna height was recorded with this point (older firmware); no ground height."):null};}
function fixLonLat(fix){if(fix&&Number.isSafeInteger(fix.lat_e8)&&Number.isSafeInteger(fix.lon_e8))return[fix.lon_e8/1e8,fix.lat_e8/1e8];return fix&&Number.isFinite(fix.lat)&&Number.isFinite(fix.lon)?[fix.lon,fix.lat]:null;}
const fixLabel=fix=>({0:"NO FIX",1:"GPS FIX",2:"DGPS",4:"RTK FIXED",5:"RTK FLOAT"})[Number((fix||{}).qual)]||String((fix||{}).fix_status_text||"NO FIX").replaceAll("_"," ");
// RTCM ages only shade the display (warn from 5 s); red follows rtcm.stale, i.e. the device setting ntrip_max_epoch_age_sec, and the fallback mirrors its default.
// STORAGE_RESERVE_RATIO mirrors tracking.MIN_FLASH_RESERVE_PERCENT; the control tolerances are a repeatability tolerance of a control point, not a gate value.
const RTCM_EPOCH_WARN_SEC=5,RTCM_EPOCH_STALE_FALLBACK_SEC=15,RTCM_ARRIVAL_LIVE_SEC=5,STORAGE_RESERVE_RATIO=.2,CONTROL_TOLERANCE_H_M=.05,CONTROL_TOLERANCE_V_M=.1;
function routeStartupLabel(live){
  const line=live&&live.active_line,startup=live&&live.route_startup;
  if(!line||line.recording_mode!=="route"||line.state!=="recording"||!startup||startup.ready)return "";
  if(startup.reason==="waiting_clock")return localText("Waiting for GNSS time");
  if(startup.reason==="waiting_position")return localText("Waiting for usable position");
  return `${localText("Stabilizing position")} ${startup.stable_sec||0}/${startup.required_sec||10} s`;
}
const duration=value=>{let seconds=Math.max(0,Math.floor(Number(value)||0)),hours=Math.floor(seconds/3600);seconds%=3600;const minutes=Math.floor(seconds/60);return hours?`${hours} h ${minutes} min`:`${minutes} min ${seconds%60} s`;};
const formatClock=value=>{const seconds=Math.max(0,Math.floor(Number(value)||0));return`${String(Math.floor(seconds/60)).padStart(2,"0")}:${String(seconds%60).padStart(2,"0")}`;};
const quantity=(count,one,many)=>`${count} ${globalThis.RtkI18n&&RtkI18n.getLanguage&&RtkI18n.getLanguage()==="pl"&&RtkI18n.plural?RtkI18n.plural(one,many,count):localText(Number(count)===1?one:many)}`;
const when=(timestamp,clock)=>{if(!timestamp)return"—";const age=Math.max(0,Number(clock)-Number(timestamp));if(age<60)return tr("ui.just_now","just now");if(age<3600)return tr("ui.minutes_ago",`${Math.floor(age/60)} min ago`,{count:Math.floor(age/60)});const date=new Date(Date.now()-age*1000);return globalThis.RtkI18n?RtkI18n.formatDateTime(date):date.toLocaleString();};
// The measurement method is a device value (#24-UI); drop the old browser-local copy once.
try{localStorage.removeItem("rtk-samples");}catch(_){}
const sampleLabel=count=>count==="quality"?localText("Until quality is OK"):count===1?tr("ui.sample_single","Instant measurement"):tr("ui.sample_avg",`${count}-sample average`,{count});
let keepAwake=localStorage.getItem("rtk-keep-awake")==="true",wakeLock=null;async function syncWakeLock(){if(wakeLock&&!keepAwake){await wakeLock.release().catch(()=>{});wakeLock=null;}if(keepAwake&&!document.hidden&&navigator.wakeLock&&!wakeLock){try{wakeLock=await navigator.wakeLock.request("screen");wakeLock.addEventListener("release",()=>{wakeLock=null;});}catch(_){wakeLock=null;}}}


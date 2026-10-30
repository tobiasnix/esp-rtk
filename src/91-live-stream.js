// SPDX-License-Identifier: AGPL-3.0-only
// Live stream (#27 E13-E15): one fix source keeps an EventSource open or leaves its caller polling. A live frame of the
// current generation raises requestSerial (rule R2: every older running poll is dropped) and goes to options.accept;
// a heartbeat only restarts the watchdog, never the data age. A closed stream, two errors within 10 s, 22 s without an
// event or any bye fall back: the stream closes, options.restart() resumes polling and the source tries again after
// 30 s (300 s after eviction or the kill switch); a fallback also polls once at once, because the stream attempt had
// stopped the poll interval and the new one would only poll a full interval later. bye session takes the same fallback
// (Codex review, integrator ruling): a single forced poll would be lost to a poll already in flight and leave no timer;
// with the interval the next poll's 401 leads to the sign-in, and declining it keeps polling. The restart hold (#23c)
// closes the stream without a fallback.
// The retry timer clears retryAt itself: a coarse performance.now() may read a moment short of it when the timer fires.
// start() opens a stream only while options.wanted() (visible and online; Codex review, integrator ruling): an app loaded in
// a hidden background tab or offline polls instead (pollLive rests while hidden), so it holds none of the device's three
// streams; the visible and online transitions of src/99-boot.js call startPolling and so start() again.
const LIVE_STREAM_URL="/api/events",LIVE_STREAM_WATCHDOG_MS=22000,LIVE_STREAM_ERROR_WINDOW_MS=10000,LIVE_STREAM_RETRY_MS=30000,LIVE_STREAM_YIELD_MS=300000;
function createFixSource(options){let source=null,generation=0,state="off",retryAt=0,errors=[],watchdog=0,retryTimer=0;const close=()=>{if(source)source.close();source=null;++generation;clearTimeout(watchdog);},stop=()=>{close();state="off";},fallback=wait=>{close();state="poll";retryAt=performance.now()+wait;clearTimeout(retryTimer);retryTimer=setTimeout(()=>{retryAt=0;if(options.wanted())options.restart();},wait);options.restart();pollLive();},arm=()=>{clearTimeout(watchdog);watchdog=setTimeout(()=>fallback(LIVE_STREAM_RETRY_MS),LIVE_STREAM_WATCHDOG_MS);},connect=()=>{const Source=options.EventSource||globalThis.EventSource;if(!Source)return false;let instance;try{instance=new Source(options.url);}catch(_){return false;}const own=++generation,on=(name,fn)=>instance.addEventListener(name,event=>{if(own!==generation)return;if(restartHoldActive())return stop();fn(event);});source=instance;state="stream";errors=[];arm();on("live",event=>{arm();let live;try{live=JSON.parse(event.data);}catch(_){return;}++requestSerial;options.accept(live);});on("heartbeat",arm);on("bye",event=>{let reason=null;try{reason=JSON.parse(event.data).reason;}catch(_){}fallback(reason==="evicted"||reason==="disabled"?LIVE_STREAM_YIELD_MS:LIVE_STREAM_RETRY_MS);});on("error",()=>{if(instance.readyState===2)return fallback(LIVE_STREAM_RETRY_MS);const now=performance.now();errors=errors.filter(at=>now-at<LIVE_STREAM_ERROR_WINDOW_MS);errors.push(now);if(errors.length>=2)fallback(LIVE_STREAM_RETRY_MS);});return true;};return{start(){if(source)return true;if(options.url===null||!options.wanted()||retryAt&&performance.now()<retryAt||!connect()){state="poll";return false;}return true;},stop,renew(){if(state!=="stream")return;close();if(!connect())fallback(LIVE_STREAM_RETRY_MS);},mode:()=>state};}
// While a measurement runs, the poll of its dialog is the only source of the live state (E16): frames keep the stream
// and its watchdog alive but are not taken.
const liveSource=createFixSource({url:LIVE_STREAM_URL,restart:()=>startPolling(),accept:live=>{if(!measurementRequestActive)acceptLive(live);},wanted:()=>!document.hidden&&navigator.onLine!==false});
// Rule R3: a confirmed own change drops every older poll and the stream generation that may still carry frames from
// before it; the caller then adopts the confirmed state.
function invalidateLive(){++requestSerial;liveSource.renew();}

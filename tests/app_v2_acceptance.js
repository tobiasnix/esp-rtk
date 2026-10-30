// SPDX-License-Identifier: AGPL-3.0-only
const { chromium } = require("playwright");
const fs = require("fs"),
  assert = require("assert"),
  root = process.cwd();
const html = fs.readFileSync(process.env.RTK_APP_HTML || "/tmp/app-v2-local.html", "utf8");
const status = {
  system: {
    display_name: "RTK-TEST",
    version: "VTEST",
    uptime_sec: 120,
    reset_cause: "HARD",
    ip: "192.0.2.1",
  },
  board: {
    fs_total_bytes: 1000000,
    fs_free_bytes: 600000,
    ram_free_bytes: 7000000,
    wifi_rssi: -55,
    mcu_temp_c: 42,
    cpu_hz: 240000000,
  },
  stats: {
    wifi_ssid_aktiv: "Field",
    ble_auth_failures: 1,
    tcp_auth_failures: 1,
    nmea_tcp_auth_failures: 2,
    ntrip_gga_sent: 3,
    ntrip_bytes: 4096,
  },
  fix: { qual: 1, fix_status_text: "GPS_FIX", sats: 12 },
  ble: { clients: 1 },
  nmea_tcp: { clients: 2, port: 10110 },
  rtcm: { msm: ["MSM5"], mount: "TEST", last_msg_age_sec: 0.2, arrival_age_sec: 0.2, epoch_age_sec: 1.1,
    epoch_source: "GPS", stale: false },
  log_entries: 4,
};
const tracking = {
  device_time: 2000000,
  active_project_id: "p1",
  capacity: {used:31,limit:5000,remaining:4969,percent:1,level:"ok",writable:true},
  projects: [
    {
      id: "p1",
      name: "Project",
      line_count: 1,
      point_count: 1,
      created_at: 900,
      updated_at: 990,
    },
    { id: "p2", name: "Archive", line_count: 0, point_count: 0, created_at: 800, updated_at: 850 },
  ],
  active_project_lines: [
    {
      id: "l1",
      name: "Line",
      state: "finished",
      vertex_count: 30,
      length_m: 12.3,
    },
  ],
  control_checks: [{ name: "C1", measurements: 2, horizontal_delta_m: 0.01 }],
  active_line: null,
  current_quality: {
    accepted: false,
    limits: {
      fix_quality: "RTK_FIXED",
      max_gst_horizontal_sigma_m: 0.05,
      max_gst_vertical_sigma_m: 0.1,
      min_satellites: 10,
      max_hdop: 1.5,
      max_correction_age_sec: 5,
      max_horizontal_spread_m: 0.03,
    },
  },
};
const live = {
  schema_version: 1,
  current_fix: {
    qual: 1,
    fix_status_text: "GPS_FIX",
    sats: 12,
    hdop: 0.8,
    lat: 50,
    lon: 8,
    alt: 100,
    receiver_accuracy: { horizontal_sigma_m: 0.2, altitude_sigma_m: 0.3 },
  },
  current_quality: tracking.current_quality,
  quality_streak: { gate_streak_sec: 20 },
  measurement_progress: { active: false, collected: 0, total: 5 },
  rtcm: { mount: "TEST", last_msg_age_sec: 0.2, arrival_age_sec: 0.2, epoch_age_sec: 1.1, epoch_source: "GPS", stale: false },
  board: status.board,
  capacity: tracking.capacity,
  active_project: { id: "p1", name: "Project", line_count: 1, point_count: 1 },
  active_line: null,
  system: { boot_id: "a1b2c3d4" },
};
const profileLimits = { fix_quality: "RTK_FIXED", min_satellites: 10, max_hdop: 1.5, max_correction_age_sec: 5,
  max_gst_horizontal_sigma_m: 0.05, max_gst_vertical_sigma_m: 0.1, max_horizontal_spread_m: 0.05,
  quality_window_samples: 5 };
const softenedGate = [
  { key: "required_fix", value: "DGPS", default: "RTK_FIXED" },
  { key: "max_hdop", value: 2, default: 1.5 },
  { key: "max_gst_horizontal_sigma_m", value: 2, default: 0.05 },
  { key: "quality_window_samples", value: 3, default: 5 }];
// The device answers a gate change with the deviations it now has; the fixture mirrors that.
const setGateDeviations = list => { live.gate_deviations = list; status.gate_deviations = list; };
const fieldStatus={enabled:false,error:null,samples_this_boot:0,stored_bytes:0,limit_bytes:4194304};
const fieldMarkers=[];
// failFieldPost: true answers the field diagnostics POST with 500 "storage busy", a string answers with that message.
let failFieldPost=false;
// Test access (#21c, --feedback-only only): inserted before the closing })(); of the served app.js copy, never in src/*.js or app.js.
const FEEDBACK_HOOK='window.__rtkFeedbackTest={store,showFeedback:typeof showFeedback==="function"?showFeedback:null,showError,dismissFeedback:typeof dismissFeedback==="function"?dismissFeedback:null,capturedPointSpec:typeof capturedPointSpec==="function"?capturedPointSpec:null,undoCaptured:typeof undoCaptured==="function"?undoCaptured:null};\n';
// Live stream (#27 B4): /api/events is refused like the firmware of an older release (404, the page polls) unless
// streamMock.frame is set. Then the page's own EventSource gets that live frame after streamMock.delay and a response that
// ends (retry: 60000 keeps the reconnect out of the test). requests holds the arrival time of every /api/events request.
const streamMock={frame:null,delay:1500,requests:[]};
// Stakeout (WP3, --stakeout-only): /api/stakeout* is answered by stakeoutMock. The set documents are what the device
// stores for the sample package (stakeout.validate_import on the host), the live packet is tests/fixtures/stakeout_live_packet.json.
// The packet names no target until a select_target (like the device). liveQueue holds answers for the next live requests ({packet} or {status, body}); liveHold() keeps every live request open
// until the returned release(); liveRequests lists each live request ({at, open} with the number open at its start).
// overviewAnswers / actionAnswers[action] / settingsAnswers / importAnswers are queues of {status, body, delay} answers
// (a status 200 or no status answers normally after the delay). wp4a adds the WP4 A progress fields of the overview
// (sets[i].progress from entry.progress, null leaves it out); progress answers GET /api/stakeout/progress (404 in WP3).
// STAKEOUT_MOCK_ACTIONS answers POST /api/stakeout per action like the device.
// WP4 B (--stakeout-actions-only): progressLive answers GET /api/stakeout/progress from targets ({tid: {status, gen, mode,
// point_id}}) and floors ({set_id: n}) like the device (E18), and confirm/skip/reopen follow the decision table E20; an answer
// {abort:true} drops the connection, with lost:true after the device acted (a lost answer). exports lists the export requests
// that pass the page route (a download of an a[download] does not). progressFail "busy" answers the progress list with
// 503 stakeout_busy, "abort" drops its connection (the overview itself still answers).
const STAKEOUT_SET_DOC=(()=>{
  const script="import sys,json,asyncio;sys.path[:0]=['tests','.'];import support,stakeout\n"+
    "pkg=json.load(open('tests/fixtures/stakeout_sample_package.json'));pkg['project_id']='p1'\n"+
    "doc=asyncio.run(stakeout.validate_import(pkg));doc['imported_at']=845000000;print(json.dumps(doc))";
  try{const out=require("child_process").execFileSync("python3",["-c",script],{cwd:root,encoding:"utf8",stdio:["ignore","pipe","ignore"]});return JSON.parse(out.trim().split("\n").pop());}
  catch(error){return null;}
})();
const STAKEOUT_PACKET=JSON.parse(fs.readFileSync(root+"/tests/fixtures/stakeout_live_packet.json","utf8"));
const stakeoutMock={on:false};
function stakeoutReset(){
  Object.assign(stakeoutMock,{on:true,sets:{p1:[structuredClone(STAKEOUT_SET_DOC)],p2:[structuredClone(STAKEOUT_SET_DOC)]},extraSummaries:{},
    packet:{...structuredClone(STAKEOUT_PACKET),target:null},seq:100,autoSeq:true,liveQueue:[],hold:null,liveRequests:[],liveOpen:0,liveMaxOpen:0,liveFail:false,
    overviewAnswers:[],overviewRequests:[],setRequests:[],setDelay:0,actionAnswers:{},settingsAnswers:[],importAnswers:[],posts:[],settingsPuts:[],imports:[],
    settings:{tolerance_m:0.1,fine_radius_m:5},selected:null,orders:{},wp4a:false,writeUncertain:false,activeTid:null,progress:null,progressEntry:{},
    progressLive:false,progressFail:null,targets:{},floors:{},points:0,exports:[]});
}
const stakeoutLiveHold=()=>{let release;const promise=new Promise(resolve=>{release=resolve;});stakeoutMock.hold={promise,release:()=>{stakeoutMock.hold=null;release();}};return stakeoutMock.hold;};
const stakeoutSummary=(projectId,doc)=>{const summary={set_id:doc.set_id,state:"ok",imported_at:doc.imported_at,
  source:{filename:doc.source.filename,bytes:doc.source.bytes,features:doc.source.features,skipped_total:doc.source.skipped_total},
  parts:doc.parts.length,targets:doc.targets.length,coords:doc.parts.reduce((sum,part)=>sum+part.coords_e8.length,0)};
  if(stakeoutMock.wp4a){const entry=stakeoutMock.progressLive?stakeoutProgressBody(projectId).counts[doc.set_id]:stakeoutMock.progressEntry[doc.set_id];if(entry!==null)summary.progress=entry||{confirmed:0,verified:0,manual:0,skipped:0};}
  return summary;};
function stakeoutOverviewBody(projectId){
  const m=stakeoutMock,sets=(m.sets[projectId]||[]).map(doc=>stakeoutSummary(projectId,doc)).concat(m.extraSummaries[projectId]||[]);
  const body={schema:1,project_id:projectId,sets,totals:{sets:sets.length,targets:sets.reduce((sum,set)=>sum+(set.targets||0),0),orphaned:0},
    limits:{sets:4,targets_per_set:500,coords_per_set:2000,import_bytes:131072,set_file_bytes:393216},load_ms:3,load_block_ms:1,settings:m.settings,
    session:{target:projectId===tracking.active_project_id?m.selected:null,orders:projectId===tracking.active_project_id?m.orders:{},on_fix_us_max:100,fixes_seen:5}};
  if(m.wp4a)body.progress={active_tid:m.activeTid,damaged_lines:0,write_uncertain:Boolean(m.writeUncertain)};
  return body;
}
const stakeoutFindTarget=tid=>{for(const docs of Object.values(stakeoutMock.sets))for(const doc of docs){const target=doc.targets.find(item=>item.tid===tid);if(target)return{tid:target.tid,set_id:doc.set_id,part_id:target.part_id,seq:target.seq,name:target.name,lat_e8:target.lat_e8,lon_e8:target.lon_e8,alt_import:target.alt_import};}return null;};
const stakeoutTargetState=tid=>stakeoutMock.targets[tid]||{status:"open",gen:stakeoutMock.floors[tid.split("/")[0]]||0,mode:null,point_id:null};
function stakeoutProgressBody(projectId){
  const m=stakeoutMock,floors={},counts={},states=[];
  for(const doc of m.sets[projectId]||[]){floors[doc.set_id]=m.floors[doc.set_id]||0;counts[doc.set_id]={confirmed:0,verified:0,manual:0,skipped:0};}
  for(const [tid,state] of Object.entries(m.targets)){const count=counts[tid.split("/")[0]];if(!count)continue;states.push([tid,state.status,state.gen,state.mode,state.point_id]);
    if(state.status==="confirmed"){count.confirmed++;count[state.mode]++;}else if(state.status==="skipped")count.skipped++;}
  return{schema:1,project_id:projectId,active_tid:m.activeTid,write_uncertain:Boolean(m.writeUncertain),orders:m.orders,floors,counts,states};
}
// E20 in the mock: one decision per action and state; the selected target's live status follows the change.
function stakeoutDecide(post){
  const m=stakeoutMock,tid=post.tid,state=stakeoutTargetState(tid),g=state.gen,answer=(next,repeated)=>{m.targets[tid]=next;if(m.packet.target&&m.packet.target.tid===tid)m.packet.target={...m.packet.target,status:next.status};
    return{body:{persisted:true,repeated,tid,gen:next.gen,status:next.status,mode:next.mode,point_id:next.point_id,dn_m:null,de_m:null,dist_m:null,tolerance_m:null}};};
  const refuse=(code,status)=>({status:status||409,body:{error:code,message:"Refused: "+code,status:state.status,mode:state.mode,gen:g}});
  if(!stakeoutFindTarget(tid))return{status:404,body:{error:"target_not_found",message:"No such target."}};
  if(post.action==="reopen"){if(g>post.expect_gen&&state.status==="open")return answer(state,true);if(g!==post.expect_gen)return refuse("target_changed");if(state.status==="open")return refuse("target_open");
    return answer({status:"open",gen:g+1,mode:null,point_id:null},false);}
  if(post.expect_gen!==g)return refuse("target_changed");
  if(post.action==="skip"){if(state.status==="skipped")return answer(state,true);if(state.status==="confirmed")return refuse("target_not_open");return answer({status:"skipped",gen:g,mode:null,point_id:null},false);}
  if(state.status==="confirmed"&&state.mode===post.mode)return answer(state,true);
  if(state.status!=="open")return refuse("target_not_open");
  m.points++;return answer({status:"confirmed",gen:g,mode:post.mode,point_id:"stakeout-"+m.points},false);
}
const STAKEOUT_MOCK_ACTIONS={
  confirm:stakeoutDecide,skip:stakeoutDecide,reopen:stakeoutDecide,
  select_target(post){const m=stakeoutMock;if(post.tid===null){m.selected=null;m.packet.target=null;return{body:{selected:null,changed:true,remembered:true}};}
    const target=stakeoutFindTarget(post.tid);if(!target)return{status:404,body:{error:"target_not_found",message:"No such target."}};
    const changed=!m.selected||m.selected.tid!==target.tid;m.selected=target;m.packet.target={tid:target.tid,lat_e8:target.lat_e8,lon_e8:target.lon_e8,status:"open"};
    return{body:{selected:target,changed,remembered:true}};},
  set_order(post){const m=stakeoutMock,[set_id,part]=[post.part_id.split("/")[0],Number(post.part_id.split("/")[1])],list=new Set(m.orders[set_id]||[]);
    if(post.reversed)list.add(part);else list.delete(part);m.orders[set_id]=[...list].sort((a,b)=>a-b);if(!m.orders[set_id].length)delete m.orders[set_id];
    return{body:{part_id:post.part_id,reversed:post.reversed,remembered:true}};},
  delete_set(post){const m=stakeoutMock,project=post.project_id||tracking.active_project_id;m.sets[project]=(m.sets[project]||[]).filter(doc=>doc.set_id!==post.set_id);
    return{body:{deleted:post.set_id,cleanup:"done"}};},
};
async function stakeoutAnswer(route,queue,normal){
  const answer=queue&&queue.length?queue.shift():null;
  if(answer&&answer.delay)await new Promise(resolve=>setTimeout(resolve,answer.delay));
  if(answer&&answer.abort){if(answer.lost)normal();return route.abort("connectionfailed").catch(()=>{});}
  const json=(value,status)=>route.fulfill({status:status||200,contentType:"application/json",body:JSON.stringify(value)}).catch(()=>{});
  if(answer&&answer.raw!==undefined)return route.fulfill({status:answer.status||200,contentType:"application/json",body:answer.raw}).catch(()=>{});
  if(answer&&answer.status&&answer.status!==200)return json(answer.body,answer.status);
  const result=normal();return json(result.body,result.status);
}
async function stakeoutFixture(route,url,path,method){
  const m=stakeoutMock,body=method==="GET"?null:route.request().postDataJSON();
  if(path==="/api/stakeout/live"){
    const entry={at:Date.now(),open:++m.liveOpen};m.liveRequests.push(entry);m.liveMaxOpen=Math.max(m.liveMaxOpen,m.liveOpen);
    try{
      if(m.hold)await m.hold.promise;
      if(m.liveFail){await route.abort("connectionfailed").catch(()=>{});return;}
      const next=m.liveQueue.length?m.liveQueue.shift():null;
      if(next&&next.raw!==undefined){await route.fulfill({status:200,contentType:"application/json",body:next.raw}).catch(()=>{});return;}
      if(next&&next.status){await route.fulfill({status:next.status,contentType:"application/json",body:JSON.stringify(next.body)}).catch(()=>{});return;}
      let packet=next&&next.packet?next.packet:m.packet;
      if(!(next&&next.packet)&&m.autoSeq){m.seq+=1;packet={...packet,fix_seq:m.seq};}
      await route.fulfill({status:200,contentType:"application/json",body:JSON.stringify(packet)}).catch(()=>{});
    }finally{m.liveOpen--;}
    return;
  }
  if(path==="/api/stakeout/progress"&&m.progressLive&&m.progressFail==="abort")return route.abort("connectionfailed").catch(()=>{});
  if(path==="/api/stakeout/progress"&&m.progressLive&&m.progressFail)return route.fulfill({status:503,contentType:"application/json",body:JSON.stringify({error:"stakeout_busy",message:"Busy."})}).catch(()=>{});
  if(path==="/api/stakeout/progress"&&m.progressLive)return route.fulfill({status:200,contentType:"application/json",body:JSON.stringify(stakeoutProgressBody(url.searchParams.get("project_id")))}).catch(()=>{});
  if(path==="/api/stakeout/export"){const query=Object.fromEntries(url.searchParams);m.exports.push(query);
    return route.fulfill({status:200,contentType:query.format==="csv"?"text/csv":"application/geo+json",headers:{"Content-Disposition":'attachment; filename="stakeout-'+query.project_id+"."+(query.format||"geojson")+'"'},
      body:query.format==="csv"?"tid,set_id\n":JSON.stringify({type:"FeatureCollection",kind:"stakeout_export",schema:1,project_id:query.project_id,features:[]})}).catch(()=>{});}
  if(path==="/api/stakeout/progress")return route.fulfill({status:m.progress?200:404,contentType:"application/json",body:JSON.stringify(m.progress||{error:"not_found",message:"No such stakeout endpoint."})}).catch(()=>{});
  if(path==="/api/stakeout/settings"&&method==="PUT"){m.settingsPuts.push(body);
    return stakeoutAnswer(route,m.settingsAnswers,()=>{Object.assign(m.settings,body);return{body:{values:m.settings,defaults:{tolerance_m:0.1,fine_radius_m:5}}};});}
  if(path==="/api/stakeout/import"&&method==="POST"){m.imports.push(body);
    return stakeoutAnswer(route,m.importAnswers,()=>({body:{set_id:"so-00000001",project_id:body.project_id,parts:body.parts.length,targets:body.targets.length,coords:0,skipped:body.source.skipped,skipped_total:body.source.skipped_total,bytes:1000,timing_ms:{}}}));}
  if(path==="/api/stakeout"&&method==="POST"){m.posts.push(body);
    return stakeoutAnswer(route,m.actionAnswers[body.action],()=>{const handler=STAKEOUT_MOCK_ACTIONS[body.action];return handler?handler(body):{status:400,body:{error:"unknown_action",message:"Unknown stakeout action."}};});}
  if(path==="/api/stakeout"&&method==="GET"){
    const projectId=url.searchParams.get("project_id")||tracking.active_project_id,setId=url.searchParams.get("set_id");
    if(setId){m.setRequests.push(setId);if(m.setDelay)await new Promise(resolve=>setTimeout(resolve,m.setDelay));
      const doc=(m.sets[projectId]||[]).find(item=>item.set_id===setId);
      return route.fulfill({status:doc?200:404,contentType:"application/json",body:JSON.stringify(doc||{error:"set_not_found",message:"No such set."})}).catch(()=>{});}
    m.overviewRequests.push(Date.now());
    return stakeoutAnswer(route,m.overviewAnswers,()=>({body:stakeoutOverviewBody(projectId)}));
  }
  return route.fulfill({status:404,contentType:"application/json",body:JSON.stringify({error:"not_found",message:"No such stakeout endpoint."})}).catch(()=>{});
}
let wifiScanCalls = 0, applyCalls = 0, failNextStatus = false, failLive = false, failProgress = false, finishMeasurement = null;
// Hanging live answers (#21b): blockLive() holds every /api/ui/live request until releaseLive(); lastLiveServedAt is the
// reference point of all timing assertions (the last answer the page really received), never the moment of blocking.
let holdLive=null,releaseLive=()=>{},lastLiveServedAt=0;
const blockLive=()=>{holdLive=new Promise(resolve=>{releaseLive=()=>{holdLive=null;resolve();};});};
// blockPolls() holds only the page's ordinary /api/ui/live polls (never verifyCapture's checks) until releasePolls().
let holdPolls=null,releasePolls=()=>{};
const blockPolls=()=>{holdPolls=new Promise(resolve=>{releasePolls=()=>{holdPolls=null;resolve();};});};
// Mutation answers carry only the live_status() keys (tracking.py:4478-4484), never current_fix/current_quality; trackingPosts counts every POST /api/tracking.
const LIVE_STATUS_KEYS=["revision","device_time","capacity","persistence","active_project","active_line","undo_candidate"];
let partialMutations=false,trackingPosts=0;
// mutateProjects (--router-cache-only): rename_project and delete_project change the project list like the device.
let mutateProjects=false;
// Capture fixture (#21c Task 4, active once captureState is set): actionAnswers answers the next POST of an action once
// ({delay,status,body,apply,abort,cancelable,tag}); the server's undo candidates form a stack checked like Tracker.undo_last
// (five fields, 409 for expect:null); liveAnswers patch the next /api/ui/live answers ({hang}, {status}, {delay}, {reboot}).
// checkAnswers go only to the page's own result checks (verifyCapture, marked by the harness's fetch wrapper).
let actionAnswers={},liveAnswers=[],checkAnswers=[],captureState=null,pendingCapture=null,signInPageDelay=0;
const trackingPostLog=[];
const serverUndoCandidate=()=>captureState&&captureState.stack.length?captureState.stack[captureState.stack.length-1]:null;
const syncCandidate=()=>{live.undo_candidate=serverUndoCandidate();};
const pushCandidate=candidate=>{captureState.stack.push(Object.assign({inventory_epoch:captureState.epoch,request_id:null},candidate));syncCandidate();return serverUndoCandidate();};
const projectRow=()=>tracking.projects.find(item=>item.id===(live.active_project||{}).id)||{};
// A point of a second browser: only the server state changes, the page learns it with its next poll.
const addForeignPoint=name=>{const order=++captureState.order;live.active_project.point_count+=1;projectRow().point_count=live.active_project.point_count;
  return pushCandidate({kind:"asset",feature_id:"f"+order,line_id:null,label:name,index:null,record_order:order,request_id:"other"});};
// A restore of the same inventory: same ids, indices and record orders, a new epoch, no request tags.
const replaceInventory=epoch=>{captureState.epoch=epoch;captureState.stack.forEach(item=>{item.inventory_epoch=epoch;item.request_id=null;});syncCandidate();};
const UNDO_FIELDS=["inventory_epoch","kind","feature_id","index","record_order"];
function applyCapture(post,tag){
  const action=post.action,line=live.active_line,requestId=tag===false?null:typeof tag==="string"?tag:post.request_id||null;
  if(action==="add_vertex"){line.vertex_count=(line.vertex_count||0)+1;const order=++captureState.order;
    return pushCandidate({kind:"vertex",feature_id:line.id,line_id:line.id,label:`${line.name} · V-${line.vertex_count}`,index:line.vertex_count,record_order:order,request_id:requestId});}
  if(action==="add_asset"){const order=++captureState.order,id="a"+order;live.active_project.point_count+=1;projectRow().point_count=live.active_project.point_count;
    pushCandidate({kind:"asset",feature_id:id,line_id:null,label:post.name||"Object point",index:null,record_order:order,request_id:requestId});return{id,name:post.name||null};}
  if(action==="start_line"){const order=++captureState.order;live.active_line={id:"ls"+order,name:post.name,state:"recording",recording_mode:post.recording_mode,vertex_count:0,length_m:0,recording_sec:0};
    live.active_project.line_count+=1;return{id:live.active_line.id,name:post.name,state:"recording",recording_mode:post.recording_mode};}
  if(action==="pause"||action==="resume"){line.state=action==="pause"?"paused":"recording";return{};}
  if(action==="finish"){captureState.stack=captureState.stack.filter(item=>item.line_id!==line.id);live.active_line=null;syncCandidate();return{};}
  if(action==="undo"){const top=serverUndoCandidate();
    if(post.expect===null||("expect" in post&&top&&UNDO_FIELDS.some(key=>top[key]!==post.expect[key])))return{status:409,body:{error:"undo_candidate_changed",message:"The last recorded point changed. Nothing was removed."}};
    if(!top)return{status:400,body:{error:"invalid_tracking_request",message:"No recorded point can be undone."}};
    captureState.stack.pop();if(top.kind==="asset"){live.active_project.point_count-=1;projectRow().point_count=live.active_project.point_count;}else if(line&&line.id===top.line_id)line.vertex_count-=1;syncCandidate();return{};}
  return{};
}
async function captureAnswer(route,post){
  const action=post.action,answer=actionAnswers[action]||{};delete actionAnswers[action];
  if(action==="cancel_measurement"){if(pendingCapture)pendingCapture();if(answer.abort)return route.abort("connectionreset");return route.fulfill({contentType:"application/json",body:"{}"});}
  let cancelled=false;
  // hold: a promise the test resolves to let the answer through (a response held back for as long as the test wants).
  if(answer.hold)await answer.hold;
  if(answer.delay)await new Promise(resolve=>{const timer=setTimeout(resolve,answer.delay);if(answer.cancelable)pendingCapture=()=>{cancelled=true;clearTimeout(timer);resolve();};});
  pendingCapture=null;
  if(cancelled)return route.fulfill({status:400,contentType:"application/json",body:JSON.stringify({error:"invalid_tracking_request",message:"Measurement cancelled."})});
  const apply=answer.apply??!(answer.abort||answer.status>=400);
  let result={};
  if(apply){result=applyCapture(post,answer.tag);if(answer.afterApply)answer.afterApply();if(result.status)return route.fulfill({status:result.status,contentType:"application/json",body:JSON.stringify(result.body)});}
  if(answer.abort)return route.abort("connectionreset");
  if(answer.status)return route.fulfill({status:answer.status,contentType:"application/json",body:JSON.stringify(answer.body||{})});
  if(answer.body)return route.fulfill({contentType:"application/json",body:JSON.stringify(answer.body)});
  const full=servedLive(),body=Object.assign({},result,post.request_id?{request_id:post.request_id}:{},{tracking_live:Object.fromEntries(LIVE_STATUS_KEYS.filter(key=>key in full).map(key=>[key,full[key]]))});
  return route.fulfill({contentType:"application/json",body:JSON.stringify(body)});
}
let projectDelay=0,mapDelay=0,accessDelay=0,mapRequests=0,rtcmOverride={};
// mapFail (--router-cache-only, #29b Task 7): /api/tracking/map answers 503. archiveMock: archive_project moves the project into
// tracking.archives and reactivate_archive moves it back (projects keeps the removed entries).
let mapFail=false;
const archiveMock={on:false,projects:{}};
// #24-UI Task 7: GET /api/tracking (without a view) can be held until released or answered with an error.
let trackingHold=null,trackingFail=null;
// onTrackingRead (--router-cache-only, review #29b): called on every GET /api/tracking without a view before its answer, like
// a device that records between two reads.
let onTrackingRead=null;
const holdTracking=()=>{let release;const promise=new Promise(resolve=>{release=resolve;});trackingHold={promise,release};return trackingHold;};
// Height chain: one point is on the initial map load, a second arrives through the map delta.
let mapRevision=1;
// Map model (--router-cache-only, #29b Task 6): while mapModel.on, /api/tracking/map serves mapModel.maps[project] in full or
// the changes above since_revision (like Tracker.iter_map_data), /api/ui/live carries mapModel.revision, and a restore with
// mapModel.restore replaces the geometry of the same project ids and sets the revision back. mapFullOnDelta answers a request
// with since_revision in full (an exhausted change window). mapRequests counts full map requests, mapDeltaRequests the others.
const mapModel={on:false,revision:50,maps:{},changes:[],restore:null};
let mapFullOnDelta=false,mapDeltaRequests=0;
const mapModelFull=id=>({project_id:id,revision:mapModel.revision,truncated:false,profiles:{"profile-1a2b3c4d":profileLimits},
  ...structuredClone(mapModel.maps[id]||{lines:[],points:[]})});
const mapModelChange=(id,change)=>{mapModel.revision+=1;mapModel.changes.push({revision:mapModel.revision,project_id:id,...change});};
const mapModelAddPoint=(id,point)=>{mapModel.maps[id].points.push(point);mapModelChange(id,{op:"asset",point:structuredClone(point)});};
const mapModelAddVertex=(id,lineId,coordinate)=>{const line=mapModel.maps[id].lines.find(item=>item.id===lineId);line.coordinates.push(coordinate);
  line.vertex_count=line.coordinates.length;mapModelChange(id,{op:"vertex",line_id:lineId,coordinate});};
let mapLineName = "Line", lineWarnings = [];
let gateRequiredFix = "RTK_FIXED";
const fieldSetup={antenna_height_m:2,reference_frame:"",reference_frame_epoch:""};
const fieldSetupDefaults={antenna_height_m:1.8,reference_frame:"",reference_frame_epoch:""};
let fieldSetupPuts=[];
// Field setup (#24-UI Task 2): the device method of the gate mock, a log of every request, the number of answered
// /api/ui/live polls and held answers. holdNext(test) holds the next request that test() accepts after its body is
// computed (the answer keeps the state of its arrival) until release(); arrived resolves with that body.
let gateSamples=5,liveAnswered=0,gateWindow=5;
// liveVary (--router-cache-only): every answered /api/ui/live carries other satellites, sigma, correction age and streak
// values (never a different verdict), like a moving receiver; liveAnswered counts the answers.
let liveVary=false;
const requestLog=[],requestHolds=[];
// holdNext(test, status): with a status the held request is answered with that HTTP error instead of its body.
const holdNext=(test,status)=>{const entry={test,status};entry.arrived=new Promise(resolve=>{entry.arrive=resolve;});entry.released=new Promise(resolve=>{entry.release=resolve;});requestHolds.push(entry);return entry;};
// Extra object points of project p1's map (WP4 B --stakeout-actions-only: a stakeout point in the point list).
let extraMapPoints=[];
const deltaPoint={ id: "a3", type: "valve", name: "Valve 3", note: "Via delta", coordinate: [800080000, 5000080000, 120000], chainage_m: 4.5,
  measurement: { precision: "e8", profile_id: "profile-1a2b3c4d", fix_quality: 4, fix_status: "RTK_FIXED", satellites: 17, hdop: .9,
    geoid_sep_mm: 47100, alt_ellipsoid_mm: 167100, antenna_height_mm: 1800, quality: { accepted: true, reasons: [] },
    recorded_at: 12, recorded_at_utc: "2000-01-01T00:00:12Z", clock_unsynced: true } };
let labelWifiQr="WIFI:T:WPA;S:RTK-1A2B-SETUP;P:PASS\\;WORD;;";
// Connections (#24-UI Task 4): failNext[path] answers the next request of that path (also /qr.js) with that HTTP status;
// the TCP authentication and the stream key follow PUT /api/tcp-auth and POST /api/stream-token/rotate like the device.
const failNext={};
let tcpAuthRequired=true,streamToken="TOKEN",streamRotations=0;

// Frame assignment fixtures (--frame-assignment-only, #39): the three POST /api/tracking actions, an optional 409,
// live answers that carry the assignment, and a second project p2 as the active one. Inert unless frameMock.on.
const FRAME_NAME="ETRS89 (example)",FRAME_TOKEN="0123456789abcdef",FRAME_ASSIGNMENT_TOKEN="fedcba9876543210";
const frameMock={on:false,conflictNext:false,posts:[],trackingGets:0,otherActive:false,previewTotal:3397,scopeToken:FRAME_TOKEN};
const framePreview=()=>({total:frameMock.previewTotal,new_unknown:frameMock.previewTotal,vertices:frameMock.previewTotal-1,points:1,
  lines:[{id:"l1",name:mapLineName,count:frameMock.previewTotal-1}],more_lines:0,other_unknown:134,assignment:null,scope_token:frameMock.scopeToken});
const frameSummary=(count,body)=>({reference_frame:body.reference_frame,reference_frame_epoch:body.reference_frame_epoch||"",count,
  assigned_at:2000100,assigned_at_utc:"2026-10-27T10:00:00Z",token:FRAME_ASSIGNMENT_TOKEN});
// Sets the p1 entry of /api/tracking: undefined removes the key, null states "none" explicitly.
function frameSetProject(assignment){
  const project=tracking.projects[0];
  if(assignment)Object.assign(project,{frames:{[assignment.reference_frame]:assignment.count,unknown:134},mixed_frames:true,unknown_frames:false,frame_assignment:assignment});
  else{Object.assign(project,{frames:{unknown:3531},mixed_frames:false,unknown_frames:true});if(assignment===null)project.frame_assignment=null;else delete project.frame_assignment;}
}
// What the live feed of the active project p1 says about the assignment (without touching /api/tracking).
function frameSetLive(assignment){if(live.active_project&&live.active_project.id==="p1")live.active_project.frame_assignment=assignment;}
function frameReset(){
  frameSetProject(undefined);
  Object.assign(frameMock,{conflictNext:false,posts:[],trackingGets:0,otherActive:false,previewTotal:3397,scopeToken:FRAME_TOKEN});
  tracking.active_project_id="p1";
  live.active_project={id:"p1",name:"Project",line_count:1,point_count:1,frame_assignment:null};
}
function frameSetOtherActive(){
  frameMock.otherActive=true;
  tracking.active_project_id="p2";
  live.active_project={id:"p2",name:"Archive",line_count:0,point_count:0,frame_assignment:null};
}
async function frameTrackingPost(route){
  const body=route.request().postDataJSON(),json=(value,status=200)=>route.fulfill({status,contentType:"application/json",body:JSON.stringify(value)});
  frameMock.posts.push(body);
  if(body.action==="frame_assignment_preview")return json(framePreview());
  if(body.action==="assign_reference_frame"){
    if(frameMock.conflictNext||body.scope_token!==frameMock.scopeToken){
      frameMock.conflictNext=false;frameMock.previewTotal=3398;frameMock.scopeToken="abcdef0123456789";
      return json({error:"frame_scope_changed",message:"The points of this project changed during the check; review the assignment again."},409);
    }
    const summary=frameSummary(body.expected_count,body);
    frameSetProject(summary);frameSetLive(summary);
    return json({project_id:body.project_id,frame_assignment:summary,tracking_live:frameServedLive()});
  }
  assert.strictEqual(body.action,"revoke_reference_frame","frame mock: unexpected action");
  frameSetProject(undefined);frameSetLive(null);
  return json({project_id:body.project_id,frame_assignment:null,tracking_live:frameServedLive()});
}
// Like the live feed: the tally of the active project comes from the project list, the assignment from live.
function frameServedLive(){
  const listed=tracking.projects.find(item=>item.id===live.active_project.id)||{};
  return{...servedLive(),active_project:{frames:listed.frames,mixed_frames:listed.mixed_frames,unknown_frames:listed.unknown_frames,...live.active_project}};
}
// Field flow fixtures (--field-flow-only, #24-UI Task 11): one device model every browser context shares. The measurement
// method, the antenna height and the reference frame live in flow.defaults; the PUTs of the gate and of the field setup write
// into it and /api/ui/live serves it. Inert unless flow.on.
const flow={on:false,defaults:{samples:5,antenna_height_m:0,reference_frame:"",reference_frame_epoch:""},service:undefined,putDelay:0};
function flowPublish(){
  live.measurement_defaults={...flow.defaults};gateSamples=flow.defaults.samples;
  Object.assign(fieldSetup,{antenna_height_m:flow.defaults.antenna_height_m,reference_frame:flow.defaults.reference_frame,reference_frame_epoch:flow.defaults.reference_frame_epoch});
  if(flow.service!==undefined)live.frame_suggestion=flowSuggestion();
}
// Frame suggestions (--frame-suggestion-only, #38b): flow.service is the 1300 announcement of the correction service
// ({frame, epoch, anchor}); null = no 1300, undefined = the suggestion keys are not served at all. flowSuggestion applies
// the rule of RtcmCounter.frame_suggestion (E8) to the device values: new without a frame, matches on an exactly equal frame
// and an empty or equal epoch, differs otherwise (also when only the epoch differs); no case folding.
// flow.putDelay holds a field setup PUT before the device applies it.
function flowSuggestion(){
  const service=flow.service;
  if(!service)return null;
  const frame=String(flow.defaults.reference_frame||"").trim(),epoch=String(flow.defaults.reference_frame_epoch||"").trim();
  const state=!frame?"new":frame===service.frame&&(!service.epoch||service.epoch===epoch)?"matches":"differs";
  return{reference_frame:service.frame,reference_frame_epoch:service.epoch||"",anchor:service.anchor||null,state};
}
function flowWrite(path,method,route){
  if(method!=="PUT")return;
  const put=route.request().postDataJSON();
  if(path==="/api/measurement-gate"&&put&&"samples" in put)flow.defaults.samples=put.samples;
  if(path==="/api/field-setup"&&put){
    if("antenna_height_m" in put)flow.defaults.antenna_height_m=Number(put.antenna_height_m);
    for(const key of ["reference_frame","reference_frame_epoch"])if(key in put)flow.defaults[key]=String(put[key]);
  }
  flowPublish();
}
// Activating a project sets it as the active one like the device; the answer carries the live_status() keys.
function flowSelectProject(route){
  const project=tracking.projects.find(item=>item.id===route.request().postDataJSON().project_id);
  tracking.active_project_id=project.id;
  live.active_project={id:project.id,name:project.name,line_count:project.line_count,point_count:project.point_count};
  live.revision=(live.revision||1)+1;
  const full=servedLive();
  return route.fulfill({contentType:"application/json",body:JSON.stringify({tracking_live:Object.fromEntries(LIVE_STATUS_KEYS.filter(key=>key in full).map(key=>[key,full[key]]))})});
}
// Mobile layout fixtures (--mobile-layout-only): widths, a long name without a break opportunity, three archives.
const MOBILE_WIDTHS=[320,360,390,480];
const LANDSCAPE=[{width:640,height:360},{width:844,height:390}];
const LONG_UNBROKEN="Unbroken_name_without_any_spaces_0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJ";
const mobileArchives=[
  {id:"ar1",project_id:"x1",name:"Old",point_count:3,line_count:1,schema_version:5,size_bytes:1200,archived_at:100},
  {id:"ar2",project_id:"x2",name:"North ring utility corridor section south-west extension stage 7b",point_count:12345,line_count:678,schema_version:4,size_bytes:99999,archived_at:200},
  {id:"ar3",project_id:"x3",name:LONG_UNBROKEN,point_count:0,line_count:0,schema_version:5,size_bytes:10,archived_at:300}];
// Large project p3: 40 lines and 120 object points (map preview simplified), first line and point with the long name.
const largeMap={revision:1,truncated:true,profiles:{"profile-1a2b3c4d":profileLimits},
  lines:Array.from({length:40},(_,i)=>{const x=800000000+(i%8)*20000,y=5000000000+Math.floor(i/8)*20000;
    return{id:"lb"+i,name:i===0?LONG_UNBROKEN:"Line "+i,state:"finished",vertex_count:3,length_m:4.2,
      coordinates:[[x,y,100000],[x+10000,y+5000,100100],[x+20000,y+10000,100200]]};}),
  points:Array.from({length:120},(_,i)=>({id:"pb"+i,type:["valve","hydrant","manhole","marker"][i%4],name:i===0?LONG_UNBROKEN:"Point "+i,
    note:"Large project point",coordinate:[800000000+(i%12)*15000,5000000000+Math.floor(i/12)*15000,100000],chainage_m:i+.5,measurement:deltaPoint.measurement}))};
const largeProject={id:"p3",name:LONG_UNBROKEN,line_count:40,point_count:120,created_at:700,updated_at:710,
  frames:{"ETRS89 (synthetic)":100,"WGS84/ITRF":20},mixed_frames:true,unknown_frames:false};
// /api/log fixture: the log keeps its own history so the browser tests can compare the ring with the DOM.
let logSerial=0,logInitial=1,logBurst=0,logDelay=0,logGeneration=0,failLog=false,logInFlight=0,logMaxInFlight=0,logRequests=0,logSeenGeneration=0,logAll=[],logLevels=false,vertexStep=null;
const logEntry=n=>({elapsed_sec:n,level:"INFO",source:"SYS",message:"entry "+n});
async function logFixture(route,hasCursor){
  logInFlight+=1;logRequests+=1;logMaxInFlight=Math.max(logMaxInFlight,logInFlight);
  try{
    if(logDelay)await new Promise(resolve=>setTimeout(resolve,logDelay));
    if(failLog)return await route.abort("connectionfailed").catch(()=>{});
    if(logSeenGeneration!==logGeneration){logSeenGeneration=logGeneration;logAll=[];}
    let entries;
    if(!hasCursor&&logInitial===1){entries=[{elapsed_sec:1,level:"INFO",source:"SYS",message:"ready"}];if(logLevels)entries.push({elapsed_sec:2,level:"WARN",source:"WIFI",message:"weak signal"},{elapsed_sec:3,level:"ERROR",source:"NTRIP",message:"connection lost"});if(!logAll.length)logAll.push(...entries);}
    else if(!hasCursor){while(logAll.length<logInitial)logAll.push(logEntry(logAll.length));entries=logAll.slice(-logInitial);}
    else{entries=[];for(let i=0;i<logBurst;i++){const item=logEntry(logAll.length);logAll.push(item);entries.push(item);}}
    logSerial=logAll.length;
    // A page change may cancel the request before the late answer is written.
    await route.fulfill({contentType:"application/json",body:JSON.stringify({entries,cursor:"0:"+logSerial,generation:logGeneration})}).catch(()=>{});
  }finally{logInFlight-=1;}
}
// Verdict fixture: every answer that carries current_quality gets reasons that fit its accepted flag, so the
// checklist (which judges only the server reasons) and the flag never disagree by accident.
// fixtureReasons mirrors the live part of tracking.quality_check; setVerdict() names reasons explicitly,
// rawVerdict lets a test serve a deliberately contradicting accepted flag.
let verdictReasons=null,rawVerdict=false;
function fixtureReasons(fix,limits){
  const f=fix||{},reasons=[],qual=Number(f.qual||0),required=limits.fix_quality||"RTK_FIXED",
    allowed=({RTK_FIXED:[4],RTK_FLOAT:[4,5],DGPS:[2,4,5]})[required]||[4];
  if(f.lat==null||f.lon==null)reasons.push("no_position");
  if(!allowed.includes(qual))reasons.push(required==="RTK_FIXED"?"rtk_fixed_required":"required_fix_not_met");
  if(Number(f.sats||0)<limits.min_satellites)reasons.push("too_few_satellites");
  if(f.hdop==null||Number(f.hdop)>limits.max_hdop)reasons.push("hdop_too_high");
  if(f.alt==null)reasons.push("altitude_required");
  if(f.correction_age_sec!=null&&Number(f.correction_age_sec)>limits.max_correction_age_sec)reasons.push("corrections_too_old");
  const receiver=f.receiver_accuracy;
  if(!receiver)reasons.push("gst_required");
  else{
    if(Number(receiver.horizontal_sigma_m)>limits.max_gst_horizontal_sigma_m)reasons.push("gst_horizontal_error_too_high");
    if(Number(receiver.altitude_sigma_m)>limits.max_gst_vertical_sigma_m)reasons.push("gst_vertical_error_too_high");
  }
  return reasons;
}
const setVerdict=reasons=>{verdictReasons=reasons;};
const clearVerdict=()=>{verdictReasons=null;rawVerdict=false;};
function servedQuality(){
  if(live.current_quality==null)return live.current_quality;
  const quality=live.current_quality,reasons=verdictReasons||(quality.accepted?[]:fixtureReasons(live.current_fix,quality.limits));
  if(quality.accepted===false&&!verdictReasons&&!reasons.length)throw new Error("fixture verdict without a reason");
  return {...quality,reasons,accepted:rawVerdict?quality.accepted:reasons.length===0};
}
const servedLive=()=>({...live,current_quality:servedQuality(),gnss_accuracy:live.gnss_accuracy||{state:"receiving",attempts:0}});
async function fixture(route) {
  const url = new URL(route.request().url()),
    path = url.pathname,
    method = route.request().method();
  requestLog.push({path,method,search:url.search,body:method==="GET"?null:route.request().postData()});
  if(stakeoutMock.on&&path.startsWith("/api/stakeout"))return stakeoutFixture(route,url,path,method);
  if (path === "/api/events") {
    streamMock.requests.push(Date.now());
    if (!streamMock.frame) return route.fulfill({ status: 404, contentType: "application/json", body: JSON.stringify({ error: "not_found", message: "Not found" }) });
    await new Promise(resolve => setTimeout(resolve, streamMock.delay));
    return route.fulfill({ status: 200, contentType: "text/event-stream", headers: { "Cache-Control": "no-store" },
      body: "retry: 60000\n:ok\n\nevent: live\ndata: " + JSON.stringify(streamMock.frame) + "\n\n" }).catch(() => {});
  }
  if (failNext[path]) {
    const code = failNext[path];
    delete failNext[path];
    return route.fulfill({ status: code, contentType: "application/json", body: JSON.stringify({ error: "unavailable", message: "Temporarily unavailable" }) });
  }
  // signInPageDelay (--feedback-only): a slow sign-in page like the device's keeps the redirect pending for that long.
  if (path === "/") {
    if (signInPageDelay && url.searchParams.has("return")) await new Promise(resolve => setTimeout(resolve, signInPageDelay));
    return route.fulfill({ contentType: "text/html", body: html });
  }
  // A small page without app code: the known predecessor of a first visit (--navigation-only).
  if (path === "/__history-start")
    return route.fulfill({ contentType: "text/html", body: "<!doctype html><title>history start</title><p>history start</p>" });
  if (["/app.js", "/app.css", "/i18n.js", "/qr.js"].includes(path)) {
    // An asset can be held like an API answer (the QR module while the page changes).
    const heldAsset = requestHolds.find(entry => entry.test(path, method, url));
    if (heldAsset) { requestHolds.splice(requestHolds.indexOf(heldAsset), 1); heldAsset.arrive(null); await heldAsset.released; }
    const source = fs.readFileSync(root + path, "utf8");
    let served = source;
    if (path === "/app.js" && process.argv.includes("--feedback-only")) { served = source.replace(/\}\)\(\);\n$/, FEEDBACK_HOOK + "})();\n"); assert.notStrictEqual(served, source, "app.js must end with })(); for the feedback test access"); }
    return route.fulfill({
      contentType: path.endsWith(".js") ? "application/javascript" : "text/css",
      body: served,
    });
  }
  if(path==="/api/field-diagnostics"){
    if(method==="POST"){
      assert.strictEqual(route.request().headers()["x-csrf-token"],"csrf");
      if(failFieldPost)return route.fulfill({status:500,contentType:"application/json",body:JSON.stringify({message:typeof failFieldPost==="string"?failFieldPost:"storage busy"})});
      const value=route.request().postDataJSON();
      if(value.action==="start")fieldStatus.enabled=true;
      if(value.action==="stop")fieldStatus.enabled=false;
      if(value.action==="mark")fieldMarkers.push(value.label);
      live.field_diagnostics=fieldStatus;
    }
    return route.fulfill({contentType:"application/json",body:JSON.stringify(fieldStatus)});
  }
  if(path==="/api/tracking"&&method==="GET"&&projectDelay)await new Promise(resolve=>setTimeout(resolve,projectDelay));
  if(path==="/api/tracking"&&method==="GET"&&!url.searchParams.has("view")){
    if(onTrackingRead)onTrackingRead();
    if(trackingHold)await trackingHold.promise;
    if(trackingFail)return route.fulfill({status:trackingFail.status,contentType:"application/json",body:JSON.stringify(trackingFail.body)});
  }
  if(path==="/api/access"&&method==="GET"&&accessDelay)await new Promise(resolve=>setTimeout(resolve,accessDelay));
  if(path==="/api/tracking/map"){if(url.searchParams.has("since_revision"))mapDeltaRequests++;else mapRequests++;if(mapDelay)await new Promise(resolve=>setTimeout(resolve,mapDelay));}
  if(path==="/api/tracking/map"&&mapFail)return route.fulfill({status:503,contentType:"application/json",body:JSON.stringify({error:"storage_busy",message:"Storage maintenance in progress. Please wait."})});
  if (path === "/api/log") return logFixture(route, url.searchParams.has("cursor"));
  let body = {};
  if (path === "/api/tracking" && method === "POST") { trackingPosts++; trackingPostLog.push(route.request().postDataJSON()); }
  if (mutateProjects && path === "/api/tracking" && method === "POST") {
    const post = route.request().postDataJSON();
    if (post.action === "rename_project") tracking.projects.forEach(item => { if (item.id === post.project_id) item.name = post.name; });
    if (post.action === "delete_project") tracking.projects = tracking.projects.filter(item => item.id !== post.project_id);
  }
  if (frameMock.on && path === "/api/tracking" && method === "GET" && !url.searchParams.has("view")) frameMock.trackingGets++;
  if (frameMock.on && path === "/api/tracking" && method === "POST") return frameTrackingPost(route);
  if (flow.on && flow.putDelay && path === "/api/field-setup" && method === "PUT") await new Promise(resolve => setTimeout(resolve, flow.putDelay));
  if (mapModel.on && mapModel.restore && path === "/api/tracking" && method === "POST" && route.request().postDataJSON().action === "restore") {
    mapModel.maps = structuredClone(mapModel.restore); mapModel.restore = null; mapModel.revision = 5; mapModel.changes = [];
    const projects = Object.entries(mapModel.maps).map(([id, map]) => ({ id, feature_count: map.points.length }));
    return route.fulfill({ contentType: "application/json", body: JSON.stringify({ projects, legacy_values_skipped: 0 }) });
  }
  if (archiveMock.on && path === "/api/tracking" && method === "POST") {
    const post = route.request().postDataJSON();
    if (post.action === "archive_project") {
      const project = tracking.projects.find(item => item.id === post.project_id);
      archiveMock.projects["arc-" + project.id] = project;
      tracking.projects = tracking.projects.filter(item => item !== project);
      tracking.archives = [...tracking.archives, { id: "arc-" + project.id, project_id: project.id, name: project.name, point_count: project.point_count,
        line_count: project.line_count, schema_version: 5, size_bytes: 900, archived_at: 400 }];
      return route.fulfill({ contentType: "application/json", body: JSON.stringify({ tracking_live: servedLive() }) });
    }
    if (post.action === "reactivate_archive") {
      tracking.projects = [...tracking.projects, archiveMock.projects[post.archive_id]];
      tracking.archives = tracking.archives.filter(item => item.id !== post.archive_id);
      return route.fulfill({ contentType: "application/json", body: JSON.stringify({ tracking_live: servedLive() }) });
    }
  }
  if (flow.on) flowWrite(path, method, route);
  if (flow.on && path === "/api/tracking" && method === "POST" && route.request().postDataJSON().action === "select_project") return flowSelectProject(route);
  if (frameMock.on && frameMock.otherActive && path === "/api/ui/live") live.revision = (live.revision || 1) + 1;
  if (captureState && path === "/api/tracking" && method === "POST") return captureAnswer(route, route.request().postDataJSON());
  if (path === "/api/ui/live" && failLive) return route.abort("connectionfailed");
  if(path==="/api/ui/live"&&holdLive){await holdLive;return route.abort("timedout").catch(()=>{});}
  let livePatch=null;
  const checkRequest=path==="/api/ui/live"&&route.request().headers()["x-rtk-check"]==="1";
  if(path==="/api/ui/live"&&holdPolls&&!checkRequest){await holdPolls;return route.abort("timedout").catch(()=>{});}
  if(path==="/api/ui/live"&&(checkRequest&&checkAnswers.length||liveAnswers.length)){
    const entry=checkRequest&&checkAnswers.length?checkAnswers.shift():liveAnswers.shift();
    if(entry.next)liveAnswers=entry.next.slice();
    if(entry.reboot&&captureState)captureState.stack.forEach(item=>{item.request_id=null;});
    if(entry.hang)return new Promise(()=>{});
    if(entry.delay)await new Promise(resolve=>setTimeout(resolve,entry.delay));
    if(entry.status)return route.fulfill({status:entry.status,contentType:"application/json",body:"{}"});
    livePatch=Object.fromEntries(Object.entries(entry).filter(([key])=>!["reboot","delay","next"].includes(key)));
  }
  if (path === "/api/ui/live") {
    // Like the server, live carries the frame tally of the active project (the same numbers /api/tracking lists).
    const listed = live.active_project && tracking.projects.find(item => item.id === live.active_project.id) || {};
    body = { ...servedLive(), active_project: live.active_project && { frames: listed.frames, mixed_frames: listed.mixed_frames, unknown_frames: listed.unknown_frames, ...live.active_project } };
    if (mapModel.on) body.revision = mapModel.revision;
    if (livePatch) Object.assign(body, livePatch);
    if (liveVary) {
      const n = liveAnswered;
      body.current_fix = { ...body.current_fix, sats: 12 + n % 3, correction_age_sec: 1 + (n % 4) / 10,
        receiver_accuracy: { horizontal_sigma_m: 0.01 + (n % 5) / 1000, altitude_sigma_m: 0.02 } };
      body.quality_streak = { gate_streak_sec: 20 + n };
    }
    lastLiveServedAt=Date.now();
  }
  else if (path === "/status" && failNextStatus) {
    failNextStatus = false;
    return route.fulfill({ status: 503, contentType: "application/json", body: '{"message":"temporary"}' });
  }
  else if (path === "/status") body = status;
  else if (path === "/api/tracking/map" && mapModel.on) {
    const id = url.searchParams.get("project_id"), since = url.searchParams.get("since_revision");
    body = since === null || mapFullOnDelta ? mapModelFull(id) : { project_id: id, revision: mapModel.revision, delta: true, profiles: {},
      changes: mapModel.changes.filter(change => change.revision > Number(since) && change.project_id === id).map(({ revision, project_id, ...change }) => change) };
  }
  else if (path === "/api/tracking/map" && url.searchParams.has("since_revision"))
    body = { delta: true, revision: mapRevision, changes: mapRevision > 1 ? [{ op: "asset", point: deltaPoint }] : [] };
  else if (path === "/api/tracking/map" && url.searchParams.get("project_id") === "p3") body = largeMap;
  else if (path === "/api/tracking/map")
    body = url.searchParams.get("project_id") === "p2" ? { lines: [], points: [], truncated: false, revision: 1 } : {
      revision: 1,
      lines: [
        {
          id: "l1",
          name: mapLineName,
          coordinates: [
            [800000000, 5000000000, 100000],
            [800100000, 5000100000, 101000],
          ],
        },
      ],
      profiles: { "profile-1a2b3c4d": profileLimits },
      points: [{ id: "a1", type: "valve", name: "Valve 1", note: "North side", coordinate: [800000000, 5000000000, 100000], chainage_m: 2.5,
        receiver_accuracy: { horizontal_sigma_m: 0.012, altitude_sigma_m: .024 },
        measurement: { precision: "e8", profile_id: "profile-1a2b3c4d", fix_quality: 4, fix_status: "RTK_FIXED", satellites: 18,
          hdop: .7, correction_age_sec: .8, station_id: "0054",
          geoid_sep_mm: 47100, alt_ellipsoid_mm: 147100, antenna_height_mm: 1800,
          recorded_at: 841057764, recorded_at_utc: "2026-08-26T11:09:24Z", clock_unsynced: false,
          receiver_accuracy: { source: "NMEA_GST", horizontal_sigma_mean_m: .012,
            vertical_sigma_mean_m: .024, samples: 5, expected_samples: 5 },
          accuracy_observation: { samples: 5, duration_sec: 4.2 },
          quality: { accepted: true, reasons: [] } } },
      { id: "a2", type: "valve", name: "Valve 2", note: "Converted", coordinate: [800050000, 5000050000, 100500], chainage_m: 3.5,
        measurement: { precision: "legacy_float32", profile_id: "profile-deadbeef", fix_quality: 4, fix_status: "RTK_FIXED", satellites: 16,
          hdop: .8, quality: { accepted: true, reasons: [] } } }, ...(mapRevision > 1 ? [deltaPoint] : []), ...extraMapPoints],
    };
  else if (path === "/api/tracking/line" && (url.searchParams.get("line_id") || "").startsWith("lb")) {
    const line = largeMap.lines.find(item => item.id === url.searchParams.get("line_id")) || largeMap.lines[0];
    body = { id: line.id, name: line.name, vertex_count: 3, legacy_vertex_count: 0, quality_accepted_count: 3, quality_total_count: 3, warnings: [],
      vertices: [1, 2, 3].map(number => ({ number, precision: "e8", fix_status: "RTK_FIXED", satellites: 18, hdop: 0.7, correction_age_sec: 1, segment_length_m: 1,
        recorded_at: 841057764, recorded_at_utc: "2026-08-26T11:09:24Z", clock_unsynced: false, receiver_accuracy: { horizontal_sigma_m: 0.01, altitude_sigma_m: 0.02 } })),
      has_more_vertices: false };
  } else if (path === "/api/tracking/line") {
    const offset = Number(url.searchParams.get("offset") || 0);
    body = {
      id: "l1",
      name: mapLineName,
      vertex_count: 30,
      legacy_vertex_count: 1,
      quality_accepted_count: 30,
      quality_total_count: 30,
      warnings: lineWarnings,
      vertices: [
        {
          number: offset + 1,
          precision: offset === 0 ? "e8" : "legacy_float32",
          fix_status: "RTK_FIXED",
          satellites: 18,
          hdop: 0.7,
          correction_age_sec: 1,
          segment_length_m: 1,
          recorded_at: offset === 0 ? 841057764 : 12,
          recorded_at_utc: offset === 0 ? "2026-08-26T11:09:24Z" : "2000-01-01T00:00:12Z",
          clock_unsynced: offset !== 0,
          ...(vertexStep != null && offset === 0 ? { vertical_step_m: vertexStep } : {}),
          receiver_accuracy: {
            horizontal_sigma_m: 0.01,
            altitude_sigma_m: 0.02,
          },
        },
      ],
      has_more_vertices: offset === 0,
    };
  } else if (path === "/api/tracking" && method === "POST" && route.request().postDataJSON().action === "archive_project") {
    const id=route.request().postDataJSON().project_id;
    assert.notStrictEqual(id,tracking.active_line&&tracking.active_project_id);
    const project=tracking.projects.find(item=>item.id===id);
    tracking.projects=tracking.projects.filter(item=>item.id!==id);
    live.capacity={...live.capacity,used:live.capacity.used-project.feature_count,
      remaining:live.capacity.remaining+project.feature_count,level:"ok"};
    tracking.capacity=live.capacity;
    body={tracking_live:servedLive()};
  } else if (path === "/api/tracking" && method === "POST" && route.request().postDataJSON().action === "select_project") {
    // Activating sets the active project like the server; the answer is the tracking state.
    tracking.active_project_id = route.request().postDataJSON().project_id;
    body = { ...tracking };
  } else if (path === "/api/tracking" && method === "POST" && route.request().postDataJSON().action === "add_vertex") {
    await new Promise(resolve => { finishMeasurement = resolve; });
    body = { tracking_live: servedLive() };
  } else if (partialMutations && path === "/api/tracking" && method === "POST") {
    live.revision = (live.revision || 1) + 1;
    const full = servedLive();
    body = { ok: true, tracking_live: Object.fromEntries(LIVE_STATUS_KEYS.filter(key => key in full).map(key => [key, full[key]])) };
  } else if (path === "/api/tracking" && url.searchParams.get("view") === "progress") {
    if (failProgress) return route.abort("connectionfailed");
    body = { current_fix: live.current_fix, current_quality: servedQuality(),
      measurement_progress: live.measurement_progress, ...(live.network ? { network: live.network } : {}) };
  } else if (path === "/api/tracking" && url.searchParams.get("view") === "name_check") {
    const name = url.searchParams.get("name");
    body = { available: name !== "Valve 1", existing: name === "Valve 1" ? "Valve 1" : null };
  } else if (path === "/api/tracking") body = { ...tracking, current_quality: { ...servedQuality(), limits: tracking.current_quality.limits } };
  else if (path === "/api/session") body = { csrf_token: "csrf" };
  else if (path === "/api/config" && method === "PUT") {
    const put = route.request().postDataJSON();
    // Only a Wi-Fi change still needs the staged apply; caster edits are live (#23b).
    body = { pending: false, restart_required: put.wifi_ssid !== "Field", applied: true, staged: false };
  } else if (path === "/api/config")
    body = {
      wifi_ssid: "Field",
      wifi_pass_set: true,
      wifi_networks: [{ ssid: "Fallback", password_set: true }],
      ntrip_host: "caster",
      ntrip_port: 2101,
      ntrip_mount: "MOUNT",
      ntrip_user: "user",
      ntrip_pass_set: true,
      staging: { revision: "c0ffee01-0", keys: [], restart_required: false },
    };
  else if (path === "/api/config-result") body = { schema_version: 2, status: "none", details: {}, uptime_sec: 0, boot_id: null, apply_id: null };
  else if (path === "/api/measurement-gate") {
    // Like the device: a PUT with gate fields changes the limits, its optional samples the device method (live follows).
    const put = method === "PUT" ? route.request().postDataJSON() : null;
    if (put && Object.keys(put).some(key => key !== "samples")) setGateDeviations([{ key: "max_hdop", value: 2, default: 1.5 }]);
    if (put && "samples" in put) { gateSamples = put.samples; if (live.measurement_defaults) live.measurement_defaults = { ...live.measurement_defaults, samples: put.samples }; }
    if (method === "DELETE") setGateDeviations([]);
    // The sample window follows a PUT and returns to its factory value with DELETE.
    if (put && "quality_window_samples" in put) gateWindow = Number(put.quality_window_samples);
    if (method === "DELETE") gateWindow = 5;
    body = {
    deviations: status.gate_deviations || [],
    values: { required_fix: gateRequiredFix, quality_window_samples: gateWindow, min_satellites: 10, max_hdop: 1.5,
      max_correction_age_sec: 5, max_horizontal_spread_m: .05,
      max_gst_horizontal_sigma_m: .05, max_gst_vertical_sigma_m: .1 },
    defaults: { required_fix: "RTK_FIXED", quality_window_samples: 5, min_satellites: 10, max_hdop: 1.5,
      max_correction_age_sec: 5, max_horizontal_spread_m: .05,
      max_gst_horizontal_sigma_m: .05, max_gst_vertical_sigma_m: .1 },
    samples: gateSamples, samples_default: 5,
    };
  }
  else if (path === "/api/field-setup") {
    if (method === "PUT") {
      assert.strictEqual(route.request().headers()["x-csrf-token"], "csrf");
      const put = route.request().postDataJSON();
      fieldSetupPuts.push(put);
      Object.assign(fieldSetup, put);
      if ("antenna_height_m" in put) fieldSetup.antenna_height_m = Number(put.antenna_height_m);
    }
    body = { values: fieldSetup, defaults: fieldSetupDefaults };
    if (flow.on && flow.service !== undefined) body.suggestion = flowSuggestion();
  }
  else if (path === "/api/tcp-auth") {
    if (method === "PUT") tcpAuthRequired = route.request().postDataJSON().required !== false;
    body = { required: tcpAuthRequired, default: true };
  }
  else if (path === "/api/stream-token/rotate") { streamRotations += 1; streamToken = "TOKEN-" + streamRotations; body = { ok: true }; }
  else if (path === "/api/config/apply") { applyCalls += 1; body = { accepted: true }; }
  else if (path === "/api/wifi-scan") {
    wifiScanCalls += 1;
    body = wifiScanCalls === 1 ? { status: "pending" } : {
      status: "ready", networks: [{ ssid: "Visible", rssi: -48 }],
    };
  }
  else if (path === "/api/access")
    body = {
      hostname: "rtk.local",
      nmea_tcp: {
        canonical_port: 10110,
        legacy_ports: [2947],
        authentication_required: tcpAuthRequired,
        authentication: "AUTH token",
      },
      ble_nus: {
        name: "RTK",
        service_uuid: "service",
        rx_uuid: "rx",
        tx_uuid: "tx",
        mitm_protection: "passkey",
      },
    };
  else if (path === "/api/label")
    body = {
      schema_version: 1,
      display_name: "RTK-TEST",
      device_id: "dev-1",
      device_code: "CODE",
      stream_token: streamToken,
      ble_pin: "123456",
      hostname: "rtk-test.local",
      wifi_qr: labelWifiQr,
      portal_qr: "http://rtk-test.local/setup#code=CODE",
    };
  else if (path === "/rtcm")
    body = {
      last_msg_age_sec: 1,
      arrival_age_sec: 1,
      epoch_age_sec: 1.1,
      epoch_source: "GPS",
      stale: false,
      mount: "MOUNT",
      msm: ["MSM5"],
      messages: { 1075: 2 },
      ...rtcmOverride,
    };
  else if (path === "/api/diagnostics") body = { healthy: true };
  else if (path === "/gnss")
    return route.fulfill({ contentType: "text/plain", body: "receiver ready" });
  else if (path === "/errors")
    return route.fulfill({
      contentType: "text/plain",
      body: "[1] INFO SYS ready",
    });
  else if (path.includes("/export") || path.includes("/backup"))
    return route.fulfill({ contentType: "application/json", body: "{}" });
  const payload = JSON.stringify(body), held = requestHolds.find(entry => entry.test(path, method, url));
  if (held) {
    requestHolds.splice(requestHolds.indexOf(held), 1);
    held.arrive(JSON.parse(payload));
    await held.released;
    if (path === "/api/ui/live") liveAnswered += 1;
    // The page may have cancelled the request meanwhile.
    if (held.status) return route.fulfill({ status: held.status, contentType: "application/json", body: JSON.stringify({ error: "busy", message: "Device busy" }) }).catch(() => {});
    return route.fulfill({ contentType: "application/json", body: payload }).catch(() => {});
  }
  if (path === "/api/ui/live") liveAnswered += 1;
  return route.fulfill({
    contentType: "application/json",
    body: payload,
  });
}
// Connections (#24-UI Task 4, label mode): one page with the configuration form and the device access group. Every block
// fails on its own with a local retry; access actions next to an unsaved form never reload it; a late answer never touches
// the next view but is still reported in the feedback dock (late-action ruling). Checks read the page and the requests.
async function testConnections(page){
  const en=require(root+"/i18n.js").dictionaries.en;
  const logged=(since,path,method="GET")=>requestLog.slice(since).filter(entry=>entry.path===path&&entry.method===method);
  const waitFor=async(check,message,timeout=8000)=>{const end=Date.now()+timeout;while(!check()){if(Date.now()>end)assert.fail(message);await page.waitForTimeout(50);}};
  const settled=()=>page.waitForFunction(()=>document.querySelector("#app-main").getAttribute("aria-busy")==="false");
  const sheetClosed=()=>page.waitForFunction(()=>!document.querySelector("dialog[open]")&&!(history.state&&history.state.rtkSheet));
  const hash=()=>page.evaluate(()=>location.hash);
  const groupTop=name=>page.evaluate(value=>document.querySelector(`.connection-group[data-group="${value}"]`).getBoundingClientRect().top,name);
  const scrollPadding=()=>page.evaluate(()=>parseFloat(getComputedStyle(document.documentElement).scrollPaddingTop)||0);
  const atTop=async name=>{const top=await groupTop(name),padding=await scrollPadding();assert(Math.abs(top-padding)<=2,`${name} group starts at the top (${top} vs ${padding})`);};
  const ssid=page.locator('input[name="wifi_ssid"]'),form=page.locator("form.configuration-layout");
  const row=label=>page.locator(".secret-panel .data-row").filter({has:page.locator("span",{hasText:new RegExp("^"+label+"$")})}).locator("b");
  const clean=()=>page.evaluate(()=>{const node=document.querySelector("form.configuration-layout");if(node)node.dataset.dirty="false";});
  const clearDock=()=>page.evaluate(()=>document.querySelectorAll("#feedback-dock .feedback-item").forEach(node=>node.remove()));
  const reloadAt=async(route,selector)=>{await clean();await page.evaluate(value=>{location.hash=value;},route);await page.reload();await page.waitForSelector(selector);await settled();};

  console.log("connections: access group, QR codes and one /api/access per mount");
  let since=requestLog.length;
  await go(page,"projects",".project-list");
  since=requestLog.length;
  await go(page,"device/connections/access","#app-main > *");
  assert.strictEqual(await page.locator('.connection-group[data-group="access"] .label-grid').count(),1,"QR codes in the access group");
  assert.strictEqual(logged(since,"/api/access").length,1,"/api/access once per mount");
  assert.strictEqual(logged(since,"/api/label").length,1,"/api/label once per mount");
  assert.strictEqual(logged(since,"/api/config").length,1,"the form loaded once");
  await atTop("access");
  assert.strictEqual(await page.locator(".label-grid .qr-graphic svg").count(),2,"portal and hotspot QR");
  const portal=page.locator(".label-card").filter({has:page.locator("h2",{hasText:"Direct web portal"})});
  assert.strictEqual(await portal.locator("code").innerText(),"CODE","portal QR text is the device code");
  assert.strictEqual(await page.locator('.connection-group[data-group="internet"] form.configuration-layout').count(),1);
  assert.strictEqual(await page.locator(".connections-layout .connection-group").count(),2);

  console.log("connections: the old configuration link lands on the form at the top");
  const formSince=requestLog.length;
  await go(page,"device/configuration",'.connection-group[data-group="internet"] form.configuration-layout');
  assert.strictEqual(await hash(),"#device/connections/internet");
  assert(await page.evaluate(()=>scrollY)>0,"scrolled to the group");
  await atTop("internet");
  assert.strictEqual(await page.locator('.connection-group[data-group="internet"] a[href="/#device/maintenance"]').innerText(),en["ui.connections_backup_link"]);

  console.log("connections: group jumps keep the hash and the unsaved form");
  await ssid.fill("Edited");
  assert.strictEqual(await form.getAttribute("data-dirty"),"true");
  await page.locator(".connection-jump button",{hasText:en["ui.group_access"]}).click();
  await atTop("access");
  assert.strictEqual(await hash(),"#device/connections/internet","jump keeps the hash");
  await page.locator(".connection-jump button",{hasText:en["ui.group_internet"]}).click();
  await atTop("internet");
  assert.strictEqual(await hash(),"#device/connections/internet","jump keeps the hash");
  assert.strictEqual(await page.locator("dialog[open]").count(),0,"no dirty dialog for a group jump");
  await page.locator('#mobile-nav a[data-route="projects"]').click();
  await page.waitForSelector("dialog[open]");
  assert.strictEqual(await hash(),"#device/connections/internet","the dirty dialog asks first (#23c)");
  await page.locator("dialog[open]").getByRole("button",{name:"Cancel"}).click();
  await sheetClosed();
  assert.strictEqual(await ssid.inputValue(),"Edited");

  console.log("connections: access actions beside an unsaved form");
  since=requestLog.length;
  await page.locator(".tcp-auth-card .switch").click();
  await page.waitForSelector("dialog[open]");
  await page.locator("dialog[open]").getByRole("button",{name:"Disable authentication"}).click();
  await page.waitForFunction(()=>{const node=document.querySelector(".tcp-auth-card .switch");return node&&node.getAttribute("aria-pressed")==="false";});
  await sheetClosed();
  assert.strictEqual(logged(since,"/api/tcp-auth","PUT").length,1,"one PUT /api/tcp-auth");
  assert.strictEqual(await page.locator(".tcp-auth-card .notice.critical").count(),1,"the group shows the new state");
  assert.strictEqual(await ssid.inputValue(),"Edited");
  assert.strictEqual(await form.getAttribute("data-dirty"),"true");
  assert.strictEqual(await page.locator("dialog[open]").count(),0,"no dirty dialog");
  assert.strictEqual(logged(formSince,"/api/config").length,1,"the form is not reloaded");
  since=requestLog.length;
  await page.getByRole("button",{name:"Rotate stream key"}).click();
  await page.waitForSelector("dialog[open]");
  await page.locator("dialog[open]").getByRole("button",{name:"Rotate key"}).click();
  await waitFor(()=>logged(since,"/api/label").length===1,"the access group reloads its label");
  await settled();
  await page.waitForSelector(".secret-panel");
  await sheetClosed();
  assert.strictEqual(logged(since,"/api/stream-token/rotate","POST").length,1,"one POST /api/stream-token/rotate");
  assert.strictEqual(await ssid.inputValue(),"Edited");
  assert.strictEqual(await form.getAttribute("data-dirty"),"true");
  assert.strictEqual(logged(formSince,"/api/config").length,1,"the form is not reloaded");
  assert.strictEqual(await page.locator("dialog[open]").count(),0,"no dirty dialog");
  tcpAuthRequired=true;

  console.log("connections: QR module fails, the secrets stand");
  await page.addInitScript(()=>{window.__clipboard=[];Object.defineProperty(navigator,"clipboard",{configurable:true,value:{writeText:async value=>{window.__clipboard.push(value);}}});});
  failNext["/qr.js"]=404;
  since=requestLog.length;
  await reloadAt("device/connections/access",'[data-notice="qr-unavailable"]');
  assert.strictEqual(logged(since,"/qr.js").length,1);
  assert(await page.locator(".access-grid").isVisible());
  await ssid.fill("QR");
  assert.strictEqual(await form.getAttribute("data-dirty"),"true","the form stays usable");
  assert.strictEqual(await row("Device code").innerText(),"CODE");
  assert.strictEqual(await row("BLE pairing PIN").innerText(),"123456");
  await page.getByRole("button",{name:"Show and copy stream key"}).click();
  await page.waitForFunction(token=>[...document.querySelectorAll(".secret-panel .data-row b")].some(node=>node.textContent===token),streamToken);
  assert.strictEqual(await row("Stream key").innerText(),streamToken);
  assert.deepStrictEqual(await page.evaluate(()=>window.__clipboard),[streamToken],"the clipboard got the stream key");
  await page.locator('[data-notice="qr-unavailable"]').getByRole("button",{name:"Try again"}).click();
  await page.waitForSelector(".label-grid");
  assert.strictEqual(await page.locator(".label-grid .qr-graphic svg").count(),2);
  assert.strictEqual(await portal.locator("code").innerText(),"CODE");
  assert.strictEqual(logged(since,"/qr.js").length,2,"the retry loads the module again");
  assert.strictEqual(logged(since,"/api/label").length,1,"the retry does not reload the label");
  assert.strictEqual(await ssid.inputValue(),"QR");

  console.log("connections: no label, the secrets have their own retry");
  failNext["/api/label"]=500;
  since=requestLog.length;
  await reloadAt("device/connections/access",'.secret-slot [data-notice="label-unavailable"]');
  assert(await page.locator(".access-grid").isVisible());
  assert.strictEqual(await page.locator(".secret-panel").count(),0);
  assert.strictEqual(logged(since,"/qr.js").length,0,"no QR module without a label");
  assert.strictEqual(await page.locator("#feedback-dock .feedback-item.critical").count(),0,"no error message of the page");
  await page.locator('[data-notice="label-unavailable"]').getByRole("button",{name:"Try again"}).click();
  await page.waitForSelector(".secret-panel");
  assert.strictEqual(await row("Device code").innerText(),"CODE");
  await page.waitForSelector(".label-grid");
  assert.strictEqual(await page.locator(".label-grid .qr-graphic svg").count(),2);

  console.log("connections: a temporary 503 of /api/access stays in its group");
  failNext["/api/access"]=503;
  since=requestLog.length;
  await go(page,"device/connections/internet",'.connection-group[data-group="access"] [data-notice="access-unavailable"]');
  assert((await page.locator('[data-notice="access-unavailable"]').innerText()).includes(en["ui.access_unavailable"]));
  assert.strictEqual(await page.locator('.connection-group[data-group="internet"] form.configuration-layout').count(),1);
  assert.strictEqual(logged(since,"/api/config").length,1,"no router retry of the whole page");
  await page.locator('[data-notice="access-unavailable"]').getByRole("button",{name:"Try again"}).click();
  await page.waitForSelector(".access-grid");
  assert.strictEqual(logged(since,"/api/access").length,2);
  assert.strictEqual(logged(since,"/api/config").length,1);

  for(const failure of [null,500]){
    console.log(`connections: a late TCP answer (${failure?"failure":"success"}) after navigation`);
    tcpAuthRequired=true;
    await clean();
    await go(page,"device/connections/access",".tcp-auth-card .switch");
    // The mock configuration has no ntrip_enabled, so the fresh form already differs from its baseline: mark it clean.
    await clean();
    const late=holdNext((path,method)=>path==="/api/tcp-auth"&&method==="PUT",failure||undefined);
    await page.locator(".tcp-auth-card .switch").click();
    await page.waitForSelector("dialog[open]");
    await page.locator("dialog[open]").getByRole("button",{name:"Disable authentication"}).click();
    await late.arrived;
    if(failure)tcpAuthRequired=true;
    await sheetClosed();
    await page.locator('#mobile-nav a[data-route="measure"]').click();
    await page.waitForSelector(".status-setup a.setup-edit");
    await page.locator(".status-setup a.setup-edit").click();
    await page.waitForSelector(".field-setup-layout");
    await settled();
    const antenna=page.locator("#field-setup-antenna-height");
    await antenna.fill("1.5");
    await clearDock();
    const released=requestLog.length;
    late.release();
    await page.waitForSelector(`#feedback-dock .feedback-item.${failure?"critical":"ok"}`);
    await page.waitForTimeout(500);
    assert.strictEqual(await antenna.inputValue(),"1.5");
    for(const path of ["/api/field-setup","/api/measurement-gate","/api/access","/api/label"])
      assert.strictEqual(logged(released,path).length,0,`no ${path} after the late answer`);
    assert.strictEqual(await page.locator(".connections-layout").count(),0);
    if(!failure)assert((await page.locator("#feedback-dock .feedback-item.ok").innerText()).includes("TCP authentication disabled"),"the late result is reported");
    assert.strictEqual(await page.locator("dialog[open]").count(),0);
  }
  tcpAuthRequired=true;

  console.log("connections: navigation while the QR module loads");
  await reloadAt("projects",".project-list");
  const qrHold=holdNext(path=>path==="/qr.js");
  since=requestLog.length;
  await page.evaluate(()=>{location.hash="device/connections/access";});
  await qrHold.arrived;
  assert.strictEqual(logged(since,"/api/label").length,1,"the API answers are already there");
  await page.waitForSelector(".secret-panel");
  await clean();
  await page.locator('#mobile-nav a[data-route="projects"]').click();
  await page.waitForSelector(".project-list");
  qrHold.release();
  await page.waitForTimeout(500);
  assert(await page.locator(".project-list").isVisible(),"the project library stays");
  assert.strictEqual(await page.locator(".connections-layout").count(),0);
  assert.strictEqual(await page.locator("#feedback-dock .feedback-item.critical").count(),0,"no error message");

  console.log("connections: no overflow at 320 px");
  await page.setViewportSize({width:320,height:800});
  await go(page,"device/connections/internet",".label-grid");
  assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),"connections overflow at 320 px");
  await page.setViewportSize({width:360,height:900});
}
// Field setup (#24-UI Task 2, app mode, at the end of the run): Measure → Field setup with the device-wide method.
// Every check reads the page (aria-pressed, sheet text) and the requests; the encapsulated store is never read.
async function testFieldSetup(page){
  const en=require(root+"/i18n.js").dictionaries.en;
  const label={1:en["ui.sample_single"],5:en["ui.sample_avg"].replace("{count}","5"),10:en["ui.sample_avg"].replace("{count}","10"),quality:"Until quality is OK"};
  const choice=value=>page.locator(".measurement-choice").getByRole("button",{name:label[value],exact:true});
  const pressed=()=>page.locator('.measurement-choice button[aria-pressed="true"]').allInnerTexts();
  const waitPressed=(value,timeout=8000)=>page.waitForFunction(text=>{const on=[...document.querySelectorAll('.measurement-choice button[aria-pressed="true"]')].map(node=>node.textContent);return on.length===1&&on[0]===text;},label[value],{timeout});
  const disabled=()=>page.locator(".measurement-choice button").evaluateAll(nodes=>nodes.map(node=>node.disabled));
  const logged=(since,path,method,search)=>requestLog.slice(since).filter(entry=>entry.path===path&&entry.method===method&&(search===undefined||entry.search===search));
  const gatePuts=since=>logged(since,"/api/measurement-gate","PUT").map(entry=>JSON.parse(entry.body));
  const liveArrivals=since=>logged(since,"/api/ui/live","GET").length;
  const progressArrivals=since=>logged(since,"/api/tracking","GET","?view=progress").length;
  const assetPosts=since=>logged(since,"/api/tracking","POST").map(entry=>JSON.parse(entry.body)).filter(post=>post.action==="add_asset");
  const waitFor=async(check,message,timeout=8000)=>{const end=Date.now()+timeout;while(!check()){if(Date.now()>end)assert.fail(message);await page.waitForTimeout(50);}};
  const answeredLive=async(count=1)=>{const start=liveAnswered;await waitFor(()=>liveAnswered>=start+count,`${count} answered live poll(s)`);};
  const markGateCard=()=>page.evaluate(()=>{document.querySelector('[data-card="gate"]').dataset.old="1";});
  // Tab Measure while its live answer is held (or every poll is blocked): the field setup summary already shows the
  // confirmed method, so it comes from the page's own state, not from a later poll (r2-7).
  const de=require(root+"/i18n.js").dictionaries.de;
  const summaryShowsTen=async(language,pollsBlocked)=>{
    const answered=liveAnswered,held=pollsBlocked?null:holdNext(path=>path==="/api/ui/live");
    if(language==="de")await page.evaluate(()=>RtkI18n.setLanguage("de"));
    await page.locator('#mobile-nav a[data-route="measure"]').click();
    await page.waitForSelector(".status-setup");
    if(held)await held.arrived;
    assert.strictEqual(await page.locator('.status-setup [data-context="method"]').count(),1,`${language}: method row in the Measure summary`);
    const expected=language==="de"?de["ui.sample_avg"].replace("{count}","10"):label[10];
    assert.strictEqual(await page.locator('.status-setup [data-context="method"] b').innerText(),expected,`${language}: Measure summary before any live answer`);
    assert.strictEqual(liveAnswered,answered,"no live answer reached the page");
    if(held)held.release();
    if(language==="de")await page.evaluate(()=>RtkI18n.setLanguage("en"));
  };
  const gateRedrawn=()=>page.waitForSelector('[data-card="gate"]:not([data-old])');
  const defaults=samples=>({antenna_height_m:2,reference_frame:"",reference_frame_epoch:"",samples});
  const saved={fix:live.current_fix,accepted:live.current_quality.accepted,progress:live.measurement_progress,line:live.active_line,defaults:live.measurement_defaults};
  await page.setViewportSize({width:360,height:900});
  await page.evaluate(()=>RtkI18n.setLanguage("en"));
  live.current_fix=Object.assign({},live.current_fix,{qual:4,fix_status_text:"RTK_FIXED",correction_age_sec:1.2});
  live.current_quality.accepted=true;
  live.active_line=null;
  live.measurement_progress={active:false,collected:0,total:5};
  // One object point from Measure; the POST answer is held until release(), progress is served once the sheet is open.
  const startAsset=async(name,progress)=>{
    await go(page,"measure",".readiness-hero");
    await page.waitForFunction(()=>[...document.querySelectorAll(".field-actions > button")].some(node=>node.textContent==="Object point"&&!node.disabled),null,{timeout:8000});
    const post=holdNext((path,method)=>path==="/api/tracking"&&method==="POST");
    await page.locator(".field-actions > button",{hasText:"Object point"}).first().click();
    await page.locator('dialog.asset-sheet input[name="name"]').fill(name);
    await page.locator("dialog.asset-sheet").getByRole("button",{name:"Save point"}).click();
    // Open: the closing asset sheet's history.back() no longer closes the new sheet (#24-UI Task 8, R2).
    await page.waitForSelector(".measurement-sheet[open]");
    live.measurement_progress=progress;
    await post.arrived;
    return post;
  };
  const finishAsset=async post=>{live.measurement_progress={active:false,collected:0,total:5};post.release();await page.waitForSelector(".measurement-sheet",{state:"detached"});};

  console.log("field-setup: live without measurement_defaults");
  delete live.measurement_defaults;gateSamples=10;
  await page.evaluate(()=>{location.hash="measure";});
  await page.reload();
  await page.waitForSelector(".readiness-hero");
  await go(page,"measure/setup","#app-main > *");
  assert.strictEqual(await page.locator(".field-setup-layout").count(),1,"#measure/setup shows the field setup page");
  await answeredLive();
  assert.deepStrictEqual(await pressed(),[],"no device method in live: nothing pressed (the gate GET says 10)");
  live.measurement_defaults=defaults(10);
  await waitPressed(10);

  console.log("field-setup: focus and the display page");
  await go(page,"measure/setup/frame",".field-setup-layout");
  await page.waitForFunction(()=>document.activeElement&&document.activeElement.id==="field-setup-reference-frame");
  await go(page,"device/settings",".settings-layout");
  assert.strictEqual(await page.evaluate(()=>location.hash),"#device/display");
  assert.strictEqual(await page.locator('.settings-layout :is([data-card="method"], [data-card="gate"], [data-card="field-setup"])').count(),0);

  console.log("field-setup: one PUT, double click, success without a further poll");
  live.measurement_defaults=defaults(5);gateSamples=5;
  await go(page,"measure/setup",".field-setup-layout");
  await waitPressed(5);
  let since=requestLog.length;
  const put=holdNext((path,method)=>path==="/api/measurement-gate"&&method==="PUT");
  blockPolls();
  const answeredAtClick=liveAnswered;
  await choice(10).click();
  await put.arrived;
  assert.deepStrictEqual(await disabled(),[true,true,true,true],"buttons locked during the PUT");
  await choice(1).click({force:true});
  await choice(10).click({force:true});
  await page.waitForTimeout(300);
  assert.deepStrictEqual(gatePuts(since),[{samples:10}],"exactly one PUT with the new method");
  put.release();
  await waitPressed(10,1000);
  assert.deepStrictEqual(await disabled(),[false,false,false,false]);
  assert.strictEqual(liveAnswered,answeredAtClick,"the selection follows the PUT answer, not a poll");
  await summaryShowsTen("en",true);
  await go(page,"measure/setup",".field-setup-layout");
  releasePolls();
  await answeredLive();
  assert.deepStrictEqual(await pressed(),[label[10]],"live with samples 10 keeps the selection");
  live.measurement_defaults=defaults("quality");
  await waitPressed("quality");

  console.log("field-setup: a delayed old live answer");
  live.measurement_defaults=defaults(5);
  await waitPressed(5);
  const oldLive=holdNext(path=>path==="/api/ui/live");
  assert.strictEqual((await oldLive.arrived).measurement_defaults.samples,5);
  await choice(10).click();
  await waitPressed(10,1000);
  oldLive.release();
  await page.waitForTimeout(300);
  assert.deepStrictEqual(await pressed(),[label[10]],"the old live answer does not reset the selection");
  await answeredLive();
  assert.deepStrictEqual(await pressed(),[label[10]]);
  await summaryShowsTen("de",false);
  since=requestLog.length;
  let asset=await startAsset("Method 10",{active:true,mode:"fixed",phase:"collecting",collected:1,elapsed_sec:1,rejected_samples:0});
  await page.waitForFunction(()=>document.querySelector(".measurement-state").textContent.includes("1 of 10"));
  assert.strictEqual(assetPosts(since)[0].samples,10);
  await finishAsset(asset);

  console.log("field-setup: an old gate GET after the method PUT");
  live.measurement_defaults=defaults(5);gateSamples=5;
  await go(page,"measure/setup",".field-setup-layout");
  await waitPressed(5);
  const oldGate=holdNext((path,method)=>path==="/api/measurement-gate"&&method==="GET");
  await markGateCard();
  await page.getByRole("button",{name:"Restore factory values (measurement rules)"}).click();
  await page.locator("dialog[open]").getByRole("button",{name:"Restore defaults"}).click();
  assert.strictEqual((await oldGate.arrived).samples,5);
  await choice(10).click();
  await waitPressed(10,1000);
  oldGate.release();
  await gateRedrawn();
  await page.waitForTimeout(300);
  assert.deepStrictEqual(await pressed(),[label[10]],"the old gate GET does not reset the selection");
  await summaryShowsTen("en",false);
  since=requestLog.length;
  asset=await startAsset("Method 10 after rules",{active:false,collected:0,total:10});
  assert.strictEqual(assetPosts(since)[0].samples,10);
  await finishAsset(asset);

  console.log("field-setup: a method change while a measurement sheet is open");
  live.measurement_defaults=defaults(5);gateSamples=5;
  await go(page,"measure",".readiness-hero");
  await answeredLive(2);
  since=requestLog.length;
  asset=await startAsset("Open sheet",{active:true,mode:"fixed",phase:"collecting",total:5,collected:2,elapsed_sec:2,rejected_samples:0});
  assert.strictEqual(assetPosts(since)[0].samples,5);
  live.measurement_defaults=defaults("quality");
  await page.waitForTimeout(300);
  const sheetSince=requestLog.length;
  await waitFor(()=>progressArrivals(sheetSince)>=2,"two progress polls while the sheet is open");
  await page.waitForFunction(()=>document.querySelector(".measurement-state").textContent.includes("2 of 5"));
  assert.strictEqual(liveArrivals(sheetSince),0,"no live poll while the measurement sheet is open");
  await finishAsset(asset);
  await answeredLive(2);
  since=requestLog.length;
  asset=await startAsset("Quality",{active:true,mode:"quality",phase:"collecting",collected:1,elapsed_sec:1,rejected_samples:0});
  assert.strictEqual(assetPosts(since)[0].samples,"quality");
  await page.waitForFunction(()=>document.querySelector(".measurement-state").textContent.includes("valid samples"));
  assert(!/ of \d/.test(await page.locator(".measurement-state").innerText()),"quality mode shows no \"of N\"");
  await finishAsset(asset);

  console.log("field-setup: rules actions keep unsaved field setup input");
  await go(page,"measure/setup",".field-setup-layout");
  const antenna=page.locator('[data-card="field-setup"] input[name="antenna_height_m"]');
  await antenna.fill("1.5");
  since=requestLog.length;
  await markGateCard();
  await page.getByRole("button",{name:"Restore factory values (measurement rules)"}).click();
  await page.locator("dialog[open]").getByRole("button",{name:"Restore defaults"}).click();
  await gateRedrawn();
  assert.strictEqual(logged(since,"/api/measurement-gate","DELETE").length,1);
  assert.strictEqual(await antenna.inputValue(),"1.5");
  assert.strictEqual(logged(since,"/api/field-setup","GET").length,0,"no field setup reload after the reset");
  await page.waitForFunction(()=>!document.querySelector("dialog[open]")&&!(history.state&&history.state.rtkSheet));
  await markGateCard();
  await page.getByRole("button",{name:"Configure acceptance rules"}).click();
  await page.waitForSelector("dialog[open]");
  await page.getByRole("button",{name:"Save acceptance rules"}).click();
  await gateRedrawn();
  assert.strictEqual(await antenna.inputValue(),"1.5");
  assert.strictEqual(logged(since,"/api/field-setup","GET").length,0,"no field setup reload after saving the rules");
  await page.waitForFunction(()=>!document.querySelector("dialog[open]")&&!(history.state&&history.state.rtkSheet));

  console.log("field-setup: the method hint follows the rules' sample window");
  const qualityHint=count=>en["ui.method_quality_help"].replaceAll("{count}",String(count));
  const hintText=()=>page.evaluate(()=>document.querySelector('[data-card="method"] [data-hint="method-quality"]').textContent);
  const waitHint=(count,why)=>page.waitForFunction(text=>document.querySelector('[data-card="method"] [data-hint="method-quality"]').textContent===text,qualityHint(count),{timeout:3000})
    .catch(async()=>assert.fail(`${why}: expected the hint with ${count}, got "${await hintText()}"`));
  const frameInput=page.locator('[data-card="field-setup"] input[name="reference_frame"]');
  await frameInput.fill("ETRS89 unsaved");
  assert.strictEqual(await hintText(),qualityHint(5),"the hint starts with the window of the mount");
  await markGateCard();
  await page.getByRole("button",{name:"Configure acceptance rules"}).click();
  await page.waitForSelector("dialog[open]");
  await page.locator('dialog[open] input[name="quality_window_samples"]').fill("10");
  await page.getByRole("button",{name:"Save acceptance rules"}).click();
  await gateRedrawn();
  await waitHint(10,"window 5 -> 10");
  assert.strictEqual(await antenna.inputValue(),"1.5","the unsaved antenna height stays after the window change");
  assert.strictEqual(await frameInput.inputValue(),"ETRS89 unsaved","the unsaved frame stays after the window change");
  await page.waitForFunction(()=>!document.querySelector("dialog[open]")&&!(history.state&&history.state.rtkSheet));
  await markGateCard();
  await page.getByRole("button",{name:"Restore factory values (measurement rules)"}).click();
  await page.locator("dialog[open]").getByRole("button",{name:"Restore defaults"}).click();
  await gateRedrawn();
  await waitHint(5,"factory values");
  assert.strictEqual(await antenna.inputValue(),"1.5","the unsaved antenna height stays after the reset");
  assert.strictEqual(await frameInput.inputValue(),"ETRS89 unsaved","the unsaved frame stays after the reset");
  assert.strictEqual(logged(since,"/api/field-setup","GET").length,0,"no field setup reload for the hint");
  await page.waitForFunction(()=>!document.querySelector("dialog[open]")&&!(history.state&&history.state.rtkSheet));

  console.log("field-setup: a late rules reset after navigation");
  await go(page,"measure/setup",".field-setup-layout");
  const lateDelete=holdNext((path,method)=>path==="/api/measurement-gate"&&method==="DELETE");
  await page.getByRole("button",{name:"Restore factory values (measurement rules)"}).click();
  await page.locator("dialog[open]").getByRole("button",{name:"Restore defaults"}).click();
  await lateDelete.arrived;
  await page.waitForFunction(()=>!document.querySelector("dialog[open]")&&!(history.state&&history.state.rtkSheet));
  since=requestLog.length;
  await go(page,"device/connections/internet","form.configuration-layout");
  await page.locator('input[name="wifi_ssid"]').fill("Edited");
  await page.evaluate(()=>document.querySelectorAll("#feedback-dock .feedback-item").forEach(node=>node.remove()));
  const released=requestLog.length;
  lateDelete.release();
  await page.waitForTimeout(800);
  assert.strictEqual(await page.locator('input[name="wifi_ssid"]').inputValue(),"Edited");
  assert.strictEqual(await page.locator("form.configuration-layout").getAttribute("data-dirty"),"true");
  assert.strictEqual(logged(since,"/api/config","GET").length,1,"the form loaded once");
  assert.strictEqual(logged(released,"/api/measurement-gate","GET").length,0,"no rules reload for a page that is gone");
  assert.strictEqual(await page.locator("#feedback-dock .feedback-item").count(),0,"no message from the late action");
  assert.strictEqual(await page.locator("dialog[open]").count(),0,"no dirty dialog");

  console.log("field-setup: navigation while the field setup loads");
  await go(page,"projects",".project-list");
  const lateSetup=holdNext((path,method)=>path==="/api/field-setup"&&method==="GET");
  await page.evaluate(()=>{location.hash="measure/setup";});
  await lateSetup.arrived;
  await page.locator('#mobile-nav a[data-route="projects"]').click();
  await page.waitForSelector(".project-list");
  lateSetup.release();
  await page.waitForTimeout(500);
  assert.strictEqual(await page.locator(".field-setup-layout").count(),0);
  assert(await page.locator(".project-list").isVisible());
  assert.strictEqual(await page.locator("#feedback-dock .feedback-item.critical").count(),0,"no error message");

  live.current_fix=saved.fix;live.current_quality.accepted=saved.accepted;live.measurement_progress=saved.progress;live.active_line=saved.line;
  if(saved.defaults===undefined)delete live.measurement_defaults;else live.measurement_defaults=saved.defaults;
  gateSamples=5;gateWindow=5;
  console.log("field-setup-browser=ok");
}
// Project view (#24-UI Task 8): opening never activates, activating is its own step, the project actions carry one export
// entry and deleting stands apart. The export sheet closes by button, Escape and Back without leaving a history entry behind.
async function testProjectView(page,browser){
  const i18n=require(root+"/i18n.js"),dictionaries=i18n.dictionaries,v2de=i18n.v2de;
  const text=(lang,key)=>dictionaries[lang][key],literal=(lang,english)=>lang==="de"?v2de[english]:english;
  const posts=since=>requestLog.slice(since).filter(entry=>entry.path==="/api/tracking"&&entry.method==="POST").map(entry=>JSON.parse(entry.body));
  const sheet=page.locator("dialog.export-sheet[open]");
  const sheetGone=()=>page.waitForFunction(()=>!document.querySelector("dialog[open]")&&!(history.state&&history.state.rtkSheet));
  const hash=()=>page.evaluate(()=>location.hash);
  const errors=[];
  page.on("pageerror",error=>errors.push(error.message));
  const savedActive=tracking.active_project_id;
  // A fresh pair of entries (library, project) so history.length counts from a known place.
  const openProject=async id=>{await go(page,"projects",".project-list");await go(page,"projects/"+id,".map-panel");assert.strictEqual(await page.locator(".map-panel .project-actions").count(),1,"project view: one project actions section");};
  const openExport=async lang=>{await page.locator(".project-actions").getByRole("button",{name:text(lang,"ui.project_export"),exact:true}).click();await sheet.waitFor();};
  const closeButton=lang=>sheet.getByRole("button",{name:text(lang,"ui.why_close"),exact:true});
  for(const lang of["en","de"]){
    await setLanguage(page,lang);
    for(const width of[320,1280]){
      const label=`${lang} ${width}`;
      tracking.active_project_id="p1";
      await page.setViewportSize({width,height:width===1280?900:780});
      console.log(`project view: ${label}`);
      await go(page,"measure",".status-project");
      const projectRow=await page.locator(".status-project b").innerText();
      // Opening an inactive project sends nothing; the hint says so.
      let since=requestLog.length;
      await openProject("p2");
      assert.strictEqual(posts(since).filter(post=>post.action==="select_project").length,0,label+": opening does not activate");
      const activation=page.locator(".project-activation");
      assert.strictEqual((await activation.locator("p.muted").innerText()).trim(),text(lang,"ui.project_open_not_active"),label+": hint under the activation");
      // The way back is the navigation: Measure still shows the active project.
      await page.locator('a[data-route="measure"]:visible').first().click();
      await page.waitForSelector(".status-project");
      assert.strictEqual(await page.locator(".status-project b").innerText(),projectRow,label+": Measure keeps the active project");
      await openProject("p2");
      since=requestLog.length;
      await activation.getByRole("button",{name:literal(lang,"Activate for measurement"),exact:true}).click();
      await page.waitForSelector(".project-activation .notice.ok");
      assert.deepStrictEqual(posts(since).filter(post=>post.action==="select_project").map(post=>post.project_id),["p2"],label+": one activation");
      assert.strictEqual(await hash(),"#projects/p2",label+": activating stays in the project");
      const toMeasure=page.locator('.project-activation a.btn.secondary[href="/#measure"]');
      assert.strictEqual((await toMeasure.innerText()).trim(),text(lang,"ui.go_measure"),label+": link to Measure");
      assert.strictEqual((await page.locator(".project-activation .notice.ok").innerText()).trim(),literal(lang,"Active project for field recording"));
      // Actions: one export entry, rename, archive; the empty tools slot; delete in its own area 24 px or more below.
      assert.strictEqual((await page.locator(".project-actions h3").innerText()).trim(),text(lang,"ui.project_actions"));
      assert.deepStrictEqual((await page.locator(".project-action-list .btn").allInnerTexts()).map(value=>value.trim()),
        [text(lang,"ui.project_export"),literal(lang,"Rename"),literal(lang,"Archive")],label+": project actions");
      assert.strictEqual(await page.locator('.map-panel [data-slot="project-tools"]').count(),1,label+": tools slot");
      assert.strictEqual(await page.locator(".map-panel .export-links, .map-panel .text-actions").count(),0,label+": no old action rows");
      assert.strictEqual((await page.locator(".project-danger h3").innerText()).trim(),text(lang,"ui.project_danger_title"));
      const spacing=await page.evaluate(()=>{const actions=[...document.querySelectorAll(".project-actions .btn")],last=actions[actions.length-1].getBoundingClientRect(),button=document.querySelector(".project-danger .btn"),rect=button.getBoundingClientRect();return{gap:rect.top-last.bottom,text:button.textContent.trim(),width:rect.width,height:rect.height};});
      assert(spacing.gap>=24,`${label}: delete is ${spacing.gap}px below the last action`);
      assert.strictEqual(spacing.text,text(lang,"ui.project_delete"));
      assert(spacing.width>=48&&spacing.height>=48,`${label}: delete ${spacing.width}x${spacing.height}`);
      assertLayout(await layoutAudit(page),label+": project view");
      await page.locator(".project-danger .btn").click();
      await page.waitForSelector("dialog.action-sheet[open] input[name=confirm]");
      assert.strictEqual((await page.locator("dialog[open] h2").innerText()).trim(),literal(lang,"Delete project"),label+": delete asks as before");
      await page.locator("dialog[open]").getByRole("button",{name:text(lang,"action.cancel"),exact:true}).click();
      await sheetGone();
      // The export sheet: three formats, GeoJSON through the export flow (#46), CSV and archive as direct download links,
      // no sideways overflow at 320 px.
      await openExport(lang);
      assert.strictEqual((await sheet.locator("h2").innerText()).trim(),text(lang,"ui.export_title"));
      assert.strictEqual((await sheet.locator("p.muted").innerText()).trim(),text(lang,"ui.export_share_hint"));
      const formats=await sheet.locator("[data-export-format]").evaluateAll(nodes=>nodes.map(node=>[node.dataset.exportFormat,node.querySelector("b").textContent,node.querySelector("small").textContent]));
      assert.deepStrictEqual(formats,[["geojson",...text(lang,"ui.export_geojson").split(" – ")],["csv",...text(lang,"ui.export_csv").split(" – ")],["archive",...text(lang,"ui.export_archive").split(" – ")]],label+": formats");
      const geojsonRow=sheet.locator('[data-export-format="geojson"]');
      assert.strictEqual(await geojsonRow.locator(".export-flow [data-export-action=start]").count(),1,label+": GeoJSON starts the export flow");
      assert.strictEqual((await geojsonRow.locator("[data-export-action=start]").innerText()).trim(),text(lang,"ui.export_flow_start"),label+": start label");
      assert.strictEqual(await geojsonRow.locator(".export-flow").getAttribute("data-phase"),"idle",label+": the flow is idle");
      assert.strictEqual(await sheet.locator('a[href*="format=geojson"]').count(),0,label+": no direct GeoJSON link");
      assert.strictEqual(await sheet.locator('[data-export="geojson-share"]').count(),0,label+": no share before an export");
      assert.deepStrictEqual(await sheet.locator("a[download]").evaluateAll(nodes=>nodes.map(node=>[node.dataset.export,node.getAttribute("href"),node.textContent])),
        [["csv","/api/tracking/export?project_id=p2&format=csv",text(lang,"ui.export_download")],
         ["archive","/api/tracking/export?project_id=p2&format=backup",text(lang,"ui.export_download")]],label+": download links");
      const targets=await sheet.locator(".btn").evaluateAll(nodes=>nodes.map(node=>{const r=node.getBoundingClientRect();return[r.width,r.height];}));
      assert(targets.every(([w,hgt])=>w>=48&&hgt>=48),`${label}: export targets ${JSON.stringify(targets)}`);
      await assertSheetLayout(page,label+": export sheet");
      await closeButton(lang).click();
      await sheetGone();
      // Without a hook every way closes at once; the sheet entry is gone and one more Back leaves the project. A fresh page
      // (own context, same language and fixture) keeps history.length below Chromium's cap of 50 entries.
      const fresh=await browser.newPage({viewport:{width,height:width===1280?900:780}});
      try{
        await fresh.addInitScript(value=>localStorage.setItem("rtk-language",value),lang);
        await fresh.route("http://local.test/**",fixture);
        await fresh.goto("http://local.test/");
        await fresh.waitForSelector(".readiness-hero");
        const freshSheet=fresh.locator("dialog.export-sheet[open]");
        for(const way of["button","escape","back"]){
          await go(fresh,"projects",".project-list");
          await go(fresh,"projects/p1",".map-panel .project-actions");
          const before=await fresh.evaluate(()=>history.length);
          await fresh.locator(".project-actions").getByRole("button",{name:text(lang,"ui.project_export"),exact:true}).click();
          await freshSheet.waitFor();
          if(way==="button")await freshSheet.getByRole("button",{name:text(lang,"ui.why_close"),exact:true}).click();
          else if(way==="escape")await fresh.keyboard.press("Escape");
          else await fresh.goBack();
          await fresh.waitForFunction(()=>!document.querySelector("dialog[open]")&&!(history.state&&history.state.rtkSheet));
          assert.strictEqual(await fresh.evaluate(()=>location.hash),"#projects/p1",`${label} ${way}: still in the project`);
          assert(before<49,`${label} ${way}: short history (${before})`);
          assert.strictEqual(await fresh.evaluate(()=>history.length),before+1,`${label} ${way}: one sheet entry, used up`);
          await fresh.goBack();
          await fresh.waitForFunction(()=>location.hash==="#projects");
          await fresh.waitForSelector(".project-list");
        }
      }finally{await fresh.close();}
    }
  }
  await setLanguage(page,"en");
  await page.setViewportSize({width:360,height:800});
  // Leaving the view closes the sheet without asking.
  await openProject("p1");
  await openExport("en");
  await page.evaluate(()=>{location.hash="device";});
  await page.waitForSelector(".device-links");
  assert.strictEqual(await page.locator("dialog[open]").count(),0,"navigation closes the export sheet");
  // A navigation right after the sheet's own history.back() (before its popstate) is not undone.
  await openProject("p1");
  await openExport("en");
  await page.evaluate(()=>document.querySelector("dialog.export-sheet").addEventListener("close",()=>{location.hash="device";}));
  await closeButton("en").click();
  await page.waitForSelector(".device-links");
  await page.waitForTimeout(500);
  assert.strictEqual(await hash(),"#device","the navigation in the back window stays");
  assert.strictEqual(await page.locator(".device-links").count(),1,"the device overview stays");
  await page.goBack();
  await page.waitForFunction(()=>location.hash==="#projects/p1");
  // Existing sheets close by Back as before: the rename sheet sends nothing.
  await page.waitForSelector(".map-panel .project-actions");
  let since=requestLog.length;
  await page.locator(".project-action-list").getByRole("button",{name:"Rename",exact:true}).click();
  await page.waitForSelector("dialog[open] input[name=name]");
  await page.goBack();
  await sheetGone();
  assert.strictEqual(await hash(),"#projects/p1","Back closes the rename sheet in place");
  assert.strictEqual(posts(since).length,0,"a closed rename sheet sends nothing");
  // Sharing and downloading the GeoJSON file are cases of the export flow (#46, suite export).
  // A stakeout link opens its page inside the project (WP3) without an error.
  await page.evaluate(()=>{location.hash="projects/p1/stakeout/import";});
  await page.waitForSelector(".stakeout-page .stakeout-import");
  assert.strictEqual(await hash(),"#projects/p1/stakeout/import","the stakeout link stays");
  assert.strictEqual(await page.locator("#app-main .notice.critical:visible").count(),0,"stakeout page without an error");
  assert.deepStrictEqual(errors,[],"no page errors");
  tracking.active_project_id=savedActive;
}
// Sheet history (#24-UI Task 8, R2 #21c): a sheet opened while the closed one's history.back() is on its way stays open,
// and a modal resolves only after its own back, so the navigation it starts is never undone.
async function testSheetHistory(page){
  const saved={fix:live.current_fix,accepted:live.current_quality.accepted,line:live.active_line,progress:live.measurement_progress};
  await page.setViewportSize({width:360,height:900});
  await page.evaluate(()=>RtkI18n.setLanguage("en"));
  live.current_fix=Object.assign({},live.current_fix,{qual:4,fix_status_text:"RTK_FIXED",correction_age_sec:1.2});
  live.current_quality.accepted=true;live.active_line=null;live.measurement_progress={active:false,collected:0,total:5};
  console.log("sheet history: object point -> measure sheet stays open");
  await go(page,"measure",".readiness-hero");
  await page.waitForFunction(()=>[...document.querySelectorAll(".field-actions > button")].some(node=>node.textContent==="Object point"&&!node.disabled),null,{timeout:8000});
  const post=holdNext((path,method)=>path==="/api/tracking"&&method==="POST");
  await page.locator(".field-actions > button",{hasText:"Object point"}).first().click();
  await page.locator('dialog.asset-sheet input[name="name"]').fill("History 1");
  await page.locator("dialog.asset-sheet").getByRole("button",{name:"Save point"}).click();
  await page.waitForSelector(".measurement-sheet",{state:"attached"});
  live.measurement_progress={active:true,collected:1,total:5};
  await post.arrived;
  await page.waitForTimeout(500);
  assert.strictEqual(await page.locator(".measurement-sheet[open]").count(),1,"object point -> measure sheet stays open");
  assert.strictEqual(await page.evaluate(()=>Boolean(history.state&&history.state.rtkSheet)),true,"the measure sheet owns a history entry");
  live.measurement_progress={active:false,collected:0,total:5};
  post.release();
  await page.waitForSelector(".measurement-sheet",{state:"detached"});
  await page.waitForFunction(()=>!(history.state&&history.state.rtkSheet));
  assert.strictEqual(await page.evaluate(()=>location.hash),"#measure","the measurement ends on Measure");
  console.log("sheet history: a navigation right after a sheet's own history.back() stays");
  await page.locator(".field-actions > button",{hasText:"Object point"}).first().click();
  await page.waitForSelector("dialog.asset-sheet[open]");
  await page.evaluate(()=>document.querySelector("dialog.asset-sheet").addEventListener("close",()=>{location.hash="projects";}));
  await page.locator("dialog.asset-sheet").getByRole("button",{name:"Cancel"}).click();
  await page.waitForTimeout(800);
  assert.strictEqual(await page.evaluate(()=>location.hash),"#projects","the navigation in the back window stays");
  await page.waitForSelector(".project-list");
  await page.goBack();
  await page.waitForFunction(()=>location.hash==="#measure");
  console.log("sheet history: a modal's navigation is not undone");
  await go(page,"device/connections/internet","form.configuration-layout");
  await page.locator('input[name="wifi_ssid"]').fill("Unsaved network");
  await page.waitForFunction(()=>document.querySelector("form.configuration-layout").dataset.dirty==="true");
  await page.locator('a[data-route="device"]:visible').first().click();
  await page.waitForSelector("dialog[open]");
  await page.getByRole("button",{name:"Discard changes"}).click();
  await page.waitForTimeout(800);
  assert.strictEqual(await page.evaluate(()=>location.hash),"#device","the guarded link's navigation stays");
  await page.waitForSelector(".device-links");
  // The modal resolved after its own back: Back returns to the form page itself, not to a left-over sheet entry.
  await page.goBack();
  await page.waitForFunction(()=>location.hash==="#device/connections/internet");
  await page.waitForSelector("form.configuration-layout");
  assert.strictEqual(await page.evaluate(()=>Boolean(history.state&&history.state.rtkSheet)),false,"no sheet entry left behind the modal");
  Object.assign(live,{current_fix:saved.fix,active_line:saved.line,measurement_progress:saved.progress});live.current_quality.accepted=saved.accepted;
}
async function testCapacity(page){
  console.log("capacity: initial display");
  const panel=page.locator(".recording-capacity");
  await page.setViewportSize({width:360,height:900});
  await page.waitForFunction(()=>document.querySelector(".capacity-count").textContent.includes("5,000"));
  // Normal case (#45): title, count and meter only; the sentences of the full card stay hidden.
  assert(await panel.evaluate(node=>node.classList.contains("compact")),"idle capacity panel must be compact");
  assert((await panel.innerText()).includes("31 / 5,000"));
  assert(!(await panel.innerText()).includes("points available"),"compact panel hides the remaining count");
  assert(await panel.isVisible());
  assert((await panel.boundingBox()).height<=120,"compact panel height "+(await panel.boundingBox()).height);
  // The capacity panel follows the checklist (secondary information): it is reached by scrolling and must then
  // clear both the phone navigation and the action bar.
  await panel.scrollIntoViewIfNeeded();
  const box=await panel.boundingBox(),nav=await page.locator(".mobile-nav").boundingBox(),bar=await page.locator(".field-actions").boundingBox();
  const clear=(a,b)=>a.y+a.height<=b.y+0.5||b.y+b.height<=a.y+0.5||a.x+a.width<=b.x||b.x+b.width<=a.x;
  assert(clear(box,nav),"capacity must not sit under the phone navigation");
  assert(clear(box,bar),"capacity must not sit under the action bar");
  console.log("capacity: warning thresholds");
  const capacityEntry=page.locator('.feedback-item[data-key="capacity"]');
  for(const [used,level] of [[4000,"notice"],[4500,"warning"],[4900,"critical"],[5000,"full"]]){
    live.capacity={used,limit:5000,remaining:5000-used,level,writable:true};
    await page.waitForFunction(value=>document.querySelector(".capacity-count").textContent.startsWith(value),used.toLocaleString("en-US"));
    if(used<4900){
      // Levels 1-2 are quiet on the measuring screen: the full panel carries the message, no entry appears.
      await page.waitForFunction(()=>!document.querySelector(".recording-capacity").classList.contains("compact"));
      await page.waitForTimeout(600);
      assert.strictEqual(await capacityEntry.count(),0,"a quiet threshold must not add a message on the measuring screen");
      assert((await panel.innerText()).includes(used===4000?"Recording capacity is running low":"Archive a project soon"));
    }else{
      await page.waitForSelector('.capture-strip .feedback-item.critical[data-key="capacity"]');
      assert.strictEqual(await capacityEntry.count(),1);
      await capacityEntry.locator("[data-feedback-dismiss]").click();
      assert.strictEqual(await capacityEntry.count(),0,"dismissing removes the entry");
    }
    await page.waitForTimeout(2200);
    assert.strictEqual(await capacityEntry.count(),0,"same threshold must not notify repeatedly");
  }
  console.log("capacity: archive workflow");
  live.active_line={id:"capacity-line",name:"Paused trip",state:"paused",pause_reason:"capacity_limit",vertex_count:2000,length_m:2000,auto_distance_m:1};
  tracking.active_line={...live.active_line};tracking.active_project_id="p1";
  tracking.projects[0].feature_count=3500;tracking.projects[1].feature_count=1500;
  tracking.capacity=live.capacity;
  await page.waitForFunction(()=>document.querySelector(".readiness-hero h2").textContent.includes("Recording paused"));
  assert(await page.getByRole("button",{name:"Resume",exact:true}).isDisabled());
  assert((await panel.innerText()).includes("Your recorded points are saved"));
  await go(page,"projects/p1",".map-workspace");
  assert(await page.getByRole("button",{name:"Archive",exact:true}).isDisabled());
  assert(await page.getByRole("button",{name:"Delete project",exact:true}).isDisabled());
  await go(page,"projects/p2",".map-workspace");
  assert(await page.getByRole("button",{name:"Archive",exact:true}).isEnabled());
  assert(await page.getByRole("button",{name:"Delete project",exact:true}).isEnabled());
  await go(page,"status",".recording-capacity");
  await page.getByRole("button",{name:"Choose project to archive",exact:true}).click();
  await page.waitForSelector("dialog[open] select");
  assert.strictEqual(await page.locator('select[name="project_id"] option').count(),1);
  assert((await page.locator('select[name="project_id"]').innerText()).includes("1,500"));
  await page.getByRole("button",{name:"Archive project",exact:true}).click();
  await page.waitForFunction(()=>document.querySelector(".capacity-count").textContent.startsWith("3,500"));
  assert.strictEqual(live.active_line.state,"paused");
  assert.strictEqual(live.active_line.id,"capacity-line");
  assert(await page.getByRole("button",{name:"Resume",exact:true}).isEnabled());
  assert((await panel.innerText()).includes("Capacity is available again"));
  live.capacity={...live.capacity,writable:false};
  await page.waitForFunction(()=>document.querySelector(".readiness-hero h2").textContent.includes("storage reserve"));
  assert(await page.getByRole("button",{name:"Resume",exact:true}).isDisabled());
  live.capacity={...live.capacity,writable:true};
  failLive=true;
  await page.waitForFunction(()=>document.querySelector(".capacity-remaining").textContent.includes("unavailable"));
  assert.strictEqual(await page.locator(".capacity-count").innerText(),"—");
  failLive=false;
  await page.waitForFunction(()=>document.querySelector(".capacity-count").textContent.startsWith("3,500"));
  await page.evaluate(()=>RtkI18n.setLanguage("de"));
  await page.waitForFunction(()=>document.querySelector(".recording-capacity h2").textContent===RtkI18n.v2de["Recording capacity"]);
  assert((await panel.innerText()).includes("3.500 / 5.000"));
  assert((await panel.innerText()).includes("1.500"));
  for(const width of [320,360,1280]){
    await page.setViewportSize({width,height:900});
    assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),`capacity overflow at ${width}`);
  }
  await page.setViewportSize({width:360,height:900});
  await page.screenshot({path:"/tmp/capacity-status-phone.png",fullPage:true});
}
// Effective NTRIP cause (#30, --feedback-only): header pill, card on the measuring screen, checklist row and measurement sheet.
async function testCorrectionsCause(page){
  const i18n=require(root+"/i18n.js"),en=i18n.dictionaries.en,deDict=i18n.dictionaries.de,v2de=i18n.v2de;
  const nextPoll=async()=>{await page.waitForResponse(response=>response.url().endsWith("/api/ui/live")&&response.status()===200);await page.waitForTimeout(300);};
  const saved={fix:structuredClone(live.current_fix),accepted:live.current_quality.accepted,line:live.active_line,network:live.network,
    progress:structuredClone(live.measurement_progress),undo:live.undo_candidate,project:structuredClone(live.active_project)};
  const net=(state,config,code,extra)=>({ntrip_state:state,ntrip_config:config,last_ntrip_error_code:code,access_state:"connected",...extra});
  const ERROR=en["ntrip_error.timeout"];
  const CASES=[
    {key:"parked",id:"parked",mark:"×",short:"NTRIP stopped",action:"Check NTRIP settings",hash:"#device/connections/internet",network:net("backoff","ready","retries_exhausted"),
      text:"NTRIP stopped after repeated failures – it starts again when the caster settings change, the device restarts or Wi-Fi reconnects"},
    {key:"off",id:"switched_off",mark:"⊘",short:"NTRIP off",action:"Open NTRIP settings",hash:"#device/connections/internet",network:net("disabled","switched_off",null),
      text:"NTRIP switched off – no correction data via NTRIP"},
    {key:"incomplete",id:"incomplete",mark:"!",short:"NTRIP not set up",action:"Set up NTRIP",hash:"#device/connections/internet",network:net("disabled","incomplete",null),
      text:"NTRIP is on, but no caster with host and mountpoint is configured"},
    {key:"unavailable",id:"unavailable",mark:"!",short:"NTRIP inactive",action:"Open diagnostics",hash:"#device/diagnostics",network:net("disabled",null,null),
      text:en["ui.corrections_unavailable"],deText:deDict["ui.corrections_unavailable"]},
    {key:"waiting_wifi",id:"waiting_wifi",mark:"!",short:"NTRIP waits for Wi-Fi",action:"Check Wi-Fi",hash:"#device/connections/internet",network:net("waiting_wifi","ready",null),
      text:"Waiting for Wi-Fi before connecting to the caster"},
    {key:"auth",id:"auth_error",mark:"×",short:"NTRIP login refused",action:"Check NTRIP settings",hash:"#device/connections/internet",network:net("auth_error","ready","auth_error"),
      text:"The caster refuses the NTRIP user or password"},
    {key:"mount",id:"mount_error",mark:"×",short:"NTRIP mountpoint missing",action:"Check NTRIP settings",hash:"#device/connections/internet",network:net("mount_error","ready","mount_error"),
      text:"The caster does not offer this mountpoint"},
    {key:"failed",id:"connection_error",mark:"×",short:"NTRIP error",action:"Check NTRIP settings",hash:"#device/connections/internet",network:net("backoff","ready","timeout"),
      text:"Caster connection failed ("+ERROR+") – the device retries automatically"},
    {key:"reconnecting",id:"connection_error",mark:"!",short:"NTRIP reconnecting",action:"Check NTRIP settings",hash:"#device/connections/internet",network:net("connecting","ready","timeout"),
      text:"Connecting to the caster again after: "+ERROR}];
  const NONE=[["connecting without a code",net("connecting","ready",null)],["streaming",net("streaming","ready",null)],["streaming with an old code",net("streaming","ready","timeout")]];
  const ACTION_KEY={"Check NTRIP settings":"check_ntrip","Open NTRIP settings":"open_ntrip","Set up NTRIP":"setup","Open diagnostics":"diagnostics","Check Wi-Fi":"wifi"};
  const LINE={id:"l7",name:"Field route",state:"recording",recording_mode:"route",vertex_count:12,length_m:8.5,recording_sec:30};
  const clickBar=name=>page.evaluate(name=>{const button=[...document.querySelectorAll(".field-actions > button")].find(node=>node.textContent===name);if(!button)throw new Error("bar button "+name+" missing");button.click();},name);
  const view=async()=>{const got=await rawView();assert(got,"the .measure-help card is missing from the measuring screen");return got;};
  const rawView=()=>page.evaluate(()=>{const card=document.querySelector(".measure-help");if(!card)return null;const line=card.querySelector(".cause-line"),pill=document.querySelector("#live-state"),
    start=document.querySelector(".field-actions > button"),diag=document.querySelector('[data-gate="corrections"] .condition-diagnostic span:last-child'),symbol=card.querySelector(".cause-symbol");
    return{hidden:card.hidden,cause:card.dataset.cause,mark:symbol&&symbol.textContent,markHidden:symbol&&symbol.getAttribute("aria-hidden"),text:line&&line.querySelector(".cause-text")&&line.querySelector(".cause-text").textContent,
      lineText:line&&line.textContent,buttons:[...card.querySelectorAll("button")].map(button=>button.textContent),small:pill.querySelector("small").textContent,title:pill.title,aria:pill.getAttribute("aria-label"),
      hero:document.querySelector(".readiness-hero").className,diag:diag&&diag.textContent,start:start&&{text:start.textContent,disabled:start.disabled},
      afterHero:document.querySelector(".readiness-hero").nextElementSibling===card};});
  const expected=(c,lang)=>lang==="en"?{short:c.short,text:c.text,action:c.action}:{short:deDict["ui.cause_"+c.key+"_short"],
    text:c.deText||deDict["ui.cause_"+c.key+"_text"].replace("{error}",deDict["ntrip_error.timeout"]),action:deDict["ui.cause_action_"+ACTION_KEY[c.action]]};
  const check=async(c,lang)=>{
    live.network=c.network;await nextPoll();
    const want=expected(c,lang),got=await view();
    assert.strictEqual(got.hidden,false,`${c.key} ${lang}: card visible`);
    assert.strictEqual(got.cause,c.id,`${c.key} ${lang}: cause id`);
    assert.strictEqual(got.text,want.text,`${c.key} ${lang}: text`);
    assert.strictEqual(got.mark,c.mark,`${c.key} ${lang}: symbol`);
    assert.strictEqual(got.markHidden,"true",`${c.key} ${lang}: symbol is decoration`);
    assert.deepStrictEqual(got.buttons,[want.action],`${c.key} ${lang}: action`);
    assert(got.small.startsWith(lang==="en"?"Live · ":deDict["ui.link_live"]+" · "),`${c.key} ${lang}: pill starts with the link word "${got.small}"`);
    assert(got.small.includes(want.short),`${c.key} ${lang}: pill token "${want.short}" in "${got.small}"`);
    assert(got.title.includes(want.text)&&got.aria.includes(want.text),`${c.key} ${lang}: pill title and label carry the text`);
    assert.strictEqual(got.diag,want.text,`${c.key} ${lang}: checklist row`);
    assert(/\bready\b/.test(got.hero),`${c.key} ${lang}: the gate stays open ("${got.hero}")`);
    assert(got.start&&!got.start.disabled,`${c.key} ${lang}: the start action stays usable`);
    assert(got.afterHero,`${c.key} ${lang}: card directly after the hero`);
  };
  try{
    await page.setViewportSize({width:360,height:800});
    await page.evaluate(()=>{localStorage.setItem("rtk-language","en");location.hash="status";});
    await page.reload();await page.waitForSelector(".readiness-hero");
    live.current_fix=Object.assign({},saved.fix,{qual:4,fix_status_text:"RTK_FIXED",stale:false,fix_seq:(saved.fix.fix_seq||0)+50});
    live.current_quality.accepted=true;setVerdict([]);live.active_line=null;
    live.measurement_progress={active:false,collected:0,total:5};delete live.tracking_startup;
    console.log("feedback: cause matrix #30");
    live.network=net("streaming","ready",null);await nextPoll();
    let idle=await view();
    assert.strictEqual(idle.hidden,true,"streaming: no card");
    assert(!/NTRIP/.test(idle.small),"streaming: no token "+idle.small);
    assert.strictEqual((await view()).start.text,"Start line");
    for(const c of CASES)await check(c,"en");
    for(const [name,network] of NONE){live.network=network;await nextPoll();idle=await view();
      assert.strictEqual(idle.hidden,true,name+": no card");assert.strictEqual(idle.cause,"",name+": empty cause");
      assert(!/NTRIP/.test(idle.small),name+": no token "+idle.small);assert(/\bready\b/.test(idle.hero));}
    // A Wi-Fi error is a separate part of the waiting text.
    live.network=net("waiting_wifi","ready",null,{last_wifi_error:"access_point_not_found"});await nextPoll();
    assert.strictEqual((await view()).text,CASES[4].text+" · Hotspot not found");
    console.log("feedback: cause actions #30");
    for(const c of [CASES[1],CASES[3],CASES[4],CASES[0]]){
      await go(page,"status",".readiness-hero");
      live.network=c.network;await nextPoll();
      await page.locator(".measure-help").getByRole("button",{name:c.action,exact:true}).click();
      await page.waitForFunction(hash=>location.hash===hash,c.hash);
    }
    await go(page,"status",".readiness-hero");
    console.log("feedback: cause while recording and paused #30");
    for(const state of ["recording","paused"]){
      live.active_line=Object.assign({},LINE,{state});live.network=CASES[0].network;await nextPoll();
      const got=await view();
      assert.strictEqual(got.hidden,false,state+": card visible");assert.strictEqual(got.cause,"parked");
      assert(got.small.includes("NTRIP stopped"),state+": token in the pill "+got.small);
    }
    live.active_line=null;
    console.log("feedback: cause in the measurement sheet #30");
    live.active_line=Object.assign({},LINE);live.network=CASES[1].network;captureState={epoch:"a1b2c3d4",stack:[],order:40};actionAnswers={};
    await nextPoll();
    actionAnswers.add_vertex={delay:12000,cancelable:true};
    await clickBar("Add line point");
    await page.locator(".measurement-sheet[open]").waitFor();
    await page.waitForFunction(()=>document.querySelector(".measurement-cause")&&!document.querySelector(".measurement-cause").hidden,null,{timeout:1500});
    await page.waitForTimeout(400);
    const liveBefore=await page.evaluate(()=>window.__liveFetches.length);
    const cause=()=>page.evaluate(()=>{const node=document.querySelector(".measurement-cause");return{hidden:node.hidden,text:node.textContent,afterState:node.previousElementSibling.classList.contains("measurement-state")};});
    assert.deepStrictEqual(await cause(),{hidden:false,text:CASES[1].text,afterState:true},"sheet: switched off");
    for(const [name,network,text] of [["reactivated",net("connecting","ready",null),""],["login refused",CASES[5].network,CASES[5].text],["parked",CASES[0].network,CASES[0].text]]){
      live.network=network;const started=Date.now();
      await page.waitForFunction(text=>{const node=document.querySelector(".measurement-cause");return node.textContent===text&&node.hidden===!text;},text,{timeout:1500})
        .catch(async()=>assert.fail(`sheet ${name}: expected "${text}", got ${JSON.stringify(await cause())}`));
      assert(Date.now()-started<1500,`sheet ${name}: follows within one progress interval`);
      await page.waitForTimeout(500);
    }
    assert.strictEqual(await page.evaluate(()=>window.__liveFetches.length),liveBefore,"no /api/ui/live request while the measurement runs");
    await page.locator(".measurement-sheet").getByRole("button",{name:"Cancel measurement"}).click();
    await page.waitForFunction(()=>!document.querySelector(".measurement-sheet"),null,{timeout:4000});
    captureState=null;actionAnswers={};live.active_line=null;
    await page.evaluate(()=>{for(const button of document.querySelectorAll(".capture-strip [data-feedback-dismiss]"))button.click();});
    console.log("feedback: cause reactivation #30");
    await go(page,"status",".readiness-hero");
    live.network=net("streaming","ready",null);await nextPoll();
    await page.evaluate(()=>{window.__causeLive=[];const node=document.getElementById("rtk-feedback-live");
      new MutationObserver(()=>window.__causeLive.push(node.textContent)).observe(node,{childList:true,characterData:true,subtree:true});});
    live.network=CASES[1].network;await nextPoll();await nextPoll();
    assert.deepStrictEqual(await page.evaluate(()=>window.__causeLive),[CASES[1].text],"the new cause is announced once, polls repeat nothing");
    live.network=net("connecting","ready",null);await nextPoll();
    idle=await view();
    assert.strictEqual(idle.hidden,true,"reactivated: card gone");assert(!/NTRIP/.test(idle.small),"reactivated: token gone "+idle.small);
    live.network=net("streaming","ready",null);await nextPoll();
    assert.deepStrictEqual(await page.evaluate(()=>window.__causeLive),[CASES[1].text],"no announcement for a cause that went away");
    console.log("feedback: cause widths #30");
    for(const lang of ["en","de"]){
      live.network=CASES[0].network;
      await page.evaluate(value=>{localStorage["rtk-language"]=value;location.hash="status";},lang);
      await page.reload();await page.waitForSelector(".measure-help:not([hidden])");
      for(const width of [320,360,390,480,1280]){
        await page.setViewportSize({width,height:800});await page.waitForTimeout(150);
        const m=await page.evaluate(()=>{const card=document.querySelector(".measure-help"),button=card.querySelector("button"),bar=document.querySelector(".field-actions").getBoundingClientRect(),
          small=document.querySelector("#live-state small"),text=small.textContent,range=document.createRange();range.setStart(small.firstChild,0);range.setEnd(small.firstChild,text.split(" · ")[0].length);
          const word=range.getBoundingClientRect(),box=card.getBoundingClientRect(),b=button.getBoundingClientRect();
          return{overflow:document.documentElement.scrollWidth>innerWidth,cardOverflow:card.scrollWidth>card.clientWidth+1,right:box.right,innerWidth,buttonHeight:b.height,buttonRight:b.right,
            barTop:bar.top,barBottom:bar.bottom,innerHeight,wordWidth:word.width,wordRight:word.right,smallRight:small.getBoundingClientRect().right};});
        const label=`${lang} ${width}`;
        assert(!m.overflow,label+": no horizontal page overflow");assert(!m.cardOverflow,label+": card content fits");
        assert(m.right<=m.innerWidth+0.5&&m.buttonRight<=m.right+0.5,label+": card and action inside the viewport");
        assert(m.buttonHeight>=47.5,label+": action height "+m.buttonHeight);
        assert(m.barTop>=0&&m.barBottom<=m.innerHeight+0.5,label+": action bar inside the viewport");
        assert(m.wordWidth>0&&m.wordRight<=m.smallRight+0.5,label+": the link word of the pill is not clipped");
        if(process.env.RTK_SHOT_DIR&&(width===320||width===1280))await page.screenshot({path:`${process.env.RTK_SHOT_DIR}/cause-${lang}-${width}.png`});
      }
      if(lang==="de")for(const c of CASES)await check(c,"de");
    }
  }finally{
    captureState=null;actionAnswers={};live.network=saved.network;if(saved.network===undefined)delete live.network;
    live.current_fix=saved.fix;live.current_quality.accepted=saved.accepted;clearVerdict();live.active_line=saved.line;live.measurement_progress=saved.progress;
    live.active_project=saved.project;if(saved.undo===undefined)delete live.undo_candidate;else live.undo_candidate=saved.undo;
    await page.setViewportSize({width:360,height:800});
    await page.evaluate(()=>{localStorage.setItem("rtk-language","en");location.hash="status";});
    await page.reload();await page.waitForSelector(".readiness-hero");
  }
}
// Why-not sheet (#21c Task 8, --feedback-only): step by step from the app link to the accuracy, cause marker, live updates, widths.
async function testWhyNot(page){
  const i18n=require(root+"/i18n.js"),en=i18n.dictionaries.en,deDict=i18n.dictionaries.de;
  const nextPoll=async()=>{await page.waitForResponse(response=>response.url().endsWith("/api/ui/live")&&response.status()===200);await page.waitForTimeout(300);};
  const saved={fix:structuredClone(live.current_fix),quality:live.current_quality,accepted:live.current_quality.accepted,line:live.active_line,network:live.network,
    progress:structuredClone(live.measurement_progress),project:structuredClone(live.active_project),gst:live.gnss_accuracy,undo:live.undo_candidate};
  const net=(state,config,code)=>({ntrip_state:state,ntrip_config:config,last_ntrip_error_code:code,access_state:"ONLINE"});
  const shiftClock=ms=>page.evaluate(value=>{window.__rtkClockShift+=value;},ms);
  const setGate=(reasons,network)=>{
    live.current_fix=Object.assign({},saved.fix,{qual:1,fix_status_text:"GPS_FIX",stale:false,correction_age_sec:1.2,fix_seq:(live.current_fix.fix_seq||0)+50});
    live.current_quality=saved.quality;live.current_quality.accepted=reasons.length===0;setVerdict(reasons);
    live.gnss_accuracy={state:"receiving",attempts:0};live.network=network;
  };
  const labelRe=new RegExp("^("+[en,deDict].map(dict=>dict["ui.why_open"].replace(/[.*+?^${}()|[\]\\]/g,"\\$&")).join("|")+")$");
  const button=()=>page.locator(".measure-help").getByRole("button",{name:labelRe});
  const sheet=page.locator("dialog.why-sheet");
  const steps=()=>page.evaluate(()=>[...document.querySelectorAll("dialog.why-sheet li.why-step")].map(item=>({step:item.dataset.step,state:item.dataset.state,
    current:item.getAttribute("aria-current"),cause:item.classList.contains("cause"),text:item.textContent,buttons:[...item.querySelectorAll("button")].map(node=>node.textContent),
    mark:item.querySelector(".why-mark").textContent,markHidden:item.querySelector(".why-mark").getAttribute("aria-hidden")})));
  const causeOf=async()=>(await steps()).filter(item=>item.current==="step").map(item=>item.step);
  const waitCause=(step,why)=>page.waitForFunction(step=>{const node=document.querySelector('dialog.why-sheet li.why-step[aria-current="step"]');return node&&node.dataset.step===step;},step,{timeout:2500})
    .catch(async()=>assert.fail(`${why}: expected the cause at ${step}, got ${JSON.stringify(await steps())}`));
  const open=async()=>{await button().click();await sheet.waitFor({state:"visible"});};
  try{
    await page.setViewportSize({width:360,height:800});
    await setLanguage(page,"en");
    live.active_line=null;live.measurement_progress={active:false,collected:0,total:5};delete live.tracking_startup;
    console.log("feedback: why-not sheet #21c");
    // Projects before Measure, so the history check after the sheet action has a known predecessor.
    await go(page,"projects",".project-list");
    await go(page,"measure",".readiness-hero");
    // Switched off NTRIP behind a rejected gate: the cause is the caster, the accuracy step fails as well.
    setGate(["rtk_fixed_required"],net("disabled","switched_off",null));await nextPoll();
    assert.strictEqual(await button().count(),1,"the button is on the measuring screen");
    assert(await button().isVisible(),"the button is visible while the gate blocks");
    await open();
    let list=await steps();
    assert.deepStrictEqual(list.map(item=>item.step),["app","receiver","wifi","caster","corrections","accuracy"],"six steps in order");
    assert.deepStrictEqual(list.map(item=>item.state),["ok","ok","ok","off","off","fail"],"states "+JSON.stringify(list));
    assert.deepStrictEqual(await causeOf(),["caster"],"exactly one step is the cause");
    assert(list[3].cause&&!list[5].cause,"only the cause carries the class");
    assert.deepStrictEqual(list.map(item=>item.mark),["✓","✓","✓","⊘","⊘","×"],"marks");
    assert(list.every(item=>item.markHidden==="true"),"marks are decoration");
    assert(list[3].text.includes(en["ui.cause_off_text"]),"caster text: "+list[3].text);
    assert(list[5].text.includes("RTK FIXED"),"accuracy text names the fix reason: "+list[5].text);
    assert.deepStrictEqual(list[3].buttons,["Open NTRIP settings"],"the caster step offers its action");
    assert.strictEqual(await sheet.getAttribute("aria-labelledby"),"why-title","the sheet is labelled");
    assert.strictEqual(await sheet.locator("h2#why-title").innerText(),en["ui.why_title"]);
    await sheet.getByRole("button",{name:"Open NTRIP settings",exact:true}).click();
    await page.waitForFunction(()=>location.hash==="#device/connections/internet"&&!document.querySelector("dialog.why-sheet"),null,{timeout:3000});
    // The closed sheet leaves no history entry behind: Back returns to Measure, a second Back to the page before it.
    await page.waitForSelector('[data-group="internet"] .configuration-layout');
    assert.strictEqual(await page.evaluate(()=>Boolean(history.state&&history.state.rtkSheet)),false,"the sheet action lands on an untagged entry");
    await page.goBack();
    await page.waitForFunction(()=>location.hash==="#measure");
    await page.waitForSelector(".readiness-hero");
    await page.goBack({timeout:5000}).catch(()=>null);
    await page.waitForFunction(()=>location.hash==="#projects",null,{timeout:3000})
      .catch(async()=>assert.fail("the second Back after the sheet action stays on "+await page.evaluate(()=>location.hash)+" (closed sheet left a history entry)"));
    await page.waitForSelector(".project-list");
    // A live gate change moves the cause while the sheet is open (store subscription, no extra poll).
    await go(page,"status",".readiness-hero");
    setGate(["hdop_too_high"],net("streaming","ready",null));await nextPoll();
    await open();
    await waitCause("receiver","hdop");
    const reason=await page.locator('.condition[data-gate="satellites"] .condition-reason').first().innerText();
    list=await steps();
    assert(reason.length>0&&list[1].text.includes(reason),`receiver step carries the gate reason "${reason}": ${list[1].text}`);
    assert.deepStrictEqual(list.map(item=>item.state),["ok","fail","ok","ok","ok","ok"],"hdop states");
    assert.deepStrictEqual(list[3].buttons,[],"no action without a cause");
    setGate(["corrections_too_old"],net("streaming","ready",null));
    await waitCause("corrections","corrections too old");
    setGate(["rtk_fixed_required"],net("streaming","ready",null));
    await waitCause("accuracy","fix");
    setGate([],net("streaming","ready",null));
    await page.waitForFunction(()=>[...document.querySelectorAll("dialog.why-sheet li.why-step")].every(item=>item.dataset.state==="ok"),null,{timeout:2500})
      .catch(async()=>assert.fail("all steps ok with an open gate: "+JSON.stringify(await steps())));
    assert.deepStrictEqual(await causeOf(),[],"no cause when everything is fine");
    // #21d: without a correction age the corrections step is unknown, never the cause.
    live.current_fix=Object.assign({},live.current_fix,{correction_age_sec:null,fix_seq:(live.current_fix.fix_seq||0)+1});
    await page.waitForFunction(()=>document.querySelector('dialog.why-sheet li.why-step[data-step="corrections"]').dataset.state==="unknown",null,{timeout:2500})
      .catch(async()=>assert.fail("unknown correction age: "+JSON.stringify(await steps())));
    assert.deepStrictEqual((await steps()).map(item=>item.state),["ok","ok","ok","ok","unknown","ok"],"unknown correction age states");
    assert.deepStrictEqual(await causeOf(),[],"an unknown correction age is no cause");
    live.current_fix=Object.assign({},live.current_fix,{correction_age_sec:1.2,fix_seq:(live.current_fix.fix_seq||0)+1});
    await page.keyboard.press("Escape");
    await sheet.waitFor({state:"detached"});
    // The sheet follows the transport: lost within the ticker second, ok again with the first fresh answer.
    setGate(["hdop_too_high"],net("streaming","ready",null));await nextPoll();
    await open();
    await waitCause("receiver","before the loss");
    blockLive();
    try{
      await shiftClock(11000);
      await waitCause("app","loss");
      list=await steps();
      assert.deepStrictEqual(list.map(item=>item.state),["fail","unknown","unknown","unknown","unknown","unknown"],"lost states "+JSON.stringify(list));
      assert(list[0].text.includes(en["ui.live_lost_title"].replace(" for {seconds} s","")),"app text: "+list[0].text);
      assert(list.slice(1).every(item=>item.text.includes(en["ui.why_no_data"])),"no data text");
      assert.deepStrictEqual(list.slice(1).map(item=>item.mark),["–","–","–","–","–"],"unknown marks");
    }finally{releaseLive();}
    await waitCause("receiver","after the loss");
    list=await steps();
    assert.strictEqual(list[0].state,"ok","app step ok again");
    await page.keyboard.press("Escape");await sheet.waitFor({state:"detached"});
    // A missing device verdict is a waiting state and the cause (r2-8).
    setGate([],net("streaming","ready",null));live.current_quality=null;await nextPoll();
    assert(await button().isVisible(),"the button shows while the verdict is missing");
    await open();
    await waitCause("accuracy","missing verdict");
    list=await steps();
    assert.strictEqual(list[5].state,"warn");
    assert(list[5].text.includes(en["gate.no_verdict"]),"waiting text: "+list[5].text);
    await page.keyboard.press("Escape");await sheet.waitFor({state:"detached"});
    // Gate open: the button and the card are hidden.
    setGate([],net("streaming","ready",null));await nextPoll();
    assert.strictEqual(await button().isVisible(),false,"no button while the gate is open");
    assert.strictEqual(await page.locator(".measure-help").isVisible(),false,"no card while the gate is open");
    // German and the widths: no overflow, the sheet scrolls on a short screen, Escape closes it.
    console.log("feedback: why-not sheet widths #21c");
    setGate(["hdop_too_high","rtk_fixed_required"],net("disabled","switched_off",null));
    for(const lang of ["en","de"]){
      await setLanguage(page,lang);await nextPoll();
      const dict=lang==="en"?en:deDict;
      assert.strictEqual(await button().innerText(),dict["ui.why_open"],lang+": button label");
      for(const [width,height] of [[320,400],[360,800],[1280,800]]){
        await page.setViewportSize({width,height});await page.waitForTimeout(150);
        await open();
        const label=`${lang} ${width}x${height}`;
        assert.strictEqual(await sheet.locator("h2#why-title").innerText(),dict["ui.why_title"],label+": title");
        const m=await page.evaluate(()=>{const dialog=document.querySelector("dialog.why-sheet"),box=dialog.getBoundingClientRect(),
          wide=[...dialog.querySelectorAll("li.why-step")].filter(item=>item.scrollWidth>item.clientWidth+1).length,
          buttons=[...dialog.querySelectorAll("button")].map(node=>node.getBoundingClientRect()).map(rect=>({h:rect.height,r:rect.right}));
          dialog.scrollTop=dialog.scrollHeight;
          return{pageOverflow:document.documentElement.scrollWidth>innerWidth,dialogOverflow:dialog.scrollWidth>dialog.clientWidth+1,wide,left:box.left,right:box.right,bottom:box.bottom,top:box.top,innerWidth,innerHeight,
            scrolls:dialog.scrollHeight>dialog.clientHeight+1,scrolled:dialog.scrollTop,overflowY:getComputedStyle(dialog).overflowY,buttons};});
        assert(!m.pageOverflow,label+": no horizontal page overflow");
        assert(!m.dialogOverflow&&m.wide===0,label+": the steps fit the sheet");
        assert(m.left>=-0.5&&m.right<=m.innerWidth+0.5&&m.top>=-0.5&&m.bottom<=m.innerHeight+0.5,label+": the sheet is inside the viewport "+JSON.stringify(m));
        assert(m.buttons.every(rect=>rect.h>=47.5),label+": targets are at least 48 px "+JSON.stringify(m.buttons));
        if(height===400){assert(m.scrolls&&["auto","scroll"].includes(m.overflowY),label+": the sheet scrolls "+JSON.stringify(m));assert(m.scrolled>0,label+": it really scrolls");}
        if(process.env.RTK_SHOT_DIR&&(width===320||width===1280))await page.screenshot({path:`${process.env.RTK_SHOT_DIR}/why-${lang}-${width}.png`});
        await page.keyboard.press("Escape");await sheet.waitFor({state:"detached"});
      }
    }
  }finally{
    releaseLive();clearVerdict();
    live.current_quality=saved.quality;live.current_quality.accepted=saved.accepted;live.current_fix=saved.fix;live.gnss_accuracy=saved.gst;if(saved.gst===undefined)delete live.gnss_accuracy;
    live.network=saved.network;if(saved.network===undefined)delete live.network;live.active_line=saved.line;live.measurement_progress=saved.progress;live.active_project=saved.project;
    if(saved.undo===undefined)delete live.undo_candidate;else live.undo_candidate=saved.undo;
    await page.setViewportSize({width:360,height:800});
    await page.evaluate(()=>{localStorage.setItem("rtk-language","en");location.hash="status";});
    await page.reload();await page.waitForSelector(".readiness-hero");
  }
}
// Connected journey (#21c Task 9, --feedback-only): reliable measuring from a blocked gate to the result of a capture, EN/DE at 360 and 1280 px.
async function testJourney(page){
  const i18n=require(root+"/i18n.js"),en=i18n.dictionaries.en,deDict=i18n.dictionaries.de,v2de=i18n.v2de;
  const nextPoll=async()=>{await page.waitForResponse(response=>response.url().endsWith("/api/ui/live")&&response.status()===200);await page.waitForTimeout(300);};
  const saved={fix:structuredClone(live.current_fix),quality:live.current_quality,accepted:live.current_quality.accepted,line:live.active_line,network:live.network,gst:live.gnss_accuracy,
    progress:structuredClone(live.measurement_progress),project:structuredClone(live.active_project),undo:live.undo_candidate,capacity:structuredClone(live.capacity),
    projects:structuredClone(tracking.projects),startup:live.tracking_startup};
  const shotDir=process.env.RTK_SHOT_DIR;if(shotDir)fs.mkdirSync(shotDir,{recursive:true});
  const shiftClock=ms=>page.evaluate(value=>{window.__rtkClockShift+=value;},ms);
  const net=(state,config,code)=>({ntrip_state:state,ntrip_config:config,last_ntrip_error_code:code,access_state:"ONLINE"});
  const setGate=(reasons,network,qual)=>{
    live.current_fix=Object.assign({},saved.fix,{qual,fix_status_text:qual===4?"RTK_FIXED":"GPS_FIX",stale:false,fix_seq:(live.current_fix.fix_seq||0)+50});
    live.current_quality=saved.quality;live.current_quality.accepted=reasons.length===0;setVerdict(reasons);
    live.gnss_accuracy={state:"receiving",attempts:0};live.network=network;
  };
  const setCapacity=used=>{live.capacity={used,limit:5000,remaining:5000-used,percent:Math.floor(used*100/5000),writable:true,
    level:used>=5000?"full":used>=4900?"critical":used>=4500?"warning":used>=4000?"notice":"ok"};};
  const resetCapture=()=>{actionAnswers={};liveAnswers=[];checkAnswers=[];captureState={epoch:"a1b2c3d4",stack:[],order:40};trackingPostLog.length=0;
    live.measurement_progress={active:false,collected:0,total:5};delete live.tracking_startup;live.active_line=null;
    live.active_project={id:"p1",name:"Project",line_count:1,point_count:31};projectRow().point_count=31;syncCandidate();};
  const STRIP=".field-actions > .capture-strip";
  // Announcement records: both regions are observed for the whole case; a step reads and resets them.
  const record=()=>page.evaluate(()=>{window.__jAnn=[];window.__jLive=[];const watch=(id,list)=>{const node=document.getElementById(id);
    new MutationObserver(()=>{if(node.textContent)window[list].push(node.textContent);}).observe(node,{childList:true,characterData:true,subtree:true});};
    watch("rtk-announcer","__jAnn");watch("rtk-feedback-live","__jLive");});
  const reset=()=>page.evaluate(()=>{window.__jAnn=[];window.__jLive=[];});
  const heard=()=>page.evaluate(()=>({hero:window.__jAnn.slice(),live:window.__jLive.slice()}));
  const waitHero=(cls,timeout=2500)=>page.waitForFunction(value=>document.querySelector(".readiness-hero").classList.contains(value),cls,{timeout});
  const item=()=>page.evaluate(selector=>{const node=document.querySelector(selector+' .feedback-item[data-key="capture"]');if(!node)return null;
    return{kind:[...node.classList].find(name=>name!=="feedback-item"),text:node.querySelector(".feedback-text").textContent,reasons:[...node.querySelectorAll(".feedback-reasons li")].map(li=>li.textContent),
      actions:[...node.querySelectorAll(".feedback-actions button")].map(button=>({text:button.textContent,disabled:button.disabled}))};},STRIP);
  const waitItem=(kind,text,label)=>page.waitForFunction(([selector,kind,text])=>{const node=document.querySelector(selector+' .feedback-item[data-key="capture"]');
    return Boolean(node)&&node.classList.contains(kind)&&node.querySelector(".feedback-text").textContent===text;},[STRIP,kind,text],{timeout:8000})
    .catch(async()=>assert.fail(`${label}: capture entry ${kind} "${text}" expected, got ${JSON.stringify(await item())}`));
  const clickBar=name=>page.evaluate(name=>{const button=[...document.querySelectorAll(".field-actions > button")].find(node=>node.textContent===name);if(!button)throw new Error("bar button "+name+" missing");button.click();},name);
  const clickStrip=name=>page.evaluate(([selector,name])=>{const button=[...document.querySelectorAll(selector+" button")].find(node=>node.textContent===name);if(!button)throw new Error("strip button "+name+" missing");button.click();},[STRIP,name]);
  const settleFrames=()=>page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
  // (6) Nothing covers the hero or the project row, the action bar stays in the view and above the navigation, the dock is empty on the measuring screen.
  const uncovered=async label=>{
    await page.evaluate(()=>scrollTo({top:0,behavior:"instant"}));await settleFrames();
    for(const selector of [".readiness-hero",".status-project"])assert.deepStrictEqual(await paintProblems(page,selector),[],`${label}: ${selector}`);
    const bar=await page.evaluate(()=>{const r=document.querySelector(".field-actions").getBoundingClientRect(),nav=document.querySelector(".mobile-nav"),
      navTop=getComputedStyle(nav).display!=="none"?nav.getBoundingClientRect().top:innerHeight;return{top:r.top,bottom:r.bottom,navTop,innerHeight,left:r.left,right:r.right,innerWidth,overflow:document.documentElement.scrollWidth>innerWidth,
        dock:document.querySelectorAll("#feedback-dock .feedback-item").length};});
    assert(bar.top>=0&&bar.bottom<=bar.innerHeight+0.5&&bar.bottom<=bar.navTop+0.5,`${label}: action bar in the view and above the navigation ${JSON.stringify(bar)}`);
    assert(bar.left>=0&&bar.right<=bar.innerWidth+0.5&&!bar.overflow,`${label}: no horizontal overflow ${JSON.stringify(bar)}`);
    assert.strictEqual(bar.dock,0,`${label}: the dock stays empty on the measuring screen`);
  };
  const heroNote=async(label,max)=>{const got=await heard();assert(got.hero.length<=max,`${label}: at most ${max} hero announcement(s), got ${JSON.stringify(got.hero)}`);return got;};
  // Per message exactly one text in the feedback region (the region is reset before the triggering action).
  const liveOnce=async(text,label)=>{const got=await heard();assert.strictEqual(got.live.filter(value=>value===text||value.startsWith(text+". ")).length,1,`${label}: "${text}" announced once, got ${JSON.stringify(got.live)}`);};
  const fill=(template,values)=>template.replace(/\{(\w+)\}/g,(_,key)=>values[key]);
  try{
    for(const lang of ["en","de"])for(const size of [{width:360,height:800},{width:1280,height:800}]){
      const label=`${lang} ${size.width}`,dict=lang==="de"?deDict:en,vt=text=>lang==="de"?v2de[text]:text;
      console.log("feedback: journey "+label);
      // Mobile warning situation: 82 % of the capacity is used (quiet on the measuring screen).
      setCapacity(4100);resetCapture();setGate(["rtk_fixed_required"],net("disabled","switched_off",null),1);
      await page.setViewportSize(size);
      await setLanguage(page,lang);
      await page.waitForSelector(".measure-help:not([hidden])");
      await record();
      // 1. Device reachable, gate blocked: server reason in the hero, help card with cause and "Why not?", pill token, nothing above hero or project row.
      await waitHero("blocked");
      assert.strictEqual(await page.locator(".hero-state p").innerText(),fill(dict["gate.rtk_fixed_required"],{fix:"GPS FIX"}),`${label}: the hero names the server reason`);
      const card=await page.evaluate(()=>{const c=document.querySelector(".measure-help");return{text:c.querySelector(".cause-text").textContent,buttons:[...c.querySelectorAll("button")].map(b=>b.textContent),pill:document.querySelector("#live-state small").textContent};});
      assert.strictEqual(card.text,dict["ui.cause_off_text"],`${label}: card cause`);
      assert(card.buttons.includes(dict["ui.why_open"]),`${label}: card offers "Why not?" ${JSON.stringify(card.buttons)}`);
      assert(card.pill.includes(dict["ui.cause_off_short"]),`${label}: pill token ${card.pill}`);
      assert.strictEqual(await page.locator(".capture-strip .feedback-item").count(),0,`${label}: no entry in the strip`);
      assert.strictEqual(await page.locator('.feedback-item[data-key="capacity"]').count(),0,`${label}: capacity warning is quiet on the measuring screen`);
      await uncovered(label+" 1 blocked");
      if(shotDir)await page.screenshot({path:`${shotDir}/journey-${lang}-${size.width}-1-blocked.png`});
      await page.waitForTimeout(2500);await reset();await page.waitForTimeout(1500);
      await heroNote(label+" 1 settled",0);
      // 2. Ready: the card hides, exactly one hero announcement.
      await reset();
      setGate([],net("streaming","ready",null),4);
      await nextPoll();await waitHero("ready");
      await page.waitForFunction(()=>document.querySelector(".measure-help").hidden,null,{timeout:2500});
      await page.waitForTimeout(2500);
      assert.strictEqual((await heroNote(label+" 2 ready",1)).hero.length,1,`${label}: exactly one hero announcement for the new state`);
      await uncovered(label+" 2 ready");
      // 3. Connection lost: stale-critical, actions locked, strip empty.
      await reset();
      blockLive();
      try{
        await shiftClock(11000);
        await waitHero("stale-critical");
        const bar=await page.evaluate(()=>[...document.querySelectorAll(".field-actions button")].map(b=>b.disabled));
        assert(bar.length>0&&bar.every(Boolean),`${label}: every action is disabled ${JSON.stringify(bar)}`);
        assert.strictEqual(await page.locator(".capture-strip .feedback-item").count(),0,`${label}: strip stays empty`);
        await uncovered(label+" 3 lost");
        await page.waitForTimeout(1500);
        await heroNote(label+" 3 lost",1);
      }finally{releaseLive();}
      // 4. Recovery: ready again within a tick.
      await reset();
      await waitHero("ready",2500);
      await page.waitForFunction(()=>[...document.querySelectorAll(".field-actions > button")].some(b=>!b.disabled),null,{timeout:2500});
      await page.waitForTimeout(2000);
      await heroNote(label+" 4 recovered",1);
      await uncovered(label+" 4 recovered");
      // 5. Capture and result.
      const assetFlow=async()=>{await clickBar(vt("Object point"));await page.locator("dialog.asset-sheet").waitFor();
        await page.locator('dialog.asset-sheet input[name="name"]').fill("Valve 21c");
        await page.locator("dialog.asset-sheet").getByRole("button",{name:vt("Save point")}).click();};
      const savedText=vt("Object point saved")+" · Valve 21c",undoLabel=dict["ui.undo_point"],undoOf=entry=>entry&&entry.actions.find(action=>action.text===undoLabel);
      await reset();
      actionAnswers.add_asset={delay:1500};
      await assetFlow();
      await waitItem("running",dict["ui.capture_saving_asset"],label+" 5a");
      // The measurement sheet stays open over the page while the point is saved (#24-UI Task 8, R2: the closing asset
      // sheet's history.back() no longer closes it); the page is checked again once the result is in.
      assert.strictEqual(await page.locator("dialog.measurement-sheet[open]").count(),1,label+" 5a running: measurement sheet open");
      await waitItem("ok",savedText,label+" 5a");
      await page.waitForSelector("dialog.measurement-sheet",{state:"detached"});
      assert(undoOf(await item())&&!undoOf(await item()).disabled,`${label}: Undo offered`);
      await liveOnce(savedText,label+" 5a");
      await uncovered(label+" 5a saved");
      // Undo within 6 s: one POST with expect.
      const target=serverUndoCandidate().label;
      trackingPostLog.length=0;await reset();
      await clickStrip(undoLabel);
      const removedText=fill(vt("{label} removed"),{label:target});
      await waitItem("ok",removedText,label+" 5b");
      await page.waitForTimeout(400);
      assert.deepStrictEqual(trackingPostLog.map(post=>post.action),["undo"],`${label}: exactly one undo POST`);
      assert(trackingPostLog[0].expect&&trackingPostLog[0].expect.feature_id,`${label}: the undo carries the expected point ${JSON.stringify(trackingPostLog[0])}`);
      await liveOnce(removedText,label+" 5b");
      await uncovered(label+" 5b removed");
      // Refusal with reasons: critical, stays.
      await reset();
      actionAnswers.add_asset={status:400,body:{error:"invalid_tracking_request",message:"Changed text.",reasons:["gst_required"]}};
      await assetFlow();
      await waitItem("critical",vt("Measurement could not be completed."),label+" 5c");
      assert.deepStrictEqual((await item()).reasons,[dict["quality.gst_required"]],`${label}: refusal reasons`);
      await uncovered(label+" 5c refused");
      await page.waitForTimeout(2500);
      assert.strictEqual((await item()).kind,"critical",`${label}: the refusal stays`);
      await liveOnce(vt("Measurement could not be completed."),label+" 5c");
      // Lost answer, mutation applied: checking, then confirmed with Undo.
      await reset();
      actionAnswers.add_asset={apply:true,abort:true};
      checkAnswers=[{delay:1500}];
      await assetFlow();
      await waitItem("running",dict["ui.capture_checking"],label+" 5d");
      await uncovered(label+" 5d checking");
      await waitItem("ok",dict["ui.capture_confirmed"]+" · Valve 21c",label+" 5d");
      assert(undoOf(await item())&&!undoOf(await item()).disabled,`${label}: confirmed result offers Undo`);
      await liveOnce(dict["ui.capture_confirmed"]+" · Valve 21c",label+" 5d");
      await uncovered(label+" 5d confirmed");
      // Lost answer, nothing stored, a foreign point: unclear, no Undo.
      await reset();
      actionAnswers.add_asset={apply:false,abort:true,delay:800};
      await assetFlow();
      addForeignPoint("B1");
      await waitItem("warn",dict["ui.capture_unclear"],label+" 5e");
      assert(!undoOf(await item()),`${label}: no Undo for an unclear result`);
      await liveOnce(dict["ui.capture_unclear"],label+" 5e");
      await uncovered(label+" 5e unclear");
      if(shotDir)await page.screenshot({path:`${shotDir}/journey-${lang}-${size.width}-5-unclear.png`});
      // 7. Over the whole journey the hero never spoke twice for one state change (checked per step above); the dock stayed empty.
      await page.evaluate(()=>{for(const button of document.querySelectorAll(".capture-strip [data-feedback-dismiss]"))button.click();});
    }
  }finally{
    releaseLive();releasePolls();captureState=null;actionAnswers={};liveAnswers=[];checkAnswers=[];
    live.network=saved.network;if(saved.network===undefined)delete live.network;live.gnss_accuracy=saved.gst;if(saved.gst===undefined)delete live.gnss_accuracy;
    live.current_fix=saved.fix;live.current_quality=saved.quality;live.current_quality.accepted=saved.accepted;clearVerdict();live.active_line=saved.line;live.measurement_progress=saved.progress;
    live.active_project=saved.project;live.capacity=saved.capacity;tracking.projects=saved.projects;
    if(saved.undo===undefined)delete live.undo_candidate;else live.undo_candidate=saved.undo;
    if(saved.startup===undefined)delete live.tracking_startup;else live.tracking_startup=saved.startup;
    await page.setViewportSize({width:360,height:800});
    await setLanguage(page,"en");
  }
}
// Storage reserve: gate, status row, capacity panel and device summary share one 20 % rule (#17).
async function testStorageReserve(page){
  const board=status.board,original=board.fs_free_bytes;
  await page.evaluate(()=>{localStorage.setItem("rtk-language","en");});
  try{
    for(const [free,low] of [[150000,true],[250000,false],[600000,false]]){
      board.fs_free_bytes=free;
      await page.reload();
      await page.waitForSelector(".readiness-hero");
      await page.waitForFunction(()=>document.querySelector('[data-context="storage"] b').textContent!=="");
      const row=await page.locator('[data-context="storage"]').evaluate(node=>node.closest(".context-row").classList.contains("warn"));
      assert.strictEqual(row,low,`status storage row at ${free}`);
      await go(page,"device",".device-metric");
      await page.waitForFunction(()=>document.querySelectorAll(".device-metric")[3].querySelector("small").textContent!=="");
      const text=await page.locator(".device-metric").nth(3).locator("small").innerText();
      assert.strictEqual(text.includes("Storage is running low"),low,`device summary at ${free}: ${text}`);
      await page.evaluate(()=>{location.hash="status";});
    }
  }finally{board.fs_free_bytes=original;}
}
// Accessibility audits (#17); Tasks 6 and 8 reuse them. SVG content is out of scope here.
// Visible HTML targets below 48 px: [selector, text, width, height].
async function auditTargets(page){
  return page.evaluate(()=>{
    const describe=el=>el.tagName.toLowerCase()+(el.id?"#"+el.id:"")+[...el.classList].map(name=>"."+name).join("");
    const found=[];
    for(const el of document.querySelectorAll("button,a,[role=button],summary,select,input:not([type=hidden])")){
      if(el.closest("svg"))continue;
      const rect=el.getBoundingClientRect();
      if(!rect.width||!rect.height||!el.checkVisibility())continue;
      if(rect.width<47.5||rect.height<47.5)found.push([describe(el),(el.textContent||el.getAttribute("aria-label")||el.name||"").trim().slice(0,30),Math.round(rect.width*10)/10,Math.round(rect.height*10)/10]);
    }
    return found;
  });
}
// WCAG contrast of visible text against the first opaque ancestor background (opacity folded in).
const CONTRAST_SCOPE="#app-main *,.app-header *,.mobile-nav *,.app-sidebar *,dialog *,.feedback-item *";
async function auditContrast(page,scope=CONTRAST_SCOPE){
  return page.evaluate(scope=>{
    const parse=value=>{
      let match=/^rgba?\(([^)]+)\)$/.exec(value);
      if(match){const parts=match[1].split(/[ ,/]+/).filter(Boolean).map(Number);return [parts[0],parts[1],parts[2],parts.length>3?parts[3]:1];}
      match=/^color\(srgb ([^)]+)\)$/.exec(value);
      if(match){const parts=match[1].split(/[ /]+/).filter(Boolean).map(Number);return [parts[0]*255,parts[1]*255,parts[2]*255,parts.length>3?parts[3]:1];}
      return null;
    };
    const luminance=([r,g,b])=>[r,g,b].map(channel=>{const v=channel/255;return v<=.03928?v/12.92:((v+.055)/1.055)**2.4;}).reduce((sum,v,index)=>sum+v*[.2126,.7152,.0722][index],0);
    const blend=(top,bottom,alpha)=>top.slice(0,3).map((v,index)=>v*alpha+bottom[index]*(1-alpha));
    const background=el=>{for(let node=el;node;node=node.parentElement){const color=parse(getComputedStyle(node).backgroundColor);if(color&&color[3]>.5)return color.slice(0,3);}return [255,255,255];};
    const describe=el=>el.tagName.toLowerCase()+[...el.classList].map(name=>"."+name).join("");
    const found=[];let checked=0;
    for(const el of document.querySelectorAll(scope)){
      if(el.closest("svg,[aria-hidden=true]")||el.closest(":disabled,[aria-disabled=true]"))continue;
      if(![...el.childNodes].some(node=>node.nodeType===3&&node.textContent.trim()))continue;
      if(!el.checkVisibility()||!el.getBoundingClientRect().width)continue;
      const style=getComputedStyle(el),color=parse(style.color);
      if(!color)continue;
      checked++;
      let opacity=1;
      for(let node=el;node;node=node.parentElement)opacity*=Number(getComputedStyle(node).opacity);
      const base=background(el),foreground=blend(color,base,color[3]*opacity);
      const light=Math.max(luminance(foreground),luminance(base)),dark=Math.min(luminance(foreground),luminance(base));
      const ratio=(light+.05)/(dark+.05),size=parseFloat(style.fontSize),large=size>=24||(size>=18.66&&Number(style.fontWeight)>=700);
      if(ratio<(large?3:4.5))found.push([describe(el),el.textContent.trim().slice(0,30),Math.round(ratio*100)/100]);
    }
    window.__contrastChecked=checked;
    return found;
  },scope);
}
// The sign-in page (ui.APP_V2_LOGIN) in its own page, served from disk with a rejecting /api/session.
async function openLoginPage(browser,{lang="en",theme=null,scheme="light",viewport={width:390,height:800}}={}){
  const loginHtml=require("child_process").execFileSync("python3",["-c","import ui,sys;sys.stdout.write(ui.APP_V2_LOGIN)"],{cwd:root,encoding:"utf8"});
  const login=await browser.newPage({viewport}),errors=[];
  login.on("pageerror",error=>errors.push(error.message));
  await login.addInitScript(([language,name])=>{localStorage.setItem("rtk-language",language);if(name)localStorage.setItem("rtk-theme",name);},[lang,theme]);
  await login.emulateMedia({colorScheme:scheme});
  await login.route("http://login.test/**",route=>{
    const path=new URL(route.request().url()).pathname;
    if(path==="/")return route.fulfill({contentType:"text/html",body:loginHtml});
    if(path==="/api/session")return route.fulfill({status:401,contentType:"text/plain",body:"Sign-in failed"});
    if(["/app.js","/i18n.js","/app.css"].includes(path))return route.fulfill({contentType:path.endsWith(".js")?"application/javascript":"text/css",body:fs.readFileSync(root+path,"utf8")});
    return route.fulfill({status:404,body:""});
  });
  await login.goto("http://login.test/");
  await login.waitForSelector("#v2-login-form");
  return {login,errors};
}
// Elements that show no outline of at least 2 px after Tab, for `count` presses from the page top.
async function auditFocus(page,count){
  await page.evaluate(()=>{window.scrollTo(0,0);if(document.activeElement)document.activeElement.blur();});
  const found=[];
  for(let index=0;index<count;index++){
    await page.keyboard.press("Tab");
    const result=await page.evaluate(()=>{
      const el=document.activeElement;if(!el||el===document.body)return null;
      const style=getComputedStyle(el),visible=style.outlineStyle!=="none"&&parseFloat(style.outlineWidth)>=2;
      return visible?null:[el.tagName.toLowerCase()+[...el.classList].map(name=>"."+name).join(""),(el.textContent||"").trim().slice(0,30)];
    });
    if(result)found.push(result);
  }
  return found;
}
// Map hit areas (#17): screen geometry of every point (shape centre) and every line (hit polyline in screen pixels).
async function mapGeometry(page){
  return page.evaluate(()=>{
    const view=document.querySelector("svg.survey-map").getBoundingClientRect();
    const points=[...document.querySelectorAll(".map-point")].map(node=>{const rect=node.querySelector(".map-shape").getBoundingClientRect();return {label:node.getAttribute("aria-label"),x:rect.x+rect.width/2,y:rect.y+rect.height/2};});
    const lines=[...document.querySelectorAll(".map-line")].map(node=>{
      const poly=node.querySelector("polyline.map-hit"),matrix=poly.getScreenCTM(),vertices=[];
      for(let index=0;index<poly.points.numberOfItems;index++){const item=poly.points.getItem(index),mapped=new DOMPoint(item.x,item.y).matrixTransform(matrix);vertices.push([mapped.x,mapped.y]);}
      return {label:node.getAttribute("aria-label"),vertices};
    });
    return {points,lines,view:{x:view.x,y:view.y,width:view.width,height:view.height},viewport:{width:innerWidth,height:innerHeight}};
  });
}
// Topmost map target (point or line aria-label) at a screen position; "other" for anything else, null outside the page.
const mapTargetAt=(page,x,y)=>page.evaluate(([px,py])=>{
  const element=document.elementFromPoint(px,py);
  if(!element)return null;
  const target=element.closest(".map-point,.map-line");
  return target?target.getAttribute("aria-label"):(element.closest(".map-position")?"position":"other:"+element.tagName+"."+element.className.toString());
},[x,y]);
// Every visible point hits itself in the centre and ±20 px away from the other points; every line hits itself
// on and ±20 px beside a stretch that is 48 px away from every point.
async function auditMapHits(page,context){
  const geometry=await mapGeometry(page),margin=4,inside=(x,y)=>x>=geometry.view.x+margin&&y>=geometry.view.y+margin&&x<=geometry.view.x+geometry.view.width-margin&&y<=geometry.view.y+geometry.view.height-margin&&x>=0&&y>=0&&x<=geometry.viewport.width&&y<=geometry.viewport.height;
  const visible=geometry.points.filter(point=>inside(point.x,point.y));
  assert(visible.length>=1,`${context}: no map point visible`);
  for(const point of visible){
    const others=geometry.points.filter(other=>other!==point),probes=[[0,0],[20,0],[-20,0],[0,20],[0,-20]].filter(([dx,dy])=>(dx===0&&dy===0)||others.every(other=>Math.hypot(point.x+dx-other.x,point.y+dy-other.y)>=48));
    for(const [dx,dy] of probes){
      if(!inside(point.x+dx,point.y+dy))continue;
      assert.strictEqual(await mapTargetAt(page,point.x+dx,point.y+dy),point.label,`${context}: point "${point.label}" probe ${dx},${dy}`);
    }
  }
  for(const line of geometry.lines){
    let site=null;
    for(let index=0;index+1<line.vertices.length&&!site;index++){
      const [x1,y1]=line.vertices[index],[x2,y2]=line.vertices[index+1],length=Math.hypot(x2-x1,y2-y1),nx=-(y2-y1)/length,ny=(x2-x1)/length;
      for(let step=0;step<=length&&!site;step+=4){
        const x=x1+(x2-x1)*step/length,y=y1+(y2-y1)*step/length,spots=[[x,y],[x+nx*20,y+ny*20],[x-nx*20,y-ny*20]];
        if(spots.every(([sx,sy])=>inside(sx,sy)&&geometry.points.every(point=>Math.hypot(sx-point.x,sy-point.y)>=48)))site=spots;
      }
    }
    assert(site,`${context}: no probe site on line "${line.label}"`);
    for(const [sx,sy] of site)assert.strictEqual(await mapTargetAt(page,sx,sy),line.label,`${context}: line "${line.label}" at ${Math.round(sx)},${Math.round(sy)}`);
  }
  return geometry;
}
const intersects=(a,b)=>a.x<b.x+b.width&&b.x<a.x+a.width&&a.y<b.y+b.height&&b.y<a.y+a.height;
// Nothing paints over an element (#45): the corners and the middle of its visible part, clipped by the phone navigation and the
// action bar, hit the element or one of its descendants. Returns the problems; an empty list means free.
const paintProblems=(page,selector)=>page.evaluate(selector=>{
  const node=document.querySelector(selector);
  if(!node||!node.checkVisibility())return["missing "+selector];
  const nav=document.querySelector(".mobile-nav"),bar=document.querySelector(".field-actions"),r=node.getBoundingClientRect();
  const navTop=nav&&nav!==node&&getComputedStyle(nav).display!=="none"?nav.getBoundingClientRect().top:innerHeight;
  const barTop=bar&&bar.getBoundingClientRect().top>r.top?bar.getBoundingClientRect().top:innerHeight;
  const x0=Math.max(r.left,0),x1=Math.min(r.right,innerWidth),y0=Math.max(r.top,0),y1=Math.min(r.bottom,navTop,barTop);
  if(x1<=x0||y1<=y0)return["not in view "+selector];
  const inset=Math.min(12,(x1-x0)/4,(y1-y0)/4),problems=[];
  for(const [fx,fy] of [[0,0],[1,0],[0,1],[1,1],[.5,.5]]){
    const x=fx===.5?(x0+x1)/2:fx?x1-inset:x0+inset,y=fy===.5?(y0+y1)/2:fy?y1-inset:y0+inset,hit=document.elementFromPoint(x,y);
    if(!hit||!node.contains(hit))problems.push(selector+" at "+Math.round(x)+","+Math.round(y)+" is covered by "+(hit?hit.tagName.toLowerCase()+"."+hit.className:"nothing"));
  }
  return problems;
},selector);
// Header sign-out button (#24-UI E10): 48 px target inside the viewport, header and page without sideways overflow,
// no overlap with the title or the live pill, and the pill keeps its whole word (#21b).
async function assertHeaderSignOut(page,label){
  const found=await page.evaluate(()=>{
    const button=document.querySelector("#sign-out"),header=document.querySelector(".app-header"),pill=document.querySelector("#live-state"),word=pill.querySelector("b"),
      b=button.getBoundingClientRect(),p=pill.getBoundingClientRect(),h=document.querySelector("#view-title").getBoundingClientRect(),t=button.querySelector(".header-action-label").getBoundingClientRect(),
      overlaps=(a,c)=>a.left<c.right-.5&&c.left<a.right-.5&&a.top<c.bottom-.5&&c.top<a.bottom-.5;
    return{width:b.width,height:b.height,left:b.left,right:b.right,viewport:innerWidth,headerFits:header.scrollWidth<=header.clientWidth,pageFits:document.documentElement.scrollWidth<=innerWidth,
      wordFits:word.scrollWidth<=word.clientWidth,word:word.textContent.trim(),pillOverlap:overlaps(b,p),titleOverlap:overlaps(b,h),labelVisible:t.width>2&&t.height>2};});
  assert(found.width>=47.5&&found.height>=47.5,`sign-out target ${label}: ${found.width}x${found.height}`);
  assert(found.left>=0&&found.right<=found.viewport+.5,`sign-out inside the viewport ${label}: ${found.left}..${found.right} of ${found.viewport}`);
  assert(found.headerFits&&found.pageFits,`header without sideways overflow ${label}: ${JSON.stringify(found)}`);
  assert(found.word.length>0&&found.wordFits,`live pill keeps its word ${label}: ${JSON.stringify(found)}`);
  assert(!found.pillOverlap&&!found.titleOverlap,`sign-out overlaps the pill or the title ${label}`);
  assert.strictEqual(found.labelVisible,found.viewport>=600,`sign-out text is visible from 600 px only ${label}`);
}
async function go(page, hash, selector) {
  await page.evaluate((value) => (location.hash = value), hash);
  await page.waitForSelector(selector);
  await page.waitForFunction(
    () =>
      document.querySelector("#app-main").getAttribute("aria-busy") === "false",
  );
}
async function setLanguage(page,lang){
  await page.evaluate(value=>{localStorage["rtk-language"]=value;location.hash="measure";},lang);
  await page.reload();
  await page.waitForSelector(".readiness-hero");
}
// Layout audit of #app-main. Allowed: the pannable map world (.map-world, svg content) and the children of the
// legend and of the log filter chips (both scroll sideways on purpose) the decorative ring of the readiness hero (its own scroll width only; the content inside stays checked) and the visually hidden text (clipped to 1 px on purpose).
// Everything else, panels and overflow:hidden boxes included, is checked.
async function layoutAudit(page){
  return page.evaluate(()=>{
    const main=document.querySelector("#app-main"),name=el=>el.tagName.toLowerCase()+[...el.classList].map(c=>"."+c).join("");
    const result={width:innerWidth,scrollWidth:document.documentElement.scrollWidth,outside:[],clipped:[],spill:[]};
    const add=(list,entry)=>{if(!list.includes(entry))list.push(entry);};
    for(const el of main.querySelectorAll("*")){
      if(el.closest(".map-world")||el.closest("svg")||el.closest(".visually-hidden")||(el.parentElement&&el.parentElement.closest(".map-legend, .filter-chips")))continue;
      const rect=el.getBoundingClientRect();
      if(rect.width>0&&rect.height>0){
        if(rect.left<-1||rect.right>innerWidth+1)add(result.outside,name(el));
        for(let box=el.parentElement;box&&box!==main;box=box.parentElement){
          if(box.matches(".map-legend, .filter-chips")||getComputedStyle(box).overflowX==="visible")continue;
          const outer=box.getBoundingClientRect();
          if(rect.left<outer.left-1||rect.right>outer.right+1)add(result.clipped,name(el)+" in "+name(box));
        }
      }
      if(el.clientWidth>0&&el.scrollWidth>el.clientWidth+1&&!el.matches(".map-legend, .filter-chips, .readiness-hero"))add(result.spill,name(el)+" "+el.clientWidth+"/"+el.scrollWidth);
    }
    return result;
  });
}
function assertLayout(audit,label){
  assert(audit.scrollWidth<=audit.width+1&&!audit.outside.length&&!audit.clipped.length&&!audit.spill.length,
    `${label}: width ${audit.width}, scrollWidth ${audit.scrollWidth}, outside ${JSON.stringify(audit.outside)}, clipped ${JSON.stringify(audit.clipped)}, spill ${JSON.stringify(audit.spill)}`);
}
// The action sheet sits in the overlay, outside #app-main: nothing of it may leave the viewport or scroll sideways.
async function assertSheetLayout(page,label){
  const found=await page.evaluate(()=>{
    const dialog=document.querySelector("dialog[open]"),out=[];
    if(!dialog)return{missing:true};
    const name=el=>el.tagName.toLowerCase()+[...el.classList].map(c=>"."+c).join("");
    for(const el of[dialog,...dialog.querySelectorAll("*")]){
      const rect=el.getBoundingClientRect();
      if(rect.width>0&&(rect.left<-1||rect.right>innerWidth+1))out.push("outside "+name(el));
      if(el.clientWidth>0&&el.scrollWidth>el.clientWidth+1)out.push("spill "+name(el)+" "+el.clientWidth+"/"+el.scrollWidth);
    }
    return{missing:false,out,scrollWidth:document.documentElement.scrollWidth,width:innerWidth};
  });
  assert(!found.missing,label+": no open dialog");
  assert(found.scrollWidth<=found.width+1&&!found.out.length,`${label}: ${JSON.stringify(found)}`);
}
async function assertArchiveCards(page,label){
  const cards=await page.evaluate(()=>{
    const box=el=>{const r=el.getBoundingClientRect();return{x:r.x,y:r.y,width:r.width,height:r.height,bottom:r.bottom,right:r.right};};
    return[...document.querySelectorAll(".archive-library .archive-card")].map(card=>{
      const info=card.querySelector(".archive-info"),b=info.querySelector("b"),small=info.querySelector("small");
      return{label:card.getAttribute("aria-label"),clientWidth:card.clientWidth,box:box(card),info:box(info),name:b.innerText,meta:small.innerText,
        bFits:b.scrollWidth<=b.clientWidth+1,smallFits:small.scrollWidth<=small.clientWidth+1,innerWidth,
        actions:[...card.querySelectorAll(".archive-actions>*")].map(el=>({tag:el.tagName,cls:el.className,href:el.getAttribute("href"),...box(el)}))};
    });
  });
  assert.strictEqual(cards.length,mobileArchives.length,`${label}: archive cards`);
  cards.forEach((card,i)=>{
    const item=mobileArchives[i],where=`${label}: archive ${item.id}`;
    assert.strictEqual(card.label,item.name,where+" aria-label");
    assert.strictEqual(card.name,item.name,where+" name");
    assert(card.meta.includes(String(item.point_count))&&card.meta.includes(String(item.line_count))&&card.meta.endsWith(String(item.schema_version)),`${where} details "${card.meta}"`);
    assert(card.box.x>=0&&card.box.right<=card.innerWidth,where+" card inside the viewport");
    assert(card.info.width>=card.clientWidth-40,`${where} info ${card.info.width} of ${card.clientWidth}`);
    assert(card.bFits&&card.smallFits,where+" name and details fit");
    assert.strictEqual(card.actions.length,3,where+" actions");
    assert.deepStrictEqual(card.actions.map(a=>a.tag),["A","BUTTON","BUTTON"],where+" action kinds");
    card.actions.forEach((a,k)=>{
      assert(a.width>=48&&a.height>=48,`${where} action ${k} size ${a.width}x${a.height}`);
      assert(a.x>=card.box.x&&a.right<=card.box.right&&a.y>=card.box.y&&a.bottom<=card.box.bottom,`${where} action ${k} inside its card`);
      assert(a.y>=card.info.bottom-0.5&&!intersects(a,card.info),`${where} action ${k} below the information`);
      for(let j=k+1;j<card.actions.length;j++)assert(!intersects(a,card.actions[j]),`${where} actions ${k}/${j} overlap`);
    });
    assert.strictEqual(card.actions[0].href,`/api/tracking/export?format=archive&archive_id=${encodeURIComponent(item.id)}`,where+" download href");
  });
}
// #24-UI Task 7: the library links to the archive view and labels the survey data backup; the actions are real touch targets.
async function assertLibraryEntryAndDataManagement(page,ui,v2de,lang,label){
  const entry=page.locator("a.archive-entry");
  assert.strictEqual(await entry.count(),1,`${label}: one archive entry`);
  assert.strictEqual(await entry.innerText(),ui.dictionaries[lang]["ui.archive_entry"].replace("{count}",String(mobileArchives.length)),`${label}: archive entry text`);
  assert.strictEqual(await entry.getAttribute("href"),"/#archive",`${label}: archive entry target`);
  assert((await entry.boundingBox()).height>=47.5,`${label}: archive entry is a touch target`);
  await page.evaluate(()=>{document.querySelector("details.data-management").open=true;});
  const found=await page.evaluate(()=>{
    const d=document.querySelector("details.data-management"),box=el=>{const r=el.getBoundingClientRect();return{h:r.height,left:r.left,right:r.right};};
    return{summary:d.querySelector("summary").textContent.trim(),scope:d.querySelector("p").textContent.trim(),link:{href:d.querySelector("a.text-link").getAttribute("href"),...box(d.querySelector("a.text-link"))},
      actions:[...d.querySelectorAll(".inline-actions>*")].map(el=>({text:el.textContent.trim(),href:el.getAttribute("href"),...box(el)})),viewport:innerWidth,
      exports:document.querySelectorAll(".export-card,a[href*='format=geojson'],a[href*='format=csv']").length};});
  const texts=ui.dictionaries[lang],translate=text=>lang==="de"?v2de[text]:text;
  assert.strictEqual(found.summary,texts["ui.data_management_title"],`${label}: data management title`);
  assert.strictEqual(found.scope,texts["ui.data_management_scope"],`${label}: data management scope names Device and Maintenance`);
  assert.strictEqual(found.link.href,"/#device/maintenance",`${label}: link to Maintenance`);
  assert(found.link.h>=47.5,`${label}: Maintenance link is a touch target`);
  assert.deepStrictEqual(found.actions.map(item=>item.text),[texts["ui.survey_backup_download"],translate("Restore backup"),translate("Optimize storage")],`${label}: three actions`);
  assert.strictEqual(found.actions[0].href,"/api/tracking/export?format=backup",`${label}: backup download href`);
  for(const action of found.actions){
    assert(action.h>=47.5,`${label}: action "${action.text}" height ${action.h}`);
    assert(action.left>=0&&action.right<=found.viewport+.5,`${label}: action "${action.text}" inside the viewport`);
  }
  assert.strictEqual(found.exports,0,`${label}: no second export place in the library`);
  assertLayout(await layoutAudit(page),`${label}: library with data management open`);
  await page.evaluate(()=>{document.querySelector("details.data-management").open=false;});
}
// Library -> archive entry -> archive view (navigation state, cards, back link) -> browser back.
async function assertArchiveRoute(page,ui,lang,size,label){
  await page.locator("a.archive-entry").click();
  await page.waitForSelector(".archive-library .archive-card");
  await page.waitForFunction(()=>document.querySelector("#app-main").getAttribute("aria-busy")==="false");
  assert.strictEqual(await page.evaluate(()=>location.hash),"#archive",`${label}: entry opens #archive`);
  const projectsCurrent=await page.evaluate(()=>[...document.querySelectorAll('[data-route="projects"]')].filter(el=>el.getAttribute("aria-current")==="page").length);
  assert(projectsCurrent>=1,`${label}: Projects stays the current area`);
  assert.strictEqual(await page.evaluate(()=>[...document.querySelectorAll('[data-route]')].filter(el=>el.getAttribute("aria-current")==="page").map(el=>el.dataset.route).filter((route,index,all)=>all.indexOf(route)===index).join()),"projects",`${label}: only Projects is current`);
  if(size.width>=1000){
    const current=await page.locator("#desktop-context-nav a[aria-current='page']").allInnerTexts();
    assert.deepStrictEqual(current,[ui.dictionaries[lang]["ui.route_archive"]],`${label}: the context navigation marks the archive`);
  }
  assert.strictEqual(await page.locator("#view-title").innerText(),ui.dictionaries[lang]["ui.route_archive"],`${label}: archive title`);
  assertLayout(await layoutAudit(page),`${label}: archive view`);
  await assertArchiveCards(page,`${label}: archive view`);
  const back=page.locator(".text-link[href='/#projects']");
  assert.strictEqual(await back.count(),1,`${label}: one way back`);
  assert((await back.boundingBox()).height>=47.5,`${label}: back link is a touch target`);
  await back.click();
  await page.waitForSelector(".project-list");
  assert.strictEqual(await page.evaluate(()=>location.hash),"#projects",`${label}: the back link returns to #projects`);
  await page.locator("a.archive-entry").click();
  await page.waitForSelector(".archive-library .archive-card");
  await page.goBack();
  await page.waitForSelector(".project-list");
  assert.strictEqual(await page.evaluate(()=>location.hash),"#projects",`${label}: browser back returns to #projects`);
}
// Parses the map world transform "translate(Xpx, Ypx) scale(Z)".
const mapTransform=async page=>{
  const text=await page.locator(".map-canvas .map-world").evaluate(node=>node.style.transform),
    match=/^translate\((-?[\d.]+)px, (-?[\d.]+)px\) scale\(([\d.]+)\)$/.exec(text);
  assert(match,"map world transform: "+text);
  return{text,x:Number(match[1]),y:Number(match[2]),scale:Number(match[3])};
};
// #24-UI Task 9: visible map height at 360 x 780 (bottom of .map-tools to bottom of .map-canvas) before the tool row was cut down.
const MAP_VIEW_BEFORE_360=353.33;
// The tool row keeps four buttons and the zoom display; layers and full screen live in the menu (summary shows "{on}/3").
async function assertMapMenu(page,label,{measure=false}={}){
  await page.waitForSelector(".map-tools");
  assert.strictEqual(await page.locator(".map-tools > .btn").count(),4,label+": direct tool buttons");
  assert.strictEqual(await page.locator(".map-tools > output.map-zoom").count(),1,label+": zoom display");
  assert.strictEqual(await page.locator(".map-tools > details.map-menu").count(),1,label+": one menu");
  assert.strictEqual(await page.locator(".map-menu .map-layers .btn").count(),3,label+": layer switches in the menu");
  assert.strictEqual(await page.locator(".map-menu > .btn").count(),1,label+": full screen in the menu");
  assert.strictEqual(await page.locator(".map-tools > .map-layers").count(),0,label+": no layer switches beside the buttons");
  const order=await page.locator(".map-tools > *").evaluateAll(nodes=>nodes.map(node=>node.matches(".btn")?"button":node.tagName.toLowerCase()+(node.className?"."+node.className.split(" ")[0]:"")));
  assert.deepStrictEqual(order,["button","output.map-zoom","button","button","button","details.map-menu"],label+": tool order");
  assert.deepStrictEqual(await page.locator(".map-tools > .btn").evaluateAll(nodes=>nodes.slice(0,2).map(node=>node.textContent)),["−","+"],label+": zoom buttons around the display");
  assert(/^\d\/3$/.test((await page.locator(".map-menu-state").innerText()).trim()),label+": menu state text");
  const found=await page.evaluate(()=>{
    const box=el=>{const r=el.getBoundingClientRect();return{x:r.x,y:r.y,width:r.width,height:r.height,right:r.right,bottom:r.bottom};};
    return{innerWidth,controls:[...document.querySelectorAll(".map-tools > .btn,.map-tools > output,.map-tools > .map-menu > summary")].map(node=>({name:node.className||node.tagName,...box(node)})),
      tools:box(document.querySelector(".map-tools")),canvas:box(document.querySelector(".map-canvas"))};
  });
  assert.strictEqual(found.controls.length,6,label+": six visible controls");
  for(const c of found.controls){
    assert(c.width>=48&&c.height>=48,`${label}: ${c.name} ${c.width}x${c.height}`);
    assert(c.x>=0&&c.right<=found.innerWidth,label+": "+c.name+" inside the viewport");
  }
  return found.canvas.bottom-found.tools.bottom;
}
async function assertProjectView(page,id,label){
  await go(page,"projects/"+id,".map-workspace");
  await page.waitForFunction(()=>{const chip=document.querySelector(".quality-chip");return chip&&!chip.classList.contains("warn");});
  assertLayout(await layoutAudit(page),label);
  const found=await page.evaluate(()=>{
    const box=el=>{const r=el.getBoundingClientRect();return{x:r.x,right:r.right,width:r.width,height:r.height};},panel=document.querySelector(".map-panel");
    return{innerWidth,panel:{scrollWidth:panel.scrollWidth,clientWidth:panel.clientWidth},
      actions:[...document.querySelectorAll(".project-activation .btn,.project-actions .btn,.project-danger .btn")].map(box),tools:[...document.querySelectorAll(".map-tools .btn")].map(box)};
  });
  assert(found.panel.scrollWidth<=found.panel.clientWidth+1,`${label}: panel scrolls sideways ${JSON.stringify(found.panel)}`);
  assert(found.actions.length>0,label+": project actions");
  for(const a of found.actions){
    assert(a.width>=48&&a.height>=48,`${label}: action ${a.width}x${a.height}`);
    assert(a.x>=0&&a.right<=found.innerWidth,label+": action inside the viewport");
  }
  for(const t of found.tools)assert(t.x>=0&&t.right<=found.innerWidth,label+": map tool inside the viewport");
}
// Opens the first line and the first object point of the project and checks the layout with each detail open.
async function assertDetailsOpen(page,expectedLineName,expectedLineId,label){
  const row=page.locator(".line-row").first();
  if(expectedLineId)assert.strictEqual(await row.getAttribute("data-line-id"),expectedLineId,label+": first line row");
  await row.click();
  await page.waitForSelector(".line-quality h3");
  assert.strictEqual(await page.locator(".line-quality h3").innerText(),expectedLineName,label+": line detail name");
  assertLayout(await layoutAudit(page),label+" line detail");
  await page.locator(".point-row").first().click();
  await page.waitForSelector(".point-card");
  assertLayout(await layoutAudit(page),label+" point detail");
}
// Zoom, then drag the map from a free point: the moved map must stay inside its canvas and the page width must not change.
async function assertMapPanStaysInside(page,label){
  await page.locator(".map-tools button").nth(2).click();
  assert.strictEqual((await mapTransform(page)).text,"translate(0px, 0px) scale(1)",label+": fit project resets the view");
  await page.locator(".map-tools button").nth(1).click();
  await page.locator(".map-tools button").nth(1).click();
  const zoomed=await mapTransform(page);
  assert(zoomed.text.includes("scale(2)"),`${label}: zoom ${zoomed.text}`);
  await page.locator(".map-canvas").scrollIntoViewIfNeeded();
  const spot=await page.evaluate(()=>{
    const svg=document.querySelector(".map-canvas svg"),r=svg.getBoundingClientRect();
    for(let y=r.top+20;y<Math.min(r.bottom-20,innerHeight);y+=12)for(let x=r.right-20;x>r.left+20;x-=12){
      if(x<0||x>=innerWidth)continue;
      const hit=document.elementFromPoint(x,y);
      if(hit&&svg.contains(hit)&&!hit.closest(".map-line,.map-point,.map-position,.map-tools,.quality-chip,.map-legend"))return[x,y];
    }
    return null;
  });
  assert(spot,label+": no free map point in the viewport");
  const canvasBox=()=>page.locator(".map-canvas").evaluate(node=>{const r=node.getBoundingClientRect();return{x:r.x,y:r.y,width:r.width,height:r.height,right:r.right,bottom:r.bottom};});
  const before=await canvasBox();
  await page.mouse.move(spot[0],spot[1]);
  await page.mouse.down();
  await page.mouse.move(spot[0]-120,spot[1],{steps:6});
  await page.mouse.up();
  const moved=await mapTransform(page);
  assert(Math.abs(moved.x-(zoomed.x-120))<=2,`${label}: dragged ${zoomed.text} -> ${moved.text}`);
  assertLayout(await layoutAudit(page),label+" after pan");
  const after=await canvasBox();
  assert.deepStrictEqual(after,before,label+": canvas rectangle unchanged by the pan");
  assert(after.x>=0&&after.right<=await page.evaluate(()=>innerWidth),label+": canvas inside the viewport");
  const inside=await page.evaluate(()=>{
    const canvas=document.querySelector(".map-canvas").getBoundingClientRect(),svg=document.querySelector(".map-canvas svg").getBoundingClientRect();
    return svg.left>=canvas.left-1&&svg.right<=canvas.right+1&&svg.top>=canvas.top-1&&svg.bottom<=canvas.bottom+1;
  });
  assert(inside,label+": map graphic inside its canvas");
  const outside=await page.evaluate(()=>{
    const canvas=document.querySelector(".map-canvas").getBoundingClientRect();
    if(canvas.bottom+10>=innerHeight)scrollBy(0,canvas.bottom+11-innerHeight);
    const now=document.querySelector(".map-canvas").getBoundingClientRect(),hit=document.elementFromPoint(now.left+now.width/2,now.bottom+10);
    return hit?!!hit.closest(".map-world"):false;
  });
  assert(!outside,label+": the panned map paints over the panel below");
}
async function assertEmptyProjectCard(page,label){
  const found=await page.evaluate(()=>{
    const box=el=>{const r=el.getBoundingClientRect();return{x:r.x,y:r.y,width:r.width,height:r.height,bottom:r.bottom,right:r.right};},
      wraps=[...document.querySelectorAll(".project-card-wrap")];
    return{count:wraps.length,innerWidth,wrap:wraps[0]&&box(wraps[0]),card:wraps[0]&&box(wraps[0].querySelector(".project-card")),button:wraps[0]&&box(wraps[0].querySelector(".btn"))};
  });
  assert.strictEqual(found.count,1,label+": one empty project card");
  const {wrap,card,button}=found;
  assert(card.width>=0.55*wrap.width,`${label}: card ${card.width} of ${wrap.width}`);
  assert(button.width>=48&&button.height>=48,`${label}: button ${button.width}x${button.height}`);
  assert(!intersects(card,button),label+": card and button overlap");
  for(const part of[card,button])assert(part.x>=0&&part.right<=found.innerWidth,label+": inside the viewport");
  assert(button.y<card.bottom&&card.y<button.bottom,label+": button beside the card, not below it");
}
// Negative test: a name clipped by overflow:hidden must make the detector fail, whatever the page state.
async function assertDetectorCatchesClipping(page,containerSelector,label){
  await page.evaluate(([selector,text])=>{
    const probe=document.createElement("div"),inner=document.createElement("b");
    probe.className="layout-probe";probe.style.cssText="overflow:hidden;width:60px";
    inner.className="layout-probe";inner.style.whiteSpace="nowrap";inner.textContent=text;
    probe.append(inner);document.querySelector(selector).append(probe);
  },[containerSelector,LONG_UNBROKEN]);
  const audit=await layoutAudit(page);
  assert(audit.clipped.includes("b.layout-probe in div.layout-probe"),`${label}: clipped ${JSON.stringify(audit.clipped)}`);
  assert(audit.spill.some(entry=>entry.startsWith("div.layout-probe ")),`${label}: spill ${JSON.stringify(audit.spill)}`);
  assert.throws(()=>assertLayout(audit,label),/layout-probe/,`${label}: assertLayout must throw`);
  await page.evaluate(()=>document.querySelectorAll(".layout-probe").forEach(node=>node.remove()));
  const clean=await layoutAudit(page);
  for(const list of[clean.outside,clean.clipped,clean.spill])assert(!list.some(entry=>entry.includes("layout-probe")),`${label}: probe left behind`);
}
// Stakeout WP4 part B (--stakeout-actions-only): the action bar in WP3's dock against stakeoutMock with the live progress list.
async function testStakeoutActions(page,browser){
  assert(STAKEOUT_SET_DOC&&STAKEOUT_SET_DOC.set_id==="so-3a4739b9","the sample set document comes from stakeout.validate_import");
  const errors=[];page.on("pageerror",error=>errors.push(error.message));
  const T=require(root+"/i18n.js").dictionaries,m=stakeoutMock,SET="so-3a4739b9",P2="so-3a4739b9/2/1",P2_PATH="projects/p1/stakeout/target/so-3a4739b9%2F2%2F1";
  const go=hash=>page.evaluate(value=>{location.hash=value;},hash);
  const posts=action=>m.posts.filter(post=>post.action===action);
  const progressGets=()=>requestLog.filter(entry=>entry.path==="/api/stakeout/progress").length;
  const reset=()=>{stakeoutReset();Object.assign(m,{wp4a:true,progressLive:true});tracking.active_project_id="p1";};
  const clearDock=()=>page.evaluate(()=>document.querySelectorAll("#feedback-dock .feedback-item").forEach(node=>node.remove()));
  const dockShows=(kind,text,timeout)=>page.waitForFunction(([k,t])=>[...document.querySelectorAll("#feedback-dock .feedback-item."+k+" .feedback-text")].some(node=>node.textContent===t),[kind,text],{timeout:timeout||10000});
  const button=action=>page.locator('.stakeout-actions [data-stakeout-actions] [data-action="'+action+'"]');
  const buttons=()=>page.evaluate(()=>[...document.querySelectorAll(".stakeout-actions [data-stakeout-actions] button")].map(node=>[node.dataset.action,node.disabled,node.textContent]));
  const enabled=action=>page.waitForSelector('.stakeout-actions [data-stakeout-actions] [data-action="'+action+'"]:not([disabled])');
  const openTarget=async(path=P2_PATH)=>{await go("projects");await page.waitForFunction(()=>!document.querySelector(".stakeout-page"));await go(path);await page.waitForSelector(".stakeout-guide .guide-name");};
  const openReady=async()=>{await openTarget();await enabled("confirm");};
  for(const lang of ["en","de"]){
    const D=T[lang],label="stakeout actions "+lang;
    reset();await setLanguage(page,lang);
    // The stakeout entry sits right below the project map (user request 30.10.): order map, project-tools, lists. At 360 x 640 the
    // map alone (min-height 460 px) ends at about 571 px and the mobile navigation covers the rest, so the button cannot be in view
    // without scrolling; it is the first thing below the map, and one short scroll shows it uncovered.
    // (j) uses the same visit: the map of p1 carries a stakeout point.
    extraMapPoints=[{id:"s1",type:"stakeout",name:"Point 2",note:"",coordinate:[999999943,5100000027,412345],chainage_m:null,
      measurement:{precision:"e8",fix_quality:4,fix_status:"RTK_FIXED",satellites:20,hdop:.6,quality:{accepted:true,reasons:[]},source:"stakeout_verified"}}];
    await page.setViewportSize({width:360,height:640});await go("projects/p1");
    await page.waitForSelector(".map-canvas .survey-map");
    // User decision 30.10.: a "Stakeout" overlay in a free corner of the map as well: at 360 x 640 in view without scrolling,
    // not under the navigation, 48 px, no map tool, chip, legend, scale or cache stamp covered; 320 px without overflow; after a partial
    // (delta) and a full map update exactly one overlay.
    const overlay=()=>page.evaluate(()=>{const link=document.querySelector(".map-canvas .map-stakeout"),b=link.getBoundingClientRect(),hit=document.elementFromPoint(b.left+b.width/2,b.top+b.height/2),
      boxes=[...document.querySelectorAll(".map-canvas .map-tools .btn,.map-canvas .map-tools .map-zoom,.map-canvas .map-menu>summary,.map-canvas .quality-chip,.map-canvas .map-legend,.map-canvas .map-label,.map-canvas>.stale-stamp")].filter(el=>el.getClientRects().length&&getComputedStyle(el).visibility!=="hidden").map(el=>Object.assign(el.getBoundingClientRect().toJSON(),{name:el.className.baseVal??el.className})),
      canvas=document.querySelector(".map-canvas").getBoundingClientRect();
      return{count:document.querySelectorAll(".map-stakeout").length,top:b.top,bottom:b.bottom,left:b.left,right:b.right,width:b.width,height:b.height,viewport:innerHeight,scroll:scrollY,
        hit:Boolean(hit&&(hit===link||link.contains(hit))),overlap:boxes.filter(r=>!(r.right<=b.left+.5||r.left>=b.right-.5||r.bottom<=b.top+.5||r.top>=b.bottom-.5)).map(r=>r.name),
        inCanvas:b.left>=canvas.left&&b.right<=canvas.right&&b.top>=canvas.top&&b.bottom<=canvas.bottom,href:link.getAttribute("href"),text:link.textContent.trim(),scrollWidth:document.documentElement.scrollWidth,width0:innerWidth};});
    const overlayOk=(o,at)=>{assert(o.count===1&&o.scroll===0&&o.top>=0&&o.bottom<=o.viewport&&o.hit&&o.overlap.length===0&&o.inCanvas&&o.width>=47.5&&o.height>=47.5&&o.scrollWidth<=o.width0+1,label+": stakeout overlay "+at+" "+JSON.stringify(o));
      assert.strictEqual(o.href,"/#projects/p1/stakeout",label+": overlay link");assert(o.text.endsWith(D["stakeout.entry"]),label+": overlay text "+o.text);};
    await page.evaluate(()=>scrollTo(0,0));overlayOk(await overlay(),"360 x 640");
    await page.setViewportSize({width:320,height:640});await page.evaluate(()=>scrollTo(0,0));await page.waitForTimeout(200);
    overlayOk(await overlay(),"320 x 640");assertLayout(await layoutAudit(page),label+": project map with overlay 320");
    const pointsBefore=await page.locator(".map-point").count();mapRevision=2;live.revision=2;
    await page.waitForFunction(n=>document.querySelectorAll(".map-point").length>n,pointsBefore,{timeout:8000});
    assert.strictEqual(await page.locator(".map-stakeout").count(),1,label+": one overlay after a delta");
    mapRevision=1;live.revision=1;
    await page.waitForFunction(n=>document.querySelectorAll(".map-point").length===n,pointsBefore,{timeout:8000});
    assert.strictEqual(await page.locator(".map-stakeout").count(),1,label+": one overlay after a full redraw");
    if(lang==="en"){for(const theme of ["light","dark","high-contrast"]){
      await page.evaluate(value=>localStorage.setItem("rtk-theme",value),theme);await page.reload();
      await page.waitForSelector(`html[data-theme="${theme}"]`);await page.waitForSelector(".map-canvas .map-stakeout");
      assert.deepStrictEqual(await auditContrast(page,".map-stakeout,.map-stakeout *"),[],label+": overlay contrast in "+theme);
      assert(await page.evaluate(()=>window.__contrastChecked)>0,label+": overlay text checked in "+theme);}
      await page.evaluate(()=>localStorage.removeItem("rtk-theme"));await page.reload();await page.waitForSelector(".map-canvas .map-stakeout");}
    await page.setViewportSize({width:360,height:640});await page.evaluate(()=>scrollTo(0,0));
    const entry=await page.evaluate(()=>{const link=document.querySelector('[data-slot="project-tools"] .stakeout-entry a'),b=link.getBoundingClientRect(),
      hit=document.elementFromPoint(b.left+b.width/2,b.top+b.height/2),map=document.querySelector(".map-canvas").getBoundingClientRect(),
      lists=document.querySelector(".map-panel details").getBoundingClientRect();
      return{top:b.top,bottom:b.bottom,viewport:innerHeight,scroll:scrollY,hit:Boolean(hit&&(hit===link||link.contains(hit))),mapBottom:map.bottom,listsTop:lists.top,text:link.textContent};});
    assert(entry.mapBottom<=entry.top+1&&entry.top-entry.mapBottom<=48&&entry.bottom<=entry.listsTop,label+": map, then right below it the stakeout entry, then the lists "+JSON.stringify(entry));
    const shown=await page.evaluate(()=>{const link=document.querySelector('[data-slot="project-tools"] .stakeout-entry a');link.scrollIntoView({block:"center"});
      const b=link.getBoundingClientRect(),hit=document.elementFromPoint(b.left+b.width/2,b.top+b.height/2);return{scroll:scrollY,hit:Boolean(hit&&(hit===link||link.contains(hit))),top:b.top,bottom:b.bottom,viewport:innerHeight};});
    assert(shown.hit&&shown.top>=0&&shown.bottom<=shown.viewport&&shown.scroll<=entry.bottom-entry.viewport+shown.viewport/2+1,label+": one short scroll shows the entry uncovered "+JSON.stringify(shown));
    assert.strictEqual(entry.text,D["stakeout.entry"],label+": entry label");
    await page.setViewportSize({width:390,height:844});
    // (j) the project view names a stakeout point by its type text.
    await page.waitForFunction(text=>[...document.querySelectorAll("#app-main .point-row")].some(row=>row.textContent.includes(text)),D["asset.stakeout"]);
    extraMapPoints=[];
    // (a) Confirm: "Saving…" while the device answers, "Saved" only after the 200; expect_gen comes from the progress list.
    m.floors[SET]=2;m.targets[P2]={status:"open",gen:3,mode:null,point_id:null};
    await openReady();
    m.actionAnswers.confirm=[{delay:1500}];await clearDock();
    await button("confirm").click();
    await page.waitForTimeout(400);
    const saving=await buttons();
    assert(saving.every(row=>row[1]),label+": every button locked while saving "+JSON.stringify(saving));
    assert.strictEqual(saving.find(row=>row[0]==="confirm")[2],D["stakeout.saving"],label+": saving text");
    assert.strictEqual(await page.locator("#feedback-dock .feedback-item.ok").count(),0,label+": no success before the answer");
    await dockShows("ok",D["stakeout.saved"]);
    assert.deepStrictEqual(posts("confirm").map(post=>[post.tid,post.expect_gen,post.mode,/^[0-9a-f]{16}$/.test(post.cmd)]),[[P2,3,"verified",true]],label+": one confirm with expect_gen 3");
    await enabled("reopen");
    assert.strictEqual(m.targets[P2].status,"confirmed",label+": confirmed on the device");
    // (b) two lost connections (the first after the device saved) and then the repeat: saved, three equal bodies.
    reset();await openReady();await clearDock();
    m.actionAnswers.confirm=[{abort:true,lost:true},{abort:true}];
    await button("confirm").click();
    await dockShows("ok",D["stakeout.saved"]);
    assert.strictEqual(posts("confirm").length,3,label+": three requests");
    assert.strictEqual(new Set(posts("confirm").map(post=>JSON.stringify(post))).size,1,label+": the same body each time");
    assert.strictEqual(m.points,1,label+": one stakeout point");
    // (c) no answer at all: "Not confirmed – check the list" and the list loaded again.
    reset();await openReady();await clearDock();
    m.actionAnswers.confirm=[{abort:true},{abort:true},{abort:true}];
    const listsBefore=progressGets();
    await button("confirm").click();
    await dockShows("warn",D["stakeout.unconfirmed"]);
    assert.strictEqual(posts("confirm").length,3,label+": three attempts");
    for(let i=0;i<30&&progressGets()<=listsBefore;i++)await page.waitForTimeout(100);
    assert(progressGets()>listsBefore,label+": the progress list is loaded again");
    // (i) layout with a dock message: 5 widths, buttons 48 px, inside the viewport, no overlap, "Next target" stays.
    for(const width of [320,360,390,480,1280]){
      await page.setViewportSize({width,height:width>=1280?900:640});await page.waitForTimeout(300);
      const view=await page.evaluate(()=>{const r=el=>{const b=el.getBoundingClientRect();return{top:b.top,bottom:b.bottom,left:b.left,right:b.right,width:b.width,height:b.height};},dock=document.querySelector(".stakeout-actions");dock.scrollIntoView({block:"nearest"});
        return{dock:r(dock),buttons:[...dock.querySelectorAll("button")].filter(b=>b.checkVisibility()).map(b=>[b.dataset.action,r(b)]),items:[...document.querySelectorAll("#feedback-dock .feedback-item")].map(r),
          width:innerWidth,height:innerHeight,scroll:document.documentElement.scrollWidth};});
      const at=label+" "+width+" px";
      assert(view.scroll<=view.width+1,at+": no horizontal scroll");
      assert.deepStrictEqual(view.buttons.map(row=>row[0]),["confirm","found","skip","next"],at+": the action bar and Next target");
      for(const [action,b] of view.buttons){assert(b.width>=47.5&&b.height>=47.5,at+": "+action+" 48 px "+JSON.stringify(b));assert(b.left>=-1&&b.right<=view.width+1&&b.top>=-1&&b.bottom<=view.height+1,at+": "+action+" in the viewport "+JSON.stringify(b));}
      assert(view.items.length,at+": the dock message is there");
      assert(view.items.every(b=>b.bottom<=view.dock.top||b.top>=view.dock.bottom||b.right<=view.dock.left||b.left>=view.dock.right),at+": no message over the actions "+JSON.stringify(view));
      assertLayout(await layoutAudit(page),at);
    }
    await page.setViewportSize({width:390,height:844});
    // (d) 409 target_changed: a dock message and the list loaded again.
    reset();await openReady();await clearDock();
    m.actionAnswers.confirm=[{status:409,body:{error:"target_changed",message:"changed",status:"open",mode:null,gen:5}}];
    const listsChanged=progressGets();
    await button("confirm").click();
    await dockShows("warn",D["stakeout.changed"]);
    for(let i=0;i<30&&progressGets()<=listsChanged;i++)await page.waitForTimeout(100);
    assert(progressGets()>listsChanged,label+": list loaded again after target_changed");
    assert.strictEqual(posts("confirm").length,1,label+": no repeat after target_changed");
    // (e) settling or an old frame: Confirm locked, no request.
    reset();await openReady();
    m.packet={...m.packet,guidance:{...STAKEOUT_PACKET.guidance,state:"settling"}};
    await page.waitForSelector('.stakeout-actions [data-action="confirm"][disabled]');
    m.packet={...m.packet,guidance:{...STAKEOUT_PACKET.guidance}};await enabled("confirm");
    const hold=stakeoutLiveHold();
    await page.waitForSelector('.stakeout-actions [data-action="confirm"][disabled]',{timeout:6000});
    assert.strictEqual(await button("found").isDisabled(),true,label+": no found on site with an old frame");
    await button("confirm").click({force:true}).catch(()=>{});await page.waitForTimeout(300);
    assert.strictEqual(posts("confirm").length,0,label+": no confirm request with an old frame");
    hold.release();await enabled("confirm");
    // (f) found on site with a note: mode manual, the note in the body.
    await clearDock();await button("found").click();
    const sheet=page.locator("dialog[open]");await sheet.waitFor();
    assert.strictEqual(await sheet.locator('input[name="note"]').getAttribute("maxlength"),"500",label+": note up to 500");
    await sheet.locator('input[name="note"]').fill("By the fence");
    await sheet.locator('button[type="submit"]').click();
    await dockShows("ok",D["stakeout.saved"]);
    assert.deepStrictEqual(posts("confirm").map(post=>[post.mode,post.note,post.expect_gen]),[["manual","By the fence",0]],label+": found on site body");
    await enabled("reopen");
    // (g) Skip, then the list shows the status; Reopen opens it again with the next generation.
    reset();await openReady();await clearDock();
    await button("skip").click();await enabled("reopen");
    assert.deepStrictEqual(posts("skip").map(post=>post.expect_gen),[0],label+": one skip");
    await go("projects/p1/stakeout/targets");
    await page.waitForFunction(([tid,text])=>{const row=document.querySelector('a.target-row[data-tid="'+tid+'"]');return row&&row.textContent.includes(text);},[P2,D["stakeout.status.skipped"]]);
    await openTarget();await enabled("reopen");await button("reopen").click();await enabled("confirm");
    assert.deepStrictEqual(posts("reopen").map(post=>post.expect_gen),[0],label+": reopen with the skipped generation");
    assert.deepStrictEqual(m.targets[P2],{status:"open",gen:1,mode:null,point_id:null},label+": open at generation 1");
    // (h) leaving the page during a slow answer: only a dock message, no change on the next page, no further request.
    await clearDock();m.actionAnswers.skip=[{delay:2500}];
    await button("skip").click();await page.waitForTimeout(200);
    await go("projects");await page.waitForFunction(()=>!document.querySelector(".stakeout-page"));
    await page.evaluate(()=>{document.querySelector("#app-main").dataset.testMark="kept";});
    await dockShows("ok",D["stakeout.saved"]);await page.waitForTimeout(1500);
    assert.strictEqual(posts("skip").length,2,label+": no further request after leaving");
    assert.strictEqual(await page.locator("#app-main .stakeout-page").count(),0,label+": the next page stays");
    // (k) live status unknown: WP3's mark, no action buttons.
    reset();await openReady();
    m.packet={...m.packet,target:{...m.packet.target,status:"unknown"}};
    await page.waitForFunction(()=>!document.querySelector(".stakeout-actions [data-stakeout-actions] button"));
    await page.waitForFunction(tid=>{const mark=document.querySelector('.stakeout-map .stakeout-target[data-tid="'+tid+'"]');return mark&&mark.dataset.status==="unknown";},P2);
    // (l) 503 stakeout_write_uncertain: its text, exactly one request; the live reason locks every button and is shown.
    reset();await openReady();await clearDock();
    m.actionAnswers.skip=[{status:503,body:{error:"stakeout_write_uncertain",message:"A stakeout change may not be stored; restart the device."}}];
    await button("skip").click();
    await dockShows("critical",D["stakeout.error.stakeout_write_uncertain"]);await page.waitForTimeout(2500);
    assert.strictEqual(posts("skip").length,1,label+": write_uncertain is never repeated");
    m.packet={...m.packet,guidance:{...STAKEOUT_PACKET.guidance,reasons:["write_uncertain"]}};
    await page.waitForFunction(text=>document.querySelector(".stakeout-guide").textContent.includes(text),D["stakeout.reason.write_uncertain"]);
    await page.waitForFunction(()=>{const list=[...document.querySelectorAll(".stakeout-actions [data-stakeout-actions] button")];return list.length&&list.every(node=>node.disabled);});
    // (m) Codex review P2: a progress list that fails (503, lost connection) leaves no action but a notice with "Load again";
    // while it fails at most one reload per 10 s on live packets; a tap (live held, so no packet helps) or the next packets
    // after recovery bring the actions back; also when the reload after Confirm fails; the same notice in the overview.
    reset();m.progressFail="busy";
    await openTarget();
    await page.waitForSelector('.stakeout-actions [data-notice="stakeout-progress"] [data-action="reload-progress"]');
    assert((await page.textContent('.stakeout-actions [data-notice="stakeout-progress"]')).includes(D["stakeout.progress_failed"]),label+": progress notice text");
    const failFrom=progressGets();await page.waitForTimeout(12500);
    const failing=progressGets()-failFrom;
    assert(failing>=1&&failing<=2,label+": at most one automatic reload per 10 s while failing ("+failing+")");
    assert.strictEqual(await page.locator('.stakeout-actions [data-stakeout-actions] button:not([data-action="reload-progress"])').count(),0,label+": no action while the progress is unknown");
    const heldProgress=stakeoutLiveHold();m.progressFail=null;
    await page.click('[data-action="reload-progress"]');
    await enabled("skip");
    heldProgress.release();await enabled("confirm");
    await clearDock();m.progressFail="abort";
    await button("confirm").click();await dockShows("ok",D["stakeout.saved"]);
    await page.waitForSelector('.stakeout-actions [data-notice="stakeout-progress"]');
    m.progressFail=null;
    await page.waitForSelector('.stakeout-actions [data-stakeout-actions] [data-action="reopen"]:not([disabled])',{timeout:15000});
    m.progressFail="abort";
    await go("projects");await page.waitForFunction(()=>!document.querySelector(".stakeout-page"));await go("projects/p1/stakeout");
    await page.waitForSelector('.stakeout-overview [data-notice="stakeout-progress"] [data-action="reload-progress"]');
    const heldOverview=stakeoutLiveHold();m.progressFail=null;
    await page.click('.stakeout-overview [data-action="reload-progress"]');
    await page.waitForSelector('.stakeout-overview [data-notice="stakeout-progress"]',{state:"detached"});
    heldOverview.release();
    // B3 (plan change B-5): one project-wide export entry in the overview, a sheet with GeoJSON and CSV, no set_id.
    reset();m.sets.p1.push({...structuredClone(STAKEOUT_SET_DOC),set_id:"so-0000abcd"});
    await page.setViewportSize({width:320,height:640});
    await go("projects");await page.waitForFunction(()=>!document.querySelector(".stakeout-page"));await go("projects/p1/stakeout");
    await page.waitForSelector('.stakeout-overview [data-set-id="so-0000abcd"]');
    assert.strictEqual(await page.locator('.stakeout-overview [data-slot="stakeout-export"] [data-action="stakeout-export"]').count(),1,label+": one export entry");
    assert.strictEqual(await page.locator('[data-set-id] [data-action*="export"], [data-set-id] a[href*="export"]').count(),0,label+": no export per set");
    const exportButton=page.locator('[data-action="stakeout-export"]');
    assert.strictEqual((await exportButton.textContent()).trim(),D["stakeout.export"],label+": export label");
    const exportBox=await exportButton.boundingBox();assert(exportBox.height>=47.5&&exportBox.width>=47.5,label+": export button 48 px");
    assertLayout(await layoutAudit(page),label+": overview with export 320");
    await exportButton.click();
    const exportSheet=page.locator("dialog.stakeout-export-sheet[open]");await exportSheet.waitFor();
    assert.deepStrictEqual(await exportSheet.locator("a[download]").evaluateAll(list=>list.map(a=>[a.dataset.export,a.getAttribute("href")])),
      [["geojson","/api/stakeout/export?project_id=p1&format=geojson"],["csv","/api/stakeout/export?project_id=p1&format=csv"]],label+": both formats for the project");
    assert((await exportSheet.textContent()).includes(D["stakeout.export_hint"]),label+": state at export start");
    await assertSheetLayout(page,label+": export sheet 320");
    for(const format of ["geojson","csv"]){
      // Downloads of an a[download] bypass page.route, so the URL of the download is the check (no set_id).
      const download=page.waitForEvent("download");
      await exportSheet.locator('a[data-export="'+format+'"]').click();
      const file=await download;
      assert(file.url().endsWith("/api/stakeout/export?project_id=p1&format="+format),label+": "+format+" download "+file.url());
    }
    await page.keyboard.press("Escape");await page.waitForSelector("dialog.stakeout-export-sheet",{state:"detached"});
    // B3 (S8): the archive card marks an archive with stakeout data only from its metadata.
    const savedArchives=tracking.archives;
    tracking.archives=[{id:"ar1",project_id:"x1",name:"With stakeout",point_count:3,line_count:1,schema_version:5,size_bytes:1200,archived_at:100,stakeout_data:true},
      {id:"ar2",project_id:"x2",name:"Without",point_count:2,line_count:0,schema_version:5,size_bytes:900,archived_at:200}];
    await go("projects");await page.waitForSelector(".project-list");await go("archive");await page.waitForSelector(".archive-card");
    await page.waitForFunction(()=>document.querySelectorAll(".archive-card").length===2);
    assert.deepStrictEqual(await page.locator(".archive-card .archive-stakeout").evaluateAll(list=>list.map(node=>[node.closest(".archive-card").getAttribute("aria-label"),node.textContent.trim()])),
      [["With stakeout","◎ "+D["archive.stakeout_data"]]],label+": one archive mark");
    assertLayout(await layoutAudit(page),label+": archive 320");
    tracking.archives=savedArchives;
    await page.setViewportSize({width:390,height:844});
  }
  assert.deepStrictEqual(errors,[],"no page errors");
}
// Stakeout WP3 (--stakeout-only): the project's stakeout pages against stakeoutMock, in English and German.
async function testStakeout(page,browser){
  assert(STAKEOUT_SET_DOC&&STAKEOUT_SET_DOC.set_id==="so-3a4739b9","the sample set document comes from stakeout.validate_import");
  const errors=[];page.on("pageerror",error=>errors.push(error.message));
  const T=require(root+"/i18n.js").dictionaries,m=stakeoutMock,fill=(text,values)=>text.replace(/\{(\w+)\}/g,(_,key)=>values[key]);
  const number=(lang,value,digits)=>new Intl.NumberFormat(lang==="de"?"de-DE":"en-US",{minimumFractionDigits:digits,maximumFractionDigits:digits,useGrouping:false}).format(value);
  const setLanguage=lang=>page.evaluate(value=>RtkI18n.setLanguage(value),lang);
  const go=hash=>page.evaluate(value=>{location.hash=value;},hash);
  const since=list=>{const start=list.length;return()=>list.slice(start);};
  const requests=(path,from)=>requestLog.slice(from).filter(entry=>entry.path===path);
  const dockErrors=()=>page.locator(".feedback-dock .feedback-item.critical").count();
  const pill=()=>page.evaluate(()=>document.getElementById("live-state").dataset.link);
  const mark=selector=>page.evaluate(value=>{const node=document.querySelector(value);node.dataset.testMark="kept";},selector);
  const marked=selector=>page.evaluate(value=>{const node=document.querySelector(value);return Boolean(node&&node.dataset.testMark==="kept");},selector);
  // Touch targets inside the page (and an open sheet); a text link only needs the height.
  const smallTargets=()=>page.evaluate(()=>{const found=[];for(const el of document.querySelectorAll("#app-main button,#app-main a,#app-main summary,#app-main input[type=checkbox],#app-main input[type=file],dialog[open] button,dialog[open] input")){
    if(el.closest("svg"))continue;const rect=el.getBoundingClientRect();if(!rect.width||!rect.height||!el.checkVisibility())continue;
    const link=el.matches("a.text-link");if(rect.height<47.5||!link&&rect.width<47.5)found.push([el.tagName.toLowerCase()+"."+[...el.classList].join("."),(el.textContent||"").trim().slice(0,30),Math.round(rect.width),Math.round(rect.height)]);}return found;});
  const liveGaps=list=>{let worst=0;for(let i=1;i<list.length;i++)worst=Math.max(worst,list[i].at-list[i-1].at);return worst;};
  // A fresh overview mount: leave the route first, so the next hash mounts the page again.
  const openOverview=async(id="p1")=>{await go("projects");await page.waitForFunction(()=>!document.querySelector(".stakeout-page"));await go("projects/"+id+"/stakeout");await page.waitForSelector('.stakeout-overview [data-set-id], .stakeout-calm, .stakeout-overview [data-part="sets"] li');};
  stakeoutReset();
  for(const lang of ["en","de"]){
    const D=T[lang],label="stakeout "+lang;
    stakeoutReset();tracking.active_project_id="p1";await setLanguage(lang);
    // (a) the entry in the project panel opens the overview inside the project.
    await go("projects/p1");
    const entry=page.locator('[data-slot="project-tools"] .stakeout-entry a');
    await entry.waitFor();
    assert.strictEqual((await entry.textContent()).trim(),D["stakeout.entry"],label+": entry label");
    await entry.click();
    await page.waitForFunction(()=>location.hash==="#projects/p1/stakeout");
    await page.waitForSelector(".stakeout-overview [data-set-id]");
    assert.strictEqual(await page.textContent("#view-title"),"Project",label+": the title is the project name");
    assert.strictEqual(await page.getAttribute('#mobile-nav [data-route="projects"]',"aria-current"),"page",label+": Projects stays current");
    const row=page.locator('[data-set-id="so-3a4739b9"]');
    assert((await row.textContent()).includes("stakeout_sample.geojson"),label+": file name");
    assert((await row.textContent()).includes(fill(D["stakeout.set_targets"],{count:12})),label+": 12 targets");
    await page.waitForFunction(text=>document.querySelector('[data-part="counts"]').textContent.startsWith(text),fill(D["stakeout.counts"],{targets:12,open:12,confirmed:0,skipped:0}));
    // (k) the export slot holds WP4 B3's one project-wide entry (plan change B-5); no export button per set.
    assert.strictEqual(await page.locator('.stakeout-overview [data-slot="stakeout-export"]').count(),1,label+": one export slot");
    assert.strictEqual(await page.locator('[data-slot="stakeout-export"] > [data-action="stakeout-export"]').count(),1,label+": one project-wide export entry");
    assert.strictEqual(await page.locator('[data-set-id] [data-action*="export"], [data-set-id] a[href*="export"]').count(),0,label+": no export per set");
    // (b) an inactive project: a hint, no silent activation; the explicit tap refreshes the sections only.
    const posts=since(trackingPostLog),gets=requestLog.length;
    await openOverview("p2");
    await page.waitForSelector('[data-notice="stakeout-inactive"]');
    assert.strictEqual(posts().filter(post=>post.action==="select_project").length,0,label+": no select_project without a tap");
    await mark('[data-part="sets"]');
    const trackingGets=requests("/api/tracking",gets).filter(entry=>entry.method==="GET"&&!entry.search).length,liveFrom=m.liveRequests.length;
    await page.click('[data-notice="stakeout-inactive"] button');
    await page.waitForSelector('[data-notice="stakeout-inactive"]',{state:"detached"});
    assert.deepStrictEqual(posts().filter(post=>post.action==="select_project").map(post=>post.project_id),["p2"],label+": one select_project for p2");
    assert.strictEqual(requests("/api/tracking",gets).filter(entry=>entry.method==="GET"&&!entry.search).length,trackingGets,label+": no page rebuild");
    assert(await marked('[data-part="sets"]'),label+": the set list node stays");
    await page.waitForTimeout(1500);
    assert(liveGaps(m.liveRequests.slice(liveFrom-1))<=1600,label+": the live poll runs on without a gap");
    tracking.active_project_id="p1";
    // (c) the settings sheet stays open and locked while the device answers, and shows a refusal inside.
    await openOverview("p1");
    await page.click('[data-card="stakeout-settings"] button');
    const sheet=page.locator("dialog.stakeout-settings[open]");
    await sheet.waitFor();
    await sheet.locator('input[name="tolerance_m"]').fill("0.05");
    await sheet.locator('input[name="fine_radius_m"]').fill("5");
    m.settingsAnswers.push({delay:2000,status:400,body:{error:"invalid_stakeout_settings",message:"refused"}});
    await sheet.locator('[data-action="save-settings"]').click();
    await page.waitForTimeout(300);
    assert(await sheet.locator('input[name="tolerance_m"]').isDisabled(),label+": fields locked during the PUT");
    assert(await sheet.locator('[data-action="save-settings"]').isDisabled(),label+": save locked during the PUT");
    await page.keyboard.press("Escape");
    await page.evaluate(()=>history.back());
    await page.waitForTimeout(400);
    assert.strictEqual(await sheet.count(),1,label+": Escape and Back do not close the sheet during the PUT");
    await page.waitForFunction(()=>{const node=document.querySelector("dialog.stakeout-settings [data-sheet-error]");return node&&!node.hidden;});
    assert.strictEqual(await sheet.locator("[data-sheet-error]").textContent(),D["stakeout.error.invalid_stakeout_settings"],label+": refusal inside the sheet");
    assert.strictEqual(await sheet.locator('input[name="tolerance_m"]').inputValue(),"0.05",label+": values kept");
    assert(!(await sheet.locator('input[name="tolerance_m"]').isDisabled()),label+": unlocked after the refusal");
    await sheet.locator('[data-action="save-settings"]').click();
    await page.waitForSelector("dialog.stakeout-settings",{state:"detached"});
    assert.deepStrictEqual(m.settingsPuts,[{tolerance_m:0.05,fine_radius_m:5},{tolerance_m:0.05,fine_radius_m:5}],label+": the PUT bodies");
    assert.strictEqual(await page.textContent('[data-part="settings-line"]'),fill(D["stakeout.settings_line"],{tolerance:number(lang,0.05,2),fine:number(lang,5,1)}),label+": the card shows the answer");
    // (d) removing a set sends the confirmed count the device must still have.
    m.actionAnswers.delete_set=[{status:409,body:{error:"storage_busy",message:"busy"}}];
    await page.click('[data-set-id="so-3a4739b9"] [data-action="delete-set"]');
    await page.locator("dialog[open] button.danger").click();
    await page.waitForFunction(()=>{const node=document.querySelector('[data-set-id="so-3a4739b9"] [data-set-error]');return node&&!node.hidden;});
    assert.strictEqual(await page.textContent('[data-set-id="so-3a4739b9"] [data-set-error]'),D["stakeout.error.storage_busy"],label+": busy refusal at the set");
    assert.deepStrictEqual(m.posts.pop(),{action:"delete_set",set_id:"so-3a4739b9",expected_confirmed:0,project_id:"p1"},label+": delete_set body");
    // With WP4 A: the count from sets[i].progress; without it the button is locked.
    m.wp4a=true;m.progressEntry["so-3a4739b9"]={confirmed:2,verified:1,manual:1,skipped:0};
    await openOverview("p1");
    m.actionAnswers.delete_set=[{status:409,body:{error:"storage_busy",message:"busy"}}];
    await page.click('[data-set-id="so-3a4739b9"] [data-action="delete-set"]');
    await page.locator("dialog[open] button.danger").click();
    await page.waitForFunction(()=>{const node=document.querySelector('[data-set-id="so-3a4739b9"] [data-set-error]');return node&&!node.hidden;});
    assert.strictEqual(m.posts.pop().expected_confirmed,2,label+": WP4 A count");
    m.progressEntry["so-3a4739b9"]=null;
    await openOverview("p1");
    assert(await page.locator('[data-set-id="so-3a4739b9"] [data-action="delete-set"]').isDisabled(),label+": removal locked while the progress is unknown");
    assert.strictEqual(await page.textContent('[data-set-id="so-3a4739b9"] [data-blocked="delete"]'),D["stakeout.delete_blocked_unknown"],label+": the reason");
    m.wp4a=false;m.progressEntry={};
    await openOverview("p1");
    const setsBefore=m.setRequests.length;
    await page.click('[data-set-id="so-3a4739b9"] [data-action="delete-set"]');
    await page.locator("dialog[open] button.danger").click();
    await page.waitForSelector('[data-set-id="so-3a4739b9"]',{state:"detached"});
    assert.strictEqual(m.setRequests.length,setsBefore,label+": no set download after the removal");
    m.sets.p1=[structuredClone(STAKEOUT_SET_DOC)];
    // (e) four sets lock the import with a reason.
    m.extraSummaries.p1=[1,2,3].map(n=>({set_id:"so-0000000"+n,state:"damaged"}));
    await openOverview("p1");
    assert(await page.locator('[data-action="import"]').isDisabled(),label+": import locked at four sets");
    assert.strictEqual(await page.textContent('[data-blocked="import"]'),D["stakeout.import_blocked_sets"],label+": the reason");
    m.extraSummaries={};
    // (f) an unknown sub-path becomes the overview without a new history entry.
    const length=await page.evaluate(()=>history.length);
    await go("projects/p1/stakeout/bogus");
    await page.waitForFunction(()=>location.hash==="#projects/p1/stakeout");
    await page.waitForSelector(".stakeout-overview");
    assert.strictEqual(await page.evaluate(()=>history.length),length+1,label+": the bogus hash itself is the only new entry");
  }
  await setLanguage("en");
  // (g) device load: one live request per second, no other poll, the pill stays Live; a 503 shows, the next packet heals.
  stakeoutReset();await openOverview("p1");
  const from=requestLog.length,liveFrom=m.liveRequests.length;
  await page.waitForTimeout(10000);
  const liveCount=m.liveRequests.length-liveFrom;
  assert(liveCount>=9&&liveCount<=11,"stakeout: 9 to 11 live requests in 10 s, got "+liveCount);
  assert.strictEqual(requests("/api/ui/live",from).length,0,"stakeout: no /api/ui/live");
  assert.strictEqual(requests("/api/tracking/map",from).length,0,"stakeout: no map poll");
  assert.strictEqual(m.liveMaxOpen,1,"stakeout: never two live requests at once");
  await page.waitForTimeout(2000);
  assert.strictEqual(await pill(),"live","stakeout: the header pill is Live after 12 s");
  m.liveQueue.push({status:503,body:{error:"unavailable",message:"Temporarily unavailable"}});
  await page.waitForFunction(()=>document.getElementById("live-state").dataset.link==="error");
  await page.waitForFunction(()=>document.getElementById("live-state").dataset.link==="live");
  // (h) layouts and touch targets.
  for(const width of [320,360,390,480,1280]){
    await page.setViewportSize({width,height:800});await page.waitForTimeout(200);
    assertLayout(await layoutAudit(page),"stakeout overview "+width);
    assert.deepStrictEqual(await smallTargets(),[],"stakeout overview "+width+": touch targets");
  }
  await page.setViewportSize({width:320,height:640});
  await page.click('[data-card="stakeout-settings"] button');await page.waitForSelector("dialog.stakeout-settings[open]");
  await assertSheetLayout(page,"stakeout settings 320");
  await page.keyboard.press("Escape");await page.waitForSelector("dialog.stakeout-settings",{state:"detached"});
  await page.setViewportSize({width:360,height:900});
  for(const lang of ["en","de"]){
    const D=T[lang],label="stakeout calm "+lang;
    await setLanguage(lang);
    // (i) a calm first overview: one notice, no error, no automatic retry; the tap loads again.
    await go("projects");await page.waitForFunction(()=>!document.querySelector(".stakeout-page"));await page.waitForTimeout(500);
    stakeoutReset();m.overviewAnswers.push({status:503,body:{error:"stakeout_busy",message:"busy"}});
    const overviewFrom=m.overviewRequests.length;
    await go("projects/p1/stakeout");
    await page.waitForSelector('[data-notice="stakeout-calm"][data-code="stakeout_busy"]');
    assert.strictEqual(await page.locator('[data-notice="stakeout-calm"]').count(),1,label+": one calm notice");
    assert((await page.textContent('[data-notice="stakeout-calm"]')).includes(D["stakeout.calm.stakeout_busy"]),label+": calm text");
    const liveStart=m.liveRequests.length;
    await page.waitForTimeout(5000);
    assert.strictEqual(m.overviewRequests.length-overviewFrom,1,label+": no automatic retry");
    const calmLive=m.liveRequests.length-liveStart;
    assert(calmLive>=4&&calmLive<=6,label+": the live poll keeps its pace, got "+calmLive);
    assert.strictEqual(await dockErrors(),0,label+": no error in the dock");
    await page.click('[data-action="retry-overview"]');
    await page.waitForSelector(".stakeout-overview [data-set-id]");
    assert.strictEqual(m.overviewRequests.length-overviewFrom,2,label+": one more overview per tap");
    assert.strictEqual(await page.locator('[data-notice="stakeout-calm"]').count(),0,label+": the notice is gone");
    // (j) a calm refresh keeps the page; a calm live answer never turns the pill into an error.
    await openOverview("p2");
    await mark('[data-part="sets"]');
    m.overviewAnswers.push({status:503,body:{error:"stakeout_write_uncertain",message:"locked"}});
    await page.click('[data-notice="stakeout-inactive"] button');
    await page.waitForSelector('[data-notice="stakeout-calm"][data-code="stakeout_write_uncertain"]');
    assert(await marked('[data-part="sets"]'),label+": the set list stays");
    assert((await page.textContent('[data-notice="stakeout-calm"]')).includes(D["stakeout.calm.stakeout_write_uncertain"]),label+": write latch text");
    assert.strictEqual(await dockErrors(),0,label+": no dock error on a calm refresh");
    for(let i=0;i<4;i++)m.liveQueue.push({status:503,body:{error:"stakeout_write_uncertain",message:"locked"}});
    for(let i=0;i<6;i++){await page.waitForTimeout(500);assert.notStrictEqual(await pill(),"error",label+": the pill never shows unreachable");}
    assert.strictEqual(await page.locator('[data-notice="stakeout-calm"]').count(),1,label+": still one notice");
    m.actionAnswers.delete_set=[{status:503,body:{error:"stakeout_write_uncertain",message:"locked"}}];
    await page.click('[data-set-id="so-3a4739b9"] [data-action="delete-set"]');
    await page.locator("dialog[open] button.danger").click();
    await page.waitForFunction(()=>{const node=document.querySelector('[data-set-id="so-3a4739b9"] [data-set-error]');return node&&!node.hidden;});
    assert.strictEqual(await page.textContent('[data-set-id="so-3a4739b9"] [data-set-error]'),D["stakeout.error.stakeout_write_uncertain"],label+": the latch at the set");
    assert.strictEqual(await page.locator('[data-set-id="so-3a4739b9"]').count(),1,label+": the set stays");
    tracking.active_project_id="p1";
    // (j, ruling 29.10.) the device reports the latch in 200 answers: the packet reason and the overview flag.
    stakeoutReset();m.wp4a=true;m.writeUncertain=true;
    await openOverview("p1");
    await page.waitForSelector('[data-notice="stakeout-calm"][data-code="stakeout_write_uncertain"]');
    m.writeUncertain=false;m.packet.guidance={...m.packet.guidance,reasons:["write_uncertain"]};
    await page.waitForTimeout(2500);
    assert.strictEqual(await page.locator('[data-notice="stakeout-calm"][data-code="stakeout_write_uncertain"]').count(),1,label+": the packet reason keeps the notice");
    assert.strictEqual(await pill(),"live",label+": the pill stays Live with the latch");
    m.packet.guidance={...m.packet.guidance,reasons:[]};
  }
  // Task 5: the import page, in English and German.
  const golden=JSON.parse(fs.readFileSync(root+"/tests/fixtures/stakeout_sample_package.json","utf8"));
  const fileInput=".stakeout-import input[type=file]";
  const openImport=async()=>{await openOverview("p1");await page.click('[data-action="import"]');await page.waitForSelector(fileInput);};
  const pick=(name,text)=>page.setInputFiles(fileInput,text===undefined?root+"/tests/fixtures/"+name:{name,mimeType:"application/geo+json",buffer:Buffer.from(text)});
  const importError=()=>page.waitForFunction(()=>{const node=document.querySelector("[data-import-error]");return node&&!node.hidden;});
  const pointCollection=coordinates=>JSON.stringify({type:"FeatureCollection",features:[{type:"Feature",properties:{name:"P"},geometry:{type:"Point",coordinates}}]});
  for(const lang of ["en","de"]){
    const D=T[lang],label="stakeout import "+lang;
    stakeoutReset();tracking.active_project_id="p1";await setLanguage(lang);
    // (a) sample file: preview, the selection and the request body of the language's package.
    await openImport();
    assert.strictEqual(await page.evaluate(()=>location.hash),"#projects/p1/stakeout/import",label+": route");
    await pick("stakeout_sample.geojson");
    await page.waitForSelector(".import-preview .import-part");
    const names=await page.$$eval(".import-part .import-part-name",nodes=>nodes.map(node=>node.textContent));
    assert.deepStrictEqual(names,["Line A",fill(D["stakeout.default_point"],{n:2}),"Manhole","Route B (1)","Route B (2)"],label+": parts");
    assert.strictEqual(await page.locator('.import-part[data-part="0"] input[data-coord]:checked').count(),4,label+": Line A all four vertices chosen");
    const skipped=await page.textContent(".import-skipped");
    assert(skipped.includes("Polygon")&&skipped.includes(D["stakeout.skip_unsupported_geometry"]),label+": the polygon is listed as not imported");
    assert.strictEqual(await page.textContent('[data-part="counter"]'),fill(D["stakeout.import_counter"],{count:12}),label+": counter");
    assert.strictEqual(await page.textContent('[data-action="submit"]'),fill(D["stakeout.import_submit"],{count:12}),label+": submit label");
    await page.click('[data-action="submit"]');
    await page.waitForFunction(()=>location.hash==="#projects/p1/stakeout");
    const expected=structuredClone(golden);expected.project_id="p1";
    for(const item of expected.parts.concat(expected.targets))if(item.name==="Point 2")item.name=fill(D["stakeout.default_point"],{n:2});
    assert.deepStrictEqual(m.imports[0],expected,label+": the request body is the package of this language");
    // (b) the mixed file: every skip and hint, the overlap with both names, the every-n help.
    await openImport();
    await pick("stakeout_mixed.geojson");
    await page.waitForSelector(".import-preview .import-part");
    assert.strictEqual(await page.locator(".import-skipped li").count(),4,label+": four skipped features");
    for(const code of ["closed_line","zero_segments","extra_position_elements","props_trimmed","long_line","overlap","extent"])
      assert(await page.locator('.import-hints [data-hint="'+code+'"]').count()>=1,label+": hint "+code);
    const overlap=await page.textContent('.import-hints [data-hint="overlap"]');
    assert(overlap.includes("Overlap A")&&overlap.includes("Overlap B"),label+": both names of the overlap");
    assert.strictEqual(await page.textContent('[data-part="counter"]'),fill(D["stakeout.import_counter"],{count:141}),label+": all 141 chosen");
    const track=page.locator('.import-part[data-part="11"]');
    await track.locator("summary").click();
    await track.locator("[data-every]").fill("7");
    await track.locator('[data-action="every"]').click();
    assert.strictEqual(await page.textContent('[data-part="counter"]'),fill(D["stakeout.import_counter"],{count:141-120+18}),label+": every 7th of the 120");
    // (c) blocking files never send; a device refusal names its path.
    const posts=m.imports.length;
    await pick("broken.geojson",'{"type":"FeatureCollection","features":[');
    await page.waitForSelector('[data-import-block="invalid_json"]');
    assert(await page.locator('[data-action="submit"]').isDisabled(),label+": broken file blocks");
    await pick("mercator.geojson",JSON.stringify({type:"FeatureCollection",crs:{type:"name",properties:{name:"urn:ogc:def:crs:EPSG::3857"}},features:[]}));
    await page.waitForSelector('[data-import-block="crs_not_wgs84"]');
    assert((await page.textContent('[data-import-block="crs_not_wgs84"]')).includes(D["import.crs_not_wgs84"]),label+": CRS text");
    await pick("high.geojson",pointCollection([10,51,150000]));
    await page.waitForSelector('[data-import-block="coordinate_out_of_range"]');
    assert((await page.textContent('[data-import-block="coordinate_out_of_range"]')).includes("features[0].geometry.coordinates[2]"),label+": height path");
    assert.strictEqual(m.imports.length,posts,label+": no POST for blocked files");
    await pick("stakeout_sample.geojson");
    await page.waitForSelector(".import-preview .import-part");
    m.importAnswers.push({status:400,body:{error:"coordinate_out_of_range",message:"Out of range",path:"targets[3].lat_e8"}});
    await page.click('[data-action="submit"]');
    await importError();
    const refusal=await page.textContent("[data-import-error]");
    assert(refusal.includes(D["import.coordinate_out_of_range"])&&refusal.includes("targets[3].lat_e8"),label+": the device path "+refusal);
    // (d) a recording line: pause, then the same import again; already imported: a way to the overview.
    m.importAnswers.push({status:409,body:{error:"recording_active",message:"Recording"}});
    await page.click('[data-action="submit"]');
    await importError();
    const pauses=trackingPostLog.length;
    await page.click(".import-extra button");
    await page.waitForFunction(()=>!document.querySelector(".import-extra button"));
    assert.deepStrictEqual(trackingPostLog.slice(pauses).map(post=>post.action),["pause"],label+": one pause");
    assert(!(await page.locator('[data-action="submit"]').isDisabled()),label+": send again possible");
    m.importAnswers.push({status:409,body:{error:"already_imported",message:"Imported",set_id:"so-3a4739b9"}});
    await page.click('[data-action="submit"]');
    await importError();
    assert.strictEqual(await page.getAttribute('.import-extra [data-action="overview"]',"href"),"/#projects/p1/stakeout",label+": link to the overview");
    // (e) leaving during a slow import: nothing lands on the next page, the result goes to the dock.
    m.importAnswers.push({delay:3000});
    await page.click('[data-action="submit"]');
    await page.waitForTimeout(300);
    await go("projects/p1/stakeout/targets");
    await page.waitForSelector(".stakeout-targets",{state:"attached"});
    await mark(".stakeout-targets");
    await page.waitForSelector(".feedback-dock .feedback-item.ok",{timeout:8000});
    assert.strictEqual(await page.evaluate(()=>location.hash),"#projects/p1/stakeout/targets",label+": the late success does not navigate");
    assert(await marked(".stakeout-targets"),label+": the next page is untouched");
    // (review, finding 2) a running import keeps the button, the checkboxes and the file choice locked; one import only.
    await openImport();await pick("stakeout_sample.geojson");await page.waitForSelector(".import-preview .import-part");
    const sent=m.imports.length;m.importAnswers.push({delay:2500});
    await page.click('[data-action="submit"]');
    await page.waitForTimeout(300);
    await page.evaluate(()=>document.querySelector('.import-part[data-part="0"] input[data-coord="1"]').click());
    assert(await page.locator('[data-action="submit"]').isDisabled(),label+": the button stays locked after a checkbox change");
    assert(await page.locator(fileInput).isDisabled(),label+": no new file during the import");
    assert(await page.locator('.import-part[data-part="0"] input[data-coord="1"]').isDisabled(),label+": the selection is locked during the import");
    await page.evaluate(()=>document.querySelector('[data-action="submit"]').click());
    await page.waitForFunction(()=>location.hash==="#projects/p1/stakeout");
    assert.strictEqual(m.imports.length-sent,1,label+": one import only");
    // (f) narrow layout with a long part name.
    await page.setViewportSize({width:320,height:700});
    await openImport();
    await pick("long.geojson",JSON.stringify({type:"FeatureCollection",features:[{type:"Feature",properties:{name:"W".repeat(80)},geometry:{type:"LineString",coordinates:[[10,51],[10.0001,51]]}}]}));
    await page.waitForSelector(".import-preview .import-part");
    assertLayout(await layoutAudit(page),label+": import 320");
    assert.deepStrictEqual(await smallTargets(),[],label+": import 320 touch targets");
    await page.setViewportSize({width:360,height:900});
  }
  // Task 6: the target list in walking order.
  const openList=async(id="p1")=>{await go("projects");await page.waitForFunction(()=>!document.querySelector(".stakeout-page"));await go("projects/"+id+"/stakeout/targets");await page.waitForSelector(".stakeout-targets a.target-row");};
  const rowStatus=tid=>page.getAttribute('a.target-row[data-tid="'+tid+'"]',"data-status");
  const roverAtReference=()=>{m.packet={...m.packet,position:{...m.packet.position,lat_e8:5100000000,lon_e8:1000000000}};};
  for(const lang of ["en","de"]){
    const D=T[lang],label="stakeout list "+lang;
    stakeoutReset();tracking.active_project_id="p1";await setLanguage(lang);roverAtReference();
    // (a) twelve rows in file order, all open, the distance of Point 2 from the rover; "—" without a usable frame.
    await openList();
    const tids=await page.$$eval("a.target-row",rows=>rows.map(row=>row.dataset.tid));
    assert.deepStrictEqual(tids,STAKEOUT_SET_DOC.targets.map(target=>target.tid),label+": file order");
    const open=await page.$$eval("a.target-row",rows=>rows.map(row=>row.dataset.status));
    assert.deepStrictEqual([...new Set(open)],["open"],label+": every target open without WP4");
    assert((await page.textContent('a.target-row[data-tid="so-3a4739b9/1/1"]')).includes(D["stakeout.status.open"]),label+": status text");
    await page.waitForFunction(text=>{const node=document.querySelector('a.target-row[data-tid="so-3a4739b9/2/1"] .target-distance');return node&&node.textContent===text;},number(lang,0.05,2)+" m");
    assert.strictEqual(await page.getAttribute('a.target-row[data-tid="so-3a4739b9/2/1"]',"href"),"/#projects/p1/stakeout/target/so-3a4739b9%2F2%2F1",label+": row link");
    m.packet={...m.packet,stale:true};
    await page.waitForFunction(()=>document.querySelector('a.target-row[data-tid="so-3a4739b9/2/1"] .target-distance').textContent==="—");
    m.packet={...m.packet,stale:false};
    // (b) reversing Line A: one set_order, the rows turn round, the switch is pressed.
    await page.click('.target-part[data-part-id="so-3a4739b9/1"] [data-action="reverse"]');
    await page.waitForFunction(()=>document.querySelector('.target-part[data-part-id="so-3a4739b9/1"] [data-action="reverse"]').getAttribute("aria-pressed")==="true");
    const post=m.posts.pop();
    assert.deepStrictEqual({action:post.action,part_id:post.part_id,reversed:post.reversed},{action:"set_order",part_id:"so-3a4739b9/1",reversed:true},label+": set_order");
    const lineA=await page.$$eval('.target-part[data-part-id="so-3a4739b9/1"] a.target-row',rows=>rows.map(row=>row.dataset.tid));
    assert.deepStrictEqual(lineA,["so-3a4739b9/1/4","so-3a4739b9/1/3","so-3a4739b9/1/2","so-3a4739b9/1/1"],label+": P004 to P001");
    m.actionAnswers.set_order=[{status:409,body:{error:"project_not_active",message:"Not active"}}];
    await page.click('.target-part[data-part-id="so-3a4739b9/4"] [data-action="reverse"]');
    await page.waitForSelector('.stakeout-targets [data-notice="stakeout-inactive"]');
    assert.strictEqual(await page.getAttribute('.target-part[data-part-id="so-3a4739b9/4"] [data-action="reverse"]',"aria-pressed"),"false",label+": order unchanged on a refusal");
    // (c) a slow set download and leaving: no list on the next page.
    stakeoutReset();m.setDelay=2500;
    await go("projects/p1/stakeout/targets");
    await page.waitForSelector(".stakeout-targets");
    await go("projects/p1/stakeout");
    await page.waitForSelector(".stakeout-overview");
    await page.waitForTimeout(3000);
    assert.strictEqual(await page.locator("a.target-row").count(),0,label+": the late set does not render on the overview");
    m.setDelay=0;
    // (d) the packet's status of the selected target wins over the model.
    stakeoutReset();
    await openList();
    m.packet={...m.packet,target:{tid:"so-3a4739b9/2/1",lat_e8:5100000027,lon_e8:999999943,status:"confirmed"}};
    await page.waitForFunction(()=>document.querySelector('a.target-row[data-tid="so-3a4739b9/2/1"]').dataset.status==="confirmed");
    assert((await page.textContent('a.target-row[data-tid="so-3a4739b9/2/1"]')).includes("● "+D["stakeout.status.confirmed"]),label+": confirmed mark");
    assert.strictEqual(await rowStatus("so-3a4739b9/1/1"),"open",label+": the others stay");
    // (e) narrow layout.
    await page.setViewportSize({width:320,height:700});await page.waitForTimeout(200);
    assertLayout(await layoutAudit(page),label+": list 320");
    assert.deepStrictEqual(await smallTargets(),[],label+": list 320 touch targets");
    await page.setViewportSize({width:360,height:900});
    // (f) WP4 A without the list: every row unknown, no next open target; later a packet marks one row unknown.
    stakeoutReset();m.wp4a=true;
    await openList();
    const statuses=await page.$$eval("a.target-row",rows=>rows.map(row=>row.dataset.status));
    assert.deepStrictEqual([...new Set(statuses)],["unknown"],label+": all unknown with WP4 A");
    assert((await page.textContent('a.target-row[data-tid="so-3a4739b9/1/1"]')).includes("? "+D["stakeout.status.unknown"]),label+": unknown mark");
    await openOverview("p1");
    await page.waitForFunction(()=>document.querySelector('[data-part="counts"]').textContent.length>0);
    await page.waitForTimeout(500);
    assert(await page.locator('[data-action="next"]').isDisabled(),label+": no next open target while unknown");
    stakeoutReset();
    m.packet={...m.packet,target:{tid:"so-3a4739b9/2/1",lat_e8:5100000027,lon_e8:999999943,status:"unknown"}};
    await openList();
    await page.waitForFunction(()=>document.querySelector('a.target-row[data-tid="so-3a4739b9/2/1"]').dataset.status==="unknown");
    const others=await page.$$eval("a.target-row",rows=>rows.filter(row=>row.dataset.status==="unknown").map(row=>row.dataset.tid));
    assert.deepStrictEqual(others,["so-3a4739b9/2/1"],label+": only Point 2 unknown");
  }
  // Task 8: search and fine guidance with the map, the dial, the quality bar and the action dock.
  const P2="so-3a4739b9/2/1",P2_PATH="projects/p1/stakeout/target/so-3a4739b9%2F2%2F1";
  const openTarget=async(path=P2_PATH)=>{await go("projects");await page.waitForFunction(()=>!document.querySelector(".stakeout-page"));await go(path);await page.waitForSelector(".stakeout-guide .guide-name, .stakeout-guide [data-problem]");};
  const selects=()=>m.posts.filter(post=>post.action==="select_target");
  const guidance=(values,extra)=>{m.packet={...m.packet,...(extra||{}),guidance:{...STAKEOUT_PACKET.guidance,...values}};};
  const readyAt=()=>{m.packet={...STAKEOUT_PACKET,target:{...STAKEOUT_PACKET.target}};};
  const waitSelects=async count=>{for(let i=0;i<50&&selects().length<count;i++)await page.waitForTimeout(100);};
  const modeIs=value=>page.waitForFunction(v=>document.querySelector(".stakeout-guide").dataset.mode===v,value);
  for(const lang of ["en","de"]){
    const D=T[lang],label="stakeout guide "+lang;
    stakeoutReset();tracking.active_project_id="p1";await setLanguage(lang);
    // (a) check case 6: one select_target, fine view with the walking text, marker at (7.2, 5.4), state Ready.
    await openTarget();await waitSelects(1);
    assert.deepStrictEqual(selects().map(post=>post.tid),[P2],label+": one select_target");
    readyAt();
    await modeIs("fine");
    await page.waitForFunction(text=>document.querySelector(".guide-direction").textContent===text,
      fill(D["stakeout.go_north"],{value:number(lang,0.03,2)})+" / "+fill(D["stakeout.go_west"],{value:number(lang,0.04,2)}));
    await page.waitForFunction(text=>document.querySelector(".guide-state").textContent.trim()===text,"✓ "+D["stakeout.state_ready"]);
    await page.waitForTimeout(500);
    // Screenshots for the documentation (English), only with RTK_SHOT_DIR.
    if(lang==="en"&&process.env.RTK_SHOT_DIR){
      const shots=process.env.RTK_SHOT_DIR,size=page.viewportSize();fs.mkdirSync(shots,{recursive:true});
      for(const [suffix,view] of [["phone",{width:360,height:780}],["desktop",{width:1280,height:800}]]){
        await page.setViewportSize(view);await page.waitForTimeout(600);await page.screenshot({path:`${shots}/stakeout-${suffix}.png`});}
      await page.setViewportSize(size);await page.waitForTimeout(200);
    }
    const marker=await page.evaluate(()=>{const dial=document.querySelector(".target-dial").getBoundingClientRect(),dot=document.querySelector(".dial-rover circle").getBoundingClientRect();
      return[(dot.left+dot.width/2-dial.left)/dial.width*200-100,(dot.top+dot.height/2-dial.top)/dial.height*200-100];});
    assert(Math.abs(marker[0]-7.2)<=0.5&&Math.abs(marker[1]-5.4)<=0.5,label+": marker at "+marker);
    guidance({state:"coarse",dist_m:23.4,bearing_deg:316,dn_m:16.8,de_m:-16.2});
    await modeIs("search");
    await page.waitForFunction(text=>document.querySelector(".stakeout-distance").textContent===text,number(lang,23.4,1)+" m");
    assert.strictEqual(await page.textContent(".guide-bearing"),"316° ("+D["stakeout.cardinal_nw"]+")",label+": bearing");
    for(const [dist,mode] of [[2.9,"fine"],[4,"fine"],[5.01,"search"]]){guidance({state:"coarse",dist_m:dist,bearing_deg:316,dn_m:dist*0.7,de_m:-dist*0.7});await modeIs(mode);}
    guidance({state:"fine",dist_m:1,bearing_deg:316,dn_m:0.7,de_m:-0.7});
    await modeIs("fine");
    await page.click('[data-tool="mode"]');
    await modeIs("search");
    await page.waitForTimeout(1500);
    assert.strictEqual(await page.evaluate(()=>document.querySelector(".stakeout-guide").dataset.mode),"search",label+": the search button holds the search at 1 m");
    await page.click('[data-tool="mode"]');await modeIs("fine");
    // (b) check case 7: the movement arrow only with a valid motion, labelled, turned by the course.
    readyAt();m.packet.motion={speed_mps:0.1,course_deg:null,valid:false};
    await page.waitForTimeout(1300);
    assert.strictEqual(await page.locator(".dial-motion:visible").count(),0,label+": no arrow without motion");
    m.packet.motion={speed_mps:0.8,course_deg:90,valid:true};
    await page.waitForSelector(".dial-motion:visible");
    assert.strictEqual(await page.getAttribute(".dial-motion","transform"),"rotate(90)",label+": arrow turned by 90°");
    assert.strictEqual(await page.textContent(".dial-motion text"),D["stakeout.motion_direction"],label+": arrow label");
    // (c) the target is gone under the view: a notice and "Select again", no automatic selection.
    m.packet={...m.packet,target:null};
    await page.waitForSelector('[data-notice="target-lost"]');
    assert.strictEqual(await page.textContent('[data-notice="target-lost"]'),D["stakeout.target_lost"],label+": lost text");
    const count=selects().length;
    await page.waitForTimeout(2500);
    assert.strictEqual(selects().length,count,label+": no selection without a tap");
    await page.click('[data-action="reselect"]');
    await page.waitForSelector('[data-notice="target-lost"]',{state:"detached"});
    assert.strictEqual(selects().length,count+1,label+": one select_target per tap");
    // (review, finding 4) the map mark of the target follows its status: shape, geometry and label.
    m.packet={...m.packet,target:{tid:P2,lat_e8:5100000027,lon_e8:999999943,status:"skipped"}};
    await page.waitForFunction(()=>{const mark=document.querySelector('.stakeout-map .stakeout-target[data-tid="so-3a4739b9/2/1"]');return mark&&mark.dataset.shape==="diamond";});
    const markState=await page.evaluate(()=>{const mark=document.querySelector('.stakeout-map .stakeout-target[data-tid="so-3a4739b9/2/1"]');return{label:mark.getAttribute("aria-label"),geometry:mark.firstElementChild.localName,status:mark.dataset.status};});
    assert.deepStrictEqual(markState,{label:"Point 2: "+D["stakeout.status.skipped"],geometry:"path",status:"skipped"},label+": the mark follows the status");
    // (d) refusals of the selection.
    stakeoutReset();m.actionAnswers.select_target=[{status:409,body:{error:"recording_active",message:"Recording"}}];
    await openTarget();
    await page.waitForSelector('.guide-status .notice.critical');
    assert.strictEqual(await page.textContent(".guide-status .notice.critical"),D["stakeout.error.recording_active"],label+": recording text");
    const pauses=trackingPostLog.length;
    await page.click(".guide-status button");
    await page.waitForFunction(()=>!document.querySelector(".guide-status .notice.critical"));await waitSelects(2);
    assert.deepStrictEqual(trackingPostLog.slice(pauses).map(post=>post.action),["pause"],label+": pause");
    assert.strictEqual(selects().length,2,label+": select again after the pause");
    stakeoutReset();m.actionAnswers.select_target=[{status:409,body:{error:"project_changed",message:"Changed"}}];
    await go("projects");await page.waitForFunction(()=>!document.querySelector(".stakeout-page"));await page.waitForTimeout(800);
    const overviews=m.overviewRequests.length,trackingFrom=requestLog.length;
    await go(P2_PATH);await page.waitForSelector(".stakeout-guide .guide-name");await waitSelects(2);await page.waitForTimeout(500);
    assert.strictEqual(selects().length,2,label+": a second select_target after project_changed");
    assert.strictEqual(m.overviewRequests.length-overviews,2,label+": the mount's overview and one refresh");
    assert.strictEqual(requests("/api/tracking",trackingFrom).filter(entry=>entry.method==="GET"&&!entry.search).length,1,label+": no page rebuild");
    stakeoutReset();m.actionAnswers.select_target=[{status:404,body:{error:"target_not_found",message:"Gone"}}];
    await openTarget();
    await page.waitForSelector('.guide-status .notice.critical');
    assert.strictEqual(await page.textContent(".guide-status .notice.critical"),D["stakeout.error.target_not_found"],label+": not found text");
    assert.strictEqual(await page.locator('.guide-status [data-action="overview"]').count(),1,label+": link to the overview");
    stakeoutReset();m.actionAnswers.select_target=[{status:503,body:{error:"stakeout_busy",message:"Busy"}}];
    await openTarget();
    await page.waitForSelector('[data-notice="stakeout-calm"][data-code="stakeout_busy"]');
    assert.strictEqual(await page.locator('.guide-status [data-action="reselect"]').count(),1,label+": select again offered");
    await page.waitForTimeout(5000);
    assert.strictEqual(selects().length,1,label+": no automatic selection after busy");
    assert.strictEqual(await dockErrors(),0,label+": no dock error for busy");
    // (e) follow: the position in the middle of the map at 100, 150 and 400 %, also 2 km outside the set; dragging stops it.
    stakeoutReset();
    await openTarget();
    guidance({state:"coarse",dist_m:23.4,bearing_deg:316,dn_m:16.8,de_m:-16.2});
    await modeIs("search");
    const centre=()=>page.evaluate(()=>{const map=document.querySelector(".stakeout-map").getBoundingClientRect(),dot=document.querySelector(".stakeout-map .map-position").getBoundingClientRect();return[dot.left+dot.width/2-(map.left+map.width/2),dot.top+dot.height/2-(map.top+map.height/2),dot.width>0];});
    const zoomTo=async percent=>{for(let i=0;i<8;i++){if(await page.locator('[data-tool="zoom-out"]').isDisabled())break;await page.click('[data-tool="zoom-out"]');}for(let i=0;i<{100:1,150:2,400:7}[percent];i++)await page.click('[data-tool="zoom-in"]');};
    let scaleAtOne=null;
    for(const width of [360,320]){
      await page.setViewportSize({width,height:800});
      for(const percent of [100,150,400]){
        await zoomTo(percent);
        const scale=await page.evaluate(()=>document.querySelector(".stakeout-map .map-label").textContent);
        if(percent===100)scaleAtOne=parseFloat(scale);
        else assert.strictEqual(scale,Number((scaleAtOne/(percent/100)).toPrecision(2))+" m",label+": scale label at "+percent+" % (review, finding 5)");
        await page.waitForTimeout(1300);
        const [dx,dy,shown]=await centre();
        assert(shown&&Math.abs(dx)<=2&&Math.abs(dy)<=2,label+": centred at "+percent+" % / "+width+" px: "+dx+","+dy);
      }
    }
    m.packet={...m.packet,position:{...m.packet.position,lat_e8:5101800000,lon_e8:1000000000}};
    await page.waitForTimeout(1300);
    {const [dx,dy,shown]=await centre();assert(shown&&Math.abs(dx)<=2&&Math.abs(dy)<=2,label+": centred 2 km outside the set");}
    const box=await page.locator(".stakeout-map").boundingBox();
    await page.mouse.move(box.x+box.width/2+30,box.y+box.height/2+30);await page.mouse.down();await page.mouse.move(box.x+box.width/2+90,box.y+box.height/2+60,{steps:4});await page.mouse.up();
    assert.strictEqual(await page.getAttribute('[data-tool="follow"]',"aria-pressed"),"false",label+": dragging stops following");
    await page.click('[data-tool="follow"]');
    await zoomTo(150);
    await go("projects/p1/stakeout/targets");await page.waitForSelector(".stakeout-targets a.target-row");
    await page.goBack();await page.waitForSelector(".stakeout-map");
    await page.waitForFunction(()=>/scale\(1\.5\)/.test(document.querySelector(".stakeout-map .map-world").style.transform));
    await page.setViewportSize({width:360,height:900});
    // The stakeout map draws the set without survey geometry: 12 targets, no empty map.
    assert.strictEqual(await page.locator(".stakeout-map g.stakeout-target").count(),12,label+": 12 targets on the map");
    assert.strictEqual(await page.locator(".stakeout-guide .map-empty").count(),0,label+": no empty map");
    // (f) "Next target" on Line A / P002 goes to P003; Back returns.
    stakeoutReset();
    await openTarget("projects/p1/stakeout/target/so-3a4739b9%2F1%2F2");
    await page.click('.stakeout-actions [data-action="next"]');
    await page.waitForFunction(()=>location.hash==="#projects/p1/stakeout/target/so-3a4739b9%2F1%2F3");
    await page.goBack();
    await page.waitForFunction(()=>location.hash==="#projects/p1/stakeout/target/so-3a4739b9%2F1%2F2");
    // (g) the dock at 320 x 640 above the mobile navigation, the slot is there, a late dock message never covers it; 80-character name.
    stakeoutReset();m.sets.p1[0].targets[4].name="L".repeat(80);m.sets.p1[0].imported_at+=1; // another import time: the page cache must not answer
    await page.setViewportSize({width:320,height:640});
    await openImport();await pick("stakeout_sample.geojson");await page.waitForSelector(".import-preview .import-part");
    m.importAnswers.push({delay:1500});await page.click('[data-action="submit"]');
    await go(P2_PATH);await page.waitForSelector(".stakeout-guide .guide-name");
    await page.waitForSelector(".stakeout-actions [data-stakeout-actions]",{state:"attached"});
    await page.waitForSelector(".feedback-dock .feedback-item.ok",{timeout:8000});
    const rects=await page.evaluate(()=>{const r=el=>{const b=el.getBoundingClientRect();return{top:b.top,bottom:b.bottom,left:b.left,right:b.right,height:b.height};},dock=document.querySelector(".stakeout-actions"),nav=document.querySelector(".mobile-nav");
      return{dock:r(dock),nav:r(nav),buttons:[...dock.querySelectorAll("button")].map(r),items:[...document.querySelectorAll(".feedback-dock .feedback-item")].map(r)};});
    assert(rects.dock.bottom<=rects.nav.top+1&&rects.dock.top>=0,label+": dock above the navigation "+JSON.stringify(rects));
    assert(rects.buttons.every(b=>b.height>=47.5),label+": dock buttons 48 px");
    assert(rects.items.length&&rects.items.every(b=>b.bottom<=rects.dock.top||b.top>=rects.dock.bottom||b.right<=rects.dock.left||b.left>=rects.dock.right),label+": a dock message does not cover the actions "+JSON.stringify(rects));
    assert.strictEqual(await page.textContent(".stakeout-guide .guide-name"),"L".repeat(80),label+": long name");
    assert.deepStrictEqual(await smallTargets(),[],label+": guide 320 touch targets");
    assertLayout(await layoutAudit(page),label+": guide 320");
    await page.setViewportSize({width:360,height:900});
    // (h) a target of a thinned line beyond 2000 (Task 2 package) can be chosen and guided.
    stakeoutReset();
    const thinned=structuredClone(STAKEOUT_SET_DOC);thinned.set_id="so-0000abcd";thinned.targets=[{...thinned.targets[4],tid:"so-0000abcd/1/3000",part_id:"so-0000abcd/1",seq:3000,coord_index:2999,name:"Recorded / P3000"}];
    thinned.parts=[{...thinned.parts[0],part_id:"so-0000abcd/1",coords_total:3000,coords_thinned:true}];
    m.sets.p1.push(thinned);
    await openTarget("projects/p1/stakeout/target/so-0000abcd%2F1%2F3000");await waitSelects(1);
    assert.deepStrictEqual(selects().map(post=>post.tid),["so-0000abcd/1/3000"],label+": select_target of the thinned endpoint");
    await page.waitForFunction(()=>document.querySelector(".stakeout-guide .guide-name").textContent==="Recorded / P3000");
  }
  // Task 9: freshness, connection, hidden tab, fast navigation, offline maps, device load, restart, layouts, language, calm.
  const liveStats=()=>page.evaluate(()=>({...window.__stakeoutLive}));
  const resetLiveStats=()=>page.evaluate(()=>{window.__stakeoutLive.max=window.__stakeoutLive.open;window.__stakeoutLive.count=0;});
  const linkText=()=>page.textContent('.stakeout-quality [data-field="link"] [data-value]');
  const stateText=()=>page.evaluate(()=>document.querySelector(".guide-state").textContent.trim());
  const roverShown=()=>page.evaluate(()=>{const g=document.querySelector(".dial-rover");return Boolean(g)&&g.style.display!=="none"&&g.getBoundingClientRect().width>0;});
  const openFine=async()=>{stakeoutReset();readyAt();await openTarget();await modeIs("fine");await page.waitForFunction(text=>document.querySelector(".guide-state").textContent.trim()===text,"✓ "+T[await page.evaluate(()=>RtkI18n.getLanguage())]["stakeout.state_ready"]);};
  for(const lang of ["en","de"]){
    const D=T[lang],label="stakeout acceptance "+lang;
    await setLanguage(lang);
    // (a) check case 9: no answer at all – the frame turns stale from the ticker alone.
    await openFine();
    await resetLiveStats();
    const hold=stakeoutLiveHold(),heldAt=Date.now();
    await page.waitForFunction(text=>document.querySelector('.stakeout-quality [data-field="link"] [data-value]').textContent.includes(text),fill(D["stakeout.no_data_since"],{seconds:4}),{timeout:5500});
    assert(Date.now()-heldAt<=5600,label+": no data shown within 4.5 s of the last packet");
    assert.strictEqual(await page.getAttribute(".stakeout-guide","data-stale"),"true",label+": numbers grey");
    assert.notStrictEqual(await stateText(),"✓ "+D["stakeout.state_ready"],label+": not ready");
    assert.strictEqual(await page.getAttribute(".stakeout-actions","data-stale"),"true",label+": the dock is locked");
    assert.strictEqual(await roverShown(),false,label+": no rover marker");
    await page.waitForTimeout(Math.max(0,10500-(Date.now()-heldAt)));
    assert.notStrictEqual(await pill(),"live",label+": the header pill no longer says Live after 10 s");
    assert((await liveStats()).max<=1,label+": never two live requests open while hanging");
    m.autoSeq=false;m.packet={...m.packet,fix_seq:m.seq};hold.release();
    await page.waitForTimeout(2500);
    assert.notStrictEqual(await stateText(),"✓ "+D["stakeout.state_ready"],label+": the same fix_seq stays stale");
    m.autoSeq=true;
    await page.waitForFunction(text=>document.querySelector(".guide-state").textContent.trim()===text,"✓ "+D["stakeout.state_ready"]);
    await page.waitForFunction(()=>document.getElementById("live-state").dataset.link==="live");
    // (b) an outdated position.
    m.packet={...m.packet,stale:true,fix_age_ms:5200,guidance:{...m.packet.guidance,state:"no_position"}};
    await page.waitForFunction(text=>document.querySelector(".guide-remaining").textContent===text,fill(D["stakeout.stale_since"],{seconds:5}));
    await page.waitForFunction(()=>document.querySelector(".stakeout-map .map-position").classList.contains("stale"));
    assert.strictEqual(await roverShown(),false,label+": no rover marker for an outdated position");
    // (c) hidden tab: no request at all; visible again: one overview, the poll in pace.
    await openOverview("p1");
    const before=requestLog.length,overviewsBefore=m.overviewRequests.length;
    await page.evaluate(()=>window.__setHidden(true));
    await page.waitForTimeout(5000);
    assert.deepStrictEqual(requestLog.slice(before).map(entry=>entry.path),[],label+": no request while hidden");
    await page.evaluate(()=>window.__setHidden(false));
    await page.waitForSelector(".stakeout-overview [data-set-id]");
    const liveAfter=m.liveRequests.length;await page.waitForTimeout(3200);
    assert.strictEqual(m.overviewRequests.length-overviewsBefore,1,label+": one overview after the return");
    assert(m.liveRequests.length-liveAfter>=2,label+": the live poll runs again");
    // (d) fast navigation: no guidance on the list, at most one live request, no error.
    stakeoutReset();m.setDelay=1200;m.actionAnswers.select_target=[{delay:1500}];
    await openList();
    await resetLiveStats();const docksBefore=await dockErrors();
    await page.click('a.target-row[data-tid="so-3a4739b9/2/1"]');
    await page.waitForTimeout(200);
    await page.goBack();
    await page.waitForSelector(".stakeout-targets a.target-row");
    await page.waitForTimeout(3000);
    assert.strictEqual(await page.locator(".stakeout-guide").count(),0,label+": no guidance on the list");
    assert((await liveStats()).max<=1,label+": at most one live request open");
    assert.strictEqual(await dockErrors(),docksBefore,label+": no error message");
    m.setDelay=0;
    // (e) check case 13: no request leaves the device's origin; missing corrections are named.
    const foreign=[],outside=url=>!/^http:\/\/local\.test\//.test(url.href)&&!/^(data|blob|about):/.test(url.href),refuse=route=>{foreign.push(route.request().url());return route.abort();};
    await page.route(outside,refuse);
    stakeoutReset();await openOverview("p1");await openImport();await pick("stakeout_sample.geojson");await page.waitForSelector(".import-preview .import-part");
    await openList();await openTarget();
    guidance({state:"coarse",dist_m:23.4,bearing_deg:316,dn_m:16.8,de_m:-16.2});await modeIs("search");
    m.packet={...m.packet,corrections:{ntrip_state:"disabled",rtcm_age_sec:null,link:"missing"},gate:{accepted:false,reasons:["corrections_missing"]},
      guidance:{...STAKEOUT_PACKET.guidance,reasons:[]}};
    await modeIs("fine");
    await page.waitForFunction(text=>document.querySelector('.stakeout-quality [data-field="correction"] [data-value]').textContent===text,"⚠ "+D["stakeout.quality_corrections_missing"]);
    await page.waitForFunction(text=>document.querySelector("#live-state small").textContent.includes(text),D["ui.cause_unavailable_short"]);
    assert.deepStrictEqual(foreign,[],label+": no foreign URL");
    await page.unroute(outside,refuse);
    // (f) device load over all pages: one live request per second, nothing else periodic, sets once per page visit.
    if(lang==="en")for(const [name,path,ready] of [["overview","projects/p1/stakeout",".stakeout-overview [data-set-id]"],["import","projects/p1/stakeout/import",".stakeout-import input[type=file]"],
      ["list","projects/p1/stakeout/targets",".stakeout-targets a.target-row"],["search",P2_PATH,".stakeout-guide .guide-name"]]){
      stakeoutReset();if(name==="search")guidance({state:"coarse",dist_m:23.4,bearing_deg:316,dn_m:16.8,de_m:-16.2});
      await go("projects");await page.waitForFunction(()=>!document.querySelector(".stakeout-page"));await page.waitForTimeout(300);
      const from=requestLog.length,liveFrom=m.liveRequests.length,setFrom=m.setRequests.length,overviewFrom=m.overviewRequests.length;
      await go(path);await page.waitForSelector(ready);
      await page.waitForTimeout(10000);
      const lives=m.liveRequests.length-liveFrom;
      assert(lives>=9&&lives<=12,"stakeout load "+name+": live requests "+lives);
      assert.strictEqual(requests("/api/ui/live",from).length,0,"stakeout load "+name+": no /api/ui/live");
      assert.strictEqual(requests("/api/tracking/map",from).length,0,"stakeout load "+name+": no map request");
      assert.strictEqual(m.overviewRequests.length-overviewFrom,1,"stakeout load "+name+": one overview");
      const sets=m.setRequests.slice(setFrom);assert.strictEqual(sets.length,new Set(sets).size,"stakeout load "+name+": each set at most once");
    }
    // (g) restart and invalid packets.
    await openFine();
    m.autoSeq=false;m.packet={...m.packet,fix_seq:200,stale:true};
    await page.waitForFunction(text=>document.querySelector(".guide-state").textContent.trim()!==text,"✓ "+D["stakeout.state_ready"]);
    m.packet={...m.packet,fix_seq:3,stale:false};
    await page.waitForFunction(text=>document.querySelector(".guide-state").textContent.trim()===text,"✓ "+D["stakeout.state_ready"]);
    await page.waitForTimeout(1200);
    assert.strictEqual(await page.evaluate(()=>document.getElementById("live-state").className.includes("error")),false,label+": the pill does not stay outdated after a restart");
    m.autoSeq=true;
    await page.waitForTimeout(1200);
    m.liveQueue.push(...Array.from({length:5},()=>({packet:{...m.packet,schema:2,fix_seq:999}})));
    await page.waitForFunction(text=>document.querySelector('.stakeout-quality [data-field="link"] [data-value]').textContent.includes(text),D["stakeout.unexpected_data"]);
    assert.notStrictEqual(await stateText(),"✓ "+D["stakeout.state_ready"],label+": not ready after schema 2");
    await page.waitForFunction(()=>document.getElementById("live-state").dataset.link!=="live",null,{timeout:8000});
    // (i) a language change on the fine view keeps the target and the mode, without a second selection.
    await openFine();
    const selected=selects().length,other=lang==="en"?"de":"en";
    await setLanguage(other);
    await page.waitForSelector(".stakeout-guide .guide-name");
    await modeIs("fine");
    await page.waitForFunction(text=>document.querySelector(".guide-state").textContent.trim()===text,"✓ "+T[other]["stakeout.state_ready"]);
    await page.waitForTimeout(800);
    assert.strictEqual(selects().length,selected,label+": no second select_target after the language change");
    assert.strictEqual(await page.evaluate(()=>location.hash),"#"+P2_PATH,label+": same target");
    await setLanguage(lang);
    // (j) calm live answers: one notice, no error, the pace stays; the marker goes, the guidance comes back with the next packet.
    await openFine();
    await resetLiveStats();
    const calmFrom=m.liveRequests.length,docks=await dockErrors();
    m.liveQueue.push(...Array.from({length:6},()=>({status:503,body:{error:"stakeout_write_uncertain",message:"locked"}})));
    let worst="";const started=Date.now();
    while(Date.now()-started<6000){const link=await pill();if(link==="error")worst=link;await page.waitForTimeout(250);}
    assert.strictEqual(worst,"",label+": the pill never shows unreachable");
    assert.strictEqual(await page.locator('[data-notice="stakeout-calm"][data-code="stakeout_write_uncertain"]').count(),1,label+": one calm notice");
    assert.strictEqual(await dockErrors(),docks,label+": no error message");
    const calmLives=m.liveRequests.length-calmFrom;assert(calmLives>=5&&calmLives<=7,label+": 5 to 7 live requests in 6 s, got "+calmLives);
    assert((await liveStats()).max<=1,label+": never two open");
    assert.strictEqual(await roverShown(),false,label+": no rover marker after 4.5 s");
    assert((await linkText()).includes(D["stakeout.calm.stakeout_write_uncertain"]),label+": the link field shows the calm text");
    assert.notStrictEqual(await stateText(),"✓ "+D["stakeout.state_ready"],label+": not ready during the latch");
    await page.waitForSelector('[data-notice="stakeout-calm"]',{state:"detached"});
    await page.waitForFunction(text=>document.querySelector(".guide-state").textContent.trim()===text,"✓ "+D["stakeout.state_ready"]);
    m.liveQueue.push(...Array.from({length:2},()=>({status:503,body:{error:"stakeout_busy",message:"busy"}})));
    await page.waitForSelector('[data-notice="stakeout-calm"][data-code="stakeout_busy"]');
    assert((await page.textContent('[data-notice="stakeout-calm"]')).includes(D["stakeout.calm.stakeout_busy"]),label+": busy text");
    await page.waitForSelector('[data-notice="stakeout-calm"]',{state:"detached"});
    m.liveFail=true;
    await page.waitForFunction(()=>document.getElementById("live-state").dataset.link==="error");
    assert.strictEqual(await page.locator('[data-notice="stakeout-calm"]').count(),0,label+": a lost connection is no calm notice");
    m.liveFail=false;
    await page.waitForFunction(()=>document.getElementById("live-state").dataset.link==="live");
  }
  // (h) layout matrix: every stakeout page and the sheet at five widths in both languages.
  for(const lang of ["en","de"]){
    await setLanguage(lang);
    for(const width of [320,360,390,480,1280]){
      const label="stakeout layout "+lang+" "+width;
      await page.setViewportSize({width,height:width===1280?900:760});
      stakeoutReset();
      const audit=async name=>{await page.waitForTimeout(250);assertLayout(await layoutAudit(page),label+" "+name);assert.deepStrictEqual(await smallTargets(),[],label+" "+name+": touch targets");};
      await openOverview("p1");await audit("overview");
      await page.click('[data-card="stakeout-settings"] button');await page.waitForSelector("dialog.stakeout-settings[open]");
      await assertSheetLayout(page,label+" settings");await page.keyboard.press("Escape");await page.waitForSelector("dialog.stakeout-settings",{state:"detached"});
      await openImport();await pick("stakeout_sample.geojson");await page.waitForSelector(".import-preview .import-part");await audit("import");
      await openList();await audit("list");
      await openTarget();guidance({state:"coarse",dist_m:23.4,bearing_deg:316,dn_m:16.8,de_m:-16.2});await modeIs("search");await audit("search");
      readyAt();await modeIs("fine");await audit("fine");
      const states=await page.$$eval(".stakeout-guide .guide-state, a.target-row .target-status, .stakeout-state",nodes=>nodes.map(node=>node.querySelector(".stakeout-symbol")&&node.textContent.trim().length>2));
      assert(states.every(Boolean),label+": states with symbol and text");
    }
  }
  await page.setViewportSize({width:360,height:900});
  await setLanguage("en");
  stakeoutReset();tracking.active_project_id="p1";
  assert.deepStrictEqual(errors,[],"stakeout: page errors");
}
(async () => {
  const browser = await chromium.launch({ headless: true }),
    page = await browser.newPage({
      viewport: { width: 360, height: 900 },
      acceptDownloads: true,
    });
  page.setDefaultTimeout(20000);
  page.on("pageerror",error=>console.error("Browser error:",error));
  // #21b helpers: the page clock is shiftable, announcements and live regions are counted by observers.
  const shiftClock=ms=>page.evaluate(value=>{window.__rtkClockShift+=value;},ms);
  const recordAnnouncements=()=>page.evaluate(()=>{window.__announced=[];const node=document.getElementById("rtk-announcer");new MutationObserver(()=>window.__announced.push(node.textContent)).observe(node,{childList:true,characterData:true,subtree:true});});
  const recordLiveRegions=()=>page.evaluate(()=>{window.__liveRegionEvents=[];const live='[role="status"],[role="alert"],[aria-live]',outside=node=>!(node.closest&&node.closest("#rtk-announcer"));new MutationObserver(records=>{for(const record of records){for(const added of record.addedNodes){if(added.nodeType!==1)continue;const hits=[...(added.matches(live)?[added]:[]),...added.querySelectorAll(live)].filter(outside);hits.forEach(hit=>window.__liveRegionEvents.push("added:"+(hit.className||hit.tagName)));}const target=record.target.nodeType===1?record.target:record.target.parentElement;const region=target&&target.closest&&target.closest(live);if(region&&outside(region)&&record.type!=="attributes"&&!record.addedNodes.length)window.__liveRegionEvents.push("changed:"+(region.className||region.tagName));}}).observe(document.body,{childList:true,characterData:true,subtree:true});});
  const watchHeroCritical=()=>page.evaluate(()=>{window.__heroCritical=0;const hero=document.querySelector(".readiness-hero");new MutationObserver(()=>{if(hero.classList.contains("stale-critical"))window.__heroCritical++;}).observe(hero,{attributes:true,attributeFilter:["class"]});});
  // At least two ticker seconds after a stable hero, then 1.5 s without any announcement; counting starts only afterwards.
  const settleAnnouncements=async()=>{await page.waitForTimeout(2500);await recordAnnouncements();await page.waitForTimeout(1500);assert.deepStrictEqual(await page.evaluate(()=>window.__announced),[],"announcer baseline must be quiet");};
  const announced=()=>page.evaluate(()=>window.__announced);
  const heroClass=name=>page.waitForFunction(value=>document.querySelector(".readiness-hero").classList.contains(value),name);
  // Shiftable monotonic clock (#21b): the page measures data age with performance.now().
  await page.addInitScript(() => {
    window.__rtkClockShift = 0;
    const original = performance.now.bind(performance);
    performance.now = () => original() + window.__rtkClockShift;
  });
  // Progress poll answers (#21b): the wall-clock moment of every successful view=progress response.
  await page.addInitScript(() => {
    window.__progressAnswers = [];
    const original = window.fetch.bind(window);
    window.fetch = async (...args) => {
      const response = await original(...args);
      const target = typeof args[0] === "string" ? args[0] : args[0].url;
      if (response.ok && /view=progress/.test(target)) window.__progressAnswers.push(Date.now());
      return response;
    };
  });
  await page.addInitScript(() => {
    Object.defineProperty(navigator, "wakeLock", {
      value: {
        request: async () => ({
          release: async () => {},
          addEventListener: () => {},
        }),
      },
    });
  });
  // Result checks (#21c, --feedback-only): every /api/ui/live fetch is logged; the ones from verifyCapture or fetchLiveOnce are
  // marked by their call stack, and verifyCapture's carry a header so the fixture can answer them from checkAnswers.
  if(process.argv.includes("--feedback-only"))await page.addInitScript(()=>{
    window.__liveFetches=[];const original=window.fetch.bind(window);
    window.fetch=(input,init)=>{const target=typeof input==="string"?input:input.url;
      if(new URL(target,location.href).pathname==="/api/ui/live"){Error.stackTraceLimit=50;const stack=new Error().stack||"",check=/verifyCapture/.test(stack);
        window.__liveFetches.push({at:Date.now(),check,once:/fetchLiveOnce/.test(stack)});
        if(check)init=Object.assign({},init,{headers:Object.assign({},init&&init.headers,{"x-rtk-check":"1"})});}
      return original(input,init);};
  });
  // Test EventSource (--live-stream-only, #27 B4): window.__rtkStream.instances lists every stream the page opened, push(name,data,
  // instance) delivers an event (also to a closed instance: a buffered event of an old generation), fail(readyState,instance)
  // raises an error event. sessionStorage "rtk-stream-mode" = "real" leaves the page's own EventSource in place.
  // sessionStorage "rtk-start" = "hidden" or "offline" loads the page as a hidden background tab or offline (step j).
  if(process.argv.includes("--live-stream-only"))await page.addInitScript(()=>{
    let start=null;try{start=sessionStorage.getItem("rtk-start");}catch(_){}
    if(start==="hidden"){Object.defineProperty(document,"hidden",{configurable:true,get:()=>true});Object.defineProperty(document,"visibilityState",{configurable:true,get:()=>"hidden"});}
    if(start==="offline")Object.defineProperty(navigator,"onLine",{configurable:true,get:()=>false});
    const Real=window.EventSource;
    const real=()=>{try{return sessionStorage.getItem("rtk-stream-mode")==="real";}catch(_){return false;}};
    const stream=window.__rtkStream={instances:[],
      latest(){return this.instances[this.instances.length-1];},
      emit(instance,name,event){for(const fn of instance.listeners[name]||[])fn(event);},
      push(name,data,instance){const target=instance||this.latest();this.emit(target,name,{data:typeof data==="string"?data:JSON.stringify(data)});},
      fail(readyState,instance){const target=instance||this.latest();target.readyState=readyState;this.emit(target,"error",{});}};
    class Fake{
      constructor(url){this.url=url;this.readyState=1;this.listeners={};stream.instances.push(this);}
      addEventListener(name,fn){(this.listeners[name]=this.listeners[name]||[]).push(fn);}
      close(){this.readyState=2;}
    }
    window.EventSource=function(url){return real()?new Real(url):new Fake(url);};
  });
  // Layout shifts (#29b E13): window.__cls sums every layout-shift entry without recent input.
  // Skeletons (#29b Task 5): window.__skeletonSeen turns true once a .screen-skeleton is inserted; the tests reset it.
  if(process.argv.includes("--router-cache-only"))await page.addInitScript(()=>{
    window.__cls=0;window.__skeletonSeen=false;
    new MutationObserver(records=>{for(const record of records)for(const node of record.addedNodes)
      if(node.nodeType===1&&(node.matches(".screen-skeleton")||node.querySelector(".screen-skeleton")))window.__skeletonSeen=true;}).observe(document,{childList:true,subtree:true});
    new PerformanceObserver(list=>{for(const entry of list.getEntries())if(!entry.hadRecentInput)window.__cls+=entry.value;}).observe({type:"layout-shift",buffered:true});
  });
  // GeoJSON export flow (#46, --export-only): while window.__exportManual is true, the export request of the app is answered by
  // hand. __exportRequests lists every call ({url,aborted}), __exportRespond({status,body}) sends the headers (a JSON body for
  // status other than 200), __exportFeed(text) and __exportFeedPad(count) add body bytes, __exportFinish("close"|"error") ends
  // the stream; an abort before the headers rejects fetch with AbortError, an abort after them errors the stream. __shareMode
  // ("resolve"|"abort"|"notallowed"|null) steers navigator.share/canShare (calls in __shareCalls/__canShareCalls). __objectUrls
  // counts createObjectURL/revokeObjectURL. __abortListeners is the number of abort listeners the export component and its
  // close question hold on the signals (functions named dispose, forward, onAbort; a once listener of an aborted signal has
  // fired and counts no more): the run signals of api.request carry listeners of their own that are not the component's.
  // __closeQuestions counts the close questions that were inserted.
  if(process.argv.includes("--export-only"))await page.addInitScript(()=>{
    window.__exportManual=false;window.__exportRequests=[];window.__shareMode=null;window.__shareCalls=[];window.__canShareCalls=[];
    window.__objectUrls={created:0,revoked:0};window.__closeQuestions=0;
    let current=null;
    const original=window.fetch.bind(window),encoder=new TextEncoder();
    window.fetch=(input,init)=>{
      const target=typeof input==="string"?input:input.url;
      if(!window.__exportManual||!target.includes("/api/tracking/export")||!target.includes("format=geojson"))return original(input,init);
      const entry={url:target,aborted:false},signal=init&&init.signal;
      window.__exportRequests.push(entry);
      return new Promise((resolve,reject)=>{
        const state=current={entry,resolve,reject,controller:null,responded:false,finished:false};
        const onSignal=()=>{entry.aborted=true;
          if(!state.responded){state.responded=true;reject(new DOMException("The operation was aborted.","AbortError"));}
          else if(state.controller&&!state.finished){state.finished=true;try{state.controller.error(new DOMException("The operation was aborted.","AbortError"));}catch(_){}}};
        if(signal){if(signal.aborted)onSignal();else signal.addEventListener("abort",onSignal,{once:true});}
      });
    };
    window.__exportRespond=({status=200,body}={})=>{
      if(!current||current.responded)throw new Error("no export request waits for headers");
      current.responded=true;
      if(status!==200){current.finished=true;current.resolve(new Response(JSON.stringify(body),{status,headers:{"content-type":"application/json"}}));return;}
      const state=current;
      state.resolve(new Response(new ReadableStream({start(controller){state.controller=controller;},cancel(){state.finished=true;}}),{status:200,headers:{"content-type":"application/geo+json"}}));
    };
    window.__exportFeed=text=>{current.controller.enqueue(encoder.encode(text));};
    window.__exportFeedPad=count=>{current.controller.enqueue(new Uint8Array(count).fill(120));};
    window.__exportFinish=kind=>{current.finished=true;if(kind==="error")current.controller.error(new TypeError("network error"));else current.controller.close();};
    Object.defineProperty(navigator,"canShare",{configurable:true,value:data=>{window.__canShareCalls.push(((data&&data.files)||[]).map(file=>file.name));return window.__shareMode!==null&&Boolean(data&&data.files&&data.files.length===1&&/\.geojson$/.test(data.files[0].name));}});
    Object.defineProperty(navigator,"share",{configurable:true,value:async data=>{
      window.__shareCalls.push({title:data.title,files:data.files.map(file=>({name:file.name,size:file.size,type:file.type}))});
      if(window.__shareMode==="abort")throw new DOMException("Share canceled","AbortError");
      if(window.__shareMode==="notallowed")throw new DOMException("Not allowed","NotAllowedError");
    }});
    const create=URL.createObjectURL.bind(URL),revoke=URL.revokeObjectURL.bind(URL);
    URL.createObjectURL=object=>{window.__objectUrls.created++;return create(object);};
    URL.revokeObjectURL=address=>{window.__objectUrls.revoked++;return revoke(address);};
    const watched=new Set(["dispose","forward","onAbort"]),held=new Map(),add=EventTarget.prototype.addEventListener,remove=EventTarget.prototype.removeEventListener;
    EventTarget.prototype.addEventListener=function(type,listener,options){
      if(type==="abort"&&this instanceof AbortSignal&&typeof listener==="function"&&watched.has(listener.name)){
        if(!held.has(this))held.set(this,new Map());held.get(this).set(listener,Boolean(options&&typeof options==="object"&&options.once));}
      return add.call(this,type,listener,options);};
    EventTarget.prototype.removeEventListener=function(type,listener,options){
      if(type==="abort"&&held.has(this))held.get(this).delete(listener);
      return remove.call(this,type,listener,options);};
    Object.defineProperty(window,"__abortListeners",{get:()=>{let count=0;for(const [signal,listeners] of held)for(const once of listeners.values())if(!(once&&signal.aborted))count++;return count;}});
    new MutationObserver(records=>{for(const record of records)for(const node of record.addedNodes)
      if(node.nodeType===1&&node.matches("dialog.export-close-question"))window.__closeQuestions++;}).observe(document,{childList:true,subtree:true});
  });
  // Stakeout (--stakeout-only, WP3 Task 9): window.__stakeoutLive counts the live requests the page has open (max, count),
  // window.__setHidden(true|false) hides or shows the page like a background tab (document.hidden plus visibilitychange).
  if(process.argv.includes("--stakeout-only"))await page.addInitScript(()=>{
    window.__stakeoutLive={open:0,max:0,count:0};
    const original=window.fetch.bind(window);
    window.fetch=(input,init)=>{const target=typeof input==="string"?input:input.url;if(!/\/api\/stakeout\/live/.test(target))return original(input,init);
      const stats=window.__stakeoutLive;stats.open++;stats.count++;stats.max=Math.max(stats.max,stats.open);return original(input,init).finally(()=>{stats.open--;});};
    let hidden=false;
    Object.defineProperty(document,"hidden",{configurable:true,get:()=>hidden});
    Object.defineProperty(document,"visibilityState",{configurable:true,get:()=>hidden?"hidden":"visible"});
    window.__setHidden=value=>{hidden=value;document.dispatchEvent(new Event("visibilitychange"));};
  });
  await page.route("http://local.test/**", fixture);
  await page.goto("http://local.test/");
  await page.waitForSelector(".readiness-hero");
  if(process.argv.includes("--stakeout-only")){
    await testStakeout(page,browser);
    await browser.close();console.log("stakeout-browser=ok");return;
  }
  if(process.argv.includes("--stakeout-actions-only")){
    await testStakeoutActions(page,browser);
    await browser.close();console.log("stakeout-actions-browser=ok");return;
  }
  if(process.argv.includes("--navigation-only")){
    // #24-UI: routes, old links, history, two-tap reach, layout matrix, sign-in entries and context navigation.
    const base="http://local.test",errors=[];
    page.on("pageerror",error=>errors.push(error.message));
    const i18nModule=require(root+"/i18n.js");
    const loginHtml=require("child_process").execFileSync("python3",["-c","import ui,sys;sys.stdout.write(ui.APP_V2_LOGIN)"],{cwd:root,encoding:"utf8"});
    tracking.archives=mobileArchives;
    const busyDone=p=>p.waitForFunction(()=>document.querySelector("#app-main").getAttribute("aria-busy")==="false");
    const hashOf=p=>p.evaluate(()=>location.hash);
    // The old links are read from the shipped app.js; the node each target shows is the only thing written down here.
    const shipped=fs.readFileSync(root+"/app.js","utf8").match(/const LEGACY_ROUTES=(\{[^}]*\});/);
    assert(shipped,"app.js ships LEGACY_ROUTES");
    const legacy=JSON.parse(shipped[1]);
    const ARRIVAL={measure:".readiness-hero","device/display":'[data-card="language"]',
      "device/connections/internet":'[data-group="internet"] .configuration-layout',"device/connections/access":'[data-group="access"] .label-grid',
      "device/diagnostics/rtcm":".stream-hero","device/diagnostics/log":".log-console","device/diagnostics/console":".command-panel"};
    assert.deepStrictEqual(Object.keys(legacy).sort(),["device/access","device/configuration","device/label","device/log","device/module","device/rtcm","device/settings","status"],"the eight old links");
    for(const target of Object.values(legacy))assert(ARRIVAL[target],`arrival node for ${target}`);
    const arrivalOf=target=>base+"/#"+target;

    // 1. Old links (EN, 390): canonical hash, expected node, and the history stays that of a normal navigation.
    console.log("navigation: old links");
    await setLanguage(page,"en");
    await page.setViewportSize({width:390,height:844});
    for(const [alias,target] of Object.entries(legacy)){
      // (a) inside the app: one entry for the hash navigation, none for the replacement; Back lands on the start, not on the alias.
      await page.goto(base+"/#measure");
      await page.waitForSelector(".readiness-hero");
      await busyDone(page);
      const before=await page.evaluate(()=>history.length);
      await page.evaluate(value=>{location.hash=value;},alias);
      await page.waitForSelector(ARRIVAL[target]);
      await busyDone(page);
      assert.strictEqual(await hashOf(page),"#"+target,`${alias}: canonical hash`);
      assert.strictEqual(await page.evaluate(()=>history.length),before+1,`${alias}: the alias adds no history entry`);
      if(alias==="device/settings"){
        assert.strictEqual(await page.locator('[data-card="measure-link"] a').getAttribute("href"),"/#measure/setup","device/settings: measure link");
      }
      await page.goBack();
      await page.waitForFunction(()=>location.hash==="#measure");
      await page.waitForSelector(".readiness-hero");
      assert.strictEqual(await hashOf(page),"#measure",`${alias}: Back leaves the alias behind`);
    }
    // (b) first visit with a known predecessor: the alias and the canonical link leave the same history.
    const visitor=await browser.newContext({viewport:{width:390,height:844}});
    await visitor.addInitScript(()=>{try{localStorage.setItem("rtk-language","en");}catch(error){}});
    const firstVisit=async(hash,target)=>{
      const fresh=await visitor.newPage(),seen=[];
      fresh.on("pageerror",error=>seen.push(error.message));
      await fresh.route(base+"/**",fixture);
      await fresh.goto(base+"/__history-start");
      const start=await fresh.evaluate(()=>history.length);
      await fresh.goto(base+"/#"+hash);
      await fresh.waitForSelector(ARRIVAL[target]);
      await busyDone(fresh);
      const result={hash:await hashOf(fresh),added:await fresh.evaluate(length=>history.length-length,start)};
      await fresh.goBack();
      result.back=fresh.url();result.title=await fresh.title();result.errors=seen;
      await fresh.close();
      return result;
    };
    for(const [alias,target] of Object.entries(legacy)){
      const viaAlias=await firstVisit(alias,target),viaTarget=await firstVisit(target,target);
      assert.strictEqual(viaAlias.hash,"#"+target,`${alias}: first visit lands on the canonical hash`);
      assert.deepStrictEqual(viaAlias,{...viaTarget,errors:[]},`${alias}: same history as the canonical link`);
      assert.strictEqual(viaAlias.added,1,`${alias}: the visit adds exactly one entry`);
      assert.strictEqual(viaAlias.back,base+"/__history-start",`${alias}: Back reaches the predecessor`);
      assert.strictEqual(viaAlias.title,"history start",`${alias}: predecessor title`);
    }
    await visitor.close();
    console.log("navigation: old links ok");

    // 2. Inside the app: Back walks the views in reverse order; a sheet takes Back for itself.
    console.log("navigation: history");
    const noFault=async label=>{
      assert.strictEqual(await page.locator("#app-main .notice.critical").count(),0,`${label}: no error panel`);
      assert.strictEqual(await page.locator(".feedback-item.critical").count(),0,`${label}: no error message`);
    };
    await page.setViewportSize({width:390,height:844});
    await page.goto(base+"/#measure");
    await page.waitForSelector(".readiness-hero");
    await page.locator('#mobile-nav a[data-route="projects"]').click();
    await page.waitForSelector(".project-list");
    await page.locator(".project-card").first().click();
    await page.waitForSelector(".map-workspace");
    const projectHash=await hashOf(page);
    assert.strictEqual(projectHash,"#projects/p1","project opened");
    await page.locator('#mobile-nav a[data-route="device"]').click();
    await page.waitForSelector(".device-links");
    await page.locator('#app-main .device-links a[href="/#device/connections"]').click();
    await page.waitForSelector('[data-group="internet"] .configuration-layout');
    assert.strictEqual(await hashOf(page),"#device/connections","connections opened");
    for(const [expected,node] of [["#device",".device-links"],["#projects/p1",".map-workspace"],["#projects",".project-list"]]){
      await page.goBack();
      await page.waitForSelector(node);
      await busyDone(page);
      assert.strictEqual(await hashOf(page),expected,`Back to ${expected}`);
      await noFault(`Back to ${expected}`);
    }
    await page.goForward();
    await page.waitForFunction(()=>location.hash==="#projects/p1");
    await page.waitForSelector(".map-workspace");
    await page.locator("#app-main button",{hasText:"Export"}).first().click();
    await page.waitForSelector("dialog[open]");
    await page.goBack();
    await page.waitForFunction(()=>!document.querySelector("dialog[open]"));
    assert.strictEqual(await hashOf(page),"#projects/p1","Back closes only the sheet");
    assert.strictEqual(await page.locator(".map-workspace").count(),1,"the project view stays");
    await noFault("sheet Back");
    console.log("navigation: history ok");

    // 3. At most two taps (EN/DE, 360/1280), only real clicks on visible navigation elements.
    console.log("navigation: reach");
    const structure={};
    const REACH=[
      ["field setup","measure",'#app-main a[href="/#measure/setup"]','[data-card="method"]',"#measure/setup"],
      ["connections","device",'#app-main .device-links a[href="/#device/connections"]','[data-group="internet"]',"#device/connections"],
      ["QR code","device",'#app-main .device-links a[href="/#device/connections"]','.label-grid svg',"#device/connections"],
      ["diagnostics","device",'#app-main .device-links a[href="/#device/diagnostics"]',".diagnostics-summary","#device/diagnostics"],
      ["configuration backup","device",'#app-main .device-links a[href="/#device/maintenance"]','[data-card="config-backup"] a[href="/api/config/backup"]',"#device/maintenance"],
      ["operation and display","device",'#app-main .device-links a[href="/#device/display"]','[data-card="language"]',"#device/display"],
      ["archive","projects",'#app-main a.archive-entry',".archive-library .archive-card","#archive"]];
    for(const lang of["en","de"]){
      await setLanguage(page,lang);
      for(const width of[360,1280]){
        await page.setViewportSize({width,height:width>=900?900:780});
        const tab=area=>page.locator(`${width>=900?"#desktop-nav":"#mobile-nav"} a[data-route="${area}"]`);
        for(const [what,area,link,arrival,hash] of REACH){
          const label=`${what} ${lang} ${width}`;
          await go(page,"measure",".readiness-hero");
          let taps=0;
          const tap=async locator=>{
            const target=locator.first();
            await target.waitFor({state:"visible"}).catch(()=>assert.fail(`${label}: tap ${taps+1} is not visible`));
            await target.click();taps++;
            await busyDone(page);
          };
          await tap(tab(area));
          if(what==="field setup"){
            // The Measure tab is the screen already shown: the link is the first real step.
            assert.strictEqual(await hashOf(page),"#measure",`${label}: tab`);
          }
          await tap(page.locator(link));
          await page.waitForSelector(arrival);
          assert(await page.locator(arrival).first().isVisible()||what==="QR code","arrival visible "+label);
          assert.strictEqual(await hashOf(page),hash,`${label}: route`);
          assert(taps<=2,`${label}: ${taps} taps`);
        }
      }
    }
    console.log("navigation: reach ok");

    // 4. Layout matrix.
    console.log("navigation: layout matrix");
    const PAGES=[["measure",".readiness-hero"],["measure/setup",".field-setup-layout"],["projects",".project-list"],
      ["archive",".archive-library .archive-card"],["projects/p1",".map-workspace"],["device",".device-links"],
      ["device/connections",'[data-group="access"] .label-grid svg'],["device/display",".settings-layout"],
      ["device/diagnostics",".diagnostics-summary"],["device/diagnostics/rtcm",".stream-hero"],
      ["device/diagnostics/log",".log-console"],["device/diagnostics/console",".command-panel"],["device/maintenance",".maintenance-layout"]];
    const targetAudit=p=>p.evaluate(()=>{
      const found=[],describe=el=>el.tagName.toLowerCase()+(el.id?"#"+el.id:"")+[...el.classList].map(name=>"."+name).join("");
      for(const scope of document.querySelectorAll("#app-main, .app-header")){
        for(const el of scope.querySelectorAll("button,a.btn,[role=button],summary")){
          if(el.closest("svg")||el.matches("a.text-link")||!el.checkVisibility())continue;
          const rect=el.getBoundingClientRect();
          if(!rect.width||!rect.height)continue;
          if(rect.width<47.5||rect.height<47.5)found.push([describe(el),(el.textContent||el.getAttribute("aria-label")||"").trim().slice(0,30),Math.round(rect.width*10)/10,Math.round(rect.height*10)/10]);
        }
      }
      return found;
    });
    const headerAudit=async label=>{
      await assertHeaderSignOut(page,label);
      const found=await page.evaluate(()=>{
        const title=document.querySelector("#view-title"),pill=document.querySelector("#live-state"),t=title.getBoundingClientRect(),p=pill.getBoundingClientRect();
        return{titleText:title.textContent.trim(),titleVisible:t.width>0&&t.height>0&&title.checkVisibility(),pillVisible:p.width>0&&pill.checkVisibility(),
          overlap:t.left<p.right-.5&&p.left<t.right-.5&&t.top<p.bottom-.5&&p.top<t.bottom-.5};
      });
      assert(found.titleText.length&&found.titleVisible&&found.pillVisible&&!found.overlap,`header title and live pill ${label}: ${JSON.stringify(found)}`);
    };
    const countsOf=p=>p.evaluate(()=>({main:document.querySelectorAll("#app-main a, #app-main button, #app-main summary").length,
      tabs:document.querySelectorAll("#mobile-nav a, #desktop-nav a").length,context:document.querySelectorAll("#desktop-context-nav a").length}));
    // The target audit must see a small button before its empty verdicts mean anything.
    await page.setViewportSize({width:360,height:780});
    await go(page,"measure",".readiness-hero");
    await page.evaluate(()=>{const style=document.createElement("style");style.id="shrink-probe";style.textContent=".field-actions button{height:20px!important;min-height:0!important}";document.head.append(style);});
    assert((await targetAudit(page)).length>0,"the target audit reports a 20 px button");
    await page.evaluate(()=>document.getElementById("shrink-probe").remove());
    assert.deepStrictEqual(await targetAudit(page),[],"the probe leaves nothing behind");
    for(const lang of["en","de"]){
      await setLanguage(page,lang);
      structure[lang]={};
      for(const width of MOBILE_WIDTHS.concat([1280])){
        await page.setViewportSize({width,height:width>=900?900:780});
        for(const [hash,selector] of PAGES){
          const label=`${hash} ${lang} ${width}`;
          await go(page,hash,selector);
          if(hash==="device/connections")await page.waitForSelector('[data-group="internet"] .configuration-layout');
          assertLayout(await layoutAudit(page),label);
          assert.deepStrictEqual(await targetAudit(page),[],`touch targets ${label}`);
          await headerAudit(label);
          if(width===360||width===1280)structure[lang][`${hash} ${width}`]=await countsOf(page);
        }
        if(width===320){
          // A closed gate on Measure at 320 px: the primary action and an active reason are both in sight.
          await go(page,"measure",".readiness-hero");
          live.current_quality.accepted=false;setVerdict(["rtk_fixed_required"]);
          await page.waitForFunction(()=>document.querySelector(".readiness-hero").classList.contains("blocked"));
          const sight=await page.evaluate(()=>{
            const inView=el=>{const r=el.getBoundingClientRect();return r.width>0&&r.height>0&&r.top>=0&&r.bottom<=innerHeight&&el.checkVisibility();};
            const action=document.querySelector(".field-actions button.primary")||document.querySelector(".field-actions > button");
            const reason=document.querySelector(".hero-state p");
            return{action:Boolean(action)&&inView(action),reason:Boolean(reason)&&inView(reason)&&reason.textContent.trim().length>0,
              fail:document.querySelectorAll(".condition.fail").length};
          });
          assert(sight.action&&sight.reason&&sight.fail>=1,`Measure at 320 with a closed gate (${lang}): ${JSON.stringify(sight)}`);
          assertLayout(await layoutAudit(page),`measure blocked ${lang} 320`);
          live.current_quality.accepted=true;setVerdict([]);
          await page.waitForFunction(()=>!document.querySelector(".readiness-hero").classList.contains("blocked"));
        }
      }
    }
    assert.deepStrictEqual(structure.de,structure.en,"German and English have the same navigation entries and links per page");
    console.log("navigation: layout matrix ok");

    // 5. Sign-in entries: the session lives in the mock, a lost session sends the page to the sign-in with ?return=.
    console.log("navigation: sign-in entries");
    const entry=async(width,start)=>{
      const context=await browser.newContext({viewport:{width,height:800}});
      const seen={codes:[],on:false,expire:false,errors:[]};
      const fresh=await context.newPage();
      fresh.on("pageerror",error=>seen.errors.push(error.message));
      await fresh.route(base+"/**",route=>{
        const url=new URL(route.request().url());
        if(url.pathname==="/"&&!seen.on)return route.fulfill({contentType:"text/html",body:loginHtml});
        if(url.pathname==="/api/session"&&route.request().method()==="POST"){
          seen.codes.push(route.request().postDataJSON().device_code);seen.on=true;seen.expire=false;
          return route.fulfill({contentType:"application/json",body:"{}"});
        }
        if(seen.expire&&url.pathname.startsWith("/api/")){seen.on=false;return route.fulfill({status:401,contentType:"application/json",body:"{}"});}
        return fixture(route);
      });
      await fresh.goto(base+start);
      return{context,fresh,seen};
    };
    const signedIn=url=>new URL(url).search==="?signed_in=1";
    for(const width of[360,1280]){
      // The captive entry /#device/configuration: sign-in, then the form in the Internet group under the canonical hash.
      let session=await entry(width,"/#device/configuration");
      await session.fresh.locator("#v2-device-code").fill("CODE1");
      await session.fresh.locator("button[type=submit]").click();
      await session.fresh.waitForURL(signedIn);
      await session.fresh.waitForSelector('[data-group="internet"] .configuration-layout');
      assert.strictEqual(await hashOf(session.fresh),"#device/connections/internet",`captive entry ${width}: canonical hash`);
      assert.deepStrictEqual(session.seen.codes,["CODE1"],`captive entry ${width}: one sign-in`);
      assert.deepStrictEqual(session.seen.errors,[],`captive entry ${width}: page errors`);
      await session.context.close();
      // The QR code #code=CODE signs in by itself and lands on Measure.
      session=await entry(width,"/#code=QRCODE");
      await session.fresh.waitForURL(signedIn);
      await session.fresh.waitForSelector(".readiness-hero");
      assert.strictEqual(await hashOf(session.fresh),"#measure",`QR entry ${width}: Measure`);
      assert.deepStrictEqual(session.seen.codes,["QRCODE"],`QR entry ${width}: the code is sent once`);
      assert(!session.fresh.url().includes("code="),`QR entry ${width}: the code left the address`);
      assert.deepStrictEqual(session.seen.errors,[],`QR entry ${width}: page errors`);
      await session.context.close();
      // A lost session on a project page: sign-in with ?return=, then the same project again (E16).
      session=await entry(width,"/#code=FIRST");
      await session.fresh.waitForURL(signedIn);
      await session.fresh.waitForSelector(".readiness-hero");
      await session.fresh.evaluate(()=>{location.hash="projects/p1";});
      await session.fresh.waitForSelector(".map-workspace");
      session.seen.expire=true;
      await session.fresh.waitForURL(url=>new URL(url).searchParams.has("return"));
      assert.strictEqual(new URL(session.fresh.url()).searchParams.get("return"),"/ui-v2#projects/p1",`401 ${width}: return target`);
      await session.fresh.locator("#v2-device-code").fill("AGAIN");
      await session.fresh.locator("button[type=submit]").click();
      await session.fresh.waitForURL(signedIn);
      await session.fresh.waitForSelector(".map-workspace");
      assert.strictEqual(await hashOf(session.fresh),"#projects/p1",`401 ${width}: back on the project`);
      assert.deepStrictEqual(session.seen.codes,["FIRST","AGAIN"],`401 ${width}: sign-ins`);
      assert.deepStrictEqual(session.seen.errors,[],`401 ${width}: page errors`);
      await session.context.close();
    }
    console.log("navigation: sign-in entries ok");

    // 6. Context navigation: desktop sidebar per area with aria-current; invisible on a phone.
    console.log("navigation: context navigation");
    await setLanguage(page,"en");
    const CONTEXT={measure:["/#measure","/#measure/setup"],projects:["/#projects","/#archive"],
      device:["/#device","/#device/connections","/#device/display","/#device/diagnostics","/#device/maintenance"]};
    const CURRENT=[["measure","measure","/#measure"],["measure/setup","measure","/#measure/setup"],["projects","projects","/#projects"],
      ["projects/p1","projects","/#projects"],["archive","projects","/#archive"],["device","device","/#device"],
      ["device/connections","device","/#device/connections"],["device/display","device","/#device/display"],
      ["device/diagnostics","device","/#device/diagnostics"],["device/diagnostics/rtcm","device","/#device/diagnostics"],
      ["device/diagnostics/log","device","/#device/diagnostics"],["device/diagnostics/console","device","/#device/diagnostics"],
      ["device/maintenance","device","/#device/maintenance"]];
    const selectors=Object.fromEntries(PAGES);
    const labels=new Set();
    await page.setViewportSize({width:1280,height:900});
    for(const [hash,area,current] of CURRENT){
      await go(page,hash,selectors[hash]);
      const nav=await page.evaluate(()=>{const node=document.querySelector("#desktop-context-nav");
        return{visible:node.checkVisibility()&&node.getBoundingClientRect().width>0,label:node.getAttribute("aria-label"),
          hrefs:[...node.querySelectorAll("a")].map(link=>link.getAttribute("href")),
          current:[...node.querySelectorAll("a[aria-current='page']")].map(link=>link.getAttribute("href"))};});
      assert(nav.visible,`${hash}: context navigation visible at 1280`);
      assert.deepStrictEqual(nav.hrefs,CONTEXT[area],`${hash}: entries of ${area}`);
      assert.deepStrictEqual(nav.current,[current],`${hash}: aria-current`);
      assert(nav.label&&nav.label.length,`${hash}: aria-label`);
      labels.add(area+"|"+nav.label);
    }
    assert.strictEqual(labels.size,3,"one aria-label per area");
    await page.setViewportSize({width:360,height:780});
    for(const hash of["measure","projects","device"]){
      await go(page,hash,selectors[hash]);
      assert.strictEqual(await page.evaluate(()=>{const node=document.querySelector("#desktop-context-nav");return node.checkVisibility()&&node.getBoundingClientRect().width>0;}),false,`${hash}: no context navigation at 360`);
    }
    console.log("navigation: context navigation ok");
    assert.deepStrictEqual(errors,[],"page errors");
    await browser.close();
    console.log("navigation-browser=ok");return;
  }
  if(process.argv.includes("--field-flow-only")){
    // #24-UI Task 11 / #38a: set up and measure, two browsers, the frame reminder, a running measurement and returning.
    const base="http://local.test",errors=[],shotDir=process.env.RTK_SHOT_DIR;
    page.on("pageerror",error=>errors.push(error.message));
    const i18nModule=require(root+"/i18n.js"),dictionaries=i18nModule.dictionaries,v2de=i18nModule.v2de;
    const fill=(template,values)=>template.replace(/\{(\w+)\}/g,(_,key)=>values[key]);
    const baseFix=structuredClone(live.current_fix),baseQuality=live.current_quality,baseProgress=()=>({active:false,collected:0,total:5});
    const readyNetwork={ntrip_state:"streaming",ntrip_config:"ready",last_ntrip_error_code:null,access_state:"ONLINE"};
    let fixSeq=100;
    const setFix=(qual,text)=>{live.current_fix={...baseFix,qual,fix_status_text:text,stale:false,correction_age_sec:1,fix_seq:++fixSeq};};
    const setReady=()=>{setFix(4,"RTK_FIXED");baseQuality.accepted=true;setVerdict([]);live.gnss_accuracy={state:"receiving",attempts:0};live.network={...readyNetwork};};
    tracking.projects[1].name="Second project";
    const resetWorld=()=>{
      flow.defaults={samples:5,antenna_height_m:0,reference_frame:"",reference_frame_epoch:""};flowPublish();
      Object.assign(tracking.projects[0],{point_count:1,frames:{unknown:1},unknown_frames:true,mixed_frames:false});
      tracking.projects[1].point_count=0;tracking.active_project_id="p1";
      live.active_project={id:"p1",name:"Project",line_count:1,point_count:1};live.active_line=null;live.measurement_progress=baseProgress();
      captureState={epoch:"a1b2c3d4",stack:[],order:40};actionAnswers={};liveAnswers=[];checkAnswers=[];syncCandidate();delete live.tracking_startup;
      setReady();
    };
    flow.on=true;
    const busyDone=p=>p.waitForFunction(()=>document.querySelector("#app-main").getAttribute("aria-busy")==="false");
    const hashOf=p=>p.evaluate(()=>location.hash);
    const posts=(since,action)=>trackingPostLog.slice(since).filter(post=>!action||post.action===action);
    const requests=(since,path,method)=>requestLog.slice(since).filter(entry=>entry.path===path&&entry.method===method);
    const waitFor=async(check,message,timeout=8000)=>{const end=Date.now()+timeout;while(!check()){if(Date.now()>end)assert.fail(message);await page.waitForTimeout(50);}};
    const shiftClock=ms=>page.evaluate(value=>{window.__rtkClockShift+=value;},ms);
    // A second browser: its own context (own storage, own session), the same device model.
    const openBrowser=async(language,size,hash)=>{
      const context=await browser.newContext({viewport:size}),tab=await context.newPage();
      tab.setDefaultTimeout(20000);tab.on("pageerror",error=>errors.push(error.message));
      await context.addInitScript(value=>{try{localStorage.setItem("rtk-language",value);}catch(_){}},language);
      await context.route("http://local.test/**",fixture);
      await tab.goto(base+"/#"+hash);await tab.waitForSelector(hash==="measure"?".status-setup":"#app-main > *");await busyDone(tab);
      return{context,tab};
    };
    const method=(p)=>p.locator('.status-setup [data-context="method"] b'),frameRow=p=>p.locator('.status-setup [data-context="frame"] b');
    const REMINDER='.status-setup [data-notice="frame-reminder"]';
    const configs=[["en",360],["de",360],["en",1280]];

    for(const [language,width] of configs){
      const size={width,height:width===1280?800:780},label=`${language} ${width}`,dict=dictionaries[language],vt=text=>language==="de"?v2de[text]:text,
        formatNumber=text=>language==="de"?text.replace(".",","):text;
      const begin=async hash=>{
        resetWorld();
        await page.setViewportSize(size);
        await page.evaluate(()=>{try{sessionStorage.clear();}catch(_){}});
        await setLanguage(page,language);
        await page.waitForSelector(".status-setup");
        if(hash&&hash!=="measure")await go(page,hash,"#app-main > *");
        await busyDone(page);
      };
      const tab=name=>page.locator(`[data-route="${name}"]:visible`).first();
      const STRIP=".field-actions > .capture-strip";
      const stripItem=()=>page.evaluate(selector=>{const node=document.querySelector(selector+' .feedback-item[data-key="capture"]');
        return node?{kind:[...node.classList].find(name=>name!=="feedback-item"),text:node.querySelector(".feedback-text").textContent,actions:[...node.querySelectorAll(".feedback-actions button")].map(button=>button.textContent)}:null;},STRIP);
      const waitStrip=(kind,text,step)=>page.waitForFunction(([selector,kind,text])=>{const node=document.querySelector(selector+' .feedback-item[data-key="capture"]');
        return Boolean(node)&&node.classList.contains(kind)&&node.querySelector(".feedback-text").textContent===text;},[STRIP,kind,text],{timeout:8000})
        .catch(async()=>assert.fail(`${label} ${step}: strip ${kind} "${text}" expected, got ${JSON.stringify(await stripItem())}`));
      const clickBar=name=>page.evaluate(name=>{const button=[...document.querySelectorAll(".field-actions > button")].find(node=>node.textContent===name);if(!button)throw new Error("bar button "+name+" missing");button.click();},name);
      const clickStrip=name=>page.evaluate(([selector,name])=>{const button=[...document.querySelectorAll(selector+" button")].find(node=>node.textContent===name);if(!button)throw new Error("strip button "+name+" missing");button.click();},[STRIP,name]);
      const startObjectPoint=async name=>{
        await clickBar(vt("Object point"));await page.locator("dialog.asset-sheet").waitFor();
        await page.locator('dialog.asset-sheet input[name="name"]').fill(name);
        await page.locator("dialog.asset-sheet").getByRole("button",{name:vt("Save point")}).click();
      };
      const openProjectPoint=async id=>{
        await go(page,"projects/"+id,".map-panel");
        await page.locator(".point-row").filter({hasText:"Valve 1"}).first().click();
        await page.waitForSelector(".point-card");
        return page.locator(".point-card").innerText();
      };
      const summary=p=>p.locator(".status-setup").innerText();
      const projectRowText=p=>p.locator(".status-project").innerText();

      // A. Set up and start measuring.
      console.log(`field-flow: A set up and start ${label}`);
      await begin("projects");
      let since=trackingPostLog.length;
      await page.waitForSelector(".project-list");
      await page.locator(".project-card").filter({hasText:"Second project"}).click();
      await page.waitForFunction(()=>location.hash==="#projects/p2");await page.waitForSelector(".project-activation");await busyDone(page);
      assert.strictEqual(posts(since).length,0,`${label} A: opening a project sends nothing`);
      assert.strictEqual(await page.locator(".project-activation .muted").innerText(),dict["ui.project_open_not_active"],`${label} A: opening does not activate`);
      await tab("measure").click();
      await page.waitForSelector(".status-project");await busyDone(page);
      assert((await page.locator(".status-project-text b").innerText()).startsWith("Project ·"),`${label} A: Measure still shows p1`);
      await tab("projects").click();
      await page.waitForSelector(".project-list");
      await page.locator(".project-card").filter({hasText:"Second project"}).click();
      await page.waitForSelector(".project-activation");await busyDone(page);
      await page.getByRole("button",{name:vt("Activate for measurement"),exact:true}).click();
      await waitFor(()=>posts(since,"select_project").length===1,`${label} A: one select_project`);
      assert.deepStrictEqual(posts(since,"select_project").map(post=>post.project_id),["p2"],`${label} A: p2 activated`);
      await tab("measure").click();
      await page.waitForFunction(()=>document.querySelector(".status-project-text b")&&document.querySelector(".status-project-text b").textContent.startsWith("Second project ·"));
      assert.strictEqual(posts(since).length,1,`${label} A: only the activation was sent`);
      // The method, the frame and the antenna height are saved on the field setup page.
      await page.locator(".status-setup a.setup-edit").click();
      await page.waitForFunction(()=>location.hash==="#measure/setup");await page.waitForSelector(".measurement-choice");await busyDone(page);
      const gateSince=requestLog.length;
      await page.locator('.measurement-choice [data-method="10"]').click();
      await page.waitForFunction(()=>document.querySelector('.measurement-choice [data-method="10"]').getAttribute("aria-pressed")==="true");
      assert.deepStrictEqual(requests(gateSince,"/api/measurement-gate","PUT").map(entry=>JSON.parse(entry.body)),[{samples:10}],`${label} A: method body`);
      await page.locator("#field-setup-reference-frame").fill("ETRS89 (example)");
      await page.locator("#field-setup-antenna-height").fill("1.85");
      await page.getByRole("button",{name:dict["ui.field_setup_save"],exact:true}).click();
      await waitFor(()=>requests(gateSince,"/api/field-setup","PUT").length===1,`${label} A: field setup saved`);
      assert.deepStrictEqual(JSON.parse(requests(gateSince,"/api/field-setup","PUT")[0].body),{antenna_height_m:"1.85",reference_frame:"ETRS89 (example)",reference_frame_epoch:""},`${label} A: field setup body`);
      assert.deepStrictEqual(flow.defaults,{samples:10,antenna_height_m:1.85,reference_frame:"ETRS89 (example)",reference_frame_epoch:""},`${label} A: the device model`);
      await tab("measure").click();
      await page.waitForSelector(".status-setup");
      await page.waitForFunction(text=>document.querySelector('.status-setup [data-context="frame"] b').textContent===text,"ETRS89 (example)");
      assert.strictEqual(await method(page).innerText(),fill(dict["ui.sample_avg"],{count:"10"}),`${label} A: summary method`);
      assert.strictEqual(await page.locator('.status-setup [data-context="antenna"] b').innerText(),formatNumber("1.85")+" m",`${label} A: summary antenna height`);
      assert.strictEqual(await page.locator(".status-setup .scope-badge").innerText(),dict["ui.new_measurements_only"],`${label} A: for new measurements`);
      assert.strictEqual(await page.locator(REMINDER).count(),0,`${label} A: a set frame needs no reminder`);
      // A measurement with ten samples: the sheet counts to 10, the request carries samples:10, the strip offers Undo.
      await page.waitForFunction(()=>document.querySelector(".readiness-hero").classList.contains("ready"));
      since=trackingPostLog.length;
      actionAnswers.add_asset={delay:2500,afterApply:()=>{live.measurement_progress=baseProgress();}};
      live.measurement_progress={active:true,collected:10,total:10,mode:"fixed",elapsed_sec:7};
      await startObjectPoint("Valve 24");
      await page.waitForSelector("dialog.measurement-sheet[open]");
      await page.waitForFunction(text=>document.querySelector("dialog.measurement-sheet .measurement-state").textContent.includes(text),` ${vt("of")} 10 `);
      assert.strictEqual(await page.locator("dialog.measurement-sheet progress").getAttribute("max"),"10",`${label} A: progress bar counts to 10`);
      const savedText=vt("Object point saved")+" · Valve 24",undoLabel=dict["ui.undo_point"];
      await waitStrip("ok",savedText,"A saved");
      assert.deepStrictEqual(posts(since,"add_asset").map(post=>post.samples),[10],`${label} A: the request carries samples:10`);
      assert((await stripItem()).actions.includes(undoLabel),`${label} A: Undo offered`);
      // The page still holds the progress of the finished measurement until its live poll resumes; Undo then targets the new point directly.
      await page.waitForResponse(response=>response.url().endsWith("/api/ui/live")&&response.status()===200);await page.waitForTimeout(300);
      assert.strictEqual(live.active_project.point_count,1,`${label} A: one point in p2`);
      const target=serverUndoCandidate().label;
      await clickStrip(undoLabel);
      await waitStrip("ok",fill(vt("{label} removed"),{label:target}),"A undone");
      assert.deepStrictEqual(posts(since).map(post=>post.action),["add_asset","undo"],`${label} A: add and undo`);
      assert.strictEqual(live.active_project.point_count,0,`${label} A: point count back`);
      // The connection drops for 12 s: loss, locked approval; afterwards the summary and the project row are the same.
      const summaryBefore=await summary(page),rowBefore=await projectRowText(page);
      blockLive();
      try{
        await shiftClock(12000);
        await page.waitForFunction(()=>document.querySelector(".readiness-hero").classList.contains("stale-critical"));
        const bar=await page.evaluate(()=>[...document.querySelectorAll(".field-actions > button")].map(button=>button.disabled));
        assert(bar.length>0&&bar.every(Boolean),`${label} A: every action is locked while the link is lost ${JSON.stringify(bar)}`);
        assert.strictEqual(await summary(page),summaryBefore,`${label} A: summary unchanged during the loss`);
      }finally{releaseLive();}
      setFix(4,"RTK_FIXED");
      const released=Date.now();
      await page.waitForFunction(()=>document.querySelector(".readiness-hero").classList.contains("ready")&&[...document.querySelectorAll(".field-actions > button")].some(button=>!button.disabled),null,{timeout:8000});
      assert(lastLiveServedAt>=released,`${label} A: approval comes from a fresh answer`);
      assert.strictEqual(await summary(page),summaryBefore,`${label} A: summary unchanged after the loss`);
      assert.strictEqual(await projectRowText(page),rowBefore,`${label} A: project row unchanged after the loss`);

      // B. Two browser contexts share one device.
      console.log(`field-flow: B two browsers ${label}`);
      await begin("measure");
      since=trackingPostLog.length;
      const cardBefore=await openProjectPoint("p1");
      await go(page,"measure",".status-setup");
      const second=await openBrowser(language,size,"measure"),other=second.tab;
      try{
        assert.strictEqual(await method(other).innerText(),fill(dict["ui.sample_avg"],{count:"5"}),`${label} B: context 2 starts with 5 samples`);
        await go(page,"measure/setup",".measurement-choice");
        await page.locator('.measurement-choice [data-method="quality"]').click();
        await page.waitForFunction(()=>document.querySelector('.measurement-choice [data-method="quality"]').getAttribute("aria-pressed")==="true");
        await waitFor(()=>flow.defaults.samples==="quality",`${label} B: the device took the method`);
        const saved=Date.now();
        await other.waitForFunction(text=>document.querySelector('.status-setup [data-context="method"] b').textContent===text,vt("Until quality is OK"),{timeout:3000});
        assert(Date.now()-saved<=3200,`${label} B: context 2 follows within one poll`);
        await go(other,"measure/setup",".measurement-choice");
        assert.strictEqual(await other.locator('.measurement-choice [data-method="quality"]').getAttribute("aria-pressed"),"true",`${label} B: context 2 setup shows the method`);
        assert.strictEqual(await other.locator('.measurement-choice [data-method="5"]').getAttribute("aria-pressed"),"false");
        await go(page,"measure",".status-setup");
        await page.waitForFunction(text=>document.querySelector('.status-setup [data-context="method"] b').textContent===text,vt("Until quality is OK"));
        await other.locator("#field-setup-reference-frame").fill("ETRS89 (second)");
        await other.getByRole("button",{name:dict["ui.field_setup_save"],exact:true}).click();
        await waitFor(()=>flow.defaults.reference_frame==="ETRS89 (second)",`${label} B: the device took the frame`);
        await page.waitForFunction(text=>document.querySelector('.status-setup [data-context="frame"] b').textContent===text,"ETRS89 (second)",{timeout:3500});
        assert.deepStrictEqual(posts(since),[],`${label} B: no tracking action was sent`);
        assert.strictEqual(requestLog.slice(0).filter(entry=>entry.path==="/api/tracking"&&entry.method==="POST").length,trackingPostLog.length,`${label} B: the request log agrees`);
        assert.strictEqual(await openProjectPoint("p1"),cardBefore,`${label} B: the saved point keeps its own values`);
        assert.deepStrictEqual(tracking.projects[0].frames,{unknown:1},`${label} B: the project keeps its frame tally`);
      }finally{await second.context.close();}

      // C. Reminder #38a.
      console.log(`field-flow: C frame reminder ${label}`);
      await begin("measure");
      since=trackingPostLog.length;
      const frameTally=structuredClone(tracking.projects[0].frames);
      await page.waitForSelector(REMINDER);
      assert.strictEqual(await page.locator(REMINDER).isVisible(),true,`${label} C: reminder with an RTK fix and an empty frame`);
      assert(await page.locator(REMINDER).innerText().then(text=>text.includes(dict["ui.frame_reminder"])),`${label} C: reminder text`);
      await page.locator(REMINDER).getByRole("button",{name:dict["ui.frame_reminder_dismiss"],exact:true}).click();
      await page.waitForSelector(REMINDER,{state:"detached"});
      await tab("projects").click();await page.waitForSelector(".project-list");
      await tab("measure").click();await page.waitForSelector(".status-setup");await page.waitForTimeout(2300);
      assert.strictEqual(await page.locator(REMINDER).count(),0,`${label} C: hidden after navigation`);
      await page.reload();await page.waitForSelector(".status-setup");await page.waitForTimeout(2300);
      assert.strictEqual(await page.locator(REMINDER).count(),0,`${label} C: hidden after a reload`);
      const fresh=await openBrowser(language,size,"measure"),newer=fresh.tab;
      try{
        await newer.waitForSelector(REMINDER);
        // A browser that did not hide the reminder follows the device: back to the visible state in the first one.
        await page.evaluate(()=>{try{sessionStorage.clear();}catch(_){}});
        await page.reload();await page.waitForSelector(REMINDER);
        await newer.locator(REMINDER).getByRole("link",{name:dict["ui.frame_reminder_open"],exact:true}).click();
        await newer.waitForFunction(()=>location.hash==="#measure/setup/frame");
        await newer.waitForFunction(()=>document.activeElement&&document.activeElement.id==="field-setup-reference-frame");
        await newer.locator("#field-setup-reference-frame").fill("ETRS89 (example)");
        await newer.getByRole("button",{name:dict["ui.field_setup_save"],exact:true}).click();
        await waitFor(()=>flow.defaults.reference_frame==="ETRS89 (example)",`${label} C: the device took the frame`);
        await page.waitForSelector(REMINDER,{state:"detached",timeout:4000});
        assert.strictEqual(await frameRow(page).innerText(),"ETRS89 (example)",`${label} C: first browser shows the frame`);
        await go(newer,"measure",".status-setup");await newer.waitForTimeout(2300);
        assert.strictEqual(await newer.locator(REMINDER).count(),0,`${label} C: gone in the second context as well`);
      }finally{await fresh.context.close();}
      // The reminder follows the fix: RTK FLOAT shows it, a plain GPS fix does not.
      flow.defaults.reference_frame="";flowPublish();setFix(5,"RTK_FLOAT");
      await page.waitForSelector(REMINDER,{timeout:4000});
      setFix(1,"GPS_FIX");
      await page.waitForSelector(REMINDER,{state:"detached",timeout:4000});
      assert.deepStrictEqual(posts(since),[],`${label} C: no tracking action`);
      assert.deepStrictEqual(tracking.projects[0].frames,frameTally,`${label} C: the project points are unchanged`);

      // D. A running measurement survives navigation.
      console.log(`field-flow: D running measurement ${label}`);
      await begin("measure");
      since=trackingPostLog.length;
      let letThrough;
      const held=new Promise(resolve=>{letThrough=resolve;});
      actionAnswers.add_asset={hold:held,afterApply:()=>{live.measurement_progress=baseProgress();}};
      live.measurement_progress={active:true,collected:3,total:5,mode:"fixed",elapsed_sec:3};
      await startObjectPoint("Valve 25");
      await page.waitForSelector("dialog.measurement-sheet[open]");
      await waitFor(()=>posts(since,"add_asset").length===1,`${label} D: the measurement started`);
      await page.goBack();
      await page.waitForSelector("dialog.measurement-sheet[open]",{state:"detached"});
      assert.strictEqual(await hashOf(page),"#measure",`${label} D: Back only closed the sheet`);
      await tab("projects").click();await page.waitForSelector(".project-list");
      await tab("measure").click();await page.waitForSelector(".measurement-resume");
      assert.strictEqual(await page.locator(".measurement-resume b").innerText(),vt("Measurement running"),`${label} D: running notice`);
      assert.strictEqual(await page.locator(".measurement-resume button[data-measurement-cancel]").innerText(),vt("Cancel measurement"),`${label} D: cancel offered`);
      const locked=await page.evaluate(()=>[...document.querySelectorAll(".field-actions button:not(.danger):not([data-feedback-dismiss])")].map(button=>button.disabled));
      assert(locked.length>0&&locked.every(Boolean),`${label} D: the other actions are locked ${JSON.stringify(locked)}`);
      letThrough();
      await waitStrip("ok",vt("Object point saved")+" · Valve 25","D saved");
      assert.strictEqual(posts(since,"add_asset").length,1,`${label} D: one measurement only`);
      await page.waitForFunction(()=>!document.querySelector(".measurement-resume"));
      assert.strictEqual(posts(since,"add_asset").length,1,`${label} D: still one measurement`);

      // E. Maintenance while a measurement runs, and the way back after a restart.
      console.log(`field-flow: E maintenance and returning ${label}`);
      await begin("measure");
      since=requestLog.length;
      const trackingSince=trackingPostLog.length;
      live.measurement_progress={active:true,collected:2,total:5,mode:"fixed",elapsed_sec:2};
      await tab("device").click();
      await page.waitForFunction(()=>location.hash==="#device");await busyDone(page);
      await page.locator('#app-main a[href="/#device/maintenance"]').first().click();
      await page.waitForSelector('[data-card="restart"]');await busyDone(page);
      await page.getByRole("button",{name:vt("Restart device"),exact:true}).click();
      await page.waitForSelector("dialog[open]");
      const dialogText=await page.locator("dialog[open]").innerText();
      assert(dialogText.includes(dict["ui.guided_warn_measurement"]),`${label} E: the warning names the running measurement`);
      assert(await page.locator("dialog[open]").getByRole("button",{name:dict["ui.guided_restart_anyway"],exact:true}).isVisible(),`${label} E: Restart anyway`);
      await page.locator("dialog[open]").getByRole("button",{name:vt("Cancel"),exact:true}).click();
      await page.waitForSelector("dialog[open]",{state:"detached"});
      await page.waitForTimeout(500);
      assert.strictEqual(requests(since,"/api/reboot","POST").length,0,`${label} E: no restart was requested`);
      assert.deepStrictEqual(posts(trackingSince),[],`${label} E: the measurement was not touched`);
      assert.strictEqual(live.measurement_progress.active,true,`${label} E: the measurement runs on`);
      live.measurement_progress=baseProgress();
      // A restart marker from the old configuration route: the alias is replaced before the watcher resumes.
      await page.evaluate(()=>{try{sessionStorage.clear();}catch(_){}});
      await page.evaluate(([key,marker])=>sessionStorage.setItem(key,JSON.stringify(marker)),["rtk-restart-watch",{v:1,kind:"apply",route:"device/configuration",phase:"back",started_ms:Date.now(),apply_id:"0123456789abcdef",boot_id:"a1b2c3d4",host:"rtk-test.local",target_ssid:"NewNet",attempt:{keys:["wifi_ssid"],wifi_ssid:"NewNet"}}]);
      await page.goto("about:blank");
      await page.goto(base+"/#device/configuration");
      await page.waitForSelector("section.restart-watch");
      await page.waitForSelector('.connection-group[data-group="internet"] form.configuration-layout');
      assert.strictEqual(await hashOf(page),"#device/connections/internet",`${label} E: the form opens under the canonical route`);
      assert.strictEqual(await page.locator('.connection-group[data-group="internet"] input[name="wifi_ssid"]').count(),1,`${label} E: the Wi-Fi field is in the form`);
      await page.evaluate(()=>{try{sessionStorage.clear();}catch(_){}});

      // F. The two backups name their scope.
      console.log(`field-flow: F backup scopes ${label}`);
      await begin("device/maintenance");
      await page.waitForSelector('[data-card="config-backup"]');
      const backup=await page.locator('[data-card="config-backup"]').innerText();
      assert(backup.includes(dict["ui.guided_backup_help"]),`${label} F: the device backup names its content`);
      assert(backup.includes(dict["ui.maintenance_survey_hint"]),`${label} F: it points to the projects for survey data`);
      assert.strictEqual(await page.locator('[data-card="config-backup"] a[href="/#projects"]').count(),1,`${label} F: link to the projects`);
      await go(page,"projects",".data-management");
      await page.locator(".data-management summary").click();
      const management=await page.locator(".data-management").innerText();
      assert(management.includes(dict["ui.data_management_scope"]),`${label} F: data management names all projects and the device backup`);
      assert.strictEqual(await page.locator('.data-management a[href="/#device/maintenance"]').count(),1,`${label} F: link to the maintenance`);
      assert.strictEqual(await page.locator(".dialog-open, dialog[open]").count(),0);
    }

    // Screenshots for the documentation (English), only with RTK_SHOT_DIR.
    if(shotDir){
      console.log("field-flow: screenshots");
      fs.mkdirSync(shotDir,{recursive:true});
      const shoot=async(name,size,hash,selector,prepare,settled)=>{
        resetWorld();
        await page.setViewportSize(size);
        await page.evaluate(()=>{try{sessionStorage.clear();}catch(_){}});
        await setLanguage(page,"en");
        if(prepare)await prepare();
        await go(page,hash,selector);
        if(settled)await page.waitForFunction(settled,null,{timeout:8000});
        await page.waitForTimeout(600);
        await page.screenshot({path:`${shotDir}/${name}`});
      };
      for(const [suffix,size] of [["phone",{width:360,height:780}],["desktop",{width:1280,height:800}]]){
        await shoot(`status-${suffix}.png`,size,"measure",".readiness-hero.ready",async()=>{live.active_line={id:"ls1",name:"Water line",state:"recording",recording_mode:"survey",vertex_count:12,length_m:34.5,recording_sec:90};},()=>!document.querySelector(".field-actions").innerText.includes("Start line"));
        await shoot(`project-map-${suffix}.png`,size,"projects/p1",".map-panel");
        await page.waitForSelector(".map-world, .map-workspace");
        await shoot(`configuration-${suffix}.png`,size,"device/connections",'.connection-group[data-group="internet"] form.configuration-layout');
        resetWorld();
        await page.setViewportSize(size);
        await setLanguage(page,"en");
        let letThrough;
        const held=new Promise(resolve=>{letThrough=resolve;});
        actionAnswers.add_asset={hold:held,afterApply:()=>{live.measurement_progress=baseProgress();}};
        live.measurement_progress={active:true,collected:3,total:5,mode:"fixed",elapsed_sec:3};
        await page.evaluate(()=>{[...document.querySelectorAll(".field-actions > button")].find(node=>node.textContent==="Object point").click();});
        await page.locator("dialog.asset-sheet").waitFor();
        await page.locator('dialog.asset-sheet input[name="name"]').fill("Valve 26");
        await page.locator("dialog.asset-sheet").getByRole("button",{name:"Save point"}).click();
        await page.waitForSelector("dialog.measurement-sheet[open]");
        await page.waitForFunction(()=>document.querySelector("dialog.measurement-sheet .measurement-state").textContent.includes(" of 5 "));
        await page.screenshot({path:`${shotDir}/measure-dialog-${suffix}.png`});
        letThrough();
        await page.waitForSelector("dialog.measurement-sheet",{state:"detached"});
      }
    }
    assert.deepStrictEqual(errors,[],"page errors");
    await browser.close();
    console.log("field-flow-browser=ok");return;
  }
  if(process.argv.includes("--frame-suggestion-only")){
    // #38b Task 7: the frame the correction service reports (RTCM 1300), in both languages from 320 to 1280 px:
    // A adopt it, B dismiss it, C a differing frame or epoch, D no 1300. The device model is flow (see flowSuggestion).
    const base="http://local.test",errors=[];
    page.on("pageerror",error=>errors.push(error.message));
    const i18nModule=require(root+"/i18n.js"),dictionaries=i18nModule.dictionaries,v2de=i18nModule.v2de;
    const fill=(template,values)=>template.replace(/\{(\w+)\}/g,(_,key)=>values[key]);
    const baseFix=structuredClone(live.current_fix),baseQuality=live.current_quality,baseProgress=()=>({active:false,collected:0,total:5});
    const readyNetwork={ntrip_state:"streaming",ntrip_config:"ready",last_ntrip_error_code:null,access_state:"ONLINE"};
    let fixSeq=100;
    const setFix=(qual,text)=>{live.current_fix={...baseFix,qual,fix_status_text:text,stale:false,correction_age_sec:1,fix_seq:++fixSeq};};
    const setReady=()=>{setFix(4,"RTK_FIXED");baseQuality.accepted=true;setVerdict([]);live.gnss_accuracy={state:"receiving",attempts:0};live.network={...readyNetwork};};
    // The device before each case: the field setup and the announcement of the correction service.
    const resetWorld=(frame,epoch,service)=>{
      flow.putDelay=0;flow.service=service;
      flow.defaults={samples:5,antenna_height_m:0,reference_frame:frame,reference_frame_epoch:epoch};flowPublish();
      Object.assign(tracking.projects[0],{point_count:1,frames:{unknown:1},unknown_frames:true,mixed_frames:false});
      tracking.projects[1].point_count=0;tracking.active_project_id="p1";
      live.active_project={id:"p1",name:"Project",line_count:1,point_count:1};live.active_line=null;live.measurement_progress=baseProgress();
      captureState={epoch:"a1b2c3d4",stack:[],order:40};actionAnswers={};liveAnswers=[];checkAnswers=[];syncCandidate();delete live.tracking_startup;
      setReady();
    };
    flow.on=true;
    const requests=(since,path,method)=>requestLog.slice(since).filter(entry=>entry.path===path&&entry.method===method);
    const waitFor=async(check,message,timeout=8000)=>{const end=Date.now()+timeout;while(!check()){if(Date.now()>end)assert.fail(message);await page.waitForTimeout(50);}};
    const openBrowser=async(language,size,hash)=>{
      const context=await browser.newContext({viewport:size}),tab=await context.newPage();
      tab.setDefaultTimeout(20000);tab.on("pageerror",error=>errors.push(error.message));
      await context.addInitScript(value=>{try{localStorage.setItem("rtk-language",value);}catch(_){}},language);
      await context.route("http://local.test/**",fixture);
      await tab.goto(base+"/#"+hash);await tab.waitForSelector(".field-setup-layout");
      await tab.waitForFunction(()=>document.querySelector("#app-main").getAttribute("aria-busy")==="false");
      return{context,tab};
    };
    const REMINDER='.status-setup [data-notice="frame-reminder"]',DIFFERS='.status-setup [data-notice="frame-differs"]',BOX='[data-card="field-setup"] [data-notice="frame-suggestion"]',
      CONFIRMED='[data-card="field-setup"] [data-slot="frame-suggestion"] [data-hint="frame-confirmed"]',ADOPT=BOX+' [data-action="adopt-frame"]',DISMISS=BOX+' [data-action="dismiss-frame"]',
      FRAME_INPUT="#field-setup-reference-frame",EPOCH_INPUT="#field-setup-reference-frame-epoch",ANTENNA_INPUT="#field-setup-antenna-height";
    const SIZES=[[320,780],[360,780],[390,844],[480,900],[1280,800]];
    // Every control of the notices is at least 48 x 48 px and the page does not scroll sideways.
    const assertNoticeFits=async(p,selector,label)=>{
      const found=await p.evaluate(selector=>[...document.querySelectorAll(selector+" button, "+selector+" a.btn")].map(node=>{const box=node.getBoundingClientRect();return{text:node.textContent,width:box.width,height:box.height};}),selector);
      assert(found.length>0,`${label}: controls of ${selector}`);
      for(const item of found)assert(item.width>=48&&item.height>=48,`${label}: ${JSON.stringify(item)} is at least 48 x 48`);
      assertLayout(await layoutAudit(p),label);
    };

    for(const language of["en","de"]){
      const dict=dictionaries[language],vt=text=>language==="de"?v2de[text]:text;
      const epochText=epoch=>epoch?fill(vt("epoch {epoch}"),{epoch}):dict["ui.frame_epoch_none"];
      const differsText=(frame,epoch,current,currentEpoch)=>fill(vt("The correction service reports {frame} ({epoch}), the field setup says {current} ({current_epoch}). New RTK points are saved with the field setup value."),
        {frame,epoch:epochText(epoch),current:current||dict["ui.frame_not_set"],current_epoch:epochText(currentEpoch)});
      const newText=(frame,epoch)=>epoch?fill(vt("{frame}, epoch {epoch} – use it for new measurements?"),{frame,epoch}):fill(vt("{frame} – use it for new measurements?"),{frame});
      for(const [width,height] of SIZES){
        const size={width,height},label=`${language} ${width}`;
        const begin=async(hash,frame,epoch,service)=>{
          resetWorld(frame,epoch,service);
          await page.setViewportSize(size);
          await page.evaluate(()=>{try{sessionStorage.clear();}catch(_){}});
          await setLanguage(page,language);
          await page.waitForSelector(".status-setup");
          if(hash!=="measure")await go(page,hash,".field-setup-layout");
          await page.waitForFunction(()=>document.querySelector("#app-main").getAttribute("aria-busy")==="false");
        };
        const boxText=p=>p.locator(BOX).innerText();

        // A. Adopt the reported frame: only the click writes, and it writes exactly what was shown.
        console.log(`frame-suggestion: A adopt ${label}`);
        await begin("measure","","",{frame:"ETRS89",epoch:"2010.0",anchor:null});
        let since=requestLog.length;
        await page.waitForSelector(REMINDER);
        assert(await page.locator(REMINDER).innerText().then(text=>text.includes(fill(vt("The correction service reports {frame}."),{frame:"ETRS89"}))),`${label} A: the reminder names the reported frame`);
        assert.strictEqual(requests(since,"/api/field-setup","PUT").length,0,`${label} A: no PUT before a click (Measure)`);
        await go(page,"measure/setup",".field-setup-layout");
        await page.waitForSelector(BOX);
        assert.strictEqual(requests(since,"/api/field-setup","PUT").length,0,`${label} A: no PUT before a click (Field setup)`);
        assert.strictEqual(await page.locator(BOX+" p").nth(1).innerText(),newText("ETRS89","2010.0"),`${label} A: the question`);
        assert.strictEqual(await page.locator(BOX+" strong").innerText(),dict["ui.frame_suggestion_title"],`${label} A: the title`);
        assert.strictEqual(await page.locator(ADOPT).innerText(),dict["ui.frame_suggestion_adopt"],`${label} A: adopt button`);
        assert.strictEqual(await page.locator(DISMISS).innerText(),dict["ui.frame_suggestion_dismiss"],`${label} A: dismiss button`);
        const places=await page.evaluate(([box,input])=>({box:document.querySelector(box).getBoundingClientRect().bottom,input:document.querySelector(input).getBoundingClientRect().top}),[BOX,FRAME_INPUT]);
        assert(places.box<=places.input,`${label} A: the box sits above the frame field ${JSON.stringify(places)}`);
        await assertNoticeFits(page,BOX,`${label} A box`);
        await page.locator(ANTENNA_INPUT).fill("1.23");
        await page.locator(ADOPT).click();
        await waitFor(()=>requests(since,"/api/field-setup","PUT").length===1,`${label} A: one PUT`);
        assert.deepStrictEqual(JSON.parse(requests(since,"/api/field-setup","PUT")[0].body),{reference_frame:"ETRS89",reference_frame_epoch:"2010.0"},`${label} A: exactly the shown values`);
        await page.waitForSelector(CONFIRMED);
        assert.strictEqual(await page.locator(CONFIRMED).innerText(),dict["ui.frame_suggestion_matches"],`${label} A: confirmed`);
        assert.strictEqual(await page.locator(FRAME_INPUT).inputValue(),"ETRS89",`${label} A: frame field filled`);
        assert.strictEqual(await page.locator(EPOCH_INPUT).inputValue(),"2010.0",`${label} A: epoch field filled`);
        assert.strictEqual(await page.locator(ANTENNA_INPUT).inputValue(),"1.23",`${label} A: the unsaved antenna height stays`);
        assert.strictEqual(flow.defaults.antenna_height_m,0,`${label} A: the antenna height was not saved`);
        assert.strictEqual(await page.locator(BOX).count(),0,`${label} A: the question is gone`);
        await go(page,"measure",".status-setup");
        await page.waitForFunction(()=>!document.querySelector('.status-setup [data-notice="frame-reminder"]'));
        await page.waitForTimeout(2300);
        assert.strictEqual(await page.locator(REMINDER).count(),0,`${label} A: no reminder after adopting`);
        assert.strictEqual(await page.locator(DIFFERS).count(),0,`${label} A: no warning after adopting`);
        assert.strictEqual(requests(since,"/api/field-setup","PUT").length,1,`${label} A: still one PUT`);
        if(width===390){
          // A slow PUT: a new suggestion arrives meanwhile, a second click sends nothing, nothing unseen is saved.
          console.log(`frame-suggestion: A slow PUT ${label}`);
          await begin("measure/setup","","",{frame:"ETRS89",epoch:"2010.0",anchor:null});
          since=requestLog.length;
          await page.waitForSelector(BOX);
          flow.putDelay=8000;
          await page.evaluate(selector=>{const button=document.querySelector(selector);button.click();button.click();},ADOPT);
          await page.waitForFunction(selector=>{const button=document.querySelector(selector);return Boolean(button)&&button.disabled;},ADOPT);
          // One write lock: Save is off while the adoption runs, and a click on it sends nothing (the inputs still hold the old frame).
          const SAVE='[data-action="save-field-setup"]';
          assert.strictEqual(await page.locator(SAVE).isDisabled(),true,`${label} A: Save is disabled while the adoption runs`);
          assert.strictEqual(await page.locator(SAVE).getAttribute("aria-busy"),"true",`${label} A: Save is marked busy during the adoption`);
          await page.evaluate(selector=>{const button=document.querySelector(selector);if(button)button.click();},SAVE);
          flow.service={frame:"ETRS89",epoch:"2020.0",anchor:null};flowPublish();
          await page.waitForFunction(selector=>{const box=document.querySelector(selector);return Boolean(box)&&box.textContent.includes("2020.0");},BOX,{timeout:7000});
          assert.strictEqual(await page.locator(ADOPT).isDisabled(),true,`${label} A: the new suggestion arrives while the PUT runs and cannot be adopted yet`);
          assert.strictEqual(await page.locator(SAVE).isDisabled(),true,`${label} A: Save stays disabled while the new suggestion arrives`);
          await page.evaluate(selector=>{const button=document.querySelector(selector);if(button)button.click();},ADOPT);
          await page.evaluate(selector=>{const button=document.querySelector(selector);if(button)button.click();},SAVE);
          await page.waitForTimeout(300);
          assert.strictEqual(requests(since,"/api/field-setup","PUT").length,1,`${label} A: a double click, a click on the new suggestion and a click on Save send one PUT`);
          await waitFor(()=>flow.defaults.reference_frame==="ETRS89",`${label} A: the delayed PUT arrived`,9000);
          assert.deepStrictEqual(JSON.parse(requests(since,"/api/field-setup","PUT")[0].body),{reference_frame:"ETRS89",reference_frame_epoch:"2010.0"},`${label} A: the delayed PUT carries the shown values`);
          assert.strictEqual(flow.defaults.reference_frame_epoch,"2010.0",`${label} A: the unseen epoch was not saved`);
          await page.waitForFunction(selector=>{const box=document.querySelector(selector);return Boolean(box)&&box.textContent.includes("2020.0")&&!box.querySelector("button").disabled;},BOX,{timeout:9000});
          assert.strictEqual(requests(since,"/api/field-setup","PUT").length,1,`${label} A: the new suggestion waits for its own click`);
          assert.strictEqual(await page.locator(SAVE).isDisabled(),false,`${label} A: Save is enabled again after the adoption`);
          assert.strictEqual(await page.locator(SAVE).getAttribute("aria-busy"),null,`${label} A: Save is no longer busy`);
          assert.strictEqual(await page.locator("#field-setup-reference-frame").inputValue(),"ETRS89",`${label} A: the frame input shows the adopted frame`);
          assert.strictEqual(await page.locator("#field-setup-reference-frame-epoch").inputValue(),"2010.0",`${label} A: the epoch input shows the adopted epoch`);
          flow.putDelay=0;
        }

        // B. Dismiss: held for this tab session and exactly this suggestion.
        console.log(`frame-suggestion: B dismiss ${label}`);
        await begin("measure/setup","","",{frame:"ETRS89",epoch:"2010.0",anchor:null});
        since=requestLog.length;
        await page.waitForSelector(BOX);
        await page.locator(DISMISS).click();
        await page.waitForSelector(BOX,{state:"detached"});
        await go(page,"measure",".status-setup");await go(page,"measure/setup",".field-setup-layout");await page.waitForTimeout(2300);
        assert.strictEqual(await page.locator(BOX).count(),0,`${label} B: hidden after navigation`);
        await page.reload();await page.waitForSelector(".field-setup-layout");await page.waitForFunction(()=>document.querySelector("#app-main").getAttribute("aria-busy")==="false");await page.waitForTimeout(2300);
        assert.strictEqual(await page.locator(BOX).count(),0,`${label} B: hidden after a reload in the same tab`);
        assert.strictEqual(requests(since,"/api/field-setup","PUT").length,0,`${label} B: dismissing writes nothing`);
        assert.strictEqual(flow.defaults.reference_frame,"",`${label} B: the field setup is unchanged`);
        const fresh=await openBrowser(language,size,"measure/setup");
        try{
          await fresh.tab.waitForSelector(BOX);
          assert.strictEqual(await boxText(fresh.tab).then(text=>text.includes(newText("ETRS89","2010.0"))),true,`${label} B: a new browser context sees it`);
        }finally{await fresh.context.close();}
        flow.service={frame:"ETRS89/DREF91",epoch:"",anchor:null};flowPublish();
        await page.waitForSelector(BOX,{timeout:8000});
        assert.strictEqual(await page.locator(BOX+" p").nth(1).innerText(),newText("ETRS89/DREF91",""),`${label} B: a changed suggestion is shown again`);
        assert.strictEqual(requests(since,"/api/field-setup","PUT").length,0,`${label} B: still nothing written`);

        // C. A differing frame, then only a differing epoch, then a change from another browser.
        console.log(`frame-suggestion: C differs ${label}`);
        await begin("measure","ITRF2014","",{frame:"ETRS89",epoch:"",anchor:null});
        since=requestLog.length;
        await page.waitForSelector(DIFFERS);
        assert.strictEqual(await page.locator(DIFFERS+" p").innerText(),differsText("ETRS89","","ITRF2014",""),`${label} C: Measure warns with both frames`);
        assert.strictEqual(await page.locator(REMINDER).count(),0,`${label} C: the frame is set, no reminder`);
        await assertNoticeFits(page,DIFFERS,`${label} C measure`);
        await go(page,"measure/setup",".field-setup-layout");
        await page.waitForSelector(BOX);
        assert.strictEqual(await page.locator(BOX+" p").nth(1).innerText(),differsText("ETRS89","","ITRF2014",""),`${label} C: Field setup warns with both frames`);
        assert.strictEqual(await page.locator(BOX).evaluate(node=>node.classList.contains("warn")),true,`${label} C: warning style`);
        await assertNoticeFits(page,BOX,`${label} C box`);
        assert.strictEqual(requests(since,"/api/field-setup","PUT").length,0,`${label} C: a warning writes nothing`);
        // Only the epoch differs.
        await begin("measure","ETRS89","2010.0",{frame:"ETRS89",epoch:"2020.0",anchor:null});
        since=requestLog.length;
        await page.waitForSelector(DIFFERS);
        assert.strictEqual(await page.locator(DIFFERS+" p").innerText(),differsText("ETRS89","2020.0","ETRS89","2010.0"),`${label} C: Measure shows both epochs`);
        await go(page,"measure/setup",".field-setup-layout");
        await page.waitForSelector(BOX);
        assert.strictEqual(await page.locator(BOX+" p").nth(1).innerText(),differsText("ETRS89","2020.0","ETRS89","2010.0"),`${label} C: Field setup shows both epochs`);
        await page.locator(ADOPT).click();
        await waitFor(()=>requests(since,"/api/field-setup","PUT").length===1,`${label} C: one PUT`);
        assert.deepStrictEqual(JSON.parse(requests(since,"/api/field-setup","PUT")[0].body),{reference_frame:"ETRS89",reference_frame_epoch:"2020.0"},`${label} C: exactly the reported epoch`);
        await page.waitForSelector(CONFIRMED);
        assert.strictEqual(await page.locator(EPOCH_INPUT).inputValue(),"2020.0",`${label} C: epoch field filled`);
        // Another browser changes the field setup while this page shows a suggestion: the box follows without a reload.
        await begin("measure/setup","","",{frame:"ETRS89",epoch:"2010.0",anchor:null});
        since=requestLog.length;
        await page.waitForSelector(BOX);
        await page.locator(FRAME_INPUT).fill("typed");
        flow.defaults.reference_frame="ITRF2020";flow.defaults.reference_frame_epoch="";flowPublish();
        await page.waitForFunction(selector=>{const box=document.querySelector(selector);return Boolean(box)&&box.textContent.includes("ITRF2020");},BOX,{timeout:8000});
        assert.strictEqual(await page.locator(BOX+" p").nth(1).innerText(),differsText("ETRS89","2010.0","ITRF2020",""),`${label} C: the box follows the foreign change`);
        assert.strictEqual(await page.locator(FRAME_INPUT).inputValue(),"typed",`${label} C: the typed input is not overwritten`);
        assert.strictEqual(requests(since,"/api/field-setup","PUT").length,0,`${label} C: nothing written`);

        // D. No 1300: no box, no warning, the #38a reminder as before.
        console.log(`frame-suggestion: D no 1300 ${label}`);
        await begin("measure","","",null);
        since=requestLog.length;
        await page.waitForSelector(REMINDER);
        assert.strictEqual(await page.locator(REMINDER+" p").count(),1,`${label} D: the reminder keeps its single sentence`);
        assert.strictEqual(await page.locator(REMINDER+" p").innerText(),dict["ui.frame_reminder"],`${label} D: reminder text`);
        assert.strictEqual(await page.locator(DIFFERS).count(),0,`${label} D: no warning on Measure`);
        await go(page,"measure/setup",".field-setup-layout");await page.waitForTimeout(2300);
        assert.strictEqual(await page.locator('[data-notice="frame-suggestion"]').count(),0,`${label} D: no box`);
        await begin("measure","ITRF2014","",null);
        await page.waitForTimeout(2300);
        assert.strictEqual(await page.locator('[data-notice="frame-differs"], [data-notice="frame-reminder"]').count(),0,`${label} D: nothing with a set frame`);
        assert.strictEqual(requests(since,"/api/field-setup","PUT").length,0,`${label} D: nothing written`);
      }
    }
    assert.deepStrictEqual(errors,[],"page errors");
    await browser.close();
    console.log("frame-suggestion-browser=ok");return;
  }
  if(process.argv.includes("--export-only")){
    // #46: the GeoJSON export flow in the export sheet of a project, driven by a hand-steered export request (see the init script):
    // a delayed start, a slow stream, share and download, cancel, an incomplete or lost stream, a refusal, a stall, a hidden page,
    // leaving the view, the keyboard, live regions, closing the sheet during a run (the close question), layout and cleanup.
    const base="http://local.test",errors=[];
    page.on("pageerror",error=>errors.push(error.message));
    const i18nModule=require(root+"/i18n.js"),dictionaries=i18nModule.dictionaries;
    const dict=lang=>dictionaries[lang];
    const fill=(template,values)=>template.replace(/\{(\w+)\}/g,(_,key)=>values[key]);
    const sizeText=(lang,bytes)=>{const digits=bytes<100000?0:1,value=bytes<100000?bytes/1000:bytes/1e6;
      return new Intl.NumberFormat(lang==="de"?"de-DE":"en-US",{minimumFractionDigits:digits,maximumFractionDigits:digits}).format(value)+(bytes<100000?" kB":" MB");};
    // The test document: four parts (the three-part runs join the last two), about 2 kB each, complete only as a whole.
    const feature=(name,x)=>`{"type":"Feature","geometry":{"type":"Point","coordinates":[${x},50.2]},"properties":{"name":"${name}","note":"${"n".repeat(1900)}"}}`;
    const P=[`{"type":"FeatureCollection","features":[${feature("A",8.1)},`,`${feature("B",8.2)},`,`${feature("C",8.3)},`,`${feature("D",8.4)}]}`];
    const DOC=P.join(""),DOC_BYTES=Buffer.byteLength(DOC);
    const THREE=[P[0],P[1],P[2]+P[3]];
    const sheet=page.locator("dialog.export-sheet[open]"),flow=page.locator("dialog.export-sheet .export-flow");
    const startButton=page.locator('dialog.export-sheet [data-export-action="start"]');
    const QUESTION="dialog.export-close-question",question=page.locator(QUESTION+"[open]");
    const respond=options=>page.evaluate(value=>window.__exportRespond(value),options||{});
    const feed=text=>page.evaluate(value=>window.__exportFeed(value),text);
    const finish=kind=>page.evaluate(value=>window.__exportFinish(value),kind);
    const requests=()=>page.evaluate(()=>window.__exportRequests.map(entry=>({...entry})));
    const waitRequests=count=>page.waitForFunction(value=>window.__exportRequests.length>=value,count);
    const phaseNow=()=>page.evaluate(()=>{const node=document.querySelector(".export-flow");return node?node.dataset.phase:null;});
    const waitPhase=(phase,timeout=5000)=>page.waitForFunction(value=>{const node=document.querySelector(".export-flow");return Boolean(node)&&node.dataset.phase===value;},phase,{timeout});
    const phaseText=()=>page.evaluate(()=>document.querySelector(".export-flow-phase").textContent.trim());
    const metaText=()=>page.evaluate(()=>document.querySelector(".export-flow-meta").textContent.trim());
    const outcomeNow=()=>page.evaluate(()=>document.querySelector(".export-flow").dataset.outcome);
    const waitText=(text,timeout=5000)=>page.waitForFunction(value=>document.querySelector(".export-flow-phase").textContent.trim()===value,text,{timeout});
    const active=()=>page.evaluate(()=>{const node=document.activeElement;return node&&node!==document.body?`${node.tagName}|${node.dataset.exportAction||""}|${node.textContent.trim().slice(0,40)}`:"body";});
    const dockTexts=()=>page.evaluate(()=>[...document.querySelectorAll("#feedback-dock .feedback-item")].map(node=>node.textContent.trim()));
    const toastCount=()=>page.locator(".toast, .feedback-item.critical").count();
    const hashNow=()=>page.evaluate(()=>location.hash);
    // The hash and whether the history is on the sheet's entry (a stacked dialog tags the entry below with rtkSheetBase, which stays).
    const sheetState=()=>page.evaluate(()=>JSON.stringify([location.hash,Boolean(history.state&&history.state.rtkSheet)]));
    const sheetGone=()=>page.waitForFunction(()=>!document.querySelector("dialog[open]")&&!(history.state&&history.state.rtkSheet));
    const closeQuestions=()=>page.evaluate(()=>window.__closeQuestions);
    const abortListeners=()=>page.evaluate(()=>window.__abortListeners);
    const openSheet=async lang=>{await page.locator(".project-actions").getByRole("button",{name:dict(lang)["ui.project_export"],exact:true}).click();await sheet.waitFor();};
    // A fresh sheet on the project of the fixture; counters reset; export requests are steered by hand from now on.
    // clean: reload first, so the dock holds nothing of earlier scenarios (a message with the same key replaces the old one).
    const open=async (lang,clean=false)=>{
      if(clean)await setLanguage(page,lang);
      await go(page,"projects",".project-list");
      await go(page,"projects/p1",".map-panel .project-actions");
      await page.evaluate(()=>{window.__exportManual=true;window.__exportRequests.length=0;window.__shareMode=null;window.__shareCalls.length=0;window.__canShareCalls.length=0;window.__closeQuestions=0;});
      await openSheet(lang);
      assert.strictEqual(await phaseNow(),"idle","a fresh sheet shows the idle flow");
    };
    const startRun=async count=>{await startButton.click();await waitRequests(count);};
    const toTransferring=async count=>{await startRun(count);await respond();await feed(P[0]);await waitPhase("transferring");};
    const complete=async()=>{await feed(P[1]);await feed(P[2]+P[3]);await finish("close");await waitPhase("ready");};
    const closeWays={
      button:lang=>sheet.getByRole("button",{name:dict(lang)["ui.why_close"],exact:true}).click(),
      escape:()=>page.keyboard.press("Escape"),
      back:()=>page.goBack(),
    };
    const downloadFile=async(selector="dialog.export-sheet [data-export=geojson-download]")=>{
      const [download]=await Promise.all([page.waitForEvent("download"),page.locator(selector).click()]);
      assert(download.suggestedFilename().endsWith(".geojson"),"the file name ends on .geojson: "+download.suggestedFilename());
      assert.strictEqual(fs.readFileSync(await download.path(),"utf8"),DOC,"the download holds the whole document");
    };
    // Live regions of the export area (Codex K8): the shared recordLiveRegions misses text node changes, so this one walks to the
    // nearest live region of every record itself.
    const recordExportPhaseTexts=()=>page.evaluate(()=>{
      window.__exportPhaseTexts=[];window.__exportMetaLive=0;
      const area=document.querySelector("dialog.export-sheet .export-flow"),live='[role="status"],[aria-live]';
      new MutationObserver(records=>{for(const record of records){
        const element=record.target.nodeType===1?record.target:record.target.parentElement,region=element&&element.closest(live);
        if(!region||!area.contains(region))continue;
        if(region.closest(".export-flow-meta")){window.__exportMetaLive++;continue;}
        const text=region.textContent,list=window.__exportPhaseTexts;
        if(text!==list[list.length-1])list.push(text);}}).observe(area,{childList:true,characterData:true,subtree:true});
    });
    // A real hide or show: document.hidden and visibilityState change and visibilitychange fires, as when the screen goes off or the tab is switched.
    const showPage=visible=>page.evaluate(value=>{
      Object.defineProperty(document,"hidden",{configurable:true,get:()=>value!=="visible"});
      Object.defineProperty(document,"visibilityState",{configurable:true,get:()=>value});
      document.dispatchEvent(new Event("visibilitychange"));
    },visible?"visible":"hidden");
    const noPercent=async label=>assert(!/%/.test(await flow.innerText()),label+": no percentage anywhere in the flow");
    const stamp=text=>text.match(/(\d+):(\d\d)(?::(\d\d))?/);
    const seconds=text=>{const m=stamp(text);return m[3]?(+m[1])*3600+(+m[2])*60+(+m[3]):(+m[1])*60+(+m[2]);};

    // a b c n: delayed start, slow stream, success and download, live regions; German and English once each.
    const scenarioABC=async lang=>{
      const d=dict(lang),label=`abc ${lang}`;
      console.log("export: a b c n "+lang);
      await setLanguage(page,lang);await page.setViewportSize({width:360,height:800});
      await open(lang);
      await recordExportPhaseTexts();
      // a: no headers yet.
      await startButton.click();
      await waitPhase("preparing",300);
      assert.strictEqual(await phaseText(),d["ui.export_flow_preparing"],label+" a: preparing text");
      assert(new RegExp("^"+fill(d["ui.export_flow_meta"],{elapsed:"0:0\\d",size:"0 kB"})).test(await metaText()),label+" a: meta "+await metaText());
      await noPercent(label+" a");
      await waitRequests(1);
      // b: headers and the first part.
      await respond();await feed(THREE[0]);
      await waitPhase("transferring");
      assert.strictEqual(await phaseText(),d["ui.export_flow_transferring"],label+" b: transferring text");
      await page.waitForFunction(expected=>document.querySelector(".export-flow-meta").textContent.includes(expected),Math.round(Buffer.byteLength(THREE[0])/1000)+" kB",{timeout:3000});
      const metaA=await metaText();
      await page.waitForTimeout(1200);
      const metaB=await metaText();
      await page.waitForTimeout(1200);
      const metaC=await metaText();
      assert(seconds(metaB)>seconds(metaA)&&seconds(metaC)>seconds(metaB),`${label} b: the elapsed time keeps counting (${metaA} | ${metaB} | ${metaC})`);
      await noPercent(label+" b");
      assert.strictEqual(await startButton.isDisabled(),true,label+" b: start is disabled during the run");
      assert.strictEqual(await startButton.getAttribute("aria-busy"),"true",label+" b: start is busy");
      await page.evaluate(()=>{const button=document.querySelector('[data-export-action="start"]');for(let i=0;i<3;i++){button.click();button.dispatchEvent(new MouseEvent("click",{bubbles:true}));}});
      await page.waitForTimeout(150);
      assert.strictEqual((await requests()).length,1,label+" b: three more clicks start no second request");
      // c: the rest, then the checked result.
      await feed(THREE[1]);await feed(THREE[2]);await finish("close");
      await waitPhase("ready");
      const readyText=fill(d["ui.export_flow_ready"],{size:sizeText(lang,DOC_BYTES)});
      assert.strictEqual(await phaseText(),readyText,label+" c: ready text with the size");
      assert.strictEqual(await outcomeNow(),"ready");
      assert.deepStrictEqual(await page.evaluate(()=>window.__exportPhaseTexts),[d["ui.export_flow_preparing"],d["ui.export_flow_transferring"],readyText],label+" n: the live region announced exactly three phase texts");
      assert.strictEqual(await page.evaluate(()=>window.__exportMetaLive),0,label+" n: the meta line is no live region");
      assert.strictEqual(await page.locator(".export-flow-meta[role], .export-flow-meta[aria-live]").count(),0,label+" n: meta carries no live attribute");
      assert(/^(Dauer|Took) /.test(await metaText()),label+" c: final meta line "+await metaText());
      assert.strictEqual(await requests().then(list=>list[0].aborted),false,label+" c: a finished transfer is not aborted");
      const buttons=await page.locator("dialog.export-sheet .export-flow-actions button").evaluateAll(nodes=>nodes.map(node=>[node.dataset.exportAction,node.textContent.trim()]));
      assert.deepStrictEqual(buttons,[["download",d["ui.export_download"]],["discard",d["ui.export_flow_discard"]]],label+" c: ready actions (no share without canShare)");
      assert.strictEqual(await active(),`BUTTON|download|${d["ui.export_download"]}`,label+" c: focus on the first ready action");
      if(lang==="en")assert.strictEqual(d["ui.export_download"],"Download file",label+" c: download wording");
      assert.strictEqual(await startButton.textContent(),d["ui.export_flow_retry"],label+" c: start offers a new export");
      const [download]=await Promise.all([page.waitForEvent("download"),page.locator("[data-export=geojson-download]").click()]);
      assert(download.suggestedFilename().endsWith(".geojson"),label+" c: file name");
      assert.strictEqual(fs.readFileSync(await download.path(),"utf8"),DOC,label+" c: file content equals the document");
      assert.strictEqual(await phaseText(),d["ui.export_flow_downloaded"],label+" c: downloaded text");
      // The wording says the file was handed over and leaves the saving to the browser (exact text of the dictionary, above).
      if(lang==="en")assert(!/\bfile (was |has been |is )?saved|saved successfully/i.test(await phaseText()),label+" c: no claim that the file was saved");
    };
    await scenarioABC("de");
    await scenarioABC("en");

    // 4.8 MB: the size the meta line and the result show.
    {
      console.log("export: size");
      const head='{"type":"FeatureCollection","pad":"',tail='","features":[]}',padding=4800000-head.length-tail.length;
      for(const lang of["de","en"]){
        await setLanguage(page,lang);await open(lang);
        await startRun(1);await respond();await feed(head);
        await page.evaluate(count=>window.__exportFeedPad(count),padding);
        await page.waitForFunction(()=>/\bMB\b/.test(document.querySelector(".export-flow-meta").textContent),null,{timeout:4000});
        const meta=await metaText();
        assert(meta.includes(lang==="de"?"4,8 MB":"4.8 MB"),`size ${lang}: meta shows 4.8 MB (${meta})`);
        await feed(tail);await finish("close");await waitPhase("ready");
        assert(await phaseText().then(text=>text.includes(lang==="de"?"4,8 MB":"4.8 MB")),`size ${lang}: result shows the size`);
      }
    }

    // d: share outcomes.
    {
      const lang="de",d=dict(lang);
      console.log("export: d share");
      await setLanguage(page,lang);await open(lang);
      await page.evaluate(()=>{window.__shareMode="resolve";});
      await startRun(1);await respond();await feed(P[0]);await complete();
      const share=page.locator("[data-export=geojson-share]");
      assert.strictEqual((await share.textContent()).trim(),d["ui.export_share"],"d: share wording");
      assert.strictEqual(await active(),`BUTTON|share|${d["ui.export_share"]}`,"d: focus on the share action when it exists");
      const asked=await page.evaluate(()=>window.__canShareCalls);
      assert.strictEqual(asked.length>=1&&asked.every(names=>names.length===1&&names[0].endsWith(".geojson")),true,"d: canShare asked with exactly the .geojson file "+JSON.stringify(asked));
      await share.click();
      await waitText(d["ui.export_flow_shared"]);
      const calls=await page.evaluate(()=>window.__shareCalls);
      assert.strictEqual(calls.length,1);assert(calls[0].files.length===1&&calls[0].files[0].name.endsWith(".geojson")&&calls[0].files[0].size===DOC_BYTES,"d: the real file was shared "+JSON.stringify(calls));
      await page.evaluate(()=>{window.__shareMode="abort";});
      await share.click();await waitText(d["ui.export_flow_share_cancelled"]);
      assert.strictEqual(await page.locator("[data-export=geojson-download]").count(),1,"d: download stays after a cancelled share");
      await page.evaluate(()=>{window.__shareMode="notallowed";});
      await share.click();await waitText(d["ui.export_flow_share_unavailable"]);
      assert.strictEqual(await page.locator("[data-export=geojson-download]").count(),1,"d: download stays after a refused share");
      // canShare false: no share button.
      await open(lang);
      await startRun(1);await respond();await feed(P[0]);await complete();
      assert.strictEqual(await page.locator("[data-export=geojson-share]").count(),0,"d: no share button without canShare");
      assert.strictEqual(await page.locator("[data-export=geojson-download]").count(),1);
      // English wording.
      await setLanguage(page,"en");await open("en");await page.evaluate(()=>{window.__shareMode="resolve";});
      await startRun(1);await respond();await feed(P[0]);await complete();
      assert.strictEqual((await page.locator("[data-export=geojson-share]").textContent()).trim(),"Share file","d: English share wording");
    }

    // e cancel, f incomplete, g lost, h refused.
    {
      const lang="de",d=dict(lang);
      await setLanguage(page,lang);
      console.log("export: e cancel");
      await open(lang);
      await toTransferring(1);
      await page.locator('[data-export-action="cancel"]').click();
      await waitText(d["ui.export_flow_cancelled"]);
      assert.strictEqual((await requests())[0].aborted,true,"e: the request was aborted");
      assert.strictEqual(await phaseNow(),"cancelled");
      assert.strictEqual(await page.locator("[data-export=geojson-download], [data-export=geojson-share]").count(),0,"e: no ready actions");
      assert.strictEqual(await startButton.isDisabled(),false,"e: start is free");
      assert.strictEqual((await startButton.textContent()).trim(),d["ui.export_flow_retry"]);
      await page.waitForFunction(()=>document.activeElement&&document.activeElement.dataset.exportAction==="start");
      await startRun(2);
      assert.strictEqual((await requests()).length,2,"e: a new start makes a second request");
      await waitPhase("preparing");
      console.log("export: f g h");
      await open(lang);
      await startRun(1);await respond();await feed(P[0]);await finish("close");
      await waitText(d["ui.export_flow_incomplete"]);
      assert.strictEqual(await page.locator("[data-export=geojson-download], [data-export=geojson-share]").count(),0,"f: nothing offered");
      assert.strictEqual(await startButton.isDisabled(),false,"f: start is free");
      await open(lang);
      await startRun(1);await respond();await feed(P[0]);await finish("error");
      await waitText(d["ui.export_flow_lost"]);
      assert.strictEqual(await startButton.isDisabled(),false,"g: start is free");
      assert.strictEqual(await page.locator("[data-export=geojson-download]").count(),0);
      await open(lang,true);
      const dockBefore=(await dockTexts()).length;
      await startRun(1);
      await respond({status:503,body:{error:"storage_busy",message:"Storage maintenance in progress. Please wait."}});
      await page.waitForFunction(()=>document.querySelector(".export-flow").dataset.outcome==="refused");
      assert((await phaseText()).startsWith("Export fehlgeschlagen:"),"h: refusal text "+await phaseText());
      assert.strictEqual((await dockTexts()).length,dockBefore,"h: no dock entry");
      assert.strictEqual(await toastCount(),0,"h: no error toast");
      assert.strictEqual(await startButton.isDisabled(),false,"h: start is free");
    }

    // i stalled, j alive, j2 real deadline, k hidden.
    {
      const lang="de",d=dict(lang);
      console.log("export: i stalled");
      await open(lang);
      await toTransferring(1);
      await shiftClock(31000);
      await waitText(fill(d["ui.export_flow_stalled"],{seconds:30}),1500);
      assert.strictEqual((await requests())[0].aborted,true,"i: request aborted");
      console.log("export: j alive");
      await open(lang);
      await startRun(1);await respond();
      for(const part of P){await shiftClock(25000);await feed(part);await page.waitForTimeout(150);assert.notStrictEqual(await outcomeNow(),"stalled","j: no stall while data arrives");}
      await finish("close");await waitPhase("ready");
      assert(seconds(await metaText())>=75,"j: the meta line shows at least 1:15 "+await metaText());
      console.log("export: j2 real deadline");
      await open(lang);
      const startedAt=Date.now();
      await startRun(1);await respond();
      for(const part of P){await feed(part);await page.waitForTimeout(3000);}
      await finish("close");await waitPhase("ready");
      assert(Date.now()-startedAt>9000,"j2: the run lasted longer than the 8 s API deadline");
      assert.strictEqual((await requests())[0].aborted,false,"j2: nothing aborted the request");
      console.log("export: k hidden");
      // E12 and device check 46.4: the page is hidden with the real event and nothing arrives meanwhile. The silence is no stall, the
      // app must neither abort the route (which would close the sheet and the export) nor render it again, and on return the stall
      // reference is reset to now (without the component's reset the next tick would find 40 s of silence).
      await open(lang);
      await toTransferring(1);
      const dockBeforeK=await dockTexts();
      await showPage(false);await shiftClock(40000);
      await page.waitForTimeout(2500);
      assert.strictEqual(await phaseNow(),"transferring","k: a hidden page does not stall");
      assert.strictEqual((await requests())[0].aborted,false,"k: hiding the page does not abort the export");
      assert(await page.locator("dialog.export-sheet").count(),"k: the sheet stays open while hidden");
      await showPage(true);
      await page.waitForTimeout(2500);
      assert.strictEqual(await phaseNow(),"transferring","k: returning to the foreground does not stall");
      assert.strictEqual((await requests())[0].aborted,false,"k: showing the page does not abort the export");
      assert(await page.locator("dialog.export-sheet").count(),"k: the sheet stays open after the return");
      // The project view's own map loop stopped while the page was hidden (its timer fired then); it resumes on return, once, at the normal pace.
      {
        const before=liveAnswered;
        const t0=Date.now();while(liveAnswered===before&&Date.now()-t0<3000)await page.waitForTimeout(50);
        assert(liveAnswered>before,"k: the map loop asks for the live state again after the return with the sheet open");
        const mark=liveAnswered;await page.waitForTimeout(6000);
        const rate=liveAnswered-mark;
        assert(rate>=2&&rate<=4,`k: the resumed map loop keeps its 2 s cadence without a second loop (${rate} answers in 6 s)`);
      }
      await feed(P[1]);await feed(P[2]+P[3]);await finish("close");await waitPhase("ready");
      await page.keyboard.press("Escape");await sheetGone();
      {const afterClose=liveAnswered;await page.waitForTimeout(4500);assert(liveAnswered-afterClose>=1,"k: the map loop keeps polling after the sheet closed");}
      assert.deepStrictEqual((await dockTexts()).filter(text=>!dockBeforeK.includes(text)),[],"k: hide and show leave no dock entry");
      await page.evaluate(()=>{delete document.hidden;delete document.visibilityState;});
    }

    // l route, m keyboard.
    {
      const lang="de",d=dict(lang);
      console.log("export: l route");
      await open(lang,true);
      await toTransferring(1);
      const dockBefore=await dockTexts();
      await page.evaluate(()=>{location.hash="projects";});
      await page.waitForSelector("dialog.export-sheet",{state:"detached"});
      await page.waitForFunction(()=>window.__exportRequests[0].aborted);
      assert.strictEqual(await closeQuestions(),0,"l: no close question when the view is left");
      await sheetGone();
      // Late-action ruling (#24): the view is gone, so the result goes to the dock, once; nothing is shown as an error.
      const dockAfter=await dockTexts();
      assert.deepStrictEqual(dockAfter.filter(text=>!dockBefore.includes(text)).map(text=>text.includes(d["ui.export_flow_cancelled"])),[true],"l: one dock entry, the cancelled export "+JSON.stringify(dockAfter));
      assert.strictEqual(await toastCount(),0,"l: no critical entry");
      console.log("export: m keyboard");
      await open(lang);
      let tabs=0;
      while(!(await active()).startsWith("BUTTON|start|")&&tabs++<12)await page.keyboard.press("Tab");
      assert((await active()).startsWith("BUTTON|start|"),"m: Tab reaches the start button");
      await page.keyboard.press("Enter");
      await waitRequests(1);
      await respond();await feed(P[0]);await complete();
      assert.strictEqual(await active(),`BUTTON|download|${d["ui.export_download"]}`);
      const [download]=await Promise.all([page.waitForEvent("download"),page.keyboard.press("Enter")]);
      assert.strictEqual(fs.readFileSync(await download.path(),"utf8"),DOC,"m: Enter on the download starts it");
    }

    // o: closing the sheet during a run asks first.
    {
      const lang="de",d=dict(lang);
      const checkQuestion=async(label,lang2)=>{
        const t=dict(lang2);
        await question.waitFor();
        assert.strictEqual((await question.locator("h2").innerText()).trim(),t["ui.export_flow_close_title"],label+": title");
        assert.deepStrictEqual(await question.locator(".sheet-actions .btn").allInnerTexts(),[t["ui.export_flow_close_stay"],t["ui.export_flow_close_confirm"]],label+": buttons");
        assert.strictEqual(await page.evaluate(()=>document.activeElement.textContent.trim()),t["ui.export_flow_close_stay"],label+": focus on Keep exporting");
        assert.strictEqual(await sheet.count(),1,label+": the export sheet stays open");
        assert.strictEqual((await requests())[0].aborted,false,label+": not aborted");
        assert.strictEqual(await closeQuestions(),1,label+": exactly one question");
      };
      assert.strictEqual(d["ui.export_flow_close_stay"],"Bleiben");assert.strictEqual(d["ui.export_flow_close_confirm"],"Abbrechen");
      assert.strictEqual(dict("en")["ui.export_flow_close_stay"],"Keep exporting");assert.strictEqual(dict("en")["ui.export_flow_close_confirm"],"Cancel export");
      // (1) Keep exporting: nine combinations of the way to close and the way to answer, plus the button in preparing.
      for(const state of["transferring","preparing"])for(const way of Object.keys(closeWays))for(const answer of["stay","escape","back"]){
        if(state==="preparing"&&!(way==="button"&&answer==="stay"))continue;
        const label=`o stay ${state} ${way}/${answer}`;
        console.log("export: "+label);
        await open(lang);
        if(state==="transferring")await toTransferring(1);else{await startRun(1);await waitPhase("preparing");}
        const before=await sheetState(),focusBefore=await active();
        await closeWays[way](lang);
        await checkQuestion(label,lang);
        if(answer==="stay")await question.getByRole("button",{name:d["ui.export_flow_close_stay"],exact:true}).click();
        else if(answer==="escape")await page.keyboard.press("Escape");
        else await page.goBack();
        await page.waitForSelector(QUESTION,{state:"detached"});
        await page.waitForFunction(value=>JSON.stringify([location.hash,Boolean(history.state&&history.state.rtkSheet)])===value,before,{timeout:3000});
        assert.strictEqual(await sheet.count(),1,label+": sheet open after Keep exporting");
        const expected=way==="button"?`BUTTON||${d["ui.why_close"]}`:focusBefore;
        await page.waitForFunction(value=>{const node=document.activeElement,now=node&&node!==document.body?`${node.tagName}|${node.dataset.exportAction||""}|${node.textContent.trim().slice(0,40)}`:"body";return now===value;},expected,{timeout:3000});
        if(state==="preparing"){await respond();await feed(P[0]);await waitPhase("transferring");}
        assert.strictEqual(await phaseNow(),"transferring",label+": the run goes on");
        await complete();
        assert.strictEqual((await requests()).length,1,label+": still one request");
        assert.strictEqual((await requests())[0].aborted,false);
      }
      // (2) Cancel export: the sheet closes, the request ends, nothing is reported, the project stays.
      for(const way of Object.keys(closeWays)){
        const label=`o cancel ${way}`;
        console.log("export: "+label);
        await open(lang,true);
        await toTransferring(1);
        const dockBefore=await dockTexts();
        await closeWays[way](lang);
        await checkQuestion(label,lang);
        await question.getByRole("button",{name:d["ui.export_flow_close_confirm"],exact:true}).click();
        await sheetGone();
        await page.waitForFunction(()=>window.__exportRequests[0].aborted===true);
        assert.deepStrictEqual(await dockTexts(),dockBefore,label+": no dock entry");
        assert.strictEqual(await toastCount(),0,label+": no toast");
        assert.strictEqual(await hashNow(),"#projects/p1",label+": still on the project");
        await openSheet(lang);
        assert.strictEqual(await phaseNow(),"idle",label+": reopened sheet starts idle");
        await startRun(2);
        assert.strictEqual((await requests()).length,2,label+": a second start makes a new request");
      }
      // o3: the view changes under the question.
      console.log("export: o3 route under the question");
      await open(lang,true);
      await toTransferring(1);
      let dockBefore=await dockTexts();
      await closeWays.button(lang);
      await checkQuestion("o3",lang);
      await page.evaluate(()=>{location.hash="projects";});
      await page.waitForFunction(()=>window.__exportRequests[0].aborted===true);
      await page.waitForFunction(()=>!document.querySelector("#overlay-root dialog[open]")&&!document.querySelector("#overlay-root dialog"));
      await sheetGone();
      assert.strictEqual(await closeQuestions(),1,"o3: no second question");
      assert.deepStrictEqual((await dockTexts()).filter(text=>!dockBefore.includes(text)).map(text=>text.includes(d["ui.export_flow_cancelled"])),[true],"o3: the cancelled export is reported once, in the dock");
      assert.strictEqual(await toastCount(),0,"o3: no error entry");
      // o4: the run ends under the question.
      console.log("export: o4 run ends under the question");
      await open(lang);
      await toTransferring(1);
      await closeWays.button(lang);
      await checkQuestion("o4",lang);
      await complete();
      await page.waitForSelector(QUESTION,{state:"detached"});
      assert.strictEqual(await sheet.count(),1,"o4: the sheet stays open");
      assert.strictEqual(await phaseNow(),"ready");
      assert.strictEqual(await phaseText(),fill(d["ui.export_flow_ready"],{size:sizeText(lang,DOC_BYTES)}),"o4: the result is visible");
      assert.strictEqual((await requests())[0].aborted,false,"o4: not aborted");
      await downloadFile();
      // English wording of the question, once.
      await setLanguage(page,"en");await open("en");
      await toTransferring(1);
      await closeWays.button("en");
      await checkQuestion("o en","en");
      await question.getByRole("button",{name:"Keep exporting",exact:true}).click();
      await page.waitForSelector(QUESTION,{state:"detached"});
      await complete();
    }

    // o2: no question when nothing runs.
    {
      const lang="de";
      await setLanguage(page,lang);
      const reach={
        idle:async()=>{},
        ready:async()=>{await startRun(1);await respond();await feed(P[0]);await complete();},
        cancelled:async()=>{await toTransferring(1);await page.locator('[data-export-action="cancel"]').click();await waitPhase("cancelled");},
        incomplete:async()=>{await startRun(1);await respond();await feed(P[0]);await finish("close");await waitPhase("failed");},
        lost:async()=>{await startRun(1);await respond();await feed(P[0]);await finish("error");await waitPhase("failed");},
      };
      for(const [state,drive] of Object.entries(reach))for(const way of Object.keys(closeWays)){
        console.log(`export: o2 ${state} ${way}`);
        await open(lang);
        await drive();
        const length=await page.evaluate(()=>history.length);
        await closeWays[way](lang);
        await sheetGone();
        assert.strictEqual(await closeQuestions(),0,`o2 ${state} ${way}: no question`);
        assert.strictEqual(await hashNow(),"#projects/p1",`o2 ${state} ${way}: still on the project`);
        assert.strictEqual(await page.evaluate(()=>history.length),length,`o2 ${state} ${way}: the history stands as without a hook`);
      }
    }

    // q: three runs in one sheet, then leave; objects and listeners are released.
    {
      const lang="de",d=dict(lang);
      console.log("export: q repeat and leave");
      await setLanguage(page,lang);
      await page.evaluate(()=>{window.__exportManual=true;});
      await go(page,"projects",".project-list");
      await go(page,"projects/p1",".map-panel .project-actions");
      await page.evaluate(()=>{window.__exportManual=true;window.__exportRequests.length=0;});
      const listenersBefore=await abortListeners();
      await openSheet(lang);
      assert.strictEqual(await abortListeners(),listenersBefore,"q: opening the sheet adds no listener of the component");
      await startRun(1);await respond();await feed(P[0]);
      await waitPhase("transferring");
      assert.strictEqual(await abortListeners(),listenersBefore+2,"q: during a run the disposer and the forwarding are registered");
      await complete();
      assert.strictEqual(await abortListeners(),listenersBefore+1,"q: after run 1 only the disposer is left");
      await downloadFile();
      await startButton.click();await waitRequests(2);await respond();await feed(P[0]);await complete();
      assert.strictEqual(await abortListeners(),listenersBefore+1,"q: after run 2");
      await downloadFile();
      await page.locator('[data-export-action="discard"]').click();
      await waitPhase("idle");
      await startButton.click();await waitRequests(3);await respond();await feed(P[0]);await complete();
      assert.strictEqual(await abortListeners(),listenersBefore+1,"q: after run 3");
      await downloadFile();
      // A question opened and answered with Keep exporting leaves no listener either.
      await startButton.click();await waitRequests(4);await respond();await feed(P[0]);await waitPhase("transferring");
      await closeWays.button(lang);await question.waitFor();
      assert.strictEqual(await abortListeners(),listenersBefore+3,"q: the open question holds one more listener");
      await question.getByRole("button",{name:d["ui.export_flow_close_stay"],exact:true}).click();
      await page.waitForSelector(QUESTION,{state:"detached"});
      assert.strictEqual(await abortListeners(),listenersBefore+2,"q: the answered question holds none");
      await complete();
      assert.strictEqual(await abortListeners(),listenersBefore+1,"q: after run 4");
      await closeWays.button(lang);await sheetGone();
      assert.strictEqual(await closeQuestions(),1,"q: only the question during run 4");
      assert.strictEqual(await abortListeners(),listenersBefore,"q: closing the sheet releases the disposer");
      let urls=await page.evaluate(()=>window.__objectUrls);
      assert.deepStrictEqual(urls,{created:3,revoked:3},"q: every file object URL was released (3 downloads)");
      // Leave from ready by navigation.
      await openSheet(lang);
      await startRun(5);await respond();await feed(P[0]);await complete();
      await downloadFile();
      urls=await page.evaluate(()=>window.__objectUrls);
      assert.strictEqual(urls.created-urls.revoked,1,"q: one live URL in ready");
      await page.evaluate(()=>{location.hash="projects";});
      await page.waitForSelector("dialog.export-sheet",{state:"detached"});
      urls=await page.evaluate(()=>window.__objectUrls);
      assert.strictEqual(urls.created,urls.revoked,"q: navigation releases the URL");
      assert.strictEqual(await abortListeners(),listenersBefore,"q: navigation leaves no listener");
    }

    // p: layout in both languages from 320 to 1280 px, in transferring, ready and with the question.
    for(const lang of["de","en"]){
      await setLanguage(page,lang);
      for(const width of[320,360,390,480,1280]){
        const label=`p ${lang} ${width}`;
        console.log("export: "+label);
        await page.setViewportSize({width,height:width===1280?900:780});
        await open(lang);
        const audit=async (what,selector)=>{
          const found=await page.evaluate(selector=>{
            const frame=document.querySelector("dialog.export-sheet").getBoundingClientRect(),problems=[];
            const inView=rect=>rect.left>=-1&&rect.right<=innerWidth+1&&rect.top>=-1&&rect.bottom<=innerHeight+1;
            for(const node of document.querySelectorAll(selector)){node.scrollIntoView({block:"nearest"});const rect=node.getBoundingClientRect();
              if(rect.width<48||rect.height<48)problems.push(`${node.textContent.trim()} ${Math.round(rect.width)}x${Math.round(rect.height)}`);
              if(!inView(rect))problems.push(`${node.textContent.trim()} outside the viewport`);
              if(selector.startsWith(".export-flow")&&(rect.top<frame.top-1||rect.bottom>frame.bottom+1))problems.push(`${node.textContent.trim()} outside the sheet`);}
            const phase=document.querySelector(".export-flow-phase");
            return{problems,scroll:document.documentElement.scrollWidth<=innerWidth,phase:!phase||phase.scrollWidth<=phase.clientWidth+1,count:document.querySelectorAll(selector).length};
          },selector);
          assert(found.count>0,`${label} ${what}: buttons found`);
          assert(found.scroll,`${label} ${what}: no sideways page scroll`);
          assert(found.phase,`${label} ${what}: the phase text is not cut off`);
          assert.deepStrictEqual(found.problems,[],`${label} ${what}: buttons`);
        };
        await toTransferring(1);
        await audit("transferring",".export-flow .btn");
        await assertSheetLayout(page,label+" transferring");
        await closeWays.button(lang);await question.waitFor();
        await audit("question","dialog.export-close-question .btn");
        const overflow=await page.evaluate(()=>{const dialog=document.querySelector("dialog.export-close-question");return[...dialog.querySelectorAll("*")].filter(node=>node.clientWidth>0&&node.scrollWidth>node.clientWidth+1).map(node=>node.tagName);});
        assert.deepStrictEqual(overflow,[],label+" question: nothing spills sideways");
        await question.getByRole("button",{name:dict(lang)["ui.export_flow_close_stay"],exact:true}).click();
        await page.waitForSelector(QUESTION,{state:"detached"});
        await complete();
        await audit("ready",".export-flow .btn");
        await assertSheetLayout(page,label+" ready");
      }
    }
    await page.setViewportSize({width:360,height:800});
    assert.deepStrictEqual(errors,[],"page errors");
    await browser.close();
    console.log("export-browser=ok");return;
  }
  if(process.argv.includes("--mobile-layout-only")){
    const i18nModule=require(root+"/i18n.js"),de=i18nModule.v2de,shots=process.env.RTK_SCREENSHOT_DIR;
    if(shots)fs.mkdirSync(shots,{recursive:true});
    const sizes=[...MOBILE_WIDTHS.map(width=>({width,height:780})),...LANDSCAPE,{width:1280,height:900}];
    const sizeLabel=size=>`${size.width}x${size.height}`;
    // The detector must fail on clipped content before its verdicts mean anything.
    await page.setViewportSize({width:320,height:780});
    await go(page,"projects",".screen-toolbar");
    await assertDetectorCatchesClipping(page,".screen-toolbar","detector projects 320");
    for(const size of[{width:320,height:780},{width:1280,height:900}]){
      await page.setViewportSize(size);
      await go(page,"projects/p1",".map-panel");
      await assertDetectorCatchesClipping(page,".map-panel","detector panel "+sizeLabel(size));
    }
    // #43: the empty, inactive project p2 keeps a readable card next to its delete button.
    tracking.projects[1].name="Empty project with a fairly long name for wrapping checks";
    // #43: archived projects are cards with the information above their own actions.
    tracking.archives=mobileArchives;
    for(const lang of["en","de"]){
      await setLanguage(page,lang);
      for(const size of sizes){
        const label=`${lang} ${sizeLabel(size)}`;
        await page.setViewportSize(size);
        await go(page,"projects",".project-list");
        await page.waitForSelector("a.archive-entry");
        assertLayout(await layoutAudit(page),"projects "+label);
        assert.strictEqual(await page.locator(".archive-library").count(),0,"projects "+label+": the archive cards live on #archive");
        await assertLibraryEntryAndDataManagement(page,i18nModule,de,lang,"projects "+label);
        await assertEmptyProjectCard(page,"empty project "+label);
        if(lang==="de")assert.strictEqual(await page.locator(".project-card-wrap>.btn").innerText(),de["Delete empty project"],"empty project "+label+" button text");
        await assertArchiveRoute(page,i18nModule,lang,size,"archives "+label);
        await go(page,"archive",".archive-library");
        if(lang==="de")assert.strictEqual(await page.locator(".archive-card").first().locator("a.btn").innerText(),de["Download"],"archives "+label+" download text");
        if(shots&&size.width===320&&size.height===780)await page.locator(".archive-library").screenshot({path:`${shots}/archive-${lang}-320.png`});
      }
    }
    // #24-UI E10: the header sign-out button fits next to title and pill at every size and in both languages.
    for(const lang of["en","de"]){
      await setLanguage(page,lang);
      for(const size of sizes){
        await page.setViewportSize(size);
        await go(page,"device/display",".settings-layout");
        await assertHeaderSignOut(page,`display ${lang} ${sizeLabel(size)}`);
        await go(page,"projects",".project-list");
        await assertHeaderSignOut(page,`projects ${lang} ${sizeLabel(size)}`);
      }
    }
    // High contrast: 2 px frames must not push the 320 px layout over the edge.
    await page.evaluate(()=>{localStorage.setItem("rtk-theme","high-contrast");location.hash="status";});
    await page.reload();
    await page.waitForSelector(".readiness-hero");
    await page.setViewportSize({width:320,height:780});
    await go(page,"projects",".project-list");
    await page.waitForSelector("a.archive-entry");
    assertLayout(await layoutAudit(page),"projects high contrast de 320");
    assert.strictEqual(await page.locator("a.archive-entry").evaluate(node=>getComputedStyle(node).borderTopWidth),"2px","archive entry high-contrast frame");
    await go(page,"archive",".archive-library");
    assertLayout(await layoutAudit(page),"archive high contrast de 320");
    await assertArchiveCards(page,"archives high contrast de 320");
    assert.strictEqual(await page.locator(".archive-card").first().evaluate(card=>getComputedStyle(card).borderTopWidth),"2px","archive card high-contrast frame");
    await page.evaluate(()=>localStorage.removeItem("rtk-theme"));
    // #24-UI Task 7: without archives the library has no entry and #archive says so.
    tracking.archives=[];
    for(const lang of["en","de"]){
      await setLanguage(page,lang);
      for(const size of[{width:320,height:780},{width:1280,height:900}]){
        await page.setViewportSize(size);
        await go(page,"projects",".project-list");
        assert.strictEqual(await page.locator("a.archive-entry").count(),0,`no archives ${lang} ${sizeLabel(size)}: no entry`);
        await go(page,"archive",".empty-state");
        assert.strictEqual(await page.locator("#app-main .empty-state").innerText(),i18nModule.dictionaries[lang]["ui.archive_empty"],`no archives ${lang} ${sizeLabel(size)}: empty state`);
        assert.strictEqual(await page.locator(".archive-library").count(),0);
        assert.strictEqual(await page.locator(".text-link[href='/#projects']").count(),1,`no archives ${lang} ${sizeLabel(size)}: way back`);
        assertLayout(await layoutAudit(page),`no archives ${lang} ${sizeLabel(size)}`);
      }
    }
    tracking.archives=mobileArchives;
    // #44: project view without sideways overflow, long names wrap (lines, points, the project itself).
    tracking.projects.push(largeProject);
    tracking.projects[0].name=LONG_UNBROKEN;
    mapLineName=LONG_UNBROKEN;
    for(const lang of["en","de"]){
      await setLanguage(page,lang);
      for(const size of sizes){
        const label=`${lang} ${sizeLabel(size)}`;
        await page.setViewportSize(size);
        await go(page,"projects",".project-list");
        assertLayout(await layoutAudit(page),"projects with long names "+label);
        for(const id of["p1","p2","p3"])await assertProjectView(page,id,`project ${id} ${label}`);
        await go(page,"projects/p1",".map-workspace");
        const mapView=await assertMapMenu(page,`map menu ${label}`);
        if(size.width===360&&size.height===780)assert(mapView>MAP_VIEW_BEFORE_360,`map view at 360x780: ${mapView} px, before ${MAP_VIEW_BEFORE_360} px`);
        if(shots&&size.width===320&&size.height===780){
          await go(page,"projects/p3",".map-workspace");
          await page.screenshot({path:`${shots}/project-p3-${lang}-320.png`,fullPage:true});
        }
        await go(page,"projects/p1",".map-workspace");
        await assertDetailsOpen(page,LONG_UNBROKEN,null,`project p1 ${label}`);
        await go(page,"projects/p3",".map-workspace");
        await assertDetailsOpen(page,LONG_UNBROKEN,"lb0",`project p3 ${label}`);
        if(size.width===320||size.width===640||size.width===844){
          await go(page,"projects/p1",".map-workspace");
          await assertMapPanStaysInside(page,`map p1 ${label}`);
        }
      }
    }
    await browser.close();
    console.log("mobile-layout-browser=ok");return;
  }
  if(process.argv.includes("--frame-assignment-only")){
    const errors=[];page.on("pageerror",error=>errors.push(error.message));
    const i18n=require(root+"/i18n.js"),dictionaries=i18n.dictionaries,v2de=i18n.v2de;
    frameMock.on=true;
    const sizes=[...MOBILE_WIDTHS,1280].map(width=>({width,height:width===1280?900:780}));
    const notice=page.locator('[data-notice="frames"]'),row=page.locator("[data-frame-assignment]"),assignButton=page.locator('[data-action="assign-frame"]'),
      sheet=page.locator("dialog[open]"),failure=page.locator(".feedback-dock .feedback-item.critical");
    const box=async locator=>locator.evaluate(node=>{const r=node.getBoundingClientRect();return{width:r.width,height:r.height};});
    const assertTouchSize=async(locator,label)=>{const size=await box(locator);assert(size.width>=48&&size.height>=48,`${label}: ${size.width}x${size.height}`);};
    const openSheet=async()=>{await assignButton.click();await sheet.waitFor();};
    const submitSheet=()=>page.locator("dialog[open] button[type=submit]").click();
    const expectKind=async(kind,label)=>{await page.waitForSelector(`[data-notice="frames"][data-kind="${kind}"]`);assert.strictEqual(await notice.count(),1,label+": one frame notice");};
    const expectNoRow=async label=>{await row.waitFor({state:"detached"});assert.strictEqual(await row.count(),0,label+": no assignment row");};
    const revokeViaButton=async(label)=>{
      const before=frameMock.posts.length;
      await row.locator("button").click();await sheet.waitFor();
      await submitSheet();await page.waitForSelector("dialog[open]",{state:"detached"});
      // The dialog closes before the request leaves: wait for the POST itself.
      for(let waited=0;frameMock.posts.length===before&&waited<5000;waited+=50)await page.waitForTimeout(50);
      assert.deepStrictEqual(frameMock.posts.slice(before),[{action:"revoke_reference_frame",project_id:"p1",assignment_token:FRAME_ASSIGNMENT_TOKEN}],label+": revoke body");
    };
    for(const lang of["en","de"]){
      const dictionary=dictionaries[lang],unknownText=dictionary["ui.frame_unknown_warning"],unknownKey=lang==="de"?"unbekannt":"unknown",
        translate=text=>lang==="de"&&v2de[text]?v2de[text]:text;
      assert(unknownText,lang+": ui.frame_unknown_warning exists");
      await setLanguage(page,lang);
      for(const size of sizes){
        const label=`${lang} ${size.width}`;
        frameReset();
        await page.setViewportSize(size);
        // 1. The unknown-frame notice with the assign button.
        await go(page,"projects/p1",".map-workspace");
        await expectKind("unknown",label+" step 1");
        assert.strictEqual((await notice.innerText()).trim(),unknownText,label+": notice text");
        await assertTouchSize(assignButton,label+" assign button");
        assertLayout(await layoutAudit(page),label+" step 1");
        // 2. The dialog: pre-checked description, required frame, exact request body.
        await openSheet();
        const description=await page.locator("dialog[open] p.muted").innerText();
        for(const part of["3397","134",mapLineName])assert(description.includes(part),`${label}: description lacks ${part}: ${description}`);
        await assertSheetLayout(page,label+" step 2 dialog");
        await submitSheet();
        assert.strictEqual(await sheet.count(),1,label+": a missing frame keeps the dialog open");
        assert(await page.locator("dialog[open] input[name=reference_frame]").evaluate(input=>input.validity.valueMissing),label+": the frame is required");
        assert.strictEqual(frameMock.posts.filter(body=>body.action==="assign_reference_frame").length,0,label+": nothing assigned yet");
        await page.locator("dialog[open] input[name=reference_frame]").fill(FRAME_NAME);
        await submitSheet();
        await page.waitForSelector("dialog[open]",{state:"detached"});
        // 3. The mix notice and the assignment row (the dialog closes before the request leaves, so the body is read once the row is there).
        await expectKind("mix",label+" step 3");
        await row.waitFor();
        assert.deepStrictEqual(frameMock.posts.filter(body=>body.action==="assign_reference_frame"),
          [{action:"assign_reference_frame",project_id:"p1",reference_frame:FRAME_NAME,reference_frame_epoch:"",expected_count:3397,scope_token:FRAME_TOKEN}],label+": assign body");
        assert.strictEqual((await notice.innerText()).trim().endsWith(`${FRAME_NAME} 3397 · ${unknownKey} 134`),true,`${label}: mix tally "${await notice.innerText()}"`);
        const rowText=await row.innerText();
        assert(rowText.includes(FRAME_NAME)&&rowText.includes("3397"),`${label}: row "${rowText}"`);
        await assertTouchSize(row.locator("button"),label+" revoke button");
        assertLayout(await layoutAudit(page),label+" step 3");
        // 4. Undo: confirmed, exact body, unknown again and no row.
        await revokeViaButton(label+" step 4");
        await expectKind("unknown",label+" step 4");
        await expectNoRow(label+" step 4");
        assertLayout(await layoutAudit(page),label+" step 4");
        // 5. A 409 leaves the project as it was, shows the message in the chosen language and the next preview is the new one.
        frameMock.conflictNext=true;
        await openSheet();
        await page.locator("dialog[open] input[name=reference_frame]").fill(FRAME_NAME);
        await submitSheet();
        await failure.waitFor();
        // Since #21c an action failure is a critical entry of the feedback dock, not a toast.
        assert.strictEqual(await failure.locator(".feedback-text").innerText(),translate("The points of this project changed during the check; review the assignment again."),label+": conflict message");
        assert.strictEqual(await sheet.count(),0,label+": dialog closed after the conflict");
        await expectKind("unknown",label+" step 5");
        assert.strictEqual(await row.count(),0,label+": the conflict changed nothing");
        await failure.locator("[data-feedback-dismiss]").click();
        await openSheet();
        assert((await page.locator("dialog[open] p.muted").innerText()).includes("3398"),label+": reopened dialog shows the new preview");
        await page.locator("dialog[open] .sheet-actions .btn").first().click();
        await page.waitForSelector("dialog[open]",{state:"detached"});
        // 6. Live (B6): an assignment made elsewhere, a smaller count, then none.
        const external=count=>frameSetLive(frameSummary(count,{reference_frame:FRAME_NAME,reference_frame_epoch:""}));
        external(3397);
        await row.waitFor();
        assert((await row.innerText()).includes("3397"),label+": live assignment shows without a reload");
        external(3396);
        await page.waitForFunction(()=>{const node=document.querySelector("[data-frame-assignment]");return node&&node.innerText.includes("3396")&&!node.innerText.includes("3397");});
        frameSetLive(null);
        await expectNoRow(label+" step 6");
        await expectKind("unknown",label+" step 6");
        assertLayout(await layoutAudit(page),label+" step 6");
        // 7. The library shows the notice without a button.
        await go(page,"projects",".project-list");
        await expectKind("unknown",label+" step 7");
        assert.strictEqual(await assignButton.count(),0,label+": no assign button in the library");
        assert.strictEqual(await row.count(),0,label+": no assignment row in the library");
        assertLayout(await layoutAudit(page),label+" step 7");
      }
    }
    // 8. The shown project p1 is not the active one: the summary is fetched through /api/tracking, throttled to one fetch per 10 s.
    // Real waiting: the throttle and the observation window are wall-clock; page.clock would not move the page's own timers coherently here.
    await setLanguage(page,"en");
    await page.setViewportSize({width:390,height:780});
    frameReset();frameSetOtherActive();
    await go(page,"projects/p1",".map-workspace");
    await expectKind("unknown","other-active step 8");
    assert.strictEqual(await row.count(),0,"other-active: no row yet");
    const started=Date.now(),elapsed=()=>Date.now()-started,WITHIN_MS=12000;
    frameMock.trackingGets=0;
    frameSetProject(frameSummary(3397,{reference_frame:FRAME_NAME,reference_frame_epoch:""}));
    await row.waitFor({timeout:WITHIN_MS});
    assert((await row.innerText()).includes("3397"),"other-active: row shows the count");
    assert(elapsed()<=WITHIN_MS,"other-active: row appeared within 12 s, took "+elapsed());
    assert.strictEqual(await assignButton.count(),1,"other-active: the button is shown for the displayed project");
    frameSetProject(null);
    await row.waitFor({state:"detached",timeout:WITHIN_MS+2000});
    await expectKind("unknown","other-active step 8 after null");
    assertLayout(await layoutAudit(page),"other-active");
    await page.waitForTimeout(Math.max(0,30000-elapsed()));
    assert(frameMock.trackingGets>=1&&frameMock.trackingGets<=4,"other-active: /api/tracking fetches in 30 s: "+frameMock.trackingGets);
    frameSetProject(frameSummary(3397,{reference_frame:FRAME_NAME,reference_frame_epoch:""}));
    await row.waitFor({timeout:WITHIN_MS+2000});
    await revokeViaButton("other-active");
    await expectKind("unknown","other-active after revoke");
    await expectNoRow("other-active after revoke");
    assert.deepStrictEqual(errors,[]);
    await browser.close();
    console.log("frame-assignment-browser=ok");return;
  }
  if(process.argv.includes("--status-gate-only")){
    const errors=[];page.on("pageerror",error=>errors.push(error.message));
    const dictionaries=require(root+"/i18n.js").dictionaries,en=dictionaries.en,de=dictionaries.de;
    const fill=(template,values)=>template.replace(/\{(\w+)\}/g,(_,key)=>values[key]);
    const snapshot=structuredClone(live),savedLimits=live.current_quality.limits;
    const baseFix={qual:1,fix_status_text:"GPS_FIX",sats:7,hdop:2.25,lat:50,lon:8,alt:100,correction_age_sec:12,fix_seq:3,stale:false,
      receiver_accuracy:{horizontal_sigma_m:0.0512,altitude_sigma_m:0.0834}};
    const baseRtcm={mount:"TEST",last_msg_age_sec:0.2,arrival_age_sec:0.2,epoch_age_sec:1,epoch_source:"GPS",stale:false};
    const gateLimits={fix_quality:"RTK_FIXED",min_satellites:10,max_hdop:1.5,max_correction_age_sec:5,max_gst_horizontal_sigma_m:0.035,
      max_gst_vertical_sigma_m:0.04,max_horizontal_spread_m:0.03};
    const rowOf={rtk_fixed_required:"fix",required_fix_not_met:"fix",not_all_samples_rtk_fixed:"fix",corrections_too_old:"corrections",
      gst_horizontal_error_too_high:"accuracy",gst_vertical_error_too_high:"accuracy",horizontal_spread_too_high:"accuracy",
      too_few_satellites:"satellites",hdop_too_high:"satellites",gst_required:"gst",not_all_samples_have_gst:"gst",
      no_position:"position",altitude_required:"position"};
    const params={rtk_fixed_required:{fix:"GPS FIX"},required_fix_not_met:{fix:"GPS FIX",required:"RTK FIXED"},too_few_satellites:{count:"7",limit:"10"},
      hdop_too_high:{value:"2.25",limit:"1.50"},corrections_too_old:{age:"12.0",limit:"5.0"},gst_required:{attempt:"3"},
      gst_horizontal_error_too_high:{value:"0.051",limit:"0.035"},gst_vertical_error_too_high:{value:"0.083",limit:"0.040"}};
    const expectedText=code=>fill(en["gate."+code],params[code]||{});
    const setGate=(reasons,fixPatch={},extra={})=>{
      live.current_fix={...baseFix,...fixPatch};live.current_quality.accepted=reasons.length===0;setVerdict(reasons);
      Object.assign(live,{gnss_accuracy:{state:"requested",attempts:3},rtcm:{...baseRtcm},network:{ntrip_state:"streaming"}},extra);
    };
    const nextPoll=async()=>{await page.waitForResponse(response=>response.url().endsWith("/api/ui/live"));await page.waitForTimeout(300);};
    const subtitle=()=>page.locator(".hero-state p").innerText();
    const waitSubtitle=text=>page.waitForFunction(expected=>document.querySelector(".hero-state p").textContent===expected,text);
    const waitHero=cls=>page.waitForFunction(expected=>document.querySelector(".readiness-hero").classList.contains(expected),cls);
    const heroHas=cls=>page.locator(".readiness-hero").evaluate((node,expected)=>node.classList.contains(expected),cls);
    const failing=()=>page.locator(".condition.fail").evaluateAll(nodes=>nodes.map(node=>node.dataset.gate));
    const gate=id=>page.locator(`.condition[data-gate="${id}"]`);
    // Worst case measured for the German hero: 373 px (320 px wide, recording, longest action sentence). The bound is
    // specific to Chromium and its fonts: a regression guard against unnoticed growth, not a compactness goal.
    // #21b: with the data age line (margin-bottom:0 on .hero-freshness) the worst case measures about 393 px (320 px, recording, DE), within the E17 cap of 400 px.
    const HERO_HEIGHT_BOUND=400;
    try{
      await page.evaluate(()=>{localStorage.setItem("rtk-language","en");});
      live.current_quality.limits=gateLimits;live.active_line=null;
      setGate([]);
      await page.reload();
      await page.waitForSelector(".condition-list .condition");
      // 1. Every server code: hero blocked, exactly one red row (E2), subtitle = row reason = the action sentence.
      for(const code of Object.keys(rowOf)){
        setGate([code]);
        await waitSubtitle(expectedText(code));
        assert(await heroHas("blocked"),`${code}: hero blocked`);
        assert.deepStrictEqual(await failing(),[rowOf[code]],`${code}: one red row`);
        assert.strictEqual(await page.locator(".condition.fail .condition-reason").innerText(),expectedText(code),`${code}: row reason`);
        assert.strictEqual(await page.locator(".condition-list > .condition").count(),6,`${code}: six rows`);
      }
      // 2. No reasons: hero ready, six green rows, no collapsed group.
      setGate([]);
      await waitHero("ready");
      assert.strictEqual(await page.locator(".condition.pass").count(),6);
      assert.strictEqual(await page.locator(".condition.fail").count(),0);
      assert.strictEqual(await page.locator("details.passed-conditions").count(),0);
      // 3. Two reasons: the hero names the first one; a requested GST names the attempt and carries no diagnostic.
      setGate(["hdop_too_high","gst_required"]);
      await waitSubtitle(expectedText("hdop_too_high"));
      assert.deepStrictEqual(await failing(),["satellites","gst"]);
      assert.strictEqual(await gate("gst").locator(".condition-reason").innerText(),"Receiver sends no accuracy estimate (GST). Device is requesting it (attempt 3).");
      assert.strictEqual(await gate("gst").locator(".condition-diagnostic").count(),0);
      // 4. A dead link or a missing caster is a diagnostic only: the gate stays open.
      setGate([],{},{rtcm:{...baseRtcm,epoch_age_sec:60,stale:true}});
      await page.waitForFunction(()=>document.querySelector('.condition[data-gate="corrections"] .condition-diagnostic.fail'));
      assert(await heroHas("ready"));
      assert.strictEqual(await gate("corrections").getAttribute("class"),"condition pass");
      assert.strictEqual(await page.locator(".condition.fail").count(),0);
      // The diagnostic names its state in words (the mark is decorative): visually hidden, but in the accessibility text.
      const stateWord=gate("corrections").locator(".condition-diagnostic .visually-hidden");
      assert.strictEqual(await stateWord.textContent(),en["ui.diagnostic_fail"]);
      assert.strictEqual(await stateWord.evaluate(node=>node.getBoundingClientRect().width<=1),true,"state word is visually hidden");
      assert.strictEqual(await gate("corrections").locator(".diagnostic-mark").getAttribute("aria-hidden"),"true");
      setGate([],{},{network:{ntrip_state:"disabled"}});
      await page.waitForFunction(text=>document.querySelector('.condition[data-gate="corrections"] .condition-diagnostic').textContent.includes(text),en["ui.corrections_unavailable"]);
      assert.strictEqual(await page.locator(".condition-list > .condition").count(),6);
      assert.strictEqual(await gate("corrections").locator(".condition-diagnostic").getAttribute("data-diagnostic"),"link");
      assert(await heroHas("ready"));
      // 5. A code from a newer firmware: seventh row, hero blocked, no script error.
      setGate(["future_code"]);
      await waitSubtitle(fill(en["gate.unknown"],{code:"future_code"}));
      assert.strictEqual(await page.locator(".condition-list > .condition").count(),7);
      const other=page.locator(".condition-list > .condition").last();
      assert.strictEqual(await other.getAttribute("data-gate"),"other");
      assert((await other.getAttribute("class")).endsWith("fail"));
      assert((await other.innerText()).includes("future_code"));
      assert(await heroHas("blocked"));
      // 6. GST cycle between polls (GGA before GST): the checklist keeps its DOM, a real change renders.
      const own={horizontal_sigma_m:0.031,altitude_sigma_m:0.047};
      const withOwn=()=>setGate([],{receiver_accuracy:own,pending_gst:false});
      const pendingOnly=()=>setGate([],{receiver_accuracy:undefined,pending_gst:true,held_receiver_accuracy:own});
      withOwn();
      await page.waitForFunction(()=>document.querySelector('.condition[data-gate="accuracy"]').textContent.includes("H 0.031 m"));
      await page.evaluate(()=>{window.listChanges=0;new MutationObserver(records=>{window.listChanges+=records.length;}).observe(document.querySelector(".condition-list"),{childList:true,subtree:true,characterData:true});});
      await page.locator("details.status-diagnostics").evaluate(node=>{node.open=true;});
      for(let cycle=0;cycle<3;cycle++){
        pendingOnly();
        await page.waitForFunction(text=>document.querySelector(".status-detail-rows").textContent.includes(text),en["ui.gst_pending"]);
        withOwn();
        await page.waitForFunction(text=>!document.querySelector(".status-detail-rows").textContent.includes(text),en["ui.gst_pending"]);
      }
      assert.strictEqual(await page.evaluate(()=>window.listChanges),0,"the pending GST cycle must not touch the checklist");
      assert((await gate("accuracy").innerText()).includes("H 0.031 m"));
      setGate([],{receiver_accuracy:{horizontal_sigma_m:0.033,altitude_sigma_m:0.047}});
      await page.waitForFunction(()=>document.querySelector('.condition[data-gate="accuracy"]').textContent.includes("H 0.033 m"));
      assert(await page.evaluate(()=>window.listChanges)>0,"a real value change renders");
      // 6a. Language switch without reload and without a value change.
      withOwn();await nextPoll();
      await page.evaluate(()=>RtkI18n.setLanguage("de"));
      await page.waitForFunction(text=>document.querySelector('.condition[data-gate="corrections"] b').textContent===text,de["ui.gate_row_corrections"]);
      assert.deepStrictEqual(await page.locator(".condition > span > b").allInnerTexts(),
        ["fix","corrections","accuracy","satellites","gst","position"].map(id=>de["ui.gate_row_"+id]));
      assert((await gate("accuracy").innerText()).includes("H 0,031 m"),"values are unchanged, only formatted for German");
      assert.strictEqual(await gate("corrections").locator(".condition-diagnostic .visually-hidden").textContent(),de["ui.diagnostic_ok"]);
      await page.evaluate(()=>RtkI18n.setLanguage("en"));
      await page.waitForFunction(text=>document.querySelector('.condition[data-gate="corrections"] b').textContent===text,en["ui.gate_row_corrections"]);
      // 6b. Contradicting verdict (accepted:true with a reason): the rows decide for hero, hint and actions.
      rawVerdict=true;setGate(["hdop_too_high"]);live.current_quality.accepted=true;
      await waitHero("blocked");
      assert.strictEqual(await subtitle(),expectedText("hdop_too_high"));
      assert.strictEqual(await page.locator(".control-help").evaluate(node=>node.hidden),true);
      assert.strictEqual(await page.getByRole("button",{name:"Object point",exact:true}).isDisabled(),true);
      // "Start line" is not asserted: the route-only rule keeps it enabled with a usable fix, and the Start-line hint still
      // follows the raw accepted flag (E4 residual of a contradicting verdict).
      clearVerdict();
      // 6c. Gate loss while recording or paused: the reason heads the hero, the recording facts stay visible.
      for(const [state,title] of [["recording","Recording"],["paused","Recording paused"]]){
        live.active_line={id:"l2",name:"Field route",state,recording_mode:"route",recording_sec:125,vertex_count:12,length_m:5};
        setGate([]);
        await waitHero("ready");
        await page.waitForFunction(expected=>document.querySelector(".hero-state h2").textContent===expected,title);
        assert((await subtitle()).includes("Field route"),`${state}: open gate keeps the line facts in the subtitle`);
        assert.strictEqual(await page.locator(".hero-activity").isHidden(),true,`${state}: no activity row while the gate is open`);
        setGate(["corrections_too_old"]);
        await waitSubtitle(expectedText("corrections_too_old"));
        assert.strictEqual(await page.locator(".hero-state h2").innerText(),title);
        assert.strictEqual(await page.locator(".hero-activity").isVisible(),true);
        assert((await page.locator(".hero-activity").innerText()).includes("Field route"));
        setGate([]);
        await waitHero("ready");
        await page.waitForFunction(()=>document.querySelector(".hero-state p").textContent.includes("Field route"));
        assert.strictEqual(await page.locator(".hero-activity").isHidden(),true,`${state}: the activity row hides again`);
      }
      live.active_line=null;
      // 7. Never collapsed: three red reasons leave all six rows as direct, visible children.
      setGate(["rtk_fixed_required","hdop_too_high","gst_required"]);
      await waitSubtitle(expectedText("rtk_fixed_required"));
      const rows=page.locator(".condition-list > .condition");
      assert.strictEqual(await rows.count(),6);
      for(let index=0;index<6;index++)assert.strictEqual(await rows.nth(index).isVisible(),true,`row ${index} visible`);
      assert.strictEqual(await page.locator("details.passed-conditions").count(),0);
      // 8. Widths and languages: six readable rows, no clipped text, no horizontal scroll.
      setGate(["corrections_too_old","gst_required"]);
      for(const language of ["en","de"]){
        await page.evaluate(value=>localStorage.setItem("rtk-language",value),language);
        for(const width of [320,360,390,480,1280]){
          await page.setViewportSize({width,height:900});
          await page.reload();
          const expected=language==="de"?fill(de["gate.corrections_too_old"],{age:"12,0",limit:"5,0"}):expectedText("corrections_too_old");
          await waitSubtitle(expected);
          const list=page.locator(".condition-list > .condition");
          assert.strictEqual(await list.count(),6,`${language} ${width}: six rows`);
          for(let index=0;index<6;index++)assert.strictEqual(await list.nth(index).isVisible(),true,`${language} ${width}: row ${index} visible`);
          assert.strictEqual(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true,`${language} ${width}: horizontal scroll`);
          assert.deepStrictEqual(await page.locator(".condition span:not(.visually-hidden)").evaluateAll(nodes=>nodes.filter(node=>node.scrollWidth>node.clientWidth).length),0,`${language} ${width}: clipped row text`);
        }
      }
      // 9. Measure screen order (reading order from the DOM everywhere, geometry per layout), actions always reachable.
      // The last point card shows only with a recorded point: it is part of the chains only while it is visible.
      const order=[".status-project",".readiness-hero",".status-setup",".status-last",".field-actions",".conditions",".field-context",".recording-capacity",".status-diagnostics"];
      const lineFacts={id:"l2",name:"Field route",state:"recording",recording_mode:"route",recording_sec:125,vertex_count:12,length_m:5,
        last_point:{label:"Field route · V-12",age_sec:4,h_sigma_m:0.012}};
      const rect=selector=>page.locator(selector).first().evaluate(node=>{const box=node.getBoundingClientRect();return {x:box.x,y:box.y,top:box.top+scrollY,width:box.width,height:box.height,bottom:box.bottom};});
      const overlaps=(a,b)=>a.x<b.x+b.width&&b.x<a.x+a.width&&a.y<b.y+b.height&&b.y<a.y+a.height;
      const barInView=async(context,mobile)=>{
        const bar=await rect(".field-actions");
        assert(bar.y>=0&&bar.y+bar.height<=await page.evaluate(()=>innerHeight),`${context}: action bar outside the viewport (${bar.y}..${bar.y+bar.height})`);
        if(mobile)assert(!overlaps(bar,await rect(".mobile-nav")),`${context}: action bar under the navigation`);
      };
      for(const language of ["en","de"]){
        await page.evaluate(value=>localStorage.setItem("rtk-language",value),language);
        for(const withLine of [true,false]){
          live.active_line=withLine?lineFacts:null;
          setGate(["hdop_too_high"]);
          for(const width of [320,360,390,480,1280]){
            const context=`${language} ${width} ${withLine?"line":"no line"}`,mobile=width<900;
            await page.setViewportSize({width,height:800});
            await page.reload();
            await page.waitForSelector(".status-project");
            await waitSubtitle(language==="de"?fill(de["gate.hdop_too_high"],{value:"2,25",limit:"1,50"}):expectedText("hdop_too_high"));
            assert.strictEqual(await page.evaluate(selectors=>{const nodes=selectors.map(selector=>document.querySelector(selector));
              return nodes.every((node,index)=>node&&(!index||(nodes[index-1].compareDocumentPosition(node)&Node.DOCUMENT_POSITION_FOLLOWING)>0));},order),true,`${context}: DOM reading order`);
            assert.strictEqual(await page.locator(".field-actions button").count(),withLine?5:3,`${context}: actions`);
            assert((await page.locator(".status-project b").innerText()).startsWith(`${live.active_project.name} \u00b7`),`${context}: project name`);
            const boxes={};for(const selector of order)boxes[selector]=await rect(selector);
            assert.strictEqual(boxes[".status-last"].height>0,withLine,`${context}: last point card only with a recorded point`);
            const shown=order.filter(selector=>boxes[selector].height>0);
            if(mobile){
              const chain=shown.filter(selector=>selector!==".field-actions");
              for(let index=1;index<chain.length;index++)assert(boxes[chain[index-1]].top<boxes[chain[index]].top,`${context}: ${chain[index-1]} must start above ${chain[index]}`);
            }else{
              // The sticky bar may cover the lower cards at scroll 0 (as on phones); scrolled down it sits below them.
              const left=[".status-project",".readiness-hero",".status-setup",".status-last"].filter(selector=>shown.includes(selector)),right=[".conditions",".field-context",".recording-capacity",".status-diagnostics"];
              for(let index=1;index<left.length;index++)assert(boxes[left[index-1]].top<boxes[left[index]].top,`${context}: ${left[index-1]} above ${left[index]}`);
              for(const selector of left)assert(boxes[selector].x+boxes[selector].width<=boxes[".conditions"].x+0.5,`${context}: ${selector} left of the checklist`);
              for(let index=1;index<right.length;index++)assert(boxes[right[index-1]].top<boxes[right[index]].top,`${context}: ${right[index-1]} above ${right[index]}`);
              for(const selector of right)assert(boxes[selector].x>=boxes[".status-setup"].x+boxes[".status-setup"].width-0.5,`${context}: ${selector} right of the setup card`);
            }
            await page.evaluate(()=>scrollTo(0,0));
            await barInView(`${context} scroll 0`,mobile);
            await page.evaluate(()=>scrollTo(0,document.body.scrollHeight));
            await barInView(`${context} scrolled down`,mobile);
            if(!mobile){const bar=await rect(".field-actions"),above=await rect(shown.includes(".status-last")?".status-last":".status-setup");
              assert(bar.y>=above.bottom-0.5,`${context}: scrolled down the bar sits below the cards (${bar.y} < ${above.bottom})`);}
            await page.evaluate(()=>scrollTo(0,0));
            assert.strictEqual(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true,`${context}: horizontal scroll`);
            if(language==="en"&&withLine)await page.screenshot({path:`/tmp/esp-rtk-21a-measure-${width}.png`,fullPage:true});
          }
        }
      }
      // Long project name without spaces at 320 px: the text wraps on its own row, both actions stay whole below it.
      const savedName=live.active_project.name;
      live.active_project.name="W".repeat(80);live.active_line=null;
      for(const language of ["en","de"]){
        await page.evaluate(value=>localStorage.setItem("rtk-language",value),language);
        await page.setViewportSize({width:320,height:800});
        await page.reload();
        await page.waitForFunction(name=>document.querySelector(".status-project b").textContent.includes(name),"W".repeat(80));
        const text=await rect(".status-project-text"),button=await rect(".status-project button"),open=await rect(".status-project a.btn");
        assert.strictEqual(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true,`${language}: long name scrolls horizontally`);
        assert.strictEqual(await page.locator(".status-project-text b").evaluate(node=>node.scrollWidth<=node.clientWidth),true,`${language}: long name is clipped`);
        for(const [name,box] of [["switch",button],["open",open]]){
          assert(box.y>=text.y+text.height-0.5,`${language}: ${name} below the text`);
          assert(box.x>=0&&box.x+box.width<=320&&box.width>=48&&box.height>=48,`${language}: ${name} size ${box.width}x${box.height}`);
        }
        assert(!overlaps(button,open),`${language}: actions overlap`);
        assert.strictEqual(await page.locator(".status-project button").innerText(),language==="de"?de["ui.switch_project"]:en["ui.switch_project"]);
        assert.strictEqual(await page.locator(".status-project a.btn").innerText(),language==="de"?de["ui.open_active_project"]:en["ui.open_active_project"]);
        assert.strictEqual(await page.locator(".status-project a.btn").getAttribute("href"),"/#projects/p1");
      }
      live.active_project.name=savedName;
      // Switching and choosing a project.
      await page.evaluate(()=>localStorage.setItem("rtk-language","de"));
      await page.reload();
      await page.getByRole("button",{name:de["ui.switch_project"],exact:true}).click();
      await page.waitForFunction(()=>location.hash==="#projects");
      await page.evaluate(()=>localStorage.setItem("rtk-language","en"));
      await go(page,"status",".readiness-hero");
      // "Open map" only opens the project: the active project on Measure stays.
      const opened=requestLog.length;
      await page.locator(".status-project a.btn").click();
      await page.waitForFunction(()=>location.hash==="#projects/p1");
      await page.waitForSelector(".map-panel");
      assert.strictEqual(requestLog.slice(opened).filter(entry=>entry.method==="POST").length,0,"opening the map sends nothing");
      await go(page,"measure",".readiness-hero");
      const savedProject=live.active_project;live.active_project=null;
      await page.reload();
      await page.waitForFunction(()=>document.querySelector(".status-project a.btn").hidden);
      assert.strictEqual(await page.locator(".status-project a.btn").isHidden(),true,"no project: no map link");
      assert.strictEqual(await page.locator(".status-project b").innerText(),"No project selected");
      assert.strictEqual(await page.locator(".status-project button").innerText(),"Choose project","no project: the switch button chooses");
      await page.locator(".status-project button").click();
      await page.waitForFunction(()=>location.hash==="#projects");
      await go(page,"measure",".readiness-hero");
      assert.strictEqual(await page.locator(".field-actions").getByRole("button",{name:"Choose project",exact:true}).count(),1);
      live.active_project=savedProject;
      // A full point limit explains itself at the action.
      live.active_line=lineFacts;setGate([]);const savedLevel=live.capacity.level;
      try{
        live.capacity.level="full";
        await page.reload();
        await page.waitForFunction(()=>document.querySelector(".field-actions .action-reason"));
        assert.strictEqual(await page.locator(".field-actions .action-reason").isVisible(),true);
        assert.strictEqual(await page.locator(".field-actions .action-reason").innerText(),"Point limit reached. Archive another project to continue this line.");
      }finally{live.capacity.level=savedLevel;live.active_line=null;}
      const savedCapacityForLoss=live.capacity;
      // 11. Data age and connection loss (#21b): widths x languages with a shifted clock, then the overlay over startup and the point limit.
      try{
        live.active_line=null;setGate([]);
        // The connectivity word must stay inside the visible part of the pill detail (F1), also without a recording and with a delayed or lost link.
        const assertPillWord=async(context,link,word)=>{
          const pill=await page.evaluate(()=>{const small=document.querySelector("#live-state small"),node=small.firstChild,text=small.textContent,range=document.createRange();range.setStart(node,0);range.setEnd(node,text.split(" \u00b7 ")[0].length);return{text,link:document.querySelector("#live-state").dataset.link,wordRight:range.getBoundingClientRect().right,smallRight:small.getBoundingClientRect().right,wordWidth:range.getBoundingClientRect().width};});
          assert.strictEqual(pill.link,link,`${context}: pill link state`);
          assert(pill.text.startsWith(word+" \u00b7 ")||pill.text===word,`${context}: pill detail "${pill.text}" starts with "${word}"`);
          assert(pill.wordWidth>0&&pill.wordRight<=pill.smallRight+0.5,`${context}: ${link} word "${word}" is clipped (${pill.wordRight} > ${pill.smallRight})`);
        };
        for(const language of ["en","de"]){
          const dict=language==="de"?de:en,lostPrefix=dict["ui.live_lost_title"].split("{seconds}")[0],tapWord=language==="de"?require(root+"/i18n.js").v2de["Tap for status"]:"Tap for status";
          await page.evaluate(value=>localStorage.setItem("rtk-language",value),language);
          for(const width of [320,360,390,480,1280]){
            const context=`${language} ${width}`;
            await page.setViewportSize({width,height:900});
            await page.reload();
            await page.waitForFunction(()=>document.querySelector(".readiness-hero").classList.contains("ready"));
            blockLive();
            try{
              await shiftClock(4000);
              await page.waitForFunction(()=>{const node=document.querySelector(".hero-freshness.aging");return node&&!node.hidden;},null,{timeout:1500});
              assert(await heroHas("ready"),`${context}: delayed data keeps the hero ready`);
              await assertPillWord(context,"delayed",dict["ui.link_delayed"]);
              await shiftClock(8000);
              await page.waitForFunction(()=>document.querySelector(".readiness-hero").classList.contains("stale-critical"),null,{timeout:1500});
              assert((await page.locator(".readiness-hero h2").innerText()).startsWith(lostPrefix),`${context}: lost title`);
              const reason=page.locator(".field-actions .action-reason");
              assert.strictEqual(await reason.innerText(),dict["ui.live_lost_action"],`${context}: reason`);
              for(const selector of [".readiness-hero h2",".field-actions .action-reason","#live-state b"])
                assert.strictEqual(await page.locator(selector).evaluate(node=>node.scrollWidth<=node.clientWidth),true,`${context}: ${selector} is clipped`);
              assert.strictEqual(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true,`${context}: horizontal scroll`);
              const buttons=page.locator(".field-actions button");
              assert(await buttons.count()>0,`${context}: actions present`);
              for(let index=0;index<await buttons.count();index++){
                assert.strictEqual(await buttons.nth(index).isDisabled(),true,`${context}: action ${index} disabled`);
                assert.strictEqual(await buttons.nth(index).isVisible(),true,`${context}: action ${index} visible`);
              }
              const pill=await page.locator("#live-state small").innerText();
              assert(pill.startsWith(dict["ui.link_lost"])||pill===tapWord,`${context}: pill detail "${pill}"`);
              await assertPillWord(context,"lost",dict["ui.link_lost"]);
              assert((await rect(".readiness-hero")).height<=HERO_HEIGHT_BOUND,`${context}: lost hero height`);
            }finally{releaseLive();}
            await waitHero("ready");
            assert.strictEqual(await page.locator(".conditions").evaluate(node=>node.classList.contains("stale-critical")),false,`${context}: checklist colour returns`);
          }
        }
        // The loss overlay covers the storage and point-limit titles, and the recovery brings them back.
        await page.evaluate(()=>localStorage.setItem("rtk-language","en"));
        await page.setViewportSize({width:360,height:900});
        const title=()=>page.locator(".readiness-hero h2").innerText();
        const overlay=async(expected,context)=>{
          await page.waitForFunction(text=>document.querySelector(".readiness-hero h2").textContent===text,expected);
          blockLive();
          try{
            await shiftClock(12000);
            await page.waitForFunction(()=>/^No status data from the device/.test(document.querySelector(".readiness-hero h2").textContent),null,{timeout:3000});
          }finally{releaseLive();}
          await page.waitForFunction(text=>document.querySelector(".readiness-hero h2").textContent===text,expected);
          assert.strictEqual(await title(),expected,`${context}: title restored`);
        };
        try{
          live.tracking_startup={state:"loading"};
          await page.reload();
          await overlay("Loading survey storage","startup");
        }finally{delete live.tracking_startup;}
        try{
          live.capacity={...savedCapacityForLoss,used:savedCapacityForLoss.limit,remaining:0,percent:100,level:"full"};live.active_line=lineFacts;
          await page.reload();
          await overlay("Point limit reached","point limit");
        }finally{live.capacity=savedCapacityForLoss;live.active_line=null;}
      }finally{
        releaseLive();setGate([]);
        await page.evaluate(()=>localStorage.setItem("rtk-language","en"));
        await page.setViewportSize({width:360,height:800});
        await page.reload();
        await page.waitForSelector(".condition-list .condition");
      }
      // The hero keeps its card size for the longest German action sentence of every reason code (full sentence plus
      // the recording facts line), at the two narrowest phone widths.
      await page.evaluate(()=>localStorage.setItem("rtk-language","de"));
      await page.reload();
      let tallest=0;
      for(const width of [320,360]){
        await page.setViewportSize({width,height:800});
        for(const withLine of [false,true]){
          live.active_line=withLine?lineFacts:null;
          for(const code of Object.keys(rowOf)){
            setGate([code]);
            await waitSubtitle(fill(de["gate."+code],Object.fromEntries(Object.entries(params[code]||{}).map(([key,value])=>[key,value.replace(".",",")]))));
            const height=(await rect(".readiness-hero")).height;
            tallest=Math.max(tallest,height);
            assert(height<=HERO_HEIGHT_BOUND,`${code} at ${width}px${withLine?" while recording":""}: hero is ${height}px high`);
          }
        }
      }
      // 10. #38a reminder: an RTK fix with an empty reference frame shows a notice inside the field setup summary; it
      // hides for the tab session (navigation and reload keep it hidden, a new browsing context shows it again).
      const reminder='.status-setup [data-notice="frame-reminder"]';
      const rtkFix={qual:4,fix_status_text:"RTK_FIXED",correction_age_sec:1,fix_seq:20};
      const deviceDefaults={samples:5,antenna_height_m:1.8,reference_frame:"",reference_frame_epoch:""};
      try{
        live.measurement_defaults={...deviceDefaults};
        for(const [language,width] of [["en",360],["de",360],["en",1280]]){
          const context=`reminder ${language} ${width}`,words=language==="de"?de:en;
          setGate([],rtkFix);
          await page.setViewportSize({width,height:800});
          await page.evaluate(value=>{try{sessionStorage.clear();}catch(_){}localStorage.setItem("rtk-language",value);location.hash="measure";},language);
          await page.reload();
          await page.waitForSelector(".status-setup");
          await nextPoll();
          assert.strictEqual(await page.locator(reminder).count(),1,`${context}: reminder inside the field setup summary`);
          assert.strictEqual(await page.locator(reminder).isVisible(),true,`${context}: reminder visible`);
          assert((await page.locator(reminder).innerText()).includes(words["ui.frame_reminder"]),`${context}: reminder text`);
          assert.strictEqual(await page.locator('.status-setup [data-context="frame"] b').innerText(),words["ui.frame_not_set"],`${context}: frame row`);
          assert.strictEqual(await page.locator('.status-setup [data-context="method"] b').innerText(),fill(words["ui.sample_avg"],{count:"5"}),`${context}: method row`);
          assert.strictEqual(await page.locator('.status-setup [data-context="antenna"] b').innerText(),language==="de"?"1,80 m":"1.80 m",`${context}: antenna row`);
          assert.strictEqual(await page.locator(`.status-setup a.btn[href="/#measure/setup"]`).innerText(),words["ui.setup_summary_edit"],`${context}: edit link`);
          assert.strictEqual(await page.locator(reminder+' a[href="/#measure/setup/frame"]').innerText(),words["ui.frame_reminder_open"],`${context}: open link`);
          const hide=page.locator(reminder).getByRole("button",{name:words["ui.frame_reminder_dismiss"],exact:true});
          for(const target of [hide,page.locator(reminder+" a")]){const box=await target.boundingBox();assert(box.width>=48&&box.height>=48,`${context}: target ${box.width}x${box.height}`);}
          const hero=(await rect(".readiness-hero")).height;
          assert(hero<=HERO_HEIGHT_BOUND,`${context}: hero is ${hero}px high`);
          assert.strictEqual(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true,`${context}: horizontal scroll`);
          await page.locator(".status-setup").screenshot({path:`/tmp/esp-rtk-24ui-reminder-${language}-${width}.png`});
          await hide.click();
          await page.waitForSelector(reminder,{state:"detached"});
          assert.strictEqual(await page.evaluate(()=>document.activeElement&&document.activeElement.getAttribute("href")),"/#measure/setup",`${context}: focus moves to the edit link`);
          await go(page,"projects",".project-list");
          await go(page,"measure",".status-setup");
          await nextPoll();
          assert.strictEqual(await page.locator(reminder).count(),0,`${context}: hidden after navigation`);
          await page.reload();
          await page.waitForSelector(".status-setup");
          await nextPoll();
          assert.strictEqual(await page.locator(reminder).count(),0,`${context}: hidden after a reload`);
        }
        // A new browsing context is a new session: the reminder is back, its link opens the frame input.
        const tab=await browser.newPage({viewport:{width:360,height:800}});
        try{
          await tab.addInitScript(()=>localStorage.setItem("rtk-language","en"));
          await tab.route("http://local.test/**",fixture);
          await tab.goto("http://local.test/#measure");
          await tab.waitForSelector(reminder);
          await tab.locator(reminder).getByRole("link",{name:en["ui.frame_reminder_open"],exact:true}).click();
          await tab.waitForFunction(()=>location.hash==="#measure/setup/frame");
          await tab.waitForFunction(()=>document.activeElement&&document.activeElement.id==="field-setup-reference-frame");
        }finally{await tab.close();}
        // A set frame or a fix without RTK: no reminder.
        await page.evaluate(()=>{try{sessionStorage.clear();}catch(_){}localStorage.setItem("rtk-language","en");});
        await page.reload();
        await page.waitForSelector(reminder);
        live.measurement_defaults={...deviceDefaults,reference_frame:"ETRS89",reference_frame_epoch:"2016.0"};
        await page.waitForSelector(reminder,{state:"detached"});
        assert.strictEqual(await page.locator('.status-setup [data-context="frame"] b').innerText(),"ETRS89, epoch 2016.0");
        live.measurement_defaults={...deviceDefaults};
        await page.waitForSelector(reminder);
        setGate([],{qual:1,fix_status_text:"GPS_FIX",fix_seq:21});
        await page.waitForSelector(reminder,{state:"detached"});
        await nextPoll();
        assert.strictEqual(await page.locator(reminder).count(),0,"GPS fix: no reminder");
      }finally{delete live.measurement_defaults;setGate([]);}
      // 11. #21c help card at 320x800: with the longest German cause it can be scrolled fully above the sticky action bar.
      for(const language of ["en","de"]){
        const context=`help card ${language} 320x800`;
        setGate(["hdop_too_high"],{},{network:{ntrip_state:"backoff",last_ntrip_error_code:"retries_exhausted",ntrip_config:"complete"}});
        await page.setViewportSize({width:320,height:800});
        await page.evaluate(value=>{localStorage.setItem("rtk-language",value);location.hash="measure";},language);
        await page.reload();
        await page.waitForSelector('.measure-help[data-cause="parked"]:not([hidden])');
        const header=await page.locator(".app-header").evaluate(node=>node.getBoundingClientRect().bottom);
        await page.evaluate(top=>{const card=document.querySelector(".measure-help");scrollTo(0,card.getBoundingClientRect().top+scrollY-top-8);},header);
        await page.waitForTimeout(100);
        const card=await rect(".measure-help"),bar=await rect(".field-actions");
        assert(card.y>=header-0.5,`${context}: card top ${card.y} below the header ${header}`);
        assert(card.bottom<=bar.y+0.5,`${context}: card bottom ${card.bottom} above the action bar ${bar.y}`);
      }
      // 12. #21d pill: the dot keeps meaning the data link, the fix label names the measurement gate (not by colour alone).
      const dgpsFix={qual:2,fix_status_text:"DGPS",receiver_accuracy:{horizontal_sigma_m:5.7,altitude_sigma_m:9.1},fix_seq:30};
      const readyFix={qual:4,fix_status_text:"RTK_FIXED",sats:20,hdop:0.6,correction_age_sec:1.2,fix_seq:31,
        receiver_accuracy:{horizontal_sigma_m:0.012,altitude_sigma_m:0.02}};
      const pillState=()=>page.evaluate(()=>{const pill=document.querySelector("#live-state"),label=pill.querySelector("b"),small=pill.querySelector("small");
        return{cls:pill.className,link:pill.dataset.link,gate:label.dataset.gate,label:label.textContent,detail:small.textContent,aria:pill.getAttribute("aria-label"),
          title:pill.title,labelColor:getComputedStyle(label).color,critical:(()=>{const probe=document.createElement("i");probe.style.color="var(--critical)";pill.append(probe);const color=getComputedStyle(probe).color;probe.remove();return color;})(),
          labelClipped:label.scrollWidth>label.clientWidth,pillRight:pill.getBoundingClientRect().right,linkWordRight:(()=>{const node=small.firstChild,range=document.createRange();range.setStart(node,0);range.setEnd(node,small.textContent.split(" · ")[0].length);return range.getBoundingClientRect().right;})(),smallRight:small.getBoundingClientRect().right};});
      const waitPillGate=value=>page.waitForFunction(expected=>document.querySelector("#live-state b").dataset.gate===expected,value);
      live.active_line=null;
      try{
        for(const language of ["en","de"]){
          const dict=language==="de"?de:en;
          await page.evaluate(value=>{localStorage.setItem("rtk-language",value);localStorage.removeItem("rtk-theme");location.hash="measure";},language);
          for(const width of [320,360,390,1280]){
            const context=`pill ${language} ${width}`;
            await page.setViewportSize({width,height:800});
            setGate(["rtk_fixed_required"],dgpsFix);
            await page.reload();
            await waitPillGate("blocked");await waitHero("blocked");
            let pill=await pillState();
            assert(pill.cls.split(" ").includes("ok"),`${context}: fresh link keeps the ok dot (${pill.cls})`);
            assert.strictEqual(pill.link,"live",`${context}: data-link`);
            assert.strictEqual(pill.label,"DGPS",`${context}: label`);
            assert(pill.detail.startsWith(`${dict["ui.link_live"]} · ${dict["ui.pill_gate_blocked"]} · `),`${context}: detail "${pill.detail}"`);
            assert(pill.detail.includes(language==="de"?"H 5,7 m":"H 5.7 m"),`${context}: accuracy in the detail "${pill.detail}"`);
            assert(pill.aria.includes(dict["ui.pill_measurement_blocked"]),`${context}: aria-label "${pill.aria}"`);
            assert(pill.title.includes(dict["ui.pill_measurement_blocked"]),`${context}: title "${pill.title}"`);
            assert.strictEqual(pill.labelColor,pill.critical,`${context}: blocked label uses the critical token`);
            assert.strictEqual(pill.labelClipped,false,`${context}: label clipped`);
            assert(pill.linkWordRight<=pill.smallRight+0.5,`${context}: link word clipped`);
            assert(pill.pillRight<=width+0.5,`${context}: pill outside the viewport`);
            assert.strictEqual(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true,`${context}: horizontal scroll`);
            if(language==="en"&&width===360)await page.locator(".app-header").screenshot({path:"/tmp/esp-rtk-21d-pill-blocked-360.png"});
            setGate([],readyFix);
            await waitPillGate("ready");await waitHero("ready");
            pill=await pillState();
            assert(pill.cls.split(" ").includes("ok"),`${context}: ready dot`);
            assert.strictEqual(pill.label,"RTK FIXED",`${context}: ready label`);
            assert(!pill.detail.includes(dict["ui.pill_gate_blocked"]),`${context}: no blocked word when ready "${pill.detail}"`);
            assert(!pill.aria.includes(dict["ui.pill_measurement_blocked"]),`${context}: no blocked aria when ready`);
            assert.notStrictEqual(pill.labelColor,pill.critical,`${context}: ready label is not critical`);
          }
        }
        await page.evaluate(()=>localStorage.setItem("rtk-language","en"));
        await page.setViewportSize({width:360,height:800});
        // While recording the activity text stays; the blocked state is still named for assistive technology.
        live.active_line={id:"l2",name:"Field route",state:"recording",recording_mode:"route",recording_sec:125,vertex_count:12,length_m:5};
        setGate(["rtk_fixed_required"],dgpsFix);
        await page.reload();
        await waitPillGate("blocked");
        let pill=await pillState();
        assert(pill.detail.includes("Recording"),`recording: activity text "${pill.detail}"`);
        assert(!pill.detail.includes(en["ui.pill_gate_blocked"]),`recording: no blocked word "${pill.detail}"`);
        assert(pill.aria.includes(en["ui.pill_measurement_blocked"]),"recording: aria names the blocked gate");
        live.active_line=null;
        // Startup and a stale position: the gate is unknown, the label is muted, nothing claims blocked or ready.
        try{
          live.tracking_startup={state:"loading"};setGate(["rtk_fixed_required"],dgpsFix);
          await page.reload();
          await waitPillGate("unknown");
          pill=await pillState();
          assert(!pill.detail.includes(en["ui.pill_gate_blocked"]),"startup: no blocked word");
        }finally{delete live.tracking_startup;}
        setGate([],{...readyFix,stale:true,fix_age_ms:9000,fix_seq:32});
        await page.reload();
        await waitPillGate("unknown");
        // Contrast of the blocked label in every explicit theme.
        for(const theme of ["light","dark","high-contrast"]){
          await page.evaluate(value=>localStorage.setItem("rtk-theme",value),theme);
          setGate(["rtk_fixed_required"],{...dgpsFix,fix_seq:40});
          await page.reload();
          await page.waitForSelector(`html[data-theme="${theme}"]`);
          await waitPillGate("blocked");
          pill=await pillState();
          assert.strictEqual(pill.labelColor,pill.critical,`${theme}: blocked label uses the critical token`);
          assert.deepStrictEqual(await auditContrast(page,"#live-state, #live-state *"),[],`${theme}: pill contrast`);
          if(theme==="dark")await page.locator(".app-header").screenshot({path:"/tmp/esp-rtk-21d-pill-blocked-dark-360.png"});
        }
      }finally{
        await page.evaluate(()=>localStorage.removeItem("rtk-theme"));
        live.active_line=null;delete live.tracking_startup;
      }
      // 13. #21d checklist: an unknown correction age is unknown (neither passed nor failed); only the device reason fails the row.
      try{
        await page.evaluate(()=>localStorage.setItem("rtk-language","en"));
        await page.setViewportSize({width:360,height:800});
        const correctionsClass=()=>gate("corrections").getAttribute("class");
        const waitCorrections=value=>page.waitForFunction(expected=>{const node=document.querySelector('.condition[data-gate="corrections"]');return node&&node.className===expected;},value);
        setGate([],{...readyFix,correction_age_sec:null,fix_seq:50});
        await page.reload();
        await waitCorrections("condition unknown");await waitHero("ready");
        assert.strictEqual(await gate("corrections").locator(".condition-mark").innerText(),"?","unknown mark");
        assert((await gate("corrections").innerText()).includes("corrections age unknown (max. 5.0 s)"),"detail unchanged");
        assert.strictEqual(await page.locator(".condition.pass").count(),5,"five passed rows");
        assert.strictEqual(await page.locator(".condition.fail").count(),0,"no failed row");
        assert.strictEqual(await page.locator(".readiness-hero .metric b").nth(3).innerText(),"— s","age tile");
        assert.strictEqual(await page.getByRole("button",{name:"Object point",exact:true}).isDisabled(),false,"an unknown age blocks nothing");
        assert.strictEqual(await page.locator("#live-state b").getAttribute("data-gate"),"ready","pill follows the verdict");
        await page.locator(".conditions").screenshot({path:"/tmp/esp-rtk-21d-corrections-unknown-360.png"});
        const markColors=await page.evaluate(()=>{const probe=color=>{const node=document.createElement("i");node.style.color=color;document.body.append(node);const value=getComputedStyle(node).color;node.remove();return value;};
          return{unknown:getComputedStyle(document.querySelector('.condition[data-gate="corrections"]')).color,warn:probe("var(--warn)"),ok:probe("var(--ok)"),critical:probe("var(--critical)")};});
        assert.strictEqual(markColors.unknown,markColors.warn,"unknown row uses the warn token");
        assert.notStrictEqual(markColors.unknown,markColors.ok,"unknown row is not the pass colour");
        assert.notStrictEqual(markColors.unknown,markColors.critical,"unknown row is not the fail colour");
        for(const theme of ["light","dark","high-contrast"]){
          await page.evaluate(value=>localStorage.setItem("rtk-theme",value),theme);
          await page.reload();
          await page.waitForSelector(`html[data-theme="${theme}"]`);
          await waitCorrections("condition unknown");
          assert.deepStrictEqual(await auditContrast(page,".conditions *"),[],`${theme}: checklist contrast with an unknown row`);
        }
        await page.evaluate(()=>localStorage.removeItem("rtk-theme"));
        setGate([],{...readyFix,correction_age_sec:1.2,fix_seq:51});
        await waitCorrections("condition pass");
        assert.strictEqual(await gate("corrections").locator(".condition-mark").innerText(),"✓");
        setGate(["corrections_too_old"],{...readyFix,correction_age_sec:null,fix_seq:52});
        await waitCorrections("condition fail");await waitHero("blocked");
        assert.deepStrictEqual(await failing(),["corrections"],"the device reason fails the row");
      }finally{await page.evaluate(()=>localStorage.removeItem("rtk-theme"));}
      // 14. Hero tiles: each tile shows only the state of its own check (ok, fail or unknown), from the same rows as the checklist.
      try{
        await page.evaluate(()=>localStorage.setItem("rtk-language","en"));
        await page.setViewportSize({width:360,height:800});
        const waitTiles=expected=>page.waitForFunction(json=>JSON.stringify(Object.fromEntries([...document.querySelectorAll(".readiness-hero .metric")]
          .map(node=>[node.dataset.metric,node.dataset.state])))===json,JSON.stringify(expected),{timeout:8000}).catch(async error=>{
          const seen=await page.evaluate(()=>[...document.querySelectorAll(".readiness-hero .metric")].map(node=>[node.dataset.metric,node.dataset.state]));
          throw new Error(`tiles: expected ${JSON.stringify(expected)}, seen ${JSON.stringify(seen)} (${error.message})`);});
        const tileLook=()=>page.evaluate(()=>{const hero=document.querySelector(".readiness-hero"),probe=color=>{const node=document.createElement("i");node.style.color=color;hero.append(node);const value=getComputedStyle(node).color;node.remove();return value;};
          return{tiles:Object.fromEntries([...hero.querySelectorAll(".metric")].map(node=>[node.dataset.metric,getComputedStyle(node.querySelector("b")).color])),
            words:Object.fromEntries([...hero.querySelectorAll(".metric")].map(node=>[node.dataset.metric,node.querySelector(".visually-hidden").textContent])),
            hiddenWidths:[...hero.querySelectorAll(".metric .visually-hidden")].map(node=>node.getBoundingClientRect().width),
            values:[...hero.querySelectorAll(".metric b")].map(node=>node.textContent),
            hero:getComputedStyle(hero).color,signal:getComputedStyle(hero.querySelector(".signal")).color,
            ink:probe("var(--ink)"),warn:probe("var(--warn)"),critical:probe("var(--critical)"),ok:probe("var(--ok)")};});
        // The field report: blocked by fix type and accuracy only; 40 satellites and an unknown correction age are not failures.
        const fieldFix={qual:2,fix_status_text:"DGPS",sats:40,hdop:0.6,correction_age_sec:null,fix_seq:60,
          receiver_accuracy:{horizontal_sigma_m:5.7,altitude_sigma_m:9.1}};
        const fieldReasons=["rtk_fixed_required","gst_horizontal_error_too_high","gst_vertical_error_too_high"];
        setGate(fieldReasons,fieldFix);
        await page.reload();
        await waitHero("blocked");
        await waitTiles({horizontal:"fail",vertical:"fail",satellites:"ok",corrections:"unknown"});
        let look=await tileLook();
        assert.deepStrictEqual(look.values,["5.700 m","9.100 m","40","— s"],"field report: tile values");
        assert.deepStrictEqual(look.tiles,{horizontal:look.critical,vertical:look.critical,satellites:look.ink,corrections:look.warn},"field report: tile colours");
        assert.strictEqual(look.hero,look.critical,"field report: the hero border stays critical");
        assert.strictEqual(look.signal,look.critical,"field report: the signal stays critical");
        assert.strictEqual(await page.locator(".hero-state h2").innerText(),"Measurement blocked");
        assert.deepStrictEqual(look.words,{horizontal:` (${en["ui.diagnostic_fail"]})`,vertical:` (${en["ui.diagnostic_fail"]})`,satellites:"",corrections:` (${en["ui.why_state_unknown"]})`},"field report: state in words");
        assert(look.hiddenWidths.every(width=>width<=1),"field report: the state words are visually hidden");
        // The checklist keeps its own per-row states.
        assert.deepStrictEqual(await failing(),["fix","accuracy"],"field report: failing rows");
        assert.strictEqual(await gate("corrections").getAttribute("class"),"condition unknown","field report: corrections row unknown");
        await page.locator(".readiness-hero").screenshot({path:"/tmp/esp-rtk-21e-hero-tiles-360.png"});
        for(const theme of ["light","dark","high-contrast"]){
          await page.evaluate(value=>localStorage.setItem("rtk-theme",value),theme);
          await page.reload();
          await page.waitForSelector(`html[data-theme="${theme}"]`);
          await waitTiles({horizontal:"fail",vertical:"fail",satellites:"ok",corrections:"unknown"});
          look=await tileLook();
          assert.deepStrictEqual(look.tiles,{horizontal:look.critical,vertical:look.critical,satellites:look.ink,corrections:look.warn},`${theme}: tile colours`);
          assert.deepStrictEqual(await auditContrast(page,".readiness-hero *"),[],`${theme}: hero contrast with mixed tile states`);
          await page.locator(".readiness-hero").screenshot({path:`/tmp/esp-rtk-21e-hero-tiles-${theme}-360.png`});
        }
        await page.evaluate(()=>localStorage.removeItem("rtk-theme"));
        // German words for assistive technology.
        await page.evaluate(()=>localStorage.setItem("rtk-language","de"));
        await page.reload();
        await waitTiles({horizontal:"fail",vertical:"fail",satellites:"ok",corrections:"unknown"});
        look=await tileLook();
        assert.deepStrictEqual(look.words,{horizontal:` (${de["ui.diagnostic_fail"]})`,vertical:` (${de["ui.diagnostic_fail"]})`,satellites:"",corrections:` (${de["ui.why_state_unknown"]})`},"de: state in words");
        await page.evaluate(()=>localStorage.setItem("rtk-language","en"));
        await page.reload();
        // One axis at a time.
        setGate(["gst_horizontal_error_too_high"],{...readyFix,fix_seq:61});
        await waitTiles({horizontal:"fail",vertical:"ok",satellites:"ok",corrections:"ok"});
        setGate(["gst_vertical_error_too_high"],{...readyFix,fix_seq:62});
        await waitTiles({horizontal:"ok",vertical:"fail",satellites:"ok",corrections:"ok"});
        setGate(["too_few_satellites"],{...readyFix,sats:7,fix_seq:63});
        await waitTiles({horizontal:"ok",vertical:"ok",satellites:"fail",corrections:"ok"});
        // A correction age over the limit fails only the correction tile.
        setGate(["corrections_too_old"],{...readyFix,correction_age_sec:12,fix_seq:64});
        await waitTiles({horizontal:"ok",vertical:"ok",satellites:"ok",corrections:"fail"});
        look=await tileLook();
        assert.deepStrictEqual(look.tiles,{horizontal:look.ink,vertical:look.ink,satellites:look.ink,corrections:look.critical},"corrections too old: tile colours");
        assert.strictEqual(look.words.corrections,` (${en["ui.diagnostic_fail"]})`,"corrections too old: state in words");
        assert.deepStrictEqual(await failing(),["corrections"],"corrections too old: failing rows");
        // Ready: the hero keeps its look, the tiles stay in the hero colour; an unknown age is still marked unknown.
        setGate([],{...readyFix,fix_seq:65});
        await waitHero("ready");
        await waitTiles({horizontal:"ok",vertical:"ok",satellites:"ok",corrections:"ok"});
        look=await tileLook();
        assert.strictEqual(look.hero,look.ok,"ready: hero colour");
        assert.deepStrictEqual(look.tiles,{horizontal:look.ok,vertical:look.ok,satellites:look.ok,corrections:look.ok},"ready: tiles unchanged");
        assert.deepStrictEqual(look.words,{horizontal:"",vertical:"",satellites:"",corrections:""},"ready: no state words");
        setGate([],{...readyFix,correction_age_sec:null,fix_seq:66});
        await waitTiles({horizontal:"ok",vertical:"ok",satellites:"ok",corrections:"unknown"});
        assert(await heroHas("ready"),"ready with an unknown age");
        look=await tileLook();
        assert.strictEqual(look.tiles.corrections,look.warn,"ready: an unknown age uses the warn token");
        assert.deepStrictEqual(await auditContrast(page,".readiness-hero *"),[],"ready with an unknown age: hero contrast");
        // A stale position fails every row, and so every tile.
        setGate([],{...readyFix,stale:true,fix_age_ms:9000,fix_seq:67});
        await waitTiles({horizontal:"fail",vertical:"fail",satellites:"fail",corrections:"fail"});
        assert.strictEqual(await page.locator(".condition.fail").count(),6,"stale: every row fails");
        // A stale response may still carry a single-axis reason: staleness wins, both accuracy tiles fail.
        setGate(["gst_horizontal_error_too_high"],{...readyFix,fix_seq:68});
        await waitTiles({horizontal:"fail",vertical:"ok",satellites:"ok",corrections:"ok"});
        setGate(["gst_horizontal_error_too_high"],{...readyFix,stale:true,fix_age_ms:9000,fix_seq:69});
        await waitTiles({horizontal:"fail",vertical:"fail",satellites:"fail",corrections:"fail"});
        look=await tileLook();
        assert.strictEqual(look.words.vertical,` (${en["ui.diagnostic_fail"]})`,"stale with a horizontal reason: vertical named as failed");
      }finally{await page.evaluate(()=>{localStorage.removeItem("rtk-theme");localStorage.setItem("rtk-language","en");});}
      setGate([]);
      live.active_line=null;
      console.log(`status-gate tallest German hero: ${tallest}px`);
      assert.deepStrictEqual(errors,[],"page errors");
    }finally{
      clearVerdict();live.current_quality.limits=savedLimits;Object.assign(live,snapshot);
    }
    await browser.close();console.log("status-gate-browser=ok");return;
  }

  if(process.argv.includes("--capacity-only")){
    await testCapacity(page);
    await browser.close();
    console.log("capacity-browser-acceptance=ok");return;
  }

  if(process.argv.includes("--theme-only")){
    // #29c: choice of automatic, light, dark and high contrast under Operation & display (390 px start, English, nothing stored).
    // The shared main page came from browser.newPage() and has a context of its own that cannot open a second tab,
    // so this suite works in a context of its own (shared localStorage between its tabs, empty at the start).
    const context=await browser.newContext({viewport:{width:390,height:800}}),page=await context.newPage(),errors=[];
    page.setDefaultTimeout(20000);
    page.on("pageerror",error=>errors.push(error.message));
    const wakeLockStub=()=>{Object.defineProperty(navigator,"wakeLock",{value:{request:async()=>({release:async()=>{},addEventListener:()=>{}})}});};
    await page.addInitScript(wakeLockStub);
    await page.route("http://local.test/**",fixture);
    await page.goto("http://local.test/");
    await page.waitForSelector(".readiness-hero");
    const de=require(root+"/i18n.js").dictionaries.de;
    // Every request except the regular live poll (GET /api/ui/live, startPolling keeps running).
    let requestCount=0;
    page.on("request",request=>{if(!(request.method()==="GET"&&new URL(request.url()).pathname==="/api/ui/live"))requestCount++;});
    const nonLiveRequests=()=>requestCount;
    const surface=target=>target.evaluate(()=>getComputedStyle(document.documentElement).getPropertyValue("--surface").trim());
    const metas=target=>target.evaluate(()=>[...document.querySelectorAll('meta[name="theme-color"]')].map(meta=>[meta.getAttribute("media"),meta.getAttribute("content")]));
    const pressed=target=>target.evaluate(()=>[...document.querySelectorAll(".theme-choice button")].filter(button=>button.getAttribute("aria-pressed")==="true").map(button=>button.dataset.themeChoice));
    const labels=target=>target.evaluate(()=>[...document.querySelectorAll(".theme-choice button")].map(button=>button.textContent.trim()));
    const choose=(target,name)=>target.locator(`.theme-choice [data-theme-choice="${name}"]`).click();
    const themeAttribute=target=>target.evaluate(()=>document.documentElement.getAttribute("data-theme"));
    await page.setViewportSize({width:390,height:800});
    await page.evaluate(()=>{localStorage.clear();localStorage.setItem("rtk-language","en");});
    await page.reload();
    await page.waitForSelector(".readiness-hero");
    // 1. The choice: four buttons in this order, automatic pressed, no attribute.
    await go(page,"device/display","#app-main");
    await page.waitForSelector(".theme-choice",{timeout:5000}).catch(()=>assert.fail("theme choice on device/display"));
    assert.deepStrictEqual(await labels(page),["Automatic","Light","Dark","High contrast"]);
    assert.deepStrictEqual(await pressed(page),["auto"]);
    assert.strictEqual(await themeAttribute(page),null);
    assert.strictEqual(await page.locator(".theme-choice").getAttribute("role"),"group");
    assert.strictEqual(await page.locator(".theme-choice").getAttribute("aria-label"),"Appearance");
    // 2. A choice neither rebuilds the page nor asks the device for anything.
    await page.evaluate(()=>{
      window.__card=document.querySelector('[data-card="display"]');window.__mainChild=document.querySelector("#app-main").firstElementChild;
      window.__hash=location.hash;window.__busy=[];
      new MutationObserver(records=>records.forEach(record=>window.__busy.push(record.target.getAttribute("aria-busy")))).observe(document.querySelector("#app-main"),{attributes:true,attributeFilter:["aria-busy"]});
    });
    requestCount=0;
    await choose(page,"dark");
    await page.waitForSelector('html[data-theme="dark"]');
    assert.deepStrictEqual(await pressed(page),["dark"]);
    assert.strictEqual(await page.evaluate(()=>localStorage.getItem("rtk-theme")),"dark");
    assert.deepStrictEqual((await metas(page)).map(entry=>entry[1]),["#161e24","#161e24"]);
    await page.waitForTimeout(500);
    const kept=await page.evaluate(()=>({card:document.querySelector('[data-card="display"]')===window.__card,child:document.querySelector("#app-main").firstElementChild===window.__mainChild,
      focus:document.activeElement&&document.activeElement.dataset.themeChoice,hash:location.hash===window.__hash,busy:window.__busy}));
    assert.strictEqual(kept.card,true,"the display card is the same node");
    assert.strictEqual(kept.child,true,"the page content is the same node");
    assert.strictEqual(kept.focus,"dark","focus stays on the pressed button");
    assert.strictEqual(kept.hash,true,"the hash is unchanged");
    assert(!kept.busy.includes("true"),`no renderRoute (aria-busy ${JSON.stringify(kept.busy)})`);
    assert.strictEqual(nonLiveRequests(),0,"a choice sends no request besides the live poll");
    // 3. Persistence.
    await page.reload();
    await page.waitForSelector(".theme-choice");
    assert.strictEqual(await themeAttribute(page),"dark");
    assert.deepStrictEqual(await pressed(page),["dark"]);
    // 4. The listener goes away with the route: the detached button is not touched any more.
    await page.evaluate(()=>{window.__oldDark=document.querySelector('[data-theme-choice="dark"]');});
    await go(page,"device",".device-links");
    await page.evaluate(()=>RtkI18n.setTheme("light"));
    assert.strictEqual(await themeAttribute(page),"light");
    assert.deepStrictEqual(await page.evaluate(()=>[window.__oldDark.isConnected,window.__oldDark.getAttribute("aria-pressed")]),[false,"true"],"a detached button keeps its state");
    // 5. System dark: light stays light, automatic follows the system, also while the page is open.
    await go(page,"device/display",".theme-choice");
    await page.emulateMedia({colorScheme:"dark"});
    await choose(page,"light");
    await page.waitForSelector('html[data-theme="light"]');
    assert.strictEqual(await surface(page),"#fff");
    assert.deepStrictEqual((await metas(page)).map(entry=>entry[1]),["#ffffff","#ffffff"]);
    await choose(page,"auto");
    await page.waitForFunction(()=>!document.documentElement.hasAttribute("data-theme"));
    assert.strictEqual(await surface(page),"#161e24");
    assert.deepStrictEqual(await metas(page),[["(prefers-color-scheme: light)","#ffffff"],["(prefers-color-scheme: dark)","#161e24"]]);
    await page.emulateMedia({colorScheme:"light"});
    assert.strictEqual(await surface(page),"#fff");
    // 6. A second tab of the same browser follows a choice through the storage event, without reloading.
    const second=await context.newPage(),secondErrors=[];
    try{
      second.on("pageerror",error=>secondErrors.push(error.message));
      await second.addInitScript(wakeLockStub);
      await second.route("http://local.test/**",fixture);
      await second.goto("http://local.test/#device/display");
      await second.waitForSelector(".theme-choice");
      await second.waitForSelector('#app-main[aria-busy="false"]');
      await second.evaluate(()=>{window.__marker=1;});
      await choose(page,"dark");
      await second.waitForSelector('html[data-theme="dark"]');
      assert.deepStrictEqual(await pressed(second),["dark"]);
      assert.strictEqual(await second.evaluate(()=>window.__marker),1,"the second tab was not reloaded");
      assert.deepStrictEqual(secondErrors,[]);
    }finally{await second.close();}
    // 7. The device overview shows the choice; the language switch translates buttons and short line, each on its own page.
    await go(page,"device/display",".theme-choice");
    await choose(page,"high-contrast");
    await page.waitForSelector('html[data-theme="high-contrast"]');
    await go(page,"device",".device-links");
    assert((await page.locator('.device-links a[href="/#device/display"]').innerText()).includes("English · High contrast"));
    await setLanguage(page,"de");
    await go(page,"device/display",".theme-choice");
    assert.deepStrictEqual(await labels(page),[de["ui.theme_auto"],de["ui.theme_light"],de["ui.theme_dark"],de["ui.high_contrast"]]);
    assert.deepStrictEqual(await pressed(page),["high-contrast"]);
    await go(page,"device",".device-links");
    assert((await page.locator('.device-links a[href="/#device/display"]').innerText()).includes("Deutsch · "+de["ui.high_contrast"]));
    await setLanguage(page,"en");
    // 8. Layout, tap targets and contrast for every width, language and explicit theme.
    for(const lang of ["en","de"]){
      await setLanguage(page,lang);
      for(const width of [320,360,390,480,1280]){
        await page.setViewportSize({width,height:width===1280?900:800});
        for(const theme of ["light","dark","high-contrast"]){
          await page.evaluate(value=>RtkI18n.setTheme(value),theme);
          await go(page,"device/display",".theme-choice");
          const label=`${lang} ${width}px ${theme}`;
          assertLayout(await layoutAudit(page),label);
          const sizes=await page.evaluate(()=>[...document.querySelectorAll(".theme-choice button")].map(button=>{const rect=button.getBoundingClientRect();return [rect.width,rect.height];}));
          assert.strictEqual(sizes.length,4,label);
          assert(sizes.every(size=>size[0]>=47.5&&size[1]>=47.5),`${label}: theme buttons ${JSON.stringify(sizes)}`);
          // The desktop sidebar links (1280 px) are 31 px high by design; the phone widths hold every target to 48 px.
          if(width<1280)assert.deepStrictEqual(await auditTargets(page),[],`tap targets (${label})`);
          assert.deepStrictEqual(await auditContrast(page),[],`contrast (${label})`);
        }
      }
    }
    assert.deepStrictEqual(errors,[]);
    await browser.close();console.log("theme-browser=ok");return;
  }
  if(process.argv.includes("--a11y-only")){
    const errors=[];page.on("pageerror",error=>errors.push(error.message));
    const routes=["status","measure/setup","projects","projects/p1","device","device/diagnostics/rtcm","device/connections/access","device/diagnostics/log","device/settings","device/connections/internet","device/diagnostics","device/diagnostics/console","device/maintenance"];
    const settle=async route=>{await go(page,route,"#app-main");await page.waitForTimeout(250);};
    const reloadStatus=async()=>{await page.evaluate(()=>{location.hash="status";});await page.reload();await page.waitForSelector(".readiness-hero");};
    const openGate=()=>{live.current_fix=Object.assign({},live.current_fix,{qual:4,fix_status_text:"RTK_FIXED",correction_age_sec:1.2});live.current_quality.accepted=true;};
    const savedCapacity=live.capacity;
    await page.evaluate(()=>{localStorage.setItem("rtk-language","en");localStorage.removeItem("rtk-theme");});
    openGate();
    // The warning and error log rows and a warning vertex row must exist so the contrast audit measures them.
    logLevels=true;vertexStep=0.8;
    // 1. Tap targets on every route at three phone widths, plus the two action dialogs.
    for(const width of [320,360,480]){
      await page.setViewportSize({width,height:800});
      await reloadStatus();
      for(const route of routes){
        await settle(route);
        assert.deepStrictEqual(await auditTargets(page),[],`tap targets on ${route} at ${width}px`);
      }
      await settle("status");
      for(const name of ["Start line","Object point"]){
        await page.locator(".field-actions button",{hasText:name}).first().click();
        await page.waitForSelector("dialog.action-sheet[open]");
        assert.deepStrictEqual(await auditTargets(page),[],`tap targets in the ${name} dialog at ${width}px`);
        await page.keyboard.press("Escape");
        await page.waitForFunction(()=>!document.querySelector("dialog.action-sheet"));
      }
    }
    // 2. Contrast in the light, high-contrast, automatic dark and explicit dark themes at 360 px.
    await page.setViewportSize({width:360,height:800});
    for(const theme of ["standard","high-contrast","auto-dark","dark"]){
      await page.evaluate(value=>{if(value==="standard"||value==="auto-dark")localStorage.removeItem("rtk-theme");else localStorage.setItem("rtk-theme",value);},theme);
      await page.emulateMedia({colorScheme:theme==="auto-dark"?"dark":"light"});
      await reloadStatus();
      assert.strictEqual(await page.evaluate(()=>getComputedStyle(document.documentElement).colorScheme),theme==="auto-dark"||theme==="dark"?"dark":"light",`color-scheme (${theme})`);
      for(const route of routes){
        await settle(route);
        if(route==="projects/p1"){await page.locator(".line-row").click();await page.waitForSelector(".vertex-row.warn");}
        const found=await auditContrast(page),checked=await page.evaluate(()=>window.__contrastChecked);
        assert.deepStrictEqual(found,[],`contrast on ${route} (${theme})`);
        if(route==="device/diagnostics/log"){
          for(const selector of [".log-entry.warn",".log-entry.error"])assert(await page.locator(selector).count()>0,`${selector} present in the contrast audit (${theme})`);
          const consoleBackground={standard:"rgb(233, 238, 236)","high-contrast":"rgb(255, 255, 255)","auto-dark":"rgb(11, 16, 20)",dark:"rgb(11, 16, 20)"}[theme];
          assert.strictEqual(await page.locator(".log-console").evaluate(el=>getComputedStyle(el).backgroundColor),consoleBackground,`log console background (${theme})`);
        }
        if(route==="projects/p1")assert(await page.locator(".vertex-row.warn").count()>0,`.vertex-row.warn present in the contrast audit (${theme})`);
        if(process.env.RTK_AUDIT_REPORT)console.log("contrast-checked",theme,route,checked);
      }
      // 7. The soft warning tone exists in both themes.
      assert.notStrictEqual(await page.evaluate(()=>getComputedStyle(document.documentElement).getPropertyValue("--warn-soft").trim()),"",`--warn-soft in ${theme}`);
    }
    // 2a. An explicit light choice wins over a dark system; the legacy value "default" means automatic.
    await page.emulateMedia({colorScheme:"dark"});
    await page.evaluate(()=>localStorage.setItem("rtk-theme","light"));
    await reloadStatus();
    assert.strictEqual(await page.evaluate(()=>document.documentElement.getAttribute("data-theme")),"light","stored light sets data-theme");
    assert.strictEqual(await page.evaluate(()=>getComputedStyle(document.documentElement).getPropertyValue("--surface").trim()),"#fff","light --surface under a dark system");
    assert.deepStrictEqual(await auditContrast(page),[],"contrast with light chosen under a dark system");
    await page.evaluate(()=>localStorage.setItem("rtk-theme","default"));
    await reloadStatus();
    assert.strictEqual(await page.evaluate(()=>document.documentElement.getAttribute("data-theme")),null,"stored default is automatic: no data-theme");
    await page.evaluate(()=>localStorage.removeItem("rtk-theme"));
    await page.emulateMedia({colorScheme:"light"});
    // 2b. Sign-in page: inverse surface, panel and the error state follow the theme (hero = --hero, panel = --surface).
    const heroKicker={light:["rgb(17, 24, 32)","rgb(255, 255, 255)","rgb(174, 187, 181)"],"high-contrast":["rgb(0, 0, 0)","rgb(255, 255, 255)","rgb(255, 255, 255)"],"auto-dark":["rgb(36, 49, 58)","rgb(22, 30, 36)","rgb(185, 199, 193)"],dark:["rgb(36, 49, 58)","rgb(22, 30, 36)","rgb(185, 199, 193)"],"light-on-dark-system":["rgb(17, 24, 32)","rgb(255, 255, 255)","rgb(174, 187, 181)"]};
    const heroMeta={dark:"#24313a","light-on-dark-system":"#111820"};
    for(const [name,options] of [["light",{theme:null,scheme:"light"}],["high-contrast",{theme:"high-contrast",scheme:"light"}],["auto-dark",{theme:null,scheme:"dark"}],["dark",{theme:"dark",scheme:"light"}],["light-on-dark-system",{theme:"light",scheme:"dark"}]]){
      for(const viewport of [{width:390,height:800},{width:1280,height:900}]){
        const {login,errors:loginErrors}=await openLoginPage(browser,Object.assign({viewport},options));
        const label=`sign-in page (${name}, ${viewport.width}px)`;
        try{
          const [hero,panel,kicker]=heroKicker[name];
          assert.strictEqual(await login.locator(".v2-login").evaluate(el=>getComputedStyle(el).backgroundColor),hero,`${label}: hero background`);
          assert.strictEqual(await login.locator(".login-panel").evaluate(el=>getComputedStyle(el).backgroundColor),panel,`${label}: panel background`);
          assert.strictEqual(await login.locator(".login-intro .kicker").evaluate(el=>getComputedStyle(el).color),kicker,`${label}: kicker color`);
          if(heroMeta[name])assert.deepStrictEqual(await login.evaluate(()=>[...document.querySelectorAll('meta[name="theme-color"]')].map(meta=>meta.getAttribute("content"))),[heroMeta[name],heroMeta[name]],`${label}: theme-color follows --hero`);
          assert.deepStrictEqual(await auditContrast(login,".v2-login *"),[],`${label}: contrast`);
          assert((await login.evaluate(()=>window.__contrastChecked))>=8,`${label}: audit measured the page`);
          await login.locator("#v2-device-code").fill("WRONG");
          await login.locator("button[type=submit]").click();
          await login.waitForFunction(()=>{const el=document.querySelector("#v2-login-state.notice.critical");return el&&el.textContent.includes("Sign-in failed");});
          assert.deepStrictEqual(await auditContrast(login,".v2-login *"),[],`${label}: contrast in the error state`);
          assert.deepStrictEqual(loginErrors,[],`${label}: page errors`);
        }finally{await login.close();}
      }
    }
    // 3. Visible keyboard focus.
    await reloadStatus();
    for(const route of ["status","device/settings","measure/setup"]){
      await settle(route);
      assert.deepStrictEqual(await auditFocus(page,30),[],`focus indicator on ${route}`);
    }
    // 4. Nothing hides behind the sticky action bar or the mobile navigation.
    await page.setViewportSize({width:360,height:740});
    live.active_line={id:"test",name:"Route",state:"recording",recording_mode:"route",recording_sec:12};
    await reloadStatus();await page.waitForSelector(".field-actions button");
    const box=selector=>page.locator(selector).first().boundingBox();
    // At scroll 0 the bar itself must already clear the navigation (sticky cannot lift a bar that opens its container).
    await page.evaluate(()=>window.scrollTo(0,0));
    const bar=await box(".field-actions"),nav=await box(".mobile-nav");
    assert(bar.y+bar.height<=nav.y,`action bar at scroll 0 ends at ${bar.y+bar.height}, navigation starts at ${nav.y}`);
    await page.evaluate(()=>window.scrollTo(0,300));
    const scrolled=await box(".field-actions");
    assert(scrolled.y+scrolled.height<=nav.y,"action bar sticks under the navigation while scrolling");
    assert(await page.evaluate(()=>parseFloat(getComputedStyle(document.documentElement).scrollPaddingBottom))>=190,"status page reserves the action bar height");
    const buttons=page.locator(".field-actions button");
    for(let index=0;index<await buttons.count();index++){
      await buttons.nth(index).evaluate(node=>node.scrollIntoView({block:"nearest"}));
      assert(!intersects(await buttons.nth(index).boundingBox(),await box(".mobile-nav")),`action button ${index} under the navigation`);
    }
    const rows=page.locator(".condition,.context-row");
    for(let index=0;index<await rows.count();index++){
      const row=rows.nth(index);
      if(!await row.isVisible())continue;
      await row.evaluate(node=>node.scrollIntoView({block:"nearest"}));
      const rect=await row.boundingBox();
      assert(!intersects(rect,await box(".mobile-nav")),`status row ${index} under the navigation`);
      assert(!intersects(rect,await box(".field-actions")),`status row ${index} under the action bar`);
    }
    // The reserve is limited to pages that have an action bar.
    await settle("projects");
    assert(await page.evaluate(()=>parseFloat(getComputedStyle(document.documentElement).scrollPaddingBottom))<100,"routes without an action bar reserve no bar height");
    await settle("status");
    // 5. The capacity message (#45) sits in the page flow at the end of the page: nothing above it is covered, and it is
    // reached by scrolling without touching the header or the navigation; on the measuring screen it stays quiet.
    await settle("projects");
    await page.evaluate(()=>window.scrollTo(0,0));
    live.capacity={used:4000,limit:5000,remaining:1000,level:"notice",writable:true};
    const capacityDockEntry='#feedback-dock .feedback-item[data-key="capacity"]';
    await page.waitForSelector(capacityDockEntry);
    const firstBlock=await page.evaluate(()=>{const block=document.querySelector("#app-main").firstElementChild,r=block.getBoundingClientRect(),bar=document.querySelector(".app-header").getBoundingClientRect();
      const hit=(x,y)=>{const node=document.elementFromPoint(x,y);return Boolean(node&&block.contains(node));};
      return{top:r.top,headerBottom:bar.bottom,center:hit(r.left+r.width/2,r.top+Math.min(r.height,40)/2),topLeft:hit(r.left+2,r.top+2)};});
    assert(firstBlock.top>=firstBlock.headerBottom-0.5,"first content block starts below the header "+JSON.stringify(firstBlock));
    assert(firstBlock.center&&firstBlock.topLeft,"first content block stays uncovered at scroll 0 "+JSON.stringify(firstBlock));
    await page.evaluate(()=>window.scrollTo(0,document.documentElement.scrollHeight));
    const entryBox=await box(capacityDockEntry),viewport=page.viewportSize();
    assert(entryBox.x>=0&&entryBox.y>=0&&entryBox.x+entryBox.width<=viewport.width&&entryBox.y+entryBox.height<=viewport.height,"capacity entry leaves the viewport "+JSON.stringify(entryBox));
    assert(!intersects(entryBox,await box(".mobile-nav")),"capacity entry meets the navigation");
    assert(!intersects(entryBox,await box(".app-header")),"capacity entry meets the header");
    await settle("status");
    assert.strictEqual(await page.locator('.feedback-item[data-key="capacity"]').count(),0,"no capacity entry on the measuring screen");
    live.capacity=savedCapacity;
    await page.waitForFunction(()=>document.querySelector(".recording-capacity").classList.contains("compact"));
    // 6. The disabled state carries a pattern and a dashed border, not only a colour.
    live.active_line=null;live.current_quality.accepted=false;
    await reloadStatus();
    await page.waitForFunction(()=>document.querySelector(".field-actions button:disabled"));
    const disabled=await page.locator(".field-actions button:disabled").first().evaluate(node=>{const style=getComputedStyle(node);return [style.backgroundImage,style.borderTopStyle];});
    assert(disabled[0].includes("repeating-linear-gradient"),`disabled pattern: ${disabled[0]}`);
    assert.strictEqual(disabled[1],"dashed");
    // 8. Map tools (#24-UI): four direct buttons and the zoom display, layers and full screen in a menu, nothing floating over the map.
    await page.setViewportSize({width:320,height:800});
    await settle("projects/p1");
    await page.waitForSelector(".map-tools");
    await assertMapMenu(page,"a11y 320");
    assert.notStrictEqual(await page.locator(".map-layers").evaluate(node=>getComputedStyle(node).position),"absolute");
    for(const menuOpen of[false,true]){
      if(menuOpen)await page.locator(".map-menu summary").click();
      const tools=await box(".map-tools"),toolControls=page.locator(menuOpen?".map-tools .btn,.map-tools output,.map-tools summary":".map-tools > .btn,.map-tools > output,.map-tools summary"),placed=[];
      assert(await page.locator(".map-tools").evaluate(node=>node.scrollWidth<=node.clientWidth),"tool row scrolls sideways");
      for(let index=0;index<await toolControls.count();index++){
        const rect=await toolControls.nth(index).boundingBox();
        assert(rect.x>=0&&rect.x+rect.width<=320,`map tool ${index} leaves the viewport (menu open ${menuOpen})`);
        for(const other of placed)assert(!intersects(rect,other),`map tool ${index} overlaps another tool (menu open ${menuOpen})`);
        placed.push(rect);
        assert(rect.x>=tools.x-.5&&rect.y>=tools.y-.5&&rect.x+rect.width<=tools.x+tools.width+.5&&rect.y+rect.height<=tools.y+tools.height+.5,`map tool ${index} sticks out of the tool row (menu open ${menuOpen})`);
      }
    }
    await page.locator(".map-menu summary").click();
    // 9. Map hit areas: 48 px per point and line, point before line before receiver, shapes, working layer switches.
    for(const width of [320,360,480]){
      await page.setViewportSize({width,height:800});
      await reloadStatus();await settle("projects/p1");
      await page.waitForSelector(".map-point");
      assert.strictEqual(await page.locator(".map-point").count(),2);
      assert.strictEqual(await page.locator(".map-line").count(),1);
      assert.strictEqual((await auditMapHits(page,`map ${width}px zoom 1`)).points.length,2);
      // Overlap: line end, receiver symbol and point a1 share one place; the point wins, the receiver is never hit.
      const a1=(await mapGeometry(page)).points.find(point=>point.label.endsWith(": Valve 1"));
      assert.strictEqual(await mapTargetAt(page,a1.x,a1.y),a1.label,`point a1 on top of line end and receiver at ${width}px`);
      assert.notStrictEqual(await page.evaluate(([x,y])=>{const element=document.elementFromPoint(x,y);return element&&element.closest(".map-position")?"position":"map";},[a1.x,a1.y]),"position");
      assert.strictEqual(await page.locator(".point-card").count(),0);
      await page.mouse.click(a1.x,a1.y);
      await page.waitForSelector(".point-card");
      assert.strictEqual(await page.locator(".line-quality").count(),0,"a click on the overlap opens the point, not the line");
      // Legend: one symbol per present type, with its shape.
      assert.strictEqual(await page.locator(".map-legend .point-symbol.type-valve.shape-diamond").count(),1);
      // Layer switches hide and show their layer.
      await page.locator(".map-menu summary").click();
      for(const [name,selector] of [["Points",".map-point"],["Lines",".map-line"],["Receiver",".map-position"]]){
        const button=page.locator(".map-layers .btn",{hasText:name}).first();
        await button.click();
        assert.deepStrictEqual(await page.locator(selector).evaluateAll(nodes=>nodes.map(node=>getComputedStyle(node).display)).then(values=>[...new Set(values)]),["none"],`${name} hidden`);
        await button.click();
        assert((await page.locator(selector).evaluateAll(nodes=>nodes.map(node=>getComputedStyle(node).display))).every(value=>value!=="none"),`${name} shown again`);
      }
      await page.locator(".map-menu summary").click();
      // Zoom in twice: hit areas stay 48 px on screen.
      await page.locator(".map-tools .btn",{hasText:"+"}).first().click();
      await page.locator(".map-tools .btn",{hasText:"+"}).first().click();
      await page.waitForTimeout(400);
      await auditMapHits(page,`map ${width}px zoom 2`);
    }
    // Keyboard focus on a map point is drawn on the shape in the focus colour.
    await page.setViewportSize({width:360,height:800});
    await reloadStatus();await settle("projects/p1");await page.waitForSelector(".map-point");
    await page.evaluate(()=>{window.scrollTo(0,0);document.activeElement&&document.activeElement.blur();});
    let focused=false;
    for(let index=0;index<80&&!focused;index++){
      await page.keyboard.press("Tab");
      focused=await page.evaluate(()=>document.activeElement&&document.activeElement.classList.contains("map-point"));
    }
    assert(focused,"Tab reaches a map point");
    const focusStroke=await page.evaluate(()=>{const probe=document.createElement("i");probe.style.color="var(--focus)";document.body.append(probe);const want=getComputedStyle(probe).color;probe.remove();return [getComputedStyle(document.activeElement.querySelector(".map-shape")).stroke,want];});
    assert.strictEqual(focusStroke[0],focusStroke[1],"focused map point strokes its shape in the focus colour");
    // 10. Sign-out in the header (#24-UI E10): a clean page asks nothing; unsaved configuration asks first and cancelling sends nothing.
    await page.setViewportSize({width:320,height:800});
    await reloadStatus();
    for(const width of [320,360,390,480,1280]){
      await page.setViewportSize({width,height:800});
      await settle("device/display");
      await assertHeaderSignOut(page,`a11y ${width}`);
    }
    await page.setViewportSize({width:360,height:800});
    const deletes=since=>requestLog.slice(since).filter(entry=>entry.path==="/api/session"&&entry.method==="DELETE").length;
    await settle("device/connections/internet");
    await page.waitForSelector("form.configuration-layout");
    await page.locator('input[name="wifi_ssid"]').fill("Unsaved network");
    await page.waitForFunction(()=>document.querySelector("form.configuration-layout").dataset.dirty==="true");
    let signOutSince=requestLog.length;
    await page.locator("#sign-out").click();
    await page.waitForSelector("dialog[open]");
    assert.strictEqual(await page.locator("dialog[open] h2, dialog[open] [id=sheet-title]").first().innerText(),"Discard unsaved changes?");
    await page.getByRole("button",{name:"Cancel"}).click();
    await page.waitForFunction(()=>!document.querySelector("dialog[open]"));
    assert.strictEqual(deletes(signOutSince),0,"cancelling the sign-out sends no DELETE");
    assert.strictEqual(await page.locator('input[name="wifi_ssid"]').inputValue(),"Unsaved network","cancelling keeps the unsaved input");
    assert.strictEqual(await page.evaluate(()=>location.hash),"#device/connections/internet");
    await page.locator("#sign-out").click();
    await page.waitForSelector("dialog[open]");
    await page.getByRole("button",{name:"Discard changes"}).click();
    // The mock serves the app again at "/" (the device shows the login); the reloaded app lands on Measure.
    await page.waitForFunction(()=>location.hash==="#measure"&&document.querySelector(".readiness-hero"));
    assert.strictEqual(deletes(signOutSince),1,"accepting signs out once");
    await page.waitForSelector("#sign-out");
    await settle("projects");
    signOutSince=requestLog.length;
    await page.locator("#sign-out").click();
    await page.waitForFunction(()=>location.hash==="#measure"&&document.querySelector(".readiness-hero"));
    assert.strictEqual(deletes(signOutSince),1,"a clean page signs out without a question");
    assert.strictEqual(await page.locator("dialog[open]").count(),0);
    // A failed sign-out (network error, CSRF refusal) shows the error once, stays on the page and leaves no unhandled rejection.
    await page.waitForSelector("#sign-out");
    await settle("projects");
    await page.evaluate(()=>{window.__rtkRejections=[];addEventListener("unhandledrejection",event=>window.__rtkRejections.push(String(event.reason&&event.reason.message||event.reason)));});
    let sessionFailure="network";
    const failSession=route=>{
      if(route.request().method()!=="DELETE")return route.fallback();
      requestLog.push({path:"/api/session",method:"DELETE",body:""});
      return sessionFailure==="network"?route.abort("failed"):route.fulfill({status:403,contentType:"application/json",body:JSON.stringify({error:"csrf"})});
    };
    await page.route("http://local.test/api/session",failSession);
    try{
      const refused=require(root+"/i18n.js").dictionaries.en["ui.action_refused_reload"];
      for(const [failure,text] of [["network",null],["csrf",refused]]){
        sessionFailure=failure;signOutSince=requestLog.length;
        await page.locator("#sign-out").click();
        await page.waitForFunction(()=>document.querySelector(".feedback-item.critical"),null,{timeout:3000})
          .catch(()=>assert.fail(`sign-out ${failure}: no visible error`));
        assert.strictEqual(deletes(signOutSince),1,`sign-out ${failure}: one DELETE`);
        if(text)assert((await page.locator(".feedback-item.critical").first().innerText()).includes(text),`sign-out ${failure}: the refusal text`);
        await page.waitForFunction(()=>!document.querySelector("#sign-out").disabled,null,{timeout:3000});
        assert.strictEqual(await page.evaluate(()=>location.hash),"#projects",`sign-out ${failure}: the page stays`);
        assert.deepStrictEqual(await page.evaluate(()=>window.__rtkRejections),[],`sign-out ${failure}: no unhandled rejection`);
        await page.evaluate(()=>document.querySelectorAll(".feedback-item.critical").forEach(node=>node.remove()));
      }
    }finally{await page.unroute("http://local.test/api/session",failSession);}
    // 11. A lost session over unsaved configuration (#24, Codex P2): the app asks before the sign-in redirect, so the
    // browser's leave prompt cannot cancel a redirect the page already counts as started. Cancel keeps the page usable.
    const enText=require(root+"/i18n.js").dictionaries.en;
    let expireLive=0;
    const unloadPrompts=[],onDialog=dialog=>{unloadPrompts.push(dialog.type());dialog.dismiss().catch(()=>{});};
    const expireOnce=route=>{if(expireLive<=0)return route.fallback();expireLive-=1;
      return route.fulfill({status:401,contentType:"application/json",body:JSON.stringify({error:"admin_session_required",message:"Sign in again."})});};
    const question=async label=>{await page.waitForSelector("dialog[open]",{timeout:10000}).catch(()=>assert.fail(label+": no question before the sign-in redirect, prompts "+JSON.stringify(unloadPrompts)));
      assert.strictEqual(await page.locator("dialog[open] #sheet-title").innerText(),"Discard unsaved changes?",label+": title");
      assert.strictEqual(await page.locator("dialog[open] p.muted").first().innerText(),enText["ui.sign_in_unsaved"],label+": text");};
    page.on("dialog",onDialog);
    await page.route("http://local.test/api/ui/live",expireOnce);
    try{
      await settle("device/connections/internet");
      await page.waitForSelector("form.configuration-layout");
      await page.locator('input[name="wifi_ssid"]').fill("Unsaved network");
      await page.waitForFunction(()=>document.querySelector("form.configuration-layout").dataset.dirty==="true");
      expireLive=1;
      await question("cancel");
      await page.locator("dialog[open]").getByRole("button",{name:"Cancel"}).click();
      await page.waitForFunction(()=>!document.querySelector("dialog[open]"));
      await page.waitForTimeout(500);
      assert.deepStrictEqual(unloadPrompts,[],"cancel: no browser leave prompt");
      assert(!new URL(page.url()).searchParams.has("return"),"cancel: no sign-in redirect "+page.url());
      assert.strictEqual(await page.evaluate(()=>location.hash),"#device/connections/internet","cancel: the page stays");
      assert.strictEqual(await page.locator('input[name="wifi_ssid"]').inputValue(),"Unsaved network","cancel: the unsaved input stays");
      assert.strictEqual(await page.locator("form.configuration-layout").getAttribute("data-dirty"),"true","cancel: the form stays guarded");
      assert.strictEqual(await page.evaluate(()=>Boolean(history.state&&history.state.rtkSheet)),false,"cancel: the question left no sheet entry");
      // The next poll that answers 401 right after Cancel does not ask again (30 s pause) and does not leave.
      expireLive=1;
      for(let index=0;index<100&&expireLive>0;index++)await page.waitForTimeout(100);
      assert.strictEqual(expireLive,0,"cancel: a second poll was answered with 401");
      await page.waitForTimeout(500);
      assert.strictEqual(await page.locator("dialog[open]").count(),0,"cancel: a 401 poll in the pause asks nothing");
      assert(!new URL(page.url()).searchParams.has("return"),"cancel: a 401 poll in the pause does not leave");
      assert.strictEqual(await page.locator("form.configuration-layout").getAttribute("data-dirty"),"true","cancel: still guarded after the second 401");
      // The page stays usable: a sheet closed with its button takes its entry back, and Back closes an open sheet.
      await page.locator("#sign-out").click();
      await page.waitForSelector("dialog[open]");
      await page.locator("dialog[open]").getByRole("button",{name:"Cancel"}).click();
      await page.waitForFunction(()=>!document.querySelector("dialog[open]"));
      await page.waitForFunction(()=>!(history.state&&history.state.rtkSheet),null,{timeout:3000}).catch(()=>assert.fail("cancel: a closed sheet left its history entry"));
      await page.locator("#sign-out").click();
      await page.waitForSelector("dialog[open]");
      await page.goBack();
      await page.waitForFunction(()=>!document.querySelector("dialog[open]"),null,{timeout:3000}).catch(()=>assert.fail("cancel: Back does not close the sheet"));
      assert.strictEqual(await page.evaluate(()=>location.hash),"#device/connections/internet","cancel: Back closed only the sheet");
      await page.evaluate(()=>document.querySelector('#mobile-nav a[data-route="device"]').click());
      await page.waitForSelector("dialog[open]");
      await page.locator("dialog[open]").getByRole("button",{name:"Cancel"}).click();
      await page.waitForFunction(()=>!document.querySelector("dialog[open]"));
      assert.strictEqual(await page.evaluate(()=>location.hash),"#device/connections/internet","cancel: the link guard still asks");
      // Confirm, still in the pause: the user's own write (the TCP authentication switch) gets a 401 and asks at once;
      // confirm releases the guard for this navigation, the redirect reaches the sign-in page with the return target.
      let expireAuth=1;
      const expireAuthOnce=route=>{if(route.request().method()!=="PUT"||expireAuth<=0)return route.fallback();expireAuth-=1;
        return route.fulfill({status:401,contentType:"application/json",body:JSON.stringify({error:"admin_session_required",message:"Sign in again."})});};
      await page.route("http://local.test/api/tcp-auth",expireAuthOnce);
      await page.locator(".tcp-auth-card .switch").click();
      const disable=page.locator("dialog[open]").getByRole("button",{name:"Disable authentication"});
      await page.waitForSelector("dialog[open]");
      if(await disable.count())await disable.click();
      await page.waitForFunction(()=>{const title=document.querySelector("dialog[open] #sheet-title");return title&&title.textContent==="Discard unsaved changes?";},null,{timeout:10000})
        .catch(()=>assert.fail("confirm: the user's write in the pause does not ask"));
      assert.strictEqual(expireAuth,0,"confirm: the write was answered with 401");
      await question("confirm");
      await page.locator("dialog[open]").getByRole("button",{name:enText["ui.sign_in_again"]}).click();
      await page.waitForURL(url=>new URL(url).searchParams.has("return"),{timeout:10000});
      assert.strictEqual(new URL(page.url()).searchParams.get("return"),"/ui-v2#device/connections/internet","confirm: return target");
      await page.waitForSelector(".readiness-hero");
      assert.deepStrictEqual(unloadPrompts,[],"confirm: no browser leave prompt");
    }finally{page.off("dialog",onDialog);await page.unroute("http://local.test/api/ui/live",expireOnce);await page.unroute("http://local.test/api/tcp-auth").catch(()=>{});}
    assert.deepStrictEqual(errors,[]);
    await browser.close();console.log("a11y-browser=ok");return;
  }
  if(process.argv.includes("--i18n-only")){
    const errors=[];page.on("pageerror",error=>errors.push(error.message));
    const dictionaries=require(root+"/i18n.js"),de=dictionaries.v2de;
    // User data that equals a dictionary entry stays verbatim in German; the literals around it stay German.
    tracking.projects.push({id:"p3",name:"Log",line_count:0,point_count:0,created_at:700,updated_at:700});
    mapLineName="Cancel";
    lineWarnings=["line_has_no_horizontal_length"];
    await page.evaluate(()=>localStorage.setItem("rtk-language","de"));
    await page.reload();
    await page.waitForSelector(".readiness-hero");
    await go(page,"projects",".project-list");
    assert.deepStrictEqual(await page.locator(".project-title b").allInnerTexts(),["Project","Archive","Log"]);
    assert.strictEqual(await page.locator(".project-title b",{hasText:/^Log$/}).count(),1,"project list shows the name Log verbatim");
    await go(page,"projects/p1",".map-workspace");
    assert.deepStrictEqual(await page.locator(".line-row b").allInnerTexts(),["Cancel"],"line list shows the name Cancel verbatim");
    await page.locator(".line-row").first().click();
    await page.waitForSelector(".line-quality h3");
    assert.strictEqual(await page.locator(".line-quality h3").innerText(),"Cancel");
    assert.strictEqual(await page.locator(".line-quality .notice").first().innerText(),dictionaries.dictionaries.de["survey.line_has_no_horizontal_length"],"line warning is translated");
    for(const [route,literals,ready="#app-main > *"] of [["status",["Satellites"]],["device/connections/access",["Encrypted pairing","NMEA over TCP","Web portal","Stream key","Rotate stream key","GNSS module","Direct web portal"],".label-grid"],["device/diagnostics/console",["Send"]]]){
      await go(page,route,"#app-main > *");
      await page.waitForSelector(ready,{state:"attached"});
      const text=await page.locator("#app-main").evaluate(node=>node.textContent);
      for(const literal of literals)assert(text.includes(de[literal]),`${route}: ${literal} stays German`);
    }
    await go(page,"device/diagnostics/log",".search-input");
    assert.strictEqual(await page.locator(".search-input").getAttribute("aria-label"),de["Search log"]);
    // #24-UI Task 6: Maintenance and the expert pages of Diagnostics are German too.
    await go(page,"device/maintenance",".maintenance-layout");
    const maintenanceText=await page.locator(".maintenance-layout").innerText(),deDict=dictionaries.dictionaries.de;
    for(const key of ["ui.guided_backup_kicker","ui.maintenance_backup_title","ui.guided_backup_help","ui.maintenance_secrets","ui.maintenance_survey_hint","ui.guided_backup_download","ui.guided_backup_restore","ui.maintenance_restart_title","ui.maintenance_restart_scope","ui.device_restart"])
      assert(maintenanceText.includes(deDict[key]),`maintenance: ${key} is German`);
    await go(page,"device/diagnostics",".diagnostic-links");
    const expertText=await page.locator(".diagnostic-links").innerText();
    for(const key of ["ui.route_rtcm","ui.route_log","ui.route_console","ui.expert_only"])assert(expertText.includes(deDict[key]),`diagnostics: ${key} is German`);
    await go(page,"device/diagnostics/console",".command-panel");
    assert.strictEqual(await page.locator('#app-main a.text-link[href="/#device/maintenance"]').innerText(),deDict["ui.console_restart_link"],"console: restart link is German");
    // #24-UI: the old settings link lands on the display page; switching the language there changes the header button and the three areas.
    await go(page,"device/settings",".settings-layout");
    assert.strictEqual(await page.evaluate(()=>location.hash),"#device/display");
    const deText=dictionaries.dictionaries.de,enText=dictionaries.dictionaries.en;
    const headerState=()=>page.evaluate(()=>{const button=document.querySelector("#sign-out");return{aria:button.getAttribute("aria-label"),title:button.title,label:button.querySelector(".header-action-label").textContent,nav:[...document.querySelectorAll("#mobile-nav a")].map(link=>link.querySelector("span:last-child").textContent).join(" · ")};});
    assert.deepStrictEqual(await headerState(),{aria:deText["ui.sign_out"],title:deText["ui.sign_out"],label:deText["ui.sign_out"],nav:[deText["app.measure"],deText["app.projects"],deText["app.device"]].join(" · ")},"German header button and areas");
    assert.notStrictEqual(deText["ui.sign_out"],enText["ui.sign_out"],"the sign-out label is translated");
    await page.getByRole("button",{name:"English"}).click();
    await page.waitForFunction(()=>document.querySelector("#sign-out").getAttribute("aria-label")==="Sign out");
    assert.deepStrictEqual(await headerState(),{aria:enText["ui.sign_out"],title:"Sign out",label:"Sign out",nav:"Measure · Projects · Device"},"English header button and areas");
    assert.strictEqual(await page.locator('.settings-layout [data-card="session"]').count(),0,"the session card is gone");
    assert.strictEqual(await page.locator(".settings-layout > .muted").first().innerText(),enText["ui.display_scope"]);
    assert.strictEqual(await page.locator('[data-card="measure-link"] a').getAttribute("href"),"/#measure/setup");
    assert((await page.locator('[data-card="measure-link"]').innerText()).includes(enText["ui.moved_to_measure"]),"the measure-link card names the moved settings");
    await page.getByRole("button",{name:"Deutsch"}).click();
    await page.waitForFunction(()=>document.querySelector("#sign-out").getAttribute("aria-label")==="Abmelden");
    assert.strictEqual(await page.locator(".settings-layout > .muted").first().innerText(),deText["ui.display_scope"]);
    await page.locator('[data-card="measure-link"] a').click();
    await page.waitForSelector(".field-setup-layout");
    // Part 2: remaining literals, attributes, plural, composed prose and templates that carry user data.
    const german=dictionaries.dictionaries,
      fill=(template,values)=>template.replace(/\{(\w+)\}/g,(_,key)=>values[key]);
    const settleRoute=async route=>{await go(page,route,"#app-main > *");await page.waitForTimeout(300);};
    const openStatus=async(selector=".field-actions button:not([disabled])")=>{await page.evaluate(()=>{location.hash="status";});await page.reload();await page.waitForSelector(selector);};
    // Phrase scan: no English phrase of the dictionaries may remain on a German screen.
    const never=new Set(["+ corrections","conditions already met"]),phrases=new Set();
    const longestFixed=value=>String(value).split(/\{\w*\}/).sort((a,b)=>b.length-a.length)[0].trim();
    const consider=(english,translated)=>{const fixed=longestFixed(english);if(fixed.split(/\s+/).length>=2&&fixed.length>=8&&translated&&translated!==english&&!translated.includes(fixed)&&!never.has(fixed))phrases.add(fixed);};
    Object.entries(de).forEach(([key,value])=>consider(key,value));
    Object.entries(german.en).forEach(([key,value])=>consider(value,german.de[key]));
    assert(phrases.size>150,"phrase candidates were collected");
    const screenText=()=>page.evaluate(()=>{const roots=[document.querySelector("#app-main"),document.querySelector("#live-state"),...document.querySelectorAll("dialog[open]")].filter(Boolean);return roots.map(root=>[root,...root.querySelectorAll("[placeholder],[aria-label],[title]")].map(node=>[node===root?root.textContent:"",node.getAttribute("placeholder"),node.getAttribute("aria-label"),node.getAttribute("title")].filter(Boolean).join("\n")).join("\n")).join("\n");});
    const scan=async context=>{const text=await screenText();assert.deepStrictEqual([...phrases].filter(phrase=>text.includes(phrase)),[],`${context}: English phrases on a German screen`);};
    // The checklist is always six open rows, labelled in German; the hero names the first server reason.
    live.current_quality.accepted=false;live.current_fix=Object.assign({},live.current_fix,{qual:1,fix_status_text:"GPS_FIX"});live.rtcm=Object.assign({},live.rtcm,{epoch_age_sec:null});
    await openStatus(".condition");
    assert.strictEqual(await page.locator(".condition").count(),6);
    assert.strictEqual(await page.locator("details.passed-conditions").count(),0);
    assert.strictEqual(await page.locator('.condition[data-gate="corrections"] b').innerText(),german.de["ui.gate_row_corrections"]);
    const firstReason=fixtureReasons(live.current_fix,live.current_quality.limits)[0];
    assert.strictEqual(await page.locator(".hero-state p").innerText(),german.de["gate."+firstReason].replace("{fix}","GPS FIX"),"hero subtitle names the first server reason in German");
    live.current_quality.accepted=true;live.current_fix=Object.assign({},live.current_fix,{qual:4,fix_status_text:"RTK_FIXED",correction_age_sec:1.2});
    await openStatus();
    await scan("status");
    for(const name of ["Start line","Object point"]){
      await page.locator(".field-actions button",{hasText:de[name]}).first().click();
      await page.waitForSelector("dialog.action-sheet[open]");
      await scan(`${name} dialog`);
      if(name==="Start line")assert.strictEqual(await page.locator('dialog.action-sheet input[name="name"]').getAttribute("placeholder"),de["e.g. Water line 01"],"modal placeholder");
      await page.keyboard.press("Escape");
      await page.waitForFunction(()=>!document.querySelector("dialog.action-sheet"));
    }
    for(const route of ["measure/setup","projects","projects/p1","device","device/diagnostics/rtcm","device/connections/access","device/diagnostics/log","device/settings","device/connections/internet","device/diagnostics","device/diagnostics/console","device/maintenance"]){
      await settleRoute(route);
      if(route==="projects/p1"){await page.locator(".point-row").first().click();await page.waitForSelector(".point-card");}
      await scan(route);
    }
    // Attributes: each one is the German text of its literal.
    for(const [route,selector,attribute,key] of [["device","nav.device-links","aria-label","Device functions"],["device/diagnostics/console",".command-input","aria-label","GNSS command"],["projects/p1",".map-panel select","aria-label","Filter object points"],["projects/p1","svg.survey-map","aria-label","Survey map with surveyed lines, object points and receiver position"],["device/diagnostics/log",".search-input","placeholder","Search log — e.g. WIFI"],["device/connections/internet",'input[name="wifi_pass"]',"placeholder","Leave empty to keep saved value"],["device/connections/internet",'textarea[name="wifi_networks"]',"placeholder","SSID or SSID:password, one network per line"]]){
      await settleRoute(route);
      assert.strictEqual(await page.locator(selector).first().getAttribute(attribute),de[key],`${route} ${selector} ${attribute}`);
    }
    await settleRoute("measure/setup");
    assert.strictEqual(await page.locator(".measurement-choice").getAttribute("aria-label"),german.de["ui.method_title"],"method choice label");
    await settleRoute("device/connections/access");
    assert.deepStrictEqual(await page.locator(".qr-graphic").evaluateAll(nodes=>nodes.map(node=>node.getAttribute("aria-label"))),[de["Portal QR code"],de["Wi-Fi QR code"]],"QR code labels");
    // Line end: the template carries the raw line name, a translated count and the number.
    live.active_line={id:"l9",name:"Cancel",vertex_count:3,length_m:2.5,state:"recording",recording_sec:12};
    await openStatus();
    await page.locator(".field-actions button",{hasText:de["Finish line"]}).first().click();
    await page.waitForSelector("dialog.action-sheet[open]");
    assert.strictEqual(await page.locator("dialog.action-sheet p.muted").innerText(),fill(de["{name} · {points} · {length} m. Recording cannot be resumed after finishing."],{name:"Cancel",points:`3 ${de["line points"]}`,length:"2,5"}),"line end description");
    await page.keyboard.press("Escape");
    await page.waitForFunction(()=>!document.querySelector("dialog.action-sheet"));
    // Undo: the template names the kind of point and keeps the label.
    live.active_line=null;live.undo_candidate={kind:"asset",label:"Valve 1"};
    await openStatus();
    await page.locator(".field-actions button",{hasText:de["Undo last point"]}).first().click();
    await page.waitForSelector("dialog.action-sheet[open]");
    assert.strictEqual(await page.locator("dialog.action-sheet p.muted").innerText(),fill(de["Remove object point “{label}”?"],{label:"Valve 1"}),"undo description");
    await page.keyboard.press("Escape");
    await page.waitForFunction(()=>!document.querySelector("dialog.action-sheet"));
    live.undo_candidate={kind:"vertex",label:"V-3"};
    await openStatus();
    await page.locator(".field-actions button",{hasText:de["Undo last point"]}).first().click();
    await page.waitForSelector("dialog.action-sheet[open]");
    assert.strictEqual(await page.locator("dialog.action-sheet p.muted").innerText(),fill(de["Remove line point “{label}”?"],{label:"V-3"}),"undo description for a line point");
    await page.keyboard.press("Escape");
    await page.waitForFunction(()=>!document.querySelector("dialog.action-sheet"));
    delete live.undo_candidate;
    // Name check in the object point dialog.
    await page.locator(".field-actions button",{hasText:de["Object point"]}).first().click();
    await page.waitForSelector("dialog.asset-sheet[open]");
    const nameInput=page.locator('dialog.asset-sheet input[name="name"]'),nameHint=page.locator('dialog.asset-sheet input[name="name"] + small');
    assert.strictEqual(await nameInput.getAttribute("placeholder"),de["Optional"]);
    assert.strictEqual(await nameHint.innerText(),de["Optional"]);
    await nameInput.fill("Valve 1");
    await page.waitForFunction(text=>document.querySelector('dialog.asset-sheet input[name="name"] + small').textContent===text,fill(de["Already used: {name}"],{name:"Valve 1"}));
    await nameInput.fill("Neu 7");
    await page.waitForFunction(text=>document.querySelector('dialog.asset-sheet input[name="name"] + small').textContent===text,de["Available"]);
    await nameInput.fill("");
    assert.strictEqual(await nameHint.innerText(),de["Optional"]);
    await page.keyboard.press("Escape");
    await page.waitForFunction(()=>!document.querySelector("dialog.asset-sheet"));
    // Control row on the project detail names the control point verbatim.
    await settleRoute("projects/p1");
    assert.strictEqual(await page.locator(".control-list .data-row span").first().innerText(),fill(de["Control · {name}"],{name:"C1"}),"control row");
    // Toasts that carry a project name come from a translated template.
    await settleRoute("projects");
    await page.locator(".project-card-wrap",{has:page.locator(".project-title b",{hasText:/^Log$/})}).getByRole("button",{name:de["Delete empty project"]}).click();
    await page.waitForSelector("dialog.action-sheet[open]");
    await page.locator('dialog.action-sheet input[name="confirm"]').fill("Log");
    await page.locator("dialog.action-sheet").getByRole("button",{name:de["Delete project"]}).click();
    await page.waitForSelector(".feedback-item .feedback-text");
    assert.strictEqual(await page.locator(".feedback-item .feedback-text").first().innerText(),fill(de["Project “{name}” deleted"],{name:"Log"}),"delete message");
    // Numbers follow the language: a decimal mark of the form "1.5 m" must not survive in the measurement areas,
    // while the exact coordinates and the position line keep their locale independent form.
    live.active_line=null;
    await openStatus();
    await page.waitForFunction(()=>/0,200 m/.test(document.querySelector(".readiness-hero")?.innerText||""));
    const numberScan=async context=>{
      const leaks=await page.evaluate(()=>[...document.querySelectorAll(".readiness-hero, .conditions, #live-state, .recording-capacity, .measurement-sheet")]
        .flatMap(root=>[root,...root.querySelectorAll("*")].filter(node=>!node.closest(".gate-softened")&&node.childElementCount===0).map(node=>node.textContent))
        .filter(value=>/\d\.\d+\s?(m|s)\b/.test(value)));
      assert.deepStrictEqual(leaks,[],`${context}: English decimal mark in a measurement area`);
    };
    await numberScan("status");
    assert(/0,200 m/.test(await page.locator(".readiness-hero").innerText()),"hero metric shows 0,200 m");
    assert(/\b100\.000 m\b/.test(await page.locator('[data-context="position"]').innerText()),"position line keeps 100.000 m");
    await settleRoute("device/diagnostics/rtcm");
    await numberScan("device/diagnostics/rtcm");
    await settleRoute("projects/p1");
    await page.locator(".point-row").first().click();
    await page.waitForSelector(".point-card");
    const pointText=await page.locator(".point-card").innerText();
    assert(pointText.includes("50.00000000")&&pointText.includes("8.00000000"),"point detail keeps the exact coordinates");
    // Part 3: a rejected device code on the German login page shows the German error and unlocks the form again.
    const {login,errors:loginErrors}=await openLoginPage(browser,{lang:"de"});
    await login.locator("#v2-device-code").fill("WRONG");
    await login.locator("button[type=submit]").click();
    await login.waitForFunction(text=>document.querySelector("#v2-login-state").textContent===text,de["Sign-in failed"]);
    assert.strictEqual(await login.locator("button[type=submit]").isDisabled(),false,"submit unlocks after a rejected code");
    assert.deepStrictEqual(loginErrors,[]);
    await login.close();
    assert.deepStrictEqual(errors,[]);
    await browser.close();console.log("i18n-browser=ok");return;
  }
  if(process.argv.includes("--label-only")){
    const errors=[];page.on("pageerror",error=>errors.push(error.message));
    const version=/ASSET_VERSION\s*=\s*"([^"]+)"/.exec(fs.readFileSync(root+"/assets.py","utf8"))[1];
    assert.notStrictEqual(version,"15.1.0");
    const hotspot=()=>page.locator(".label-card").filter({has:page.locator("h2",{hasText:"Device hotspot"})});
    // #24-UI: the old label link is an alias of the access group on Connections.
    await go(page,"device/label",".label-grid");
    assert.strictEqual(await page.evaluate(()=>location.hash),"#device/connections/access");
    assert.strictEqual(await page.locator('.connection-group[data-group="access"] .label-grid').count(),1,"QR codes in the access group");
    const card=hotspot();
    assert((await card.innerText()).includes("RTK-1A2B-SETUP"),"hotspot card shows the SSID from wifi_qr");
    assert.strictEqual(await card.locator(".qr-graphic svg").count(),1,"hotspot QR is drawn");
    const text=await page.locator('.connection-group[data-group="access"]').innerText();
    assert(!text.includes("PASS")&&!text.includes("WORD"),"password never appears as text");
    assert(!(await page.locator("#app-main").textContent()).includes("WORD"),"password never appears on the page");
    assert.strictEqual(await page.locator(`script[src="/qr.js?v=${version}"]`).count(),1);
    labelWifiQr="WIFI:T:WPA;S:;P:;;";
    await page.reload();
    await go(page,"device/connections/access",".label-grid");
    const empty=hotspot();
    assert.strictEqual(await empty.locator(".qr-graphic svg").count(),0,"no QR from an empty SSID");
    assert.strictEqual(await empty.locator(".notice.warn").count(),1);
    assert.strictEqual(await page.locator(".label-grid .qr-graphic svg").count(),1,"portal QR only");
    labelWifiQr="WIFI:T:WPA;S:RTK-1A2B-SETUP;P:PASS\\;WORD;;";
    await testConnections(page);
    assert.deepStrictEqual(errors,[]);
    await browser.close();console.log("label-browser=ok");return;
  }
  if(process.argv.includes("--log-only")){
    const errors=[];page.on("pageerror",error=>errors.push(error.message));
    // The header badge is the only place a connection state shows: record every text it had and every error form.
    await page.evaluate(()=>{
      window.__badge=[];window.__liveErrors=0;
      const badge=document.querySelector("#live-state");
      new MutationObserver(()=>{window.__badge.push(badge.textContent);if(badge.classList.contains("error"))window.__liveErrors+=1;}).observe(badge,{attributes:true,childList:true,subtree:true,characterData:true});
    });
    const settle=()=>page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
    const idle=async()=>{while(logInFlight>0)await page.waitForTimeout(20);};
    // One manual refresh: the click, the answer from the fixture and a short pause for the DOM update.
    const refreshOnce=async()=>{
      const before=logRequests;
      await page.getByRole("button",{name:"Load now"}).click();
      while(logRequests===before)await page.waitForTimeout(20);
      await idle();
      await page.waitForTimeout(150);
      await settle();
    };
    const setAuto=async on=>{
      const button=page.locator(".log-actions button").first();
      if((await button.getAttribute("aria-pressed"))!==String(on))await button.click();
    };
    const gap=()=>page.evaluate(()=>{const box=document.querySelector(".log-console");return box.scrollHeight-box.scrollTop-box.clientHeight;});
    const entryCount=()=>page.locator(".log-entry").count();
    const domMessages=()=>page.evaluate(()=>[...document.querySelectorAll(".log-entry code")].map(node=>node.textContent));
    const ringMessages=()=>logAll.slice(-1000).map(item=>item.message);
    // First entry whose bottom edge lies below the top of the console, with its offset to the console.
    const topEntry=()=>page.evaluate(()=>{
      const box=document.querySelector(".log-console").getBoundingClientRect();
      const node=[...document.querySelectorAll(".log-entry")].find(item=>item.getBoundingClientRect().bottom>box.top);
      return{text:node.querySelector("code").textContent,top:node.getBoundingClientRect().top-box.top};
    });
    const scrollTo=async (fraction,pixels)=>{
      await page.evaluate(([f,px])=>{const box=document.querySelector(".log-console");box.scrollTop=px!=null?px:(box.scrollHeight-box.clientHeight)*f;},[fraction,pixels]);
      await settle();
    };
    const exportLines=async()=>{
      const download=page.waitForEvent("download");
      await page.getByRole("button",{name:"Share filtered"}).click();
      return fs.readFileSync(await (await download).path(),"utf8").split("\n").filter(line=>line.length>0);
    };
    const counterText=()=>page.locator(".log-tools > b").innerText();
    const assertRing=async context=>{
      const ring=ringMessages();
      assert.deepStrictEqual(await domMessages(),ring,`${context}: displayed entries equal the ring`);
      assert(ring.length<=1000,`${context}: ring bounded`);
      assert((await counterText()).startsWith(`${ring.length} / `),`${context}: counter ${await counterText()}`);
    };
    logAll=[];logInitial=200;
    await go(page,"device/diagnostics/log",".log-console");
    await setAuto(false);

    // 1. Stay at the bottom while new entries arrive.
    await scrollTo(1);
    assert(await gap()<=2,"starts at the bottom");
    const bottomBefore=await page.evaluate(()=>document.querySelector(".log-console").scrollTop);
    logBurst=3;
    await refreshOnce();await refreshOnce();
    assert(await gap()<=2,"still at the bottom");
    assert((await page.evaluate(()=>document.querySelector(".log-console").scrollTop))>bottomBefore,"scrolled further down");
    assert.strictEqual((await domMessages()).at(-1),"entry "+(logAll.length-1),"last node is the newest entry");

    // 2. Existing nodes survive an append.
    await scrollTo(0.5);
    await page.evaluate(()=>{
      const box=document.querySelector(".log-console").getBoundingClientRect();
      [...document.querySelectorAll(".log-entry")].find(item=>item.getBoundingClientRect().bottom>box.top).dataset.probe="1";
    });
    const before2=await entryCount();
    await refreshOnce();
    assert.strictEqual(await page.evaluate(()=>document.querySelector('[data-probe="1"]').isConnected),true,"marked node is the same object");
    assert.strictEqual(await entryCount(),before2+3,"three nodes appended");

    // 3. The reading position holds over three refreshes.
    await scrollTo(0,100);
    const held=await topEntry();
    await refreshOnce();await refreshOnce();await refreshOnce();
    const heldAfter=await topEntry();
    assert.strictEqual(heldAfter.text,held.text,"same entry on top");
    assert(Math.abs(heldAfter.top-held.top)<=2,`position held (${held.top} -> ${heldAfter.top})`);

    // 4. Overflow with the anchor staying in the ring: entries above the viewport fall out.
    logBurst=900-logAll.length;
    await refreshOnce();
    assert.strictEqual(await entryCount(),900,"ring filled to 900");
    await scrollTo(0.6);
    const anchor=await topEntry(),anchorIndex=ringMessages().indexOf(anchor.text);
    assert(anchorIndex>=60,`anchor ${anchor.text} sits deep enough in the ring (${anchorIndex})`);
    await page.evaluate(()=>{
      const box=document.querySelector(".log-console").getBoundingClientRect();
      [...document.querySelectorAll(".log-entry")].find(item=>item.getBoundingClientRect().bottom>box.top).dataset.probe="2";
    });
    logBurst=150;
    await refreshOnce();
    await settle();
    const anchorAfter=await topEntry();
    assert.strictEqual(anchorAfter.text,anchor.text,"anchor entry stays on top after the overflow");
    assert(Math.abs(anchorAfter.top-anchor.top)<=2,`anchor position held (${anchor.top} -> ${anchorAfter.top})`);
    assert.strictEqual(await page.evaluate(()=>document.querySelector('[data-probe="2"]').isConnected),true,"anchor node is the same object");
    assert.strictEqual(await entryCount(),1000,"1000 nodes");
    assert((await counterText()).startsWith("1000 / "),"counter shows 1000");
    await assertRing("overflow");

    // 4b. The anchor itself is evicted: the first remaining entry takes its place.
    await scrollTo(0,60);
    logBurst=300;
    await refreshOnce();
    await settle();
    const evicted=await page.evaluate(()=>{
      const box=document.querySelector(".log-console").getBoundingClientRect(),first=document.querySelector(".log-entry");
      return{text:first.querySelector("code").textContent,top:first.getBoundingClientRect().top-box.top};
    });
    assert.strictEqual(evicted.text,ringMessages()[0],"first node is the oldest ring entry");
    assert(Math.abs(evicted.top)<=2,`first remaining entry at the top of the console (${evicted.top})`);
    assert(await gap()>100,"evicted anchor does not jump to the bottom");
    await assertRing("evicted anchor");

    // 5. Export equals the current ring.
    const lines=await exportLines();
    assert.strictEqual(lines.length,1000,"export has one line per ring entry");
    assert(lines[0].endsWith(ringMessages()[0])&&lines.at(-1).endsWith(ringMessages().at(-1)),"export spans oldest to newest");

    // 4c. One answer with more than 1000 entries: only entries that stay in the ring get nodes.
    await page.evaluate(()=>{
      window.__added=0;
      new MutationObserver(records=>records.forEach(record=>record.addedNodes.forEach(node=>{if(node.classList&&node.classList.contains("log-entry"))window.__added+=1;}))).observe(document.querySelector(".log-console"),{childList:true});
    });
    logBurst=1500;
    await refreshOnce();
    assert.strictEqual(await page.evaluate(()=>window.__added),1000,"1500 new entries create only 1000 nodes");
    await assertRing("big batch");
    const bigLines=await exportLines();
    assert.deepStrictEqual(bigLines.map(line=>line.replace(/^.*SYS /,"")),ringMessages(),"export equals ring after a big batch");

    // 6. A filter hides new entries and the export follows the filter.
    await page.getByRole("button",{name:"Error",exact:true}).click();
    assert.strictEqual(await entryCount(),0,"no ERROR entries");
    logBurst=5;
    await refreshOnce();
    assert.strictEqual(await entryCount(),0,"new INFO entries get no nodes under the ERROR filter");
    assert((await counterText()).startsWith("0 / "),"counter under filter");
    assert.deepStrictEqual(await exportLines(),[],"export is filtered");
    await page.getByRole("button",{name:"All",exact:true}).click();
    await assertRing("filter reset");

    // 7. The timer and "Load now" share one request in flight.
    await setAuto(true);
    logBurst=0;logDelay=3000;logMaxInFlight=0;
    const requestsBefore=logRequests;
    await page.waitForTimeout(500);
    await page.getByRole("button",{name:"Load now"}).click();
    await page.waitForTimeout(3000);
    await page.getByRole("button",{name:"Load now"}).click();
    await page.waitForTimeout(3500);
    assert.strictEqual(logMaxInFlight,1,"never two log requests at once");
    assert(logRequests-requestsBefore>=2,"requests continue after the first answer");
    logDelay=0;
    await setAuto(false);
    await idle();

    // 8. A late answer after navigating away changes nothing, not even the connection state.
    logDelay=3000;
    await page.evaluate(()=>{window.__badge.length=0;window.__liveErrors=0;});
    await page.getByRole("button",{name:"Load now"}).click();
    await go(page,"device",".device-links");
    await page.waitForTimeout(4500);
    const lateState=await page.evaluate(()=>({badge:window.__badge.filter(text=>/Log update/.test(text)),liveErrors:window.__liveErrors,entries:document.querySelectorAll(".log-entry").length}));
    assert.deepStrictEqual(lateState.badge,[],"no log connection state after navigation");
    assert.strictEqual(lateState.liveErrors,0,"badge never showed an error");
    assert.strictEqual(lateState.entries,0,"no log entries on the device page");
    logDelay=0;
    await idle();

    // 9. Failure shows the error state, the next timer tick recovers.
    await go(page,"device/diagnostics/log",".log-console");
    await page.evaluate(()=>{window.__badge.length=0;window.__liveErrors=0;});
    failLog=true;
    await page.waitForFunction(()=>window.__liveErrors>0&&window.__badge.some(text=>/Log update failed/.test(text)),null,{timeout:8000});
    const countFailed=await entryCount();
    failLog=false;logBurst=2;
    await page.waitForFunction(count=>document.querySelectorAll(".log-entry").length>=count+2,countFailed,{timeout:8000});
    logBurst=0;
    assert.strictEqual(await entryCount(),countFailed+2,"two entries appended after recovery");
    await page.waitForFunction(()=>!document.querySelector("#live-state").classList.contains("error"),null,{timeout:8000});
    await setAuto(false);
    await idle();

    // 10. A new generation replaces the list and scrolls to the end.
    await scrollTo(0,0);
    logGeneration=1;logBurst=4;
    await refreshOnce();
    logBurst=0;
    assert.deepStrictEqual(await domMessages(),["entry 0","entry 1","entry 2","entry 3"],"only the new generation is shown");
    assert(await gap()<=2,"scrolled to the end");
    assert.deepStrictEqual((await exportLines()).map(line=>line.replace(/^.*SYS /,"")),["entry 0","entry 1","entry 2","entry 3"],"export equals the new generation");

    assert.strictEqual(logMaxInFlight,1,"never two log requests at once (whole run)");
    assert.deepStrictEqual(errors,[]);
    await browser.close();console.log("log-browser=ok");return;
  }
  if(process.argv.includes("--router-cache-only")){
    // #29b Task 3: the Measure screen updates in place on every poll. Buttons keep their nodes and the focus, the layout
    // does not shift, and capture (#21c), connection loss (#21b) and a measurement started elsewhere lock and free the
    // same buttons. Every step counts answered /api/ui/live responses (poll only; no live stream on this branch).
    const errors=[],failures=[];page.on("pageerror",error=>errors.push(error.message));
    const i18n=require(root+"/i18n.js"),en=i18n.dictionaries.en;
    const H30="The measurement is still waiting for stable values.";
    const H60="The measurement has taken more than one minute. Check the antenna view and correction stream.";
    for(const text of [H30,H60])assert(text in i18n.v2de,"hint text comes from the localText source: "+text);
    const waitFor=async(check,message,timeout=15000)=>{const end=Date.now()+timeout;while(!check()){if(Date.now()>end)assert.fail(message);await page.waitForTimeout(50);}};
    // n answered polls (one every 2 s on Measure), then a short pause so the page has rendered the last one.
    const answered=async(count,label)=>{const start=liveAnswered;await waitFor(()=>liveAnswered>=start+count,`${label}: ${count} answered live poll(s)`,5000+count*3000);await page.waitForTimeout(300);};
    const saved={fix:structuredClone(live.current_fix),accepted:live.current_quality.accepted,line:live.active_line,project:structuredClone(live.active_project),
      progress:structuredClone(live.measurement_progress),streak:structuredClone(live.quality_streak)};
    const BAR=".field-actions > button";
    const markBar=selector=>page.evaluate(selector=>{const list=[...document.querySelectorAll(selector)];list.forEach((node,index)=>{node.__probe=index;});window.__marked=list;return list.length;},selector);
    const marks=selector=>page.evaluate(selector=>({kept:window.__marked.every(node=>node.isConnected),same:window.__marked.length===document.querySelectorAll(selector).length,
      count:window.__marked.length,focus:document.activeElement?document.activeElement.__probe??null:null}),selector);
    const assertMarked=async(selector,label)=>{const state=await marks(selector);assert(state.count>0&&state.kept&&state.same,`${label}: the marked buttons are still the page's buttons ${JSON.stringify(state)}`);return state;};
    const barState=()=>page.evaluate(()=>[...document.querySelectorAll(".field-actions button")].map(button=>({text:button.textContent,disabled:button.disabled,
      cancel:button.hasAttribute("data-measurement-cancel"),dismiss:button.hasAttribute("data-feedback-dismiss"),danger:button.classList.contains("danger"),top:button.parentElement.classList.contains("field-actions")})));
    const focusedText=()=>page.evaluate(()=>{const node=document.activeElement;return node&&node.closest(".field-actions")?node.textContent:null;});
    // Keyboard focus: Tab from the top of the page until the named button of the action bar has the focus.
    const tabTo=async(text,label)=>{await page.evaluate(()=>{document.activeElement&&document.activeElement.blur();scrollTo(0,0);});
      for(let step=0;step<80;step+=1){if(await focusedText()===text)return;await page.keyboard.press("Tab");}assert.fail(`${label}: "${text}" not reachable with Tab`);};
    const ready=()=>page.waitForFunction(()=>[...document.querySelectorAll(".field-actions > button")].some(node=>node.textContent==="Add line point"&&!node.disabled),null,{timeout:15000});
    const step=async(name,run)=>{console.log("router-cache: "+name);try{await run();}catch(error){failures.push(`${name}: ${error.message}`);console.log(`router-cache: FAILED ${name}: ${error.message}`);}};
    try{
      live.current_fix=Object.assign({},saved.fix,{qual:4,fix_status_text:"RTK_FIXED",stale:false,fix_seq:1});
      live.current_quality.accepted=true;clearVerdict();
      live.active_project={id:"p1",name:"Project",line_count:1,point_count:1};
      live.active_line={id:"l7",name:"Line R",state:"recording",recording_mode:"survey",vertex_count:3,length_m:4.2,recording_sec:30};
      live.measurement_progress={active:false,collected:0,total:5};
      liveVary=true;
      await page.setViewportSize({width:360,height:800});
      await setLanguage(page,"en");
      await ready();
      await step("(a) identity and focus over 10 polls, 360 EN",async()=>{
        await markBar(".field-actions button");
        await tabTo("Object point","(a)");
        const focus=await page.evaluate(()=>document.activeElement.__probe);
        await answered(10,"(a)");
        const state=await assertMarked(".field-actions button","(a)");
        assert.strictEqual(state.focus,focus,"(a) the focus stays on Object point");
      });
      for(const [lang,width] of [["en",360],["de",360],["en",1280]])await step(`(b) layout shift over 10 polls, ${width} ${lang.toUpperCase()}`,async()=>{
        await page.setViewportSize({width,height:800});
        await setLanguage(page,lang);
        await page.waitForFunction(()=>document.querySelectorAll(".field-actions > button").length===5);
        await page.waitForTimeout(2000);
        await page.evaluate(()=>{window.__cls=0;});
        await answered(10,"(b)");
        const cls=await page.evaluate(()=>window.__cls);
        console.log(`router-cache: CLS ${width} ${lang} = ${cls.toFixed(5)}`);
        assert(cls<0.01,`(b) ${width} ${lang}: layout shift ${cls} must stay below 0.01`);
      });
      await page.setViewportSize({width:360,height:800});
      await setLanguage(page,"en");
      await ready();
      await step("(c) a running capture locks the bar, the answer frees the same buttons",async()=>{
        captureState={epoch:"a1b2c3d4",stack:[],order:40};actionAnswers={add_asset:{delay:6000}};
        await markBar(BAR);
        await page.evaluate(()=>[...document.querySelectorAll(".field-actions > button")].find(node=>node.textContent==="Object point").click());
        await page.locator("dialog.asset-sheet").waitFor();
        await page.locator("dialog.asset-sheet").getByRole("button",{name:"Save point"}).click();
        // Live polls pause while the own measurement runs; the progress poll answers instead.
        const progressCount=()=>page.evaluate(()=>window.__progressAnswers.length);
        const start=await progressCount();
        for(let n=1;n<=3;n+=1){
          await page.waitForFunction(count=>window.__progressAnswers.length>=count,start+n,{timeout:15000});
          const open=(await barState()).filter(button=>!button.cancel&&!button.dismiss&&!button.disabled);
          assert.deepStrictEqual(open,[],`(c) progress answer ${n}: every button but cancel and dismiss is locked`);
        }
        await page.waitForSelector('.field-actions .capture-strip .feedback-item.ok',{timeout:15000});
        await answered(1,"(c) after the answer");
        const top=(await barState()).filter(button=>button.top);
        assert(top.length>0&&top.every(button=>!button.disabled),`(c) free again after one answered poll ${JSON.stringify(top)}`);
        await assertMarked(BAR,"(c)");
      });
      captureState=null;actionAnswers={};
      await step("(d) a lost connection locks the bar, the next answer frees the same buttons",async()=>{
        await ready();
        await markBar(BAR);
        blockLive();
        try{
          await page.evaluate(value=>{window.__rtkClockShift+=value;},12000);
          await page.waitForFunction(()=>document.querySelector(".readiness-hero").classList.contains("stale-critical"),null,{timeout:15000});
          const top=(await barState()).filter(button=>button.top);
          assert(top.length>0&&top.every(button=>button.disabled),`(d) every action is locked while the link is lost ${JSON.stringify(top)}`);
          assert.strictEqual(await page.locator(".field-actions .action-reason").innerText(),en["ui.live_lost_action"],"(d) the loss reason is shown");
        }finally{releaseLive();}
        await answered(1,"(d) after the release");
        await page.waitForFunction(()=>!document.querySelector(".readiness-hero").classList.contains("stale-critical"),null,{timeout:5000});
        const top=(await barState()).filter(button=>button.top);
        assert(top.every(button=>!button.disabled),`(d) free again after one answered poll ${JSON.stringify(top)}`);
        assert.strictEqual(await page.locator(".field-actions .action-reason").count(),0,"(d) the loss reason is gone");
        await assertMarked(BAR,"(d)");
      });
      await step("(e) a measurement started elsewhere comes and goes in place",async()=>{
        await ready();
        await markBar(BAR);
        const progress=elapsed=>({active:true,phase:"collecting",mode:"fixed",collected:2,total:5,elapsed_sec:elapsed,rejected_samples:0});
        const resume=()=>page.evaluate(()=>{const block=document.querySelector(".field-actions .measurement-resume");if(!block)return null;
          const cancel=block.querySelector("[data-measurement-cancel]"),hint=[...block.querySelectorAll("small")].find(node=>!node.hidden&&node.textContent);
          return{same:Boolean(window.__cancel)&&cancel===window.__cancel,hint:hint?hint.textContent:null,focused:document.activeElement===cancel&&Boolean(cancel)};});
        const lockedActions=async label=>{const top=(await barState()).filter(button=>button.top);
          assert(top.length===5&&top.every(button=>button.disabled===!button.danger),`${label}: actions locked, danger actions free ${JSON.stringify(top)}`);};
        live.measurement_progress=progress(3);
        await answered(1,"(e) started");
        assert(await page.evaluate(()=>{window.__cancel=document.querySelector(".field-actions .measurement-resume [data-measurement-cancel]");return Boolean(window.__cancel);}),"(e) the resume block appears after one answer");
        assert.strictEqual((await resume()).hint,null,"(e) no hint at 3 s");
        await lockedActions("(e) 3 s");
        await tabTo("Cancel measurement","(e)");
        live.measurement_progress=progress(31);
        await answered(1,"(e) 31 s");
        let state=await resume();
        assert(state&&state.same,"(e) 31 s: the same resume block");
        assert.strictEqual(state.hint,H30,"(e) 31 s: the 30 s hint");
        assert(state.focused,"(e) 31 s: the cancel button keeps the focus");
        await lockedActions("(e) 31 s");
        live.measurement_progress=progress(61);
        for(let n=1;n<=3;n+=1){
          await answered(1,`(e) 61 s answer ${n}`);
          state=await resume();
          assert(state&&state.same,`(e) 61 s answer ${n}: the same resume block`);
          assert.strictEqual(state.hint,H60,`(e) 61 s answer ${n}: the 60 s hint`);
          assert(state.focused,`(e) 61 s answer ${n}: the cancel button keeps the focus`);
        }
        await lockedActions("(e) 61 s");
        live.measurement_progress={active:false,collected:0,total:5};
        await answered(1,"(e) finished");
        assert.strictEqual(await resume(),null,"(e) the resume block is gone after one answer");
        const top=(await barState()).filter(button=>button.top);
        assert(top.every(button=>!button.disabled),`(e) actions free ${JSON.stringify(top)}`);
        assert(await page.evaluate(()=>document.activeElement===document.querySelector(".field-actions > button:not([disabled])")),"(e) the focus moves to the first free action");
        await answered(3,"(e) settled");
        await assertMarked(BAR,"(e) after 10 answered polls");
      });
      await step("(f) the cancel button comes back after a lost connection and after a failed cancel",async()=>{
        // Review #29b (2): the cancel button keeps its node; its lock follows the link and its own pending request.
        await ready();
        const CANCEL=".field-actions .measurement-resume [data-measurement-cancel]";
        const cancelState=()=>page.evaluate(selector=>{const node=document.querySelector(selector);return node?{disabled:node.disabled,same:node===window.__cancelF}:null;},CANCEL);
        try{
          live.measurement_progress={active:true,phase:"collecting",mode:"fixed",collected:2,total:5,elapsed_sec:5,rejected_samples:0};
          await answered(1,"(f) started");
          assert(await page.evaluate(selector=>{window.__cancelF=document.querySelector(selector);return Boolean(window.__cancelF);},CANCEL),"(f) the resume block is there");
          assert.deepStrictEqual(await cancelState(),{disabled:false,same:true},"(f) cancel is usable with fresh data");
          blockLive();
          try{
            await page.evaluate(value=>{window.__rtkClockShift+=value;},12000);
            await page.waitForFunction(()=>document.querySelector(".readiness-hero").classList.contains("stale-critical"),null,{timeout:15000});
            assert.deepStrictEqual(await cancelState(),{disabled:true,same:true},"(f) cancel is locked while the link is lost");
          }finally{releaseLive();}
          await answered(1,"(f) after the release");
          await page.waitForFunction(()=>!document.querySelector(".readiness-hero").classList.contains("stale-critical"),null,{timeout:5000});
          assert.deepStrictEqual(await cancelState(),{disabled:false,same:true},"(f) cancel is usable again with fresh data");
          const post=holdNext((path,method)=>path==="/api/tracking"&&method==="POST",500);
          await page.locator(CANCEL).click();
          await post.arrived;
          await answered(1,"(f) during the cancel request");
          assert.deepStrictEqual(await cancelState(),{disabled:true,same:true},"(f) cancel stays locked while its request is pending");
          post.release();
          await page.waitForSelector(".feedback-item.critical",{timeout:5000});
          const error=await page.locator(".feedback-item.critical").first().innerText();
          assert(error.includes("Device busy"),`(f) the failed cancel shows its error: ${error}`);
          await answered(1,"(f) after the failed cancel");
          assert.deepStrictEqual(await cancelState(),{disabled:false,same:true},"(f) cancel is usable again after the failure");
        }finally{
          live.measurement_progress={active:false,collected:0,total:5};
          await page.evaluate(()=>document.querySelectorAll(".feedback-item.critical [data-feedback-dismiss]").forEach(button=>button.click()));
          await answered(1,"(f) finished");
        }
      });
      // #29b Task 5: the library and the archive reopen at once from the last load, marked with its time until the
      // network confirms them; Measure never shows a skeleton, uncached pages still do; own mutations forget the cache.
      const savedTracking={projects:structuredClone(tracking.projects),archives:tracking.archives};
      try{
        liveVary=false;projectDelay=0;
        tracking.archives=structuredClone(mobileArchives.slice(0,2));
        const asOf=lang=>String(i18n.dictionaries[lang]["ui.cached_as_of"]).split("{time}")[0];
        const skeletonSeen=()=>page.evaluate(()=>window.__skeletonSeen);
        // Navigates in the page and measures until selector is there; the skeleton flag starts false.
        const shownWithin=(hash,selector)=>page.evaluate(async([hash,selector])=>{window.__skeletonSeen=false;const start=performance.now();location.hash=hash;
          while(!document.querySelector(selector)){if(performance.now()-start>5000)return null;await new Promise(resolve=>setTimeout(resolve,5));}return performance.now()-start;},[hash,selector]);
        const stampState=()=>page.evaluate(()=>{const stamp=document.querySelector("#app-main .stale-stamp");if(!stamp)return null;const button=stamp.querySelector("button"),box=button&&button.getBoundingClientRect(),
          text=stamp.querySelector(".stale-stamp-text");return{state:stamp.dataset.stamp,text:(text||stamp).textContent,visibility:getComputedStyle(stamp).visibility,probe:stamp.__probe??null,
          button:button?button.textContent:null,buttonVisible:Boolean(button&&!button.hidden&&button.getClientRects().length),buttonWidth:box?box.width:0,buttonHeight:box?box.height:0};});
        const FRESH='#app-main .stale-stamp[data-stamp="fresh"]';
        const views=[["projects",".project-list",".project-card .project-title b",()=>{tracking.projects[1].name="Archiv2";},"Archiv2"],
          ["archive",".archive-library",".archive-card .archive-info b",()=>{tracking.archives[0].name="New";},"New"]];
        for(const [hash,selector,names,change,changedName] of views)for(const changed of [false,true])
          await step(`(a) #${hash} reopens from the cache, ${changed?"changed":"same"} data`,async()=>{
            projectDelay=0;
            await go(page,hash,selector);
            await go(page,"measure",".readiness-hero");
            projectDelay=4000;
            const within=await shownWithin(hash,selector);
            assert(within!==null&&within<300,`(a) ${hash}: the cached page within 300 ms (${within} ms)`);
            assert.strictEqual(await skeletonSeen(),false,`(a) ${hash}: no skeleton`);
            await page.evaluate(selector=>{const stamp=document.querySelector("#app-main .stale-stamp");if(stamp)stamp.__probe="stamp";
              const list=document.querySelector(selector);list.__probe="list";window.__listTop=list.getBoundingClientRect().top;},selector);
            let state=await stampState();
            assert(state&&state.state==="refreshing"&&state.visibility==="visible",`(a) ${hash}: the stamp says refreshing ${JSON.stringify(state)}`);
            assert(asOf("en").length>0&&state.text.startsWith(asOf("en")),`(a) ${hash}: the stamp names the time ${JSON.stringify(state)}`);
            if(changed)change();
            await page.waitForSelector(FRESH,{state:"attached",timeout:10000});
            projectDelay=0;
            state=await stampState();
            assert.strictEqual(state.probe,"stamp",`(a) ${hash}: the same stamp node`);
            assert.strictEqual(state.visibility,"hidden",`(a) ${hash}: the fresh stamp is invisible and keeps its place`);
            const after=await page.evaluate(selector=>{const list=document.querySelector(selector);return{probe:list.__probe??null,shift:Math.abs(list.getBoundingClientRect().top-window.__listTop)};},selector);
            assert(after.shift<=0.5,`(a) ${hash}: the list keeps its place (${after.shift} px)`);
            if(changed){
              const shown=await page.$$eval(names,nodes=>nodes.map(node=>node.textContent));
              assert(shown.includes(changedName),`(a) ${hash}: the new name is shown ${JSON.stringify(shown)}`);
            }else assert.strictEqual(after.probe,"list",`(a) ${hash}: same data keeps the list node`);
            await go(page,"measure",".readiness-hero");
          });
        await step("(a) the German stamp",async()=>{
          await setLanguage(page,"de");
          try{
            await go(page,"projects",".project-list");
            await go(page,"measure",".readiness-hero");
            projectDelay=4000;
            await shownWithin("projects",".project-list");
            const state=await stampState();
            assert(state&&asOf("de").length>0&&state.text.startsWith(asOf("de")),`(a) DE: ${JSON.stringify(state)}`);
            await page.waitForSelector(FRESH,{state:"attached",timeout:10000});
          }finally{projectDelay=0;await setLanguage(page,"en");}
        });
        await step("(a3) a cached library whose live telemetry changed keeps its nodes and the focus",async()=>{
          // Review #29b (3): /api/tracking also carries the fix, the streak, the measurement progress and the active line's
          // timing; they change on every read, the library does not show them.
          const keys=["current_fix","quality_streak","measurement_progress","storage_operation","revision","active_line"],saved=Object.fromEntries(keys.map(key=>[key,tracking[key]]));
          let reads=0;
          tracking.active_line={id:"l1",name:"Line",state:"recording",recording_sec:0,vertex_count:0,length_m:0};
          onTrackingRead=()=>{reads+=1;Object.assign(tracking,{current_fix:{qual:4,sats:10+reads,fix_age_ms:100*reads},quality_streak:{gate_streak_sec:reads},
            measurement_progress:{active:false,collected:reads,total:5},storage_operation:{kind:"none",checked_at:reads},revision:100+reads});
            Object.assign(tracking.active_line,{recording_sec:reads*7,vertex_count:reads,length_m:reads*1.5});};
          try{
            projectDelay=0;
            await go(page,"projects",".project-list");
            await go(page,"measure",".readiness-hero");
            projectDelay=3000;
            await shownWithin("projects",".project-list");
            await page.locator("details.data-management > summary").click();
            await page.locator("details.data-management").getByRole("button",{name:"Optimize storage",exact:true}).focus();
            await page.evaluate(()=>{document.querySelector(".project-list").__probe="list";document.querySelector("details.data-management").__probe="management";});
            await page.waitForSelector(FRESH,{state:"attached",timeout:10000});
            projectDelay=0;
            const after=await page.evaluate(()=>{const details=document.querySelector("details.data-management"),node=document.activeElement;
              return{list:document.querySelector(".project-list").__probe??null,management:details.__probe??null,open:details.open,focus:node?node.textContent:null,inside:details.contains(node)};});
            assert.deepStrictEqual(after,{list:"list",management:"management",open:true,focus:"Optimize storage",inside:true},"(a3) same nodes, backup management open, focus kept");
            assert.strictEqual((await stampState()).visibility,"hidden","(a3) the stamp turned fresh");
            assert(reads>=2,`(a3) the cached and the fresh read differ in telemetry (${reads} reads)`);
          }finally{
            onTrackingRead=null;projectDelay=0;
            for(const key of keys){if(saved[key]===undefined)delete tracking[key];else tracking[key]=saved[key];}
            await go(page,"measure",".readiness-hero");
          }
        });
        await step("(a2) Measure returns without a skeleton",async()=>{
          await go(page,"projects",".project-list");
          const within=await shownWithin("measure",".field-actions");
          assert(within!==null&&within<300,`(a2) Measure within 300 ms (${within} ms)`);
          assert.strictEqual(await skeletonSeen(),false,"(a2) no skeleton on Measure");
          assert.strictEqual(await page.locator("#app-main .stale-stamp").count(),0,"(a2) no stamp on Measure");
        });
        for(const [hash,selector] of [["device",".device-links"],["device/diagnostics",".diagnostics-summary"]])
          await step(`(b) #${hash} is not cached and shows its skeleton again`,async()=>{
            await go(page,hash,selector);
            await go(page,"measure",".readiness-hero");
            await page.evaluate(()=>{window.__skeletonSeen=false;});
            await go(page,hash,selector);
            assert.strictEqual(await skeletonSeen(),true,`(b) ${hash}: the skeleton shows`);
            assert.strictEqual(await page.locator("#app-main .stale-stamp").count(),0,`(b) ${hash}: no stamp`);
            await go(page,"measure",".readiness-hero");
          });
        await step("(c) an own rename or delete never shows the old project",async()=>{
          mutateProjects=true;
          const watchCards=()=>page.evaluate(()=>{window.__cardNames=[];if(window.__cardObserver)window.__cardObserver.disconnect();
            const seen=()=>document.querySelectorAll("#app-main .project-card .project-title b").forEach(node=>window.__cardNames.push(node.textContent));
            window.__cardObserver=new MutationObserver(seen);window.__cardObserver.observe(document.querySelector("#app-main"),{childList:true,subtree:true,characterData:true});});
          const cardNames=()=>page.evaluate(()=>{window.__cardObserver.disconnect();return window.__cardNames;});
          const oldName=tracking.projects[0].name;
          await go(page,"projects",".project-list");
          await go(page,"projects/p1",".map-workspace");
          await page.getByRole("button",{name:"Rename",exact:true}).click();
          await page.waitForSelector("dialog.action-sheet[open]");
          await page.locator('dialog.action-sheet input[name="name"]').fill("Renamed");
          await watchCards();
          await page.locator("dialog.action-sheet").getByRole("button",{name:"Rename",exact:true}).click();
          await page.waitForFunction(()=>location.hash==="#projects");
          await page.waitForSelector(".project-card .project-title b >> text=Renamed");
          let seen=await cardNames();
          assert(!seen.includes(oldName),`(c) the old name never shows after the rename ${JSON.stringify(seen)}`);
          const deleted=tracking.projects[1].name;
          await go(page,"projects/p2",".map-workspace");
          await page.getByRole("button",{name:"Delete project",exact:true}).click();
          await page.waitForSelector("dialog.action-sheet[open]");
          await page.locator('dialog.action-sheet input[name="confirm"]').fill(deleted);
          await watchCards();
          await page.locator("dialog.action-sheet").getByRole("button",{name:"Delete project",exact:true}).click();
          await page.waitForFunction(()=>location.hash==="#projects");
          await page.waitForFunction(()=>document.querySelector("#app-main").getAttribute("aria-busy")==="false"&&document.querySelector(".project-list"));
          seen=await cardNames();
          assert(seen.length>0&&!seen.includes(deleted),`(c) the deleted project never shows ${JSON.stringify(seen)}`);
          mutateProjects=false;
          await go(page,"measure",".readiness-hero");
        });
        await step("(d) a failed update keeps the cached library with a retry",async()=>{
          await go(page,"projects",".project-list");
          await go(page,"measure",".readiness-hero");
          trackingFail={status:503,body:{error:"storage_busy",message:"Storage maintenance in progress. Please wait."}};
          try{
            await page.evaluate(()=>{location.hash="projects";});
            await page.waitForSelector('#app-main .stale-stamp[data-stamp="failed"]',{timeout:10000});
            const state=await stampState();
            assert(state.text.endsWith(en["ui.cached_failed"]),`(d) the stamp says the update failed ${JSON.stringify(state)}`);
            assert(state.button==="Try again"&&state.buttonVisible&&state.buttonWidth>=47.5&&state.buttonHeight>=47.5,`(d) a 48 px retry ${JSON.stringify(state)}`);
            assert.strictEqual(await page.locator(".project-list").count(),1,"(d) the list stays");
            assert.strictEqual(await page.locator("#app-main .notice.critical").count(),0,"(d) no error page");
          }finally{trackingFail=null;}
          await page.locator("#app-main .stale-stamp").getByRole("button",{name:"Try again",exact:true}).click();
          await page.waitForSelector(FRESH,{state:"attached",timeout:10000});
          await go(page,"measure",".readiness-hero");
        });
        await step("(e) leaving during the update changes nothing on the next page",async()=>{
          await go(page,"projects",".project-list");
          await go(page,"measure",".readiness-hero");
          projectDelay=3000;
          await page.evaluate(()=>{location.hash="projects";});
          await page.waitForSelector(".project-list");
          await go(page,"device",".device-links");
          await page.evaluate(()=>{document.querySelector(".device-links").__probe="device";});
          await page.waitForTimeout(3500);
          projectDelay=0;
          assert.strictEqual(await page.locator(".project-list").count(),0,"(e) no library on the device page");
          assert.strictEqual(await page.evaluate(()=>document.querySelector(".device-links")?.__probe??null),"device","(e) the device page is unchanged");
          assert.strictEqual(await page.locator('.stale-stamp[data-stamp="failed"]').count(),0,"(e) no failed stamp");
          await go(page,"measure",".readiness-hero");
        });
        // #29b Task 6: the project map reopens from the cache with its tools, layers and selection, takes new points without
        // rebuilding its tools, ends when the project is gone and reloads in full when the device history breaks (full answer
        // to a delta request, a lower revision, a new boot, an own restore). The map model serves revisions like the device.
        const accepted={precision:"e8",fix_quality:4,fix_status:"RTK_FIXED",quality:{accepted:true,reasons:[]}};
        const mapPoint=(id,name,offset,extra)=>({id,type:"valve",name,coordinate:[800000000+offset,5000000000+offset,100000],measurement:{...accepted},...extra});
        const MAP_OLD={p1:{lines:[{id:"l1",name:"Line",state:"finished",vertex_count:2,length_m:15.7,coordinates:[[800000000,5000000000,100000],[800100000,5000100000,101000]]}],
          points:[mapPoint("c1","Cache A",0,{measurement:structuredClone(deltaPoint.measurement)}),mapPoint("c2","Cache B",50000)]},
          p2:{lines:[],points:[mapPoint("d1","Second A",20000)]}};
        const mapReset=()=>{Object.assign(mapModel,{on:true,revision:50,maps:structuredClone(MAP_OLD),changes:[],restore:null});mapFullOnDelta=false;mapDelay=0;projectDelay=0;
          tracking.projects=[{id:"p1",name:"Project",line_count:1,point_count:2,created_at:900,updated_at:990},{id:"p2",name:"Archive",line_count:0,point_count:1,created_at:800,updated_at:850}];};
        const savedBoot=live.system.boot_id;
        const mapCounts=()=>({full:mapRequests,delta:mapDeltaRequests});
        const lastMapSearch=()=>requestLog.filter(entry=>entry.path==="/api/tracking/map").pop().search;
        const mapNames=()=>page.$$eval("#app-main .map-point",nodes=>nodes.map(node=>node.getAttribute("aria-label").split(": ").slice(1).join(": ")).sort());
        const listNames=()=>page.$$eval("#app-main .point-list .point-row b",nodes=>nodes.map(node=>node.textContent).sort());
        const namesAre=(names,label)=>page.waitForFunction(names=>JSON.stringify([...document.querySelectorAll("#app-main .map-point")].map(node=>node.getAttribute("aria-label").split(": ").slice(1).join(": ")).sort())===JSON.stringify(names),
          [...names].sort(),{timeout:10000}).catch(async()=>assert.fail(`${label}: map points ${JSON.stringify(await mapNames())} expected ${JSON.stringify([...names].sort())}`));
        const mapStamp=()=>page.evaluate(()=>{const stamp=document.querySelector("#app-main .map-canvas .stale-stamp");return stamp?{state:stamp.dataset.stamp,text:stamp.querySelector(".stale-stamp-text").textContent}:null;});
        const MAP_FRESH='#app-main .map-canvas .stale-stamp[data-stamp="fresh"]';
        const mapFresh=label=>page.waitForSelector(MAP_FRESH,{state:"attached",timeout:10000}).catch(async()=>assert.fail(`${label}: the stamp turns fresh ${JSON.stringify(await mapStamp())}`));
        // Opens a project page from Measure and waits until its map shows the named points and one more poll has answered.
        const openMap=async(hash,names)=>{await go(page,"measure",".readiness-hero");await go(page,hash,".map-workspace");if(names)await namesAre(names,hash);await answered(1,hash);};
        // A reload empties the route cache; it starts on Measure.
        const reload=async()=>{await page.evaluate(()=>{location.hash="measure";});await page.reload();await page.waitForSelector(".readiness-hero");};
        try{
          mapReset();
          await reload();
          await step("(a) the project map reopens from the cache with zoom, layers and selection",async()=>{
            await openMap("projects/p1",["Cache A","Cache B"]);
            await page.locator(".map-tools").getByRole("button",{name:"+",exact:true}).click();
            await page.waitForFunction(()=>document.querySelector(".map-zoom").textContent==="150 %");
            await page.locator(".map-menu > summary").click();
            await page.locator(".map-layers").getByRole("button",{name:"Points",exact:true}).click();
            await page.waitForFunction(()=>document.querySelector(".map-menu-state").textContent==="2/3");
            await page.locator(".map-line").first().dispatchEvent("click");
            await page.waitForSelector(".line-row.selected");
            await page.waitForSelector(".line-quality");
            await go(page,"measure",".readiness-hero");
            const before=mapCounts(),trackingAsked=requestLog.length,liveBefore=liveAnswered;
            projectDelay=4000;
            const within=await shownWithin("projects/p1",".map-workspace");
            assert(within!==null&&within<300,`(a) the cached map within 300 ms (${within} ms)`);
            assert.strictEqual(await skeletonSeen(),false,"(a) no skeleton");
            let stamp=await mapStamp();
            assert(stamp&&stamp.state==="refreshing"&&stamp.text.startsWith(asOf("en")),`(a) the stamp names the time of the last load ${JSON.stringify(stamp)}`);
            assert.strictEqual(await page.locator(".map-zoom").textContent(),"150 %","(a) the zoom stays");
            assert.strictEqual(await page.locator(".map-menu-state").textContent(),"2/3","(a) the layers stay");
            await page.waitForSelector(".line-row.selected",{timeout:5000});
            assert.strictEqual(await page.locator("#app-main .map-line.selected").count(),1,"(a) the line stays selected on the map");
            await page.locator(".point-row",{hasText:"Cache A"}).click();
            const profile=await page.locator(".point-detail .gate-profile").innerText();
            assert(profile.includes("1a2b3c4d")&&profile.includes("sat"),`(a) the gate profile comes with its limits from the cached map: ${profile}`);
            // The stamp needs the first map round and the tracking answer; the tracking answer is held for 4 s.
            await waitFor(()=>liveAnswered>liveBefore,"(a) one answered live poll",5000);
            await page.waitForTimeout(800);
            stamp=await mapStamp();
            assert.strictEqual(stamp.state,"refreshing","(a) a live answer alone does not confirm the cached map");
            const overlap=await page.evaluate(()=>{const box=node=>node.getBoundingClientRect(),hit=(a,b)=>a.left<b.right&&b.left<a.right&&a.top<b.bottom&&b.top<a.bottom,
              stamp=box(document.querySelector(".map-canvas .stale-stamp"));return{chip:hit(stamp,box(document.querySelector(".quality-chip"))),tools:hit(stamp,box(document.querySelector(".map-tools"))),width:stamp.width};});
            assert(overlap.width>0&&!overlap.chip&&!overlap.tools,`(a) the stamp overlaps neither the chip nor the tools ${JSON.stringify(overlap)}`);
            await mapFresh("(a)");
            projectDelay=0;
            assert(requestLog.slice(trackingAsked).some(entry=>entry.path==="/api/tracking"&&entry.method==="GET"&&!entry.search),"(a) the tracking state was read");
            assert.deepStrictEqual(mapCounts(),before,"(a) no map read for an unchanged revision");
            // Back to the defaults for the next steps: zoom 100 %, every layer on.
            await page.locator(".map-tools").getByRole("button",{name:"Fit project",exact:true}).click();
            await page.locator(".map-layers").getByRole("button",{name:"Points",exact:true}).click();
            await page.waitForFunction(()=>document.querySelector(".map-menu-state").textContent==="3/3"&&document.querySelector(".map-zoom").textContent==="100 %");
          });
          await step("(b) a delta adds points in place, on the page and on return",async()=>{
            await openMap("projects/p1",["Cache A","Cache B"]);
            await page.waitForSelector(".line-row.selected");
            await page.waitForSelector(".line-quality");
            await page.evaluate(()=>{document.querySelector(".map-tools").__probe="tools";document.querySelector("details.map-menu").__probe="menu";});
            const zoom=await page.locator(".map-zoom").textContent(),before=mapCounts(),since=mapModel.revision;
            mapModelAddPoint("p1",mapPoint("c3","Cache C",80000));
            mapModelAddVertex("p1","l1",[800150000,5000150000,101500]);
            await namesAre(["Cache A","Cache B","Cache C"],"(b) on the page");
            await page.waitForTimeout(500);
            assert.deepStrictEqual(mapCounts(),{full:before.full,delta:before.delta+1},"(b) exactly one delta request");
            assert(lastMapSearch().includes(`since_revision=${since}`),`(b) the delta asks since ${since}: ${lastMapSearch()}`);
            assert.deepStrictEqual(await listNames(),["Cache A","Cache B","Cache C"],"(b) the point list has the new point");
            const panel=await page.evaluate(()=>({summary:[...document.querySelectorAll(".map-panel details > summary")].map(node=>node.textContent),
              counts:document.querySelector(".map-panel h2 + p.muted").textContent,vertices:document.querySelector(".map-line .map-stroke").getAttribute("points").trim().split(" ").length,
              tools:document.querySelector(".map-tools").__probe??null,menu:document.querySelector("details.map-menu").__probe??null,
              selected:document.querySelectorAll(".line-row.selected").length,lineDetail:document.querySelectorAll(".line-quality").length,zoom:document.querySelector(".map-zoom").textContent}));
            assert(panel.summary.includes("3 object points"),`(b) the summary counts the new point ${JSON.stringify(panel)}`);
            assert(panel.counts.includes("3 object points"),`(b) the project counts follow ${JSON.stringify(panel)}`);
            assert.strictEqual(panel.vertices,3,"(b) the line has the new line point");
            assert.deepStrictEqual([panel.tools,panel.menu,panel.selected,panel.lineDetail,panel.zoom],["tools","menu",1,1,zoom],"(b) tools, menu, selection, line detail and zoom stay");
            await go(page,"measure",".readiness-hero");
            const away=mapCounts();
            mapModelAddPoint("p1",mapPoint("c4","Cache D",90000));
            await shownWithin("projects/p1",".map-workspace");
            assert.strictEqual(await skeletonSeen(),false,"(b) no skeleton on return");
            await namesAre(["Cache A","Cache B","Cache C","Cache D"],"(b) on return");
            await mapFresh("(b)");
            await answered(1,"(b) settled");
            assert.deepStrictEqual(mapCounts(),{full:away.full,delta:away.delta+1},"(b) on return exactly one delta request, no full read");
          });
          await step("(b2) recording between two tracking reads renders the confirmed view once, without a revalidation loop",async()=>{
            // Review #29b (1): p1 is active with mixed frames whose counts grow on every tracking read; on return a delta is
            // still pending when the read answers. One read, one re-render after the map round, no aborted map request.
            const p1=tracking.projects.find(item=>item.id==="p1"),savedActive=structuredClone(live.active_project);
            Object.assign(p1,{frames:{ETRS89:40,unknown:2},mixed_frames:true,unknown_frames:false});
            tracking.active_project_id="p1";live.active_project={id:"p1",name:"Project",line_count:1,point_count:2};
            const failed=[],onFailed=request=>{if(request.url().includes("/api/tracking/map"))failed.push(new URL(request.url()).search);};
            try{
              await openMap("projects/p1");
              await mapFresh("(b2) before");
              await go(page,"measure",".readiness-hero");
              const names=[...mapModel.maps.p1.points.map(point=>point.name),"Cache R"];
              mapModelAddPoint("p1",mapPoint("c9","Cache R",97000));
              onTrackingRead=()=>{p1.frames.ETRS89+=1;};
              mapDelay=2500;projectDelay=800;
              page.on("requestfailed",onFailed);
              const mark=requestLog.length;
              await shownWithin("projects/p1",".map-workspace");
              await namesAre(names,"(b2) the delta catches up");
              await mapFresh("(b2)");
              await page.evaluate(()=>{document.querySelector(".map-workspace").__probe="confirmed";});
              mapDelay=0;projectDelay=0;
              await answered(3,"(b2) settled");
              const reads=requestLog.slice(mark).filter(entry=>entry.path==="/api/tracking"&&entry.method==="GET"&&!entry.search).length;
              assert.strictEqual(reads,1,"(b2) exactly one tracking read after the return");
              assert.strictEqual(await page.evaluate(()=>document.querySelector(".map-workspace").__probe??null),"confirmed","(b2) no further re-render");
              assert.deepStrictEqual(failed,[],"(b2) no map request was aborted");
              assert.strictEqual((await mapStamp()).state,"fresh","(b2) the confirmed view is fresh");
              const frames=await page.locator('#app-main [data-notice="frames"]').innerText();
              assert(frames.includes("ETRS89 41"),`(b2) the confirmed tally is shown: ${frames}`);
            }finally{
              page.off("requestfailed",onFailed);onTrackingRead=null;mapDelay=0;projectDelay=0;live.active_project=savedActive;
              delete p1.frames;delete p1.mixed_frames;delete p1.unknown_frames;
            }
          });
          await step("(c) a project removed elsewhere ends the cached map",async()=>{
            await go(page,"measure",".readiness-hero");
            const removed=tracking.projects.find(item=>item.id==="p1");
            tracking.projects=tracking.projects.filter(item=>item!==removed);
            try{
              projectDelay=1500;
              await shownWithin("projects/p1",".map-workspace");
              const stamp=await mapStamp();
              assert(stamp&&stamp.state==="refreshing","(c) the cached map shows first, with its stamp");
              await page.waitForSelector("#app-main .empty-state h2",{timeout:10000});
              projectDelay=0;
              assert.strictEqual(await page.locator("#app-main .empty-state h2").innerText(),"Project not found");
              assert.strictEqual(await page.locator("#app-main .map-position").count(),0,"(c) no position from the cache");
              const mark=requestLog.length;
              await page.waitForTimeout(5000);
              const later=requestLog.slice(mark).filter(entry=>entry.path==="/api/ui/live"||entry.path==="/api/tracking/map").map(entry=>entry.path);
              assert.deepStrictEqual(later,[],"(c) the map poll has ended");
              const full=mapRequests;
              await go(page,"measure",".readiness-hero");
              await page.evaluate(()=>{window.__skeletonSeen=false;});
              await go(page,"projects/p1","#app-main .empty-state h2");
              assert.strictEqual(await skeletonSeen(),true,"(c) the entry is gone: the skeleton shows");
              assert.strictEqual(await page.locator("#app-main .empty-state h2").innerText(),"Project not found");
              assert.strictEqual(mapRequests,full,"(c) no map read for a missing project");
            }finally{projectDelay=0;tracking.projects=[removed,...tracking.projects];}
          });
          await step("(d) a hidden browser tab returns to the cached map",async()=>{
            await openMap("projects/p1");
            projectDelay=1500;
            try{
              await page.evaluate(()=>{window.__skeletonSeen=false;Object.defineProperty(document,"hidden",{configurable:true,get:()=>true});document.dispatchEvent(new Event("visibilitychange"));});
              await page.waitForTimeout(300);
              await page.evaluate(()=>{Object.defineProperty(document,"hidden",{configurable:true,get:()=>false});document.dispatchEvent(new Event("visibilitychange"));});
              await page.waitForSelector('#app-main .map-canvas .stale-stamp[data-stamp="refreshing"]',{state:"attached",timeout:3000});
              assert.strictEqual(await skeletonSeen(),false,"(d) no skeleton after the tab returns");
              await mapFresh("(d)");
            }finally{projectDelay=0;await page.evaluate(()=>{delete document.hidden;});}
          });
          await step("(e) the cache never reports a live state",async()=>{
            await go(page,"measure",".readiness-hero");
            blockLive();
            try{
              await page.waitForFunction(()=>!document.querySelector("#live-state").classList.contains("ok"),null,{timeout:15000});
              await page.evaluate(()=>{window.__pillOk=false;const pill=document.querySelector("#live-state");
                window.__pillObserver=new MutationObserver(()=>{if(pill.classList.contains("ok"))window.__pillOk=true;});window.__pillObserver.observe(pill,{attributes:true,attributeFilter:["class"]});});
              await shownWithin("projects/p1",".map-workspace");
              await page.waitForTimeout(2000);
              const chip=await page.evaluate(()=>{const node=document.querySelector(".quality-chip");return{text:node.textContent,warn:node.classList.contains("warn")};});
              assert.deepStrictEqual(chip,{text:"Loading live status",warn:true},"(e) the chip waits for a live answer");
              assert.strictEqual(await page.evaluate(()=>window.__pillOk||document.querySelector("#live-state").classList.contains("ok")),false,"(e) the pill is not live from the cache");
            }finally{releaseLive();await page.evaluate(()=>{if(window.__pillObserver)window.__pillObserver.disconnect();});}
            await page.waitForFunction(()=>document.querySelector(".quality-chip").textContent!=="Loading live status",null,{timeout:10000});
          });
          await step("(f) a full answer to a delta request replaces the map and forgets every project page",async()=>{
            mapReset();
            await reload();
            await openMap("projects/p2",["Second A"]);
            await openMap("projects/p1",["Cache A","Cache B"]);
            await page.locator(".point-row",{hasText:"Cache A"}).click();
            await page.waitForSelector(".point-row.selected");
            const before=mapCounts();
            mapModel.maps.p1.points=mapModel.maps.p1.points.filter(point=>point.name!=="Cache B").concat([mapPoint("c9","Window New",70000)]);
            mapModel.revision+=1;
            mapFullOnDelta=true;
            try{await namesAre(["Cache A","Window New"],"(f) the new geometry");}finally{mapFullOnDelta=false;}
            await page.waitForTimeout(300);
            assert.deepStrictEqual(mapCounts(),{full:before.full,delta:before.delta+1},"(f) exactly one request with since_revision");
            assert.deepStrictEqual(await listNames(),["Cache A","Window New"],"(f) the point list has exactly the new geometry");
            const selection=await page.evaluate(()=>({rows:document.querySelectorAll(".point-row.selected,.line-row.selected").length,
              features:document.querySelectorAll(".map-point.selected,.map-line.selected").length,card:document.querySelectorAll(".point-detail .point-card,.line-detail .line-quality").length}));
            assert.deepStrictEqual(selection,{rows:0,features:0,card:0},"(f) the selection is cleared");
            await go(page,"measure",".readiness-hero");
            await page.evaluate(()=>{window.__skeletonSeen=false;});
            await go(page,"projects/p2",".map-workspace");
            assert.strictEqual(await skeletonSeen(),true,"(f) the other project page was forgotten");
          });
          await step("(g) a lower revision or a new boot reloads the cached map in full",async()=>{
            mapReset();
            await reload();
            await openMap("projects/p1",["Cache A","Cache B"]);
            await go(page,"measure",".readiness-hero");
            mapModel.revision=10;mapModel.changes=[];
            mapModel.maps.p1.points=[mapPoint("c1","Restored A",0),mapPoint("c2","Restored B",30000)];
            let before=mapCounts();
            mapDelay=1500;
            await shownWithin("projects/p1",".map-workspace");
            assert.deepStrictEqual(await mapNames(),["Cache A","Cache B"],"(g) the old map shows first");
            assert.strictEqual((await mapStamp()||{}).state,"refreshing","(g) with its stamp");
            await namesAre(["Restored A","Restored B"],"(g) after the full read");
            mapDelay=0;
            await mapFresh("(g)");
            await answered(1,"(g) settled");
            assert.deepStrictEqual(mapCounts(),{full:before.full+1,delta:before.delta},"(g) exactly one full read");
            assert(!lastMapSearch().includes("since_revision"),`(g) the read is full: ${lastMapSearch()}`);
            await go(page,"measure",".readiness-hero");
            live.system.boot_id="b2c3d4e5";
            try{
              mapModel.maps.p1.points[0].name="Rebooted A";
              before=mapCounts();
              await shownWithin("projects/p1",".map-workspace");
              await namesAre(["Rebooted A","Restored B"],"(g) after the new boot");
              await mapFresh("(g) new boot");
              await answered(1,"(g) new boot settled");
              assert.deepStrictEqual(mapCounts(),{full:before.full+1,delta:before.delta},"(g) a new boot: exactly one full read");
            }finally{live.system.boot_id=savedBoot;}
          });
          await step("(h) an own restore forgets the cached maps of the same project ids",async()=>{
            mapReset();
            await reload();
            await openMap("projects/p1",["Cache A","Cache B"]);
            await openMap("projects/p2",["Second A"]);
            mapModel.restore={p1:{lines:[],points:[mapPoint("c1","Backup A",0),mapPoint("c2","Backup B",40000)]},p2:{lines:[],points:[mapPoint("d1","Backup Second",10000)]}};
            await go(page,"projects",".project-list");
            await page.evaluate(()=>{document.querySelector("details.data-management").open=true;});
            const backup={name:"backup.json",mimeType:"application/json",buffer:Buffer.from(JSON.stringify({kind:"survey_backup",schema_version:5,
              projects:[{id:"p1",name:"Project",lines:[],points:[]},{id:"p2",name:"Archive",lines:[],points:[]}]}))};
            const [chooser]=await Promise.all([page.waitForEvent("filechooser"),page.locator("details.data-management").getByRole("button",{name:"Restore backup",exact:true}).click()]);
            await chooser.setFiles(backup);
            await page.locator("dialog.action-sheet").getByRole("button",{name:"Restore backup",exact:true}).waitFor();
            await page.evaluate(()=>{window.__mapPointNames=[];new MutationObserver(records=>{for(const record of records)for(const node of record.addedNodes){if(node.nodeType!==1)continue;
              for(const point of [...(node.matches(".map-point")?[node]:[]),...node.querySelectorAll(".map-point")])window.__mapPointNames.push(point.getAttribute("aria-label"));}}).observe(document,{childList:true,subtree:true});});
            await page.locator("dialog.action-sheet").getByRole("button",{name:"Restore backup",exact:true}).click();
            await waitFor(()=>mapModel.restore===null,"(h) the restore reached the device",10000);
            await page.waitForFunction(()=>document.querySelector("#app-main").getAttribute("aria-busy")==="false"&&document.querySelector(".project-list"));
            const before=mapCounts();
            for(const [hash,names] of [["projects/p1",["Backup A","Backup B"]],["projects/p2",["Backup Second"]]]){
              await page.evaluate(()=>{window.__skeletonSeen=false;});
              await go(page,hash,".map-workspace");
              assert.strictEqual(await skeletonSeen(),true,`(h) ${hash} opens with the skeleton`);
              await namesAre(names,`(h) ${hash}`);
            }
            assert.deepStrictEqual(mapCounts(),{full:before.full+2,delta:before.delta},"(h) one full read per project");
            const seen=await page.evaluate(()=>window.__mapPointNames);
            assert(seen.length>0&&!seen.some(label=>["Cache A","Cache B","Second A"].some(name=>label.endsWith(": "+name))),`(h) no point of the old geometry after the restore ${JSON.stringify(seen)}`);
          });
          await step("(i) a frame assignment and its undo show the project without a cached render",async()=>{
            mapReset();
            frameMock.on=true;frameReset();
            try{
              await reload();
              await openMap("projects/p1",["Cache A","Cache B"]);
              await page.locator('[data-action="assign-frame"]').click();
              await page.locator("dialog[open] input[name=reference_frame]").fill(FRAME_NAME);
              const watch=()=>page.evaluate(()=>{window.__skeletonSeen=false;window.__stampSeen=false;new MutationObserver(records=>{for(const record of records)for(const node of record.addedNodes)
                if(node.nodeType===1&&(node.matches(".stale-stamp")||node.querySelector(".stale-stamp")))window.__stampSeen=true;}).observe(document,{childList:true,subtree:true});});
              await watch();
              await page.locator("dialog[open] button[type=submit]").click();
              await page.waitForSelector("[data-frame-assignment]",{timeout:10000});
              await page.waitForFunction(()=>document.querySelector("#app-main").getAttribute("aria-busy")==="false");
              assert.deepStrictEqual(await page.evaluate(()=>({skeleton:window.__skeletonSeen,stamp:window.__stampSeen})),{skeleton:true,stamp:false},"(i) the assignment shows without a cached render");
              assert((await page.locator("[data-frame-assignment]").innerText()).includes(FRAME_NAME),"(i) the assignment row");
              await page.locator("[data-frame-assignment] button").click();
              await page.locator("dialog[open] button[type=submit]").waitFor();
              await watch();
              await page.locator("dialog[open] button[type=submit]").click();
              await page.waitForSelector("[data-frame-assignment]",{state:"detached",timeout:10000});
              await page.waitForSelector('[data-notice="frames"][data-kind="unknown"]',{timeout:10000});
              await page.waitForFunction(()=>document.querySelector("#app-main").getAttribute("aria-busy")==="false");
              assert.deepStrictEqual(await page.evaluate(()=>({skeleton:window.__skeletonSeen,stamp:window.__stampSeen})),{skeleton:true,stamp:false},"(i) the undo shows without a cached render");
            }finally{frameMock.on=false;frameReset();}
          });
          // #29b Task 7: acceptance of the whole feature. A stale-latched fix never reads as live from the cache, a 12.4 s delta
          // keeps the cached map and its stamp, the map does not shift, every stamp and loading state fits every width and
          // language, and the project round trip never shows an old project. Poll only; the live stream belongs to #27 B.
          const STALE_LATCH_FIX=()=>Object.assign({},saved.fix,{qual:4,fix_status_text:"RTK_FIXED",stale:false,fix_seq:20});
          const noMapPosition=()=>page.evaluate(()=>{const node=document.querySelector(".map-position");return !node||node.classList.contains("hidden")||node.classList.contains("stale")||node.hasAttribute("hidden")||getComputedStyle(node).display==="none";});
          const clearDock=()=>page.evaluate(()=>document.querySelectorAll("#feedback-dock .feedback-item").forEach(node=>node.remove()));
          const overlapOnMap=()=>page.evaluate(()=>{const box=node=>node.getBoundingClientRect(),hit=(a,b)=>a.left<b.right&&b.left<a.right&&a.top<b.bottom&&b.top<a.bottom,
            stamp=box(document.querySelector(".map-canvas .stale-stamp")),chip=document.querySelector(".quality-chip"),tools=document.querySelector(".map-tools");
            return{chip:hit(stamp,box(chip)),tools:hit(stamp,box(tools)),width:stamp.width};});
          await step("(j) a cached map after a stale-latched fix never reads as live",async()=>{
            mapReset();
            live.current_fix=STALE_LATCH_FIX();live.current_quality.accepted=true;clearVerdict();
            try{
              await reload();
              await openMap("projects/p1",["Cache A","Cache B"]);
              await page.waitForFunction(()=>document.querySelector(".quality-chip").textContent==="RTK FIXED",null,{timeout:10000});
              // The same fix_seq now arrives stale: wait for the answered poll that carries it, then for the page to show it.
              live.current_fix=Object.assign({},live.current_fix,{stale:true,fix_age_ms:4200});live.current_quality.accepted=false;
              await page.waitForResponse(async response=>{if(!response.url().includes("/api/ui/live"))return false;
                try{const body=await response.json();return Boolean(body.current_fix&&body.current_fix.stale===true);}catch(error){return false;}},{timeout:15000});
              await page.waitForFunction(()=>document.querySelector(".quality-chip").textContent!=="RTK FIXED"&&!document.querySelector("#live-state").classList.contains("ok"),null,{timeout:10000});
              // The pill's data-link names the data link only (it stays live while the answers arrive); the stale position turns its class to error.
              assert.strictEqual(await page.locator("#live-state").evaluate(node=>node.classList.contains("error")),true,"(j) the stale position turns the pill to error");
              assert.strictEqual(await noMapPosition(),true,"(j) the stale position is not shown as live");
              // Then the link and the project reads fail, and the map comes back from the cache.
              failLive=true;mapFail=true;trackingFail={status:503,body:{error:"storage_busy",message:"Storage maintenance in progress. Please wait."}};
              await clearDock();
              await go(page,"measure",".readiness-hero");
              const within=await shownWithin("projects/p1",".map-workspace");
              assert(within!==null&&within<300,`(j) the cached map within 300 ms (${within} ms)`);
              assert.strictEqual(await skeletonSeen(),false,"(j) no skeleton");
              const first=await mapStamp();
              assert(first&&first.text.startsWith(asOf("en")),`(j) the stamp names the time of the last load ${JSON.stringify(first)}`);
              await page.waitForSelector('#app-main .map-canvas .stale-stamp[data-stamp="failed"]',{timeout:10000});
              const failed=await mapStamp();
              assert(failed.text.includes(en["ui.cached_failed"]),`(j) the stamp says the update failed ${JSON.stringify(failed)}`);
              await page.waitForTimeout(1500);
              const state=await page.evaluate(()=>({chip:document.querySelector(".quality-chip").textContent,pillOk:document.querySelector("#live-state").classList.contains("ok"),
                feedbackOk:document.querySelectorAll("#feedback-dock .feedback-item.ok").length}));
              assert.notStrictEqual(state.chip,"RTK FIXED","(j) the chip does not claim RTK FIXED from the cache");
              assert.strictEqual(await noMapPosition(),true,"(j) no position from the cache");
              assert.strictEqual(state.pillOk,false,"(j) the connection pill is not ok");
              assert.strictEqual(state.feedbackOk,0,"(j) no success message");
            }finally{
              failLive=false;mapFail=false;trackingFail=null;live.current_fix=Object.assign({},STALE_LATCH_FIX(),{fix_seq:21});live.current_quality.accepted=true;
            }
            await go(page,"measure",".readiness-hero");
          });
          await step("(k) a 12.4 s map update with a cached map keeps the map and its stamp",async()=>{
            mapReset();
            await reload();
            await openMap("projects/p1",["Cache A","Cache B"]);
            await go(page,"measure",".readiness-hero");
            mapModelAddPoint("p1",mapPoint("c5","Slow New",95000));
            const before=mapCounts();
            mapDelay=12400;
            const start=Date.now();
            try{
              const within=await shownWithin("projects/p1",".map-workspace");
              assert(within!==null&&within<300,`(k) the cached map within 300 ms (${within} ms)`);
              assert.strictEqual(await skeletonSeen(),false,"(k) no skeleton");
              for(const at of [6000,12000]){
                await page.waitForTimeout(Math.max(0,start+at-Date.now()));
                const stamp=await mapStamp();
                assert(stamp&&stamp.state==="refreshing",`(k) the stamp still says refreshing after ${at/1000} s ${JSON.stringify(stamp)}`);
                assert.strictEqual(await page.locator("#app-main .notice.critical, #feedback-dock .feedback-item.critical, #app-main .skeleton-status").count(),0,`(k) no error and no skeleton after ${at/1000} s`);
                assert.deepStrictEqual(await mapNames(),["Cache A","Cache B"],`(k) the cached map stays after ${at/1000} s`);
              }
              await namesAre(["Cache A","Cache B","Slow New"],"(k) after the delta answer");
              await mapFresh("(k)");
              const took=Date.now()-start;
              console.log(`router-cache: 12.4 s case: the stamp turned fresh ${took} ms after the cached map appeared`);
              assert.deepStrictEqual(await listNames(),["Cache A","Cache B","Slow New"],"(k) the point list has the new point");
              assert.deepStrictEqual(mapCounts(),{full:before.full,delta:before.delta+1},"(k) one delta request, no full read");
            }finally{mapDelay=0;}
          });
          await step("(l) the map does not shift over 10 answered map polls with a delta, 360 EN",async()=>{
            mapReset();
            await page.setViewportSize({width:360,height:800});
            await reload();
            await openMap("projects/p1",["Cache A","Cache B"]);
            await page.evaluate(()=>{window.__cls=0;});
            const polls=liveAnswered,before=mapCounts();
            await answered(3,"(l) before the delta");
            mapModelAddPoint("p1",mapPoint("c6","Shift Probe",85000));
            await answered(7,"(l) after the delta");
            assert(liveAnswered-polls>=10,"(l) at least 10 answered polls");
            assert.strictEqual(mapCounts().delta,before.delta+1,"(l) the run included one delta");
            await namesAre(["Cache A","Cache B","Shift Probe"],"(l)");
            const cls=await page.evaluate(()=>window.__cls);
            console.log(`router-cache: CLS map 360 en = ${cls.toFixed(5)}`);
            assert(cls<0.01,`(l) layout shift ${cls} must stay below 0.01`);
          });
          // Layout matrix: loading status, refreshing and failed stamps on library, archive and map, every width, both languages.
          const MATRIX_WIDTHS=[320,360,390,480,1280];
          const FAILURE_503={status:503,body:{error:"storage_busy",message:"Storage maintenance in progress. Please wait."}};
          for(const lang of ["en","de"]){
            const texts=i18n.dictionaries[lang];
            await step(`(m) ${lang.toUpperCase()}: loading status and cancel button at every width`,async()=>{
              mapReset();
              await page.setViewportSize({width:360,height:800});
              await setLanguage(page,lang);
              projectDelay=7000;
              try{
                await page.evaluate(()=>{location.hash="projects";});
                await page.waitForSelector(".skeleton-status:not([hidden])",{timeout:5000});
                for(const width of MATRIX_WIDTHS){
                  await page.setViewportSize({width,height:800});
                  await page.waitForTimeout(150);
                  assertLayout(await layoutAudit(page),`(m) loading status ${lang} ${width}`);
                  const cancel=await page.locator("[data-loading-cancel]").boundingBox();
                  assert(cancel&&cancel.width>=47.5&&cancel.height>=47.5,`(m) ${lang} ${width}: the cancel button is at least 48 x 48 ${JSON.stringify(cancel)}`);
                  assert.strictEqual(await page.locator("[data-loading-cancel]").innerText(),texts["ui.loading_cancel"],`(m) ${lang}: the cancel text`);
                }
              }finally{projectDelay=0;}
              await page.waitForSelector(".project-list",{timeout:15000});
              await go(page,"archive",".archive-library");
              await go(page,"projects/p1",".map-workspace");
              await go(page,"measure",".readiness-hero");
            });
            for(const width of MATRIX_WIDTHS)await step(`(m) ${lang.toUpperCase()} ${width}: refreshing and failed stamps on library, archive and map`,async()=>{
              await page.setViewportSize({width,height:800});
              for(const [hash,selector] of [["projects",".project-list"],["archive",".archive-library"],["projects/p1",".map-workspace"]]){
                const label=`(m) ${lang} ${width} #${hash}`;
                await go(page,"measure",".readiness-hero");
                trackingFail=null;projectDelay=3500;
                try{
                  const within=await shownWithin(hash,selector);
                  assert(within!==null&&within<300,`${label}: the cached page within 300 ms (${within} ms)`);
                  const refreshing=await stampState();
                  assert(refreshing&&refreshing.state==="refreshing"&&refreshing.visibility==="visible"&&refreshing.text.startsWith(asOf(lang)),`${label}: refreshing stamp ${JSON.stringify(refreshing)}`);
                  assertLayout(await layoutAudit(page),`${label} refreshing`);
                  if(hash==="projects/p1"){const overlap=await overlapOnMap();assert(overlap.width>0&&!overlap.chip&&!overlap.tools,`${label} refreshing: the stamp overlaps the chip or the tools ${JSON.stringify(overlap)}`);}
                }finally{projectDelay=0;}
                await go(page,"measure",".readiness-hero");
                trackingFail=FAILURE_503;
                try{
                  await shownWithin(hash,selector);
                  await page.waitForSelector('#app-main .stale-stamp[data-stamp="failed"]',{timeout:10000});
                  const failed=await stampState();
                  assert(failed.text.includes(texts["ui.cached_failed"])&&failed.buttonVisible,`${label}: failed stamp ${JSON.stringify(failed)}`);
                  assert(failed.buttonWidth>=47.5&&failed.buttonHeight>=47.5,`${label}: Try again is at least 48 x 48 ${JSON.stringify(failed)}`);
                  assertLayout(await layoutAudit(page),`${label} failed`);
                  if(hash==="projects/p1"){const overlap=await overlapOnMap();assert(overlap.width>0&&!overlap.chip&&!overlap.tools,`${label} failed: the stamp overlaps the chip or the tools ${JSON.stringify(overlap)}`);}
                }finally{trackingFail=null;}
              }
              await go(page,"measure",".readiness-hero");
            });
          }
          await step("(n) a language change with a stamp keeps the cached library",async()=>{
            mapReset();
            await page.setViewportSize({width:390,height:800});
            await setLanguage(page,"en");
            await go(page,"projects",".project-list");
            await go(page,"measure",".readiness-hero");
            projectDelay=5000;
            try{
              await shownWithin("projects",".project-list");
              const english=await stampState();
              assert(english&&english.state==="refreshing"&&english.text.startsWith(asOf("en")),`(n) EN stamp ${JSON.stringify(english)}`);
              await page.evaluate(()=>{window.__skeletonSeen=false;RtkI18n.setLanguage("de");});
              await page.waitForFunction(prefix=>{const text=document.querySelector("#app-main .stale-stamp .stale-stamp-text");return Boolean(text&&text.textContent.startsWith(prefix));},asOf("de"),{timeout:3000});
              assert.strictEqual(await skeletonSeen(),false,"(n) no skeleton after the language change");
              const german=await stampState();
              assert(german.state==="refreshing"&&german.text.includes(i18n.dictionaries.de["ui.cached_refreshing"]),`(n) DE stamp ${JSON.stringify(german)}`);
              assert.strictEqual(await page.locator(".project-list .project-card").count(),2,"(n) the library comes from the cache");
            }finally{projectDelay=0;await setLanguage(page,"en");}
          });
          await step("(o) the whole round trip: open, reopen, delta, export entry, archive, reactivate, open again",async()=>{
            mapReset();
            archiveMock.on=true;archiveMock.projects={};
            const projectNames=()=>page.$$eval("#app-main .project-card .project-title b",nodes=>nodes.map(node=>node.textContent));
            const archiveNames=()=>page.$$eval("#app-main .archive-card .archive-info b",nodes=>nodes.map(node=>node.textContent));
            try{
              await page.setViewportSize({width:390,height:800});
              await reload();
              // The archive view is cached before p1 leaves it, so a forgotten entry would show the old list.
              await go(page,"archive",".archive-library");
              assert(!(await archiveNames()).includes("Project"),"(o) p1 is not archived yet");
              await go(page,"projects",".project-list");
              const full0=mapRequests;
              await go(page,"projects/p1",".map-workspace");
              await namesAre(["Cache A","Cache B"],"(o) first open");
              assert.strictEqual(mapRequests,full0+1,"(o) the first open reads the whole map");
              await go(page,"projects",".project-list");
              projectDelay=1500;
              const within=await shownWithin("projects/p1",".map-workspace");
              assert(within!==null&&within<300&&(await skeletonSeen())===false,`(o) the second open comes from the cache (${within} ms)`);
              assert.strictEqual((await mapStamp()||{}).state,"refreshing","(o) with its stamp");
              await mapFresh("(o) second open");
              projectDelay=0;
              assert.strictEqual(mapRequests,full0+1,"(o) the second open reads no whole map");
              mapModelAddPoint("p1",mapPoint("c7","Round trip point",65000));
              await namesAre(["Cache A","Cache B","Round trip point"],"(o) the delta");
              assert.deepStrictEqual(await listNames(),["Cache A","Cache B","Round trip point"],"(o) the list has the new point");
              await page.locator(".point-row",{hasText:"Round trip point"}).click();
              await page.waitForSelector(".point-detail .point-card, .point-detail .gate-profile, .point-row.selected",{timeout:5000});
              assert.strictEqual(await page.locator(".point-row.selected").count(),1,"(o) the point detail is open");
              // The export entry opens its three formats and closes without an export.
              await page.locator("#app-main .project-actions").getByRole("button",{name:en["ui.project_export"],exact:true}).click();
              await page.locator("dialog.export-sheet[open]").waitFor();
              const formats=await page.$$eval("dialog.export-sheet div.export-format",nodes=>nodes.map(node=>node.getClientRects().length>0));
              assert.deepStrictEqual(formats,[true,true,true],"(o) three export formats are visible");
              await page.locator("dialog.export-sheet").getByRole("button",{name:en["ui.why_close"],exact:true}).click();
              await page.waitForSelector("dialog.export-sheet",{state:"detached",timeout:5000});
              // Archive the project: both overviews forget it.
              await page.locator("#app-main .project-actions").getByRole("button",{name:"Archive",exact:true}).click();
              await page.locator("dialog.action-sheet").getByRole("button",{name:"Archive project",exact:true}).click();
              await page.waitForSelector(".project-list",{timeout:10000});
              await page.waitForFunction(()=>location.hash==="#projects"&&document.querySelector("#app-main").getAttribute("aria-busy")==="false");
              assert(!(await projectNames()).includes("Project"),`(o) the library no longer lists the archived project ${JSON.stringify(await projectNames())}`);
              await page.evaluate(()=>{window.__skeletonSeen=false;});
              await go(page,"archive",".archive-library");
              assert.strictEqual(await skeletonSeen(),true,"(o) the archive was forgotten and loads again");
              assert((await archiveNames()).includes("Project"),`(o) the archive lists the project ${JSON.stringify(await archiveNames())}`);
              // Reactivate it: library and map load again, in full.
              await page.locator('.archive-card[aria-label="Project"]').getByRole("button",{name:"Reactivate",exact:true}).click();
              await page.locator("dialog.action-sheet").getByRole("button",{name:"Reactivate",exact:true}).click();
              await page.waitForSelector('.archive-card[aria-label="Project"]',{state:"detached",timeout:10000});
              await page.waitForFunction(()=>document.querySelector("#app-main").getAttribute("aria-busy")==="false");
              await page.evaluate(()=>{window.__skeletonSeen=false;});
              await go(page,"projects",".project-list");
              assert.strictEqual(await skeletonSeen(),true,"(o) the library loads again after the reactivation");
              assert((await projectNames()).includes("Project"),`(o) the library lists the reactivated project ${JSON.stringify(await projectNames())}`);
              const full1=mapRequests;
              await page.evaluate(()=>{window.__skeletonSeen=false;});
              await go(page,"projects/p1",".map-workspace");
              assert.strictEqual(await skeletonSeen(),true,"(o) the reactivated project opens with the skeleton");
              await namesAre(["Cache A","Cache B","Round trip point"],"(o) the reactivated geometry");
              assert.strictEqual(mapRequests,full1+1,"(o) one whole map read");
              assert.strictEqual(await page.locator("#app-main .stale-stamp").count(),0,"(o) no stamp with an old time");
            }finally{archiveMock.on=false;archiveMock.projects={};}
          });
          await go(page,"measure",".readiness-hero");
        }finally{
          Object.assign(mapModel,{on:false,maps:{},changes:[],restore:null});mapFullOnDelta=false;mapDelay=0;live.system.boot_id=savedBoot;
        }
      }finally{
        mutateProjects=false;projectDelay=0;trackingFail=null;mapDelay=0;mapFail=false;archiveMock.on=false;
        tracking.projects=savedTracking.projects;tracking.archives=savedTracking.archives;
      }
      assert.deepStrictEqual(errors,[]);
    }finally{
      liveVary=false;captureState=null;actionAnswers={};
      live.current_fix=saved.fix;live.current_quality.accepted=saved.accepted;live.active_line=saved.line;live.active_project=saved.project;
      live.measurement_progress=saved.progress;live.quality_streak=saved.streak;
    }
    assert.deepStrictEqual(failures,[],"router-cache failures");
    await browser.close();console.log("router-cache-browser=ok");return;
  }
  if(process.argv.includes("--live-stream-only")){
    // #27 B4: the field app takes its live state from the event stream and falls back to polling. Two kinds of streams: the
    // page's own EventSource against a route.fulfill answer (spike, old firmware) and a test EventSource that the harness
    // drives (window.__rtkStream, see the init script). Timings are real: the client's watchdog and data age run on the page clock.
    const errors=[],failures=[];page.on("pageerror",error=>errors.push(error.message));
    const i18n=require(root+"/i18n.js"),dictionaries={en:i18n.dictionaries.en,de:i18n.dictionaries.de};
    const sleep=ms=>page.waitForTimeout(ms);
    const waitFor=async(check,message,timeout=8000)=>{const end=Date.now()+timeout;while(!check()){if(Date.now()>end)assert.fail(message);await sleep(50);}};
    const link=()=>page.locator("#live-state").getAttribute("data-link");
    const liveRequests=since=>requestLog.slice(since).filter(entry=>entry.path==="/api/ui/live").length;
    const eventRequests=since=>requestLog.slice(since).filter(entry=>entry.path==="/api/events").length;
    const instances=()=>page.evaluate(()=>window.__rtkStream.instances.map(instance=>instance.readyState));
    // One live frame from the fixture's current state, with patches; each push parses to a new current_fix object (new data).
    const frame=(patch={})=>JSON.stringify({...servedLive(),...patch});
    const push=(name,data,index)=>page.evaluate(([name,data,index])=>window.__rtkStream.push(name,data,index===undefined?undefined:window.__rtkStream.instances[index]),[name,data,index]);
    const pushLive=(patch,index)=>push("live",frame(patch),index);
    const projectText=()=>page.locator(".status-project-text b").innerText();
    const projectNamed=(name,timeout=3000)=>page.waitForFunction(name=>document.querySelector(".status-project-text b")?.textContent.startsWith(name+" ·"),name,{timeout});
    const step=async(name,run)=>{console.log("live-stream: "+name);try{await run();}catch(error){failures.push(`${name}: ${error.message}`);console.log(`live-stream: FAILED ${name}: ${error.message}`);}};
    // A fresh page load on the hash, the first poll answered (mountMeasure polls once), the stream (test or real) opened.
    const load=async(hash,{lang="en",mode="fake",selector=".readiness-hero"}={})=>{
      const answered=liveAnswered;
      await page.evaluate(([lang,mode,hash])=>{localStorage["rtk-language"]=lang;sessionStorage.setItem("rtk-stream-mode",mode);location.hash=hash;},[lang,mode,hash]);
      await page.reload();
      await page.waitForSelector(selector);
      await waitFor(()=>liveAnswered>answered,"the first poll is answered");
      await sleep(200);
    };
    const saved={fix:live.current_fix,accepted:live.current_quality.accepted,progress:live.measurement_progress,line:live.active_line,project:live.active_project,defaults:live.measurement_defaults};
    try{
      live.current_fix=Object.assign({},saved.fix,{qual:4,fix_status_text:"RTK_FIXED",stale:false,fix_seq:1});
      live.current_quality.accepted=true;clearVerdict();
      await page.setViewportSize({width:390,height:844});

      await step("(a) spike: the page's own EventSource takes a frame from a route.fulfill answer",async()=>{
        streamMock.frame=JSON.parse(frame({active_project:{...live.active_project,name:"Stream Project"}}));
        const before=streamMock.requests.length;
        try{
          await load("measure",{mode:"real"});
          await projectNamed("Project",1000);
          await projectNamed("Stream Project",5000);
        }finally{streamMock.frame=null;}
        assert.strictEqual(streamMock.requests.length-before,1,"one stream request");
        assert.strictEqual((await instances()).length,0,"the test EventSource is not used in real mode");
      });

      for(const [lang,seconds] of [["en",20],["de",20]])await step(`(b) no polls while the stream delivers, ${lang.toUpperCase()} 390`,async()=>{
        await load("measure",{lang});
        const since=requestLog.length,word=dictionaries[lang]["ui.link_live"];
        for(let second=0;second<seconds;second+=1){await pushLive();await sleep(1000);}
        assert.strictEqual(liveRequests(since),0,"no /api/ui/live request after the first render");
        assert.strictEqual(await link(),"live","pill link");
        assert((await page.locator("#live-state small").innerText()).includes(word),`pill word ${word}`);
        assert.deepStrictEqual(await instances(),[1],"one stream, never reopened");
        assert.strictEqual(eventRequests(since),0,"no network stream request in test mode");
      });

      await step("(c) data age: silence turns the link lost after 10 s and the stream falls back to 2-s polls after 22 s",async()=>{
        await load("measure");
        const since=requestLog.length;
        await pushLive();
        const silent=Date.now();
        await sleep(5000);
        assert.strictEqual(await link(),"delayed","delayed after 5 s");
        // The ticker redraws the pill once a second, so the state may show up to a second after the 10-s threshold.
        await page.waitForFunction(()=>document.querySelector("#live-state").dataset.link==="lost",null,{timeout:Math.max(500,silent+11500-Date.now())});
        assert(Date.now()-silent>=9500,"not lost before 10 s of silence");
        await sleep(Math.max(0,silent+20000-Date.now()));
        assert.strictEqual(liveRequests(since),0,"no poll while the stream is still waited for");
        await waitFor(()=>liveRequests(since)>=1,"the fallback polls at once",5000);
        const fallbackAt=Date.now()-silent;
        assert(fallbackAt>=21000&&fallbackAt<=26000,"fallback after about 22 s, got "+fallbackAt);
        const polls=liveRequests(since);
        await sleep(4600);
        assert(liveRequests(since)-polls>=2,`polls every 2 s after the fallback (${liveRequests(since)-polls} in 4.6 s)`);
        assert.deepStrictEqual(await instances(),[2],"the stream is closed");
        assert.strictEqual(await link(),"live","fresh poll answers turn the link live again");
      });

      await step("(d) heartbeats alone keep the stream but not the data age",async()=>{
        await load("measure");
        const since=requestLog.length,start=Date.now();
        for(let beat=0;beat<5;beat+=1){await push("heartbeat",{t:beat});await sleep(Math.max(0,start+(beat+1)*5000-Date.now()));
          if(beat===2)assert.strictEqual(await link(),"lost","lost after 15 s with heartbeats only");}
        assert.strictEqual(liveRequests(since),0,"no poll while heartbeats arrive");
        assert.deepStrictEqual(await instances(),[1],"the stream stays open");
      });

      await step("(e) bye session closes the stream and leads to the sign-in",async()=>{
        await load("measure");
        const since=requestLog.length;
        liveAnswers=[{status:401}];
        await push("bye",{reason:"session"});
        await page.waitForURL(url=>url.search.startsWith("?return="),{timeout:5000});
        const signIn=requestLog.findIndex((entry,index)=>index>=since&&entry.path==="/"&&entry.search.startsWith("?return="));
        assert(signIn>=0,"the sign-in page was asked for");
        assert.strictEqual(requestLog.slice(since,signIn).filter(entry=>entry.path==="/api/ui/live").length,1,"one poll after the bye, answered 401");
        liveAnswers=[];
      });

      await step("(f) an older firmware refuses the stream: polling, one new attempt per 30 s",async()=>{
        const before=streamMock.requests.length;
        await load("measure",{mode:"real"});
        const since=requestLog.length,start=Date.now();
        await waitFor(()=>liveRequests(since)>=2,"polls run like before the stream",7000);
        assert.strictEqual(streamMock.requests.length-before,1,"one /api/events request so far");
        await sleep(Math.max(0,start+25000-Date.now()));
        assert.strictEqual(streamMock.requests.length-before,1,"still one request after 25 s");
        await waitFor(()=>streamMock.requests.length-before>=2,"the second attempt",12000);
        const gap=streamMock.requests[before+1]-streamMock.requests[before];
        assert(gap>=29000&&gap<=32000,"the attempts are 30 s apart, got "+gap);
        await sleep(1500);
        assert.strictEqual(streamMock.requests.length-before,2,"exactly two requests");
        assert(liveRequests(since)>=10,"polling continued");
      });

      await step("(g) a hidden tab closes the stream, a visible one opens a new one",async()=>{
        await load("measure");
        const setHidden=hidden=>page.evaluate(hidden=>{Object.defineProperty(document,"hidden",{configurable:true,get:()=>hidden});document.dispatchEvent(new Event("visibilitychange"));},hidden);
        assert.deepStrictEqual(await instances(),[1]);
        try{
          await setHidden(true);
          assert.deepStrictEqual(await instances(),[2],"closed while hidden");
          await setHidden(false);
          await sleep(300);
          assert.deepStrictEqual(await instances(),[2,1],"one new stream when visible again");
        }finally{await page.evaluate(()=>{delete document.hidden;});}
      });

      await step("(h) an own change renews the stream and the old generation changes nothing",async()=>{
        const defaults=samples=>({antenna_height_m:2,reference_frame:"",reference_frame_epoch:"",samples});
        live.measurement_defaults=defaults(5);gateSamples=5;
        await load("measure/setup",{selector:".field-setup-layout"});
        const pressed=()=>page.locator('.measurement-choice button[aria-pressed="true"]').getAttribute("data-method");
        const waitPressed=(method,timeout=3000)=>page.waitForFunction(method=>document.querySelector('.measurement-choice button[aria-pressed="true"]')?.dataset.method===method,method,{timeout});
        await pushLive();
        await waitPressed("5");
        assert.deepStrictEqual(await instances(),[1]);
        await page.locator('.measurement-choice button[data-method="10"]').click();
        await waitPressed("10",2000);
        assert.deepStrictEqual(await instances(),[2,1],"the old generation is closed, a new one is open");
        await pushLive({measurement_defaults:defaults(5)},0);
        await sleep(500);
        assert.strictEqual(await pressed(),"10","a frame of the old generation changes nothing");
        await pushLive({measurement_defaults:defaults(1)},1);
        await waitPressed("1");
      });

      await step("(i) a measurement keeps its own progress poll, the stream waits",async()=>{
        await load("measure");
        captureState={epoch:"a1b2c3d4",stack:[],order:40};actionAnswers={};liveAnswers=[];checkAnswers=[];trackingPostLog.length=0;
        live.active_project={id:"p1",name:"Project",line_count:1,point_count:31};projectRow().point_count=31;
        live.active_line={id:"l7",name:"Field route",state:"recording",recording_mode:"route",vertex_count:12,length_m:8.5,recording_sec:30};
        syncCandidate();
        live.measurement_progress={active:false,collected:0,total:5};
        await pushLive();
        // Positive control: outside a measurement a frame is taken.
        await pushLive({active_project:{...live.active_project,name:"Stream Project"}});
        await projectNamed("Stream Project");
        await pushLive();
        await projectNamed("Project");
        actionAnswers.add_vertex={delay:5000};
        await page.evaluate(()=>{const button=[...document.querySelectorAll(".field-actions > button")].find(node=>node.textContent==="Add line point");if(!button)throw new Error("Add line point missing");button.click();});
        await page.locator(".measurement-sheet[open]").waitFor();
        live.measurement_progress={active:true,mode:"fixed",phase:"collecting",collected:3,total:5,elapsed_sec:2,rejected_samples:0};
        const progressBefore=await page.evaluate(()=>window.__progressAnswers.length),since=requestLog.length;
        for(let step=0;step<20;step+=1){await pushLive({active_project:{...live.active_project,name:"Stream Project"}});await sleep(200);}
        const answers=(await page.evaluate(()=>window.__progressAnswers.length))-progressBefore;
        assert(answers>=5,`progress answers keep their 650-ms rhythm (${answers} in 4 s)`);
        await page.waitForFunction(()=>document.querySelector(".measurement-state")?.textContent.includes("3 of 5"));
        assert(!(await projectText()).startsWith("Stream Project"),"frames change nothing during the measurement");
        assert.deepStrictEqual(await instances(),[1],"no fallback, no new stream during the measurement");
        assert.strictEqual(liveRequests(since),0,"no live poll during the measurement");
        live.measurement_progress={active:false,collected:0,total:5};
        await page.locator(".measurement-sheet").waitFor({state:"detached",timeout:8000});
        await sleep(300);
        assert.deepStrictEqual(await instances(),[2,1],"the confirmed answer renewed the stream once");
        await pushLive({active_project:{...live.active_project,name:"Stream Project"}});
        await projectNamed("Stream Project");
      });

      // Codex review finding 2: a page that loads in a hidden background tab or offline opens no stream (it would hold one
      // of the device's three); the visible or online transition opens exactly one.
      for(const start of ["hidden","offline"])await step(`(j) a tab that loads ${start} opens no stream until it is ${start==="hidden"?"visible":"online"}`,async()=>{
        try{
          await page.evaluate(start=>{localStorage["rtk-language"]="en";sessionStorage.setItem("rtk-stream-mode","fake");sessionStorage.setItem("rtk-start",start);location.hash="measure";},start);
          await page.reload();
          await page.waitForSelector(".readiness-hero");
          assert.strictEqual(await page.evaluate(start=>start==="hidden"?document.hidden:!navigator.onLine,start),true,`the page starts ${start}`);
          await sleep(1500);
          assert.deepStrictEqual(await instances(),[],"no stream at the load");
          await page.evaluate(start=>{
            sessionStorage.removeItem("rtk-start");
            if(start==="hidden"){Object.defineProperty(document,"hidden",{configurable:true,get:()=>false});Object.defineProperty(document,"visibilityState",{configurable:true,get:()=>"visible"});document.dispatchEvent(new Event("visibilitychange"));}
            else{Object.defineProperty(navigator,"onLine",{configurable:true,get:()=>true});dispatchEvent(new Event("online"));}
          },start);
          await sleep(500);
          assert.deepStrictEqual(await instances(),[1],"exactly one stream afterwards");
        }finally{await page.evaluate(()=>{sessionStorage.removeItem("rtk-start");delete document.hidden;delete document.visibilityState;delete navigator.onLine;});}
      });
    }finally{
      streamMock.frame=null;captureState=null;actionAnswers={};liveAnswers=[];
      live.current_fix=saved.fix;live.current_quality.accepted=saved.accepted;live.measurement_progress=saved.progress;live.active_line=saved.line;live.active_project=saved.project;
      if(saved.defaults===undefined)delete live.measurement_defaults;else live.measurement_defaults=saved.defaults;
    }
    assert.deepStrictEqual(errors,[],"page errors");
    assert.deepStrictEqual(failures,[],"live-stream failures");
    await browser.close();console.log("live-stream-browser=ok");return;
  }
  if(process.argv.includes("--start-line-only")){
    const errors=[];page.on("pageerror",error=>errors.push(error.message));
    const en=require(root+"/i18n.js").dictionaries.en;
    const saved={fix:structuredClone(live.current_fix),accepted:live.current_quality.accepted,line:live.active_line,project:live.active_project},savedProgress=structuredClone(live.measurement_progress);
    const nextPoll=async()=>{await page.waitForResponse(response=>response.url().endsWith("/api/ui/live"));await page.waitForTimeout(300);};
    const action=name=>page.locator(".field-actions button",{hasText:name}).first();
    const guard=()=>page.locator("dialog.action-sheet .fix-guard");
    const submit=()=>page.locator("dialog.action-sheet").getByRole("button",{name:"Start recording"});
    try{
      await page.evaluate(()=>{localStorage.setItem("rtk-language","en");localStorage.removeItem("rtk-recording-mode");});
      live.current_quality.accepted=false;
      live.current_fix=Object.assign({},saved.fix,{qual:1,stale:false,fix_seq:1});
      live.active_line=null;
      await page.reload();
      await page.waitForFunction(()=>[...document.querySelectorAll(".field-actions button")].some(node=>node.textContent==="Start line"&&!node.disabled));
      assert.strictEqual(await action("Start line").isDisabled(),false,"Start line stays available for route recording");
      assert.strictEqual(await action("Object point").isDisabled(),true);
      assert.strictEqual(await page.locator(".field-actions .action-reason").innerText(),en["ui.start_line_route_hint"]);
      // A running measurement removes the route hint instead of leaving it standing.
      live.measurement_progress=Object.assign({},savedProgress,{active:true,elapsed_sec:3,collected:1,total:5});
      await nextPoll();
      await page.waitForFunction(()=>document.querySelector(".measurement-resume"));
      assert.notStrictEqual(await page.locator(".field-actions .action-reason").innerText(),en["ui.start_line_route_hint"],"route hint must not stand while a measurement runs");
      live.measurement_progress=Object.assign({},savedProgress);
      await nextPoll();
      await page.waitForFunction(()=>!document.querySelector(".measurement-resume"));
      assert.strictEqual(await page.locator(".field-actions .action-reason").innerText(),en["ui.start_line_route_hint"]);
      await action("Start line").click();
      await submit().waitFor();
      await page.locator('dialog.action-sheet input[name="name"]').fill("Line A");
      await page.locator('dialog.action-sheet select[name="recording_mode"]').selectOption("survey");
      assert.strictEqual(await submit().isDisabled(),true,"survey submit locks while the gate is closed");
      assert.strictEqual(await guard().isVisible(),true);
      assert.strictEqual(await guard().innerText(),en["ui.start_line_survey_blocked"]);
      await page.locator('dialog.action-sheet select[name="recording_mode"]').selectOption("route");
      assert.strictEqual(await submit().isDisabled(),false,"route submit stays available");
      assert.strictEqual(await guard().innerText(),en["ui.route_without_approval"]);
      // The gate opens while the dialog is open: the survey mode follows the live state.
      live.current_quality.accepted=true;
      await nextPoll();
      await page.locator('dialog.action-sheet select[name="recording_mode"]').selectOption("survey");
      assert.strictEqual(await submit().isDisabled(),false,"survey submit unlocks with the gate");
      assert.strictEqual(await guard().isHidden(),true);
      // A stale position takes precedence over the gate verdict.
      live.current_fix=Object.assign({},live.current_fix,{stale:true,fix_age_ms:4200,fix_seq:2});
      await page.waitForFunction(()=>document.querySelector("dialog.action-sheet .fix-guard").textContent.includes("4 s"));
      assert.strictEqual(await submit().isDisabled(),true);
      assert(!(await guard().isHidden()));
      await page.keyboard.press("Escape");
      await page.waitForFunction(()=>!document.querySelector("dialog.action-sheet"));
      // Without a project the status page explains the next step.
      live.current_quality.accepted=false;
      live.current_fix=Object.assign({},saved.fix,{qual:1,stale:false,fix_seq:3});
      live.active_project=null;
      await page.reload();
      await page.waitForFunction(()=>[...document.querySelectorAll(".field-actions button")].some(node=>node.textContent==="Choose project"));
      assert.strictEqual(await page.locator(".field-actions .action-reason").innerText(),en["ui.no_project_hint"]);
      assert.deepStrictEqual(errors,[]);
    }finally{
      live.current_fix=saved.fix;live.current_quality.accepted=saved.accepted;live.active_line=saved.line;live.active_project=saved.project;live.measurement_progress=savedProgress;
    }
    await browser.close();console.log("start-line-browser=ok");return;
  }
  if(process.argv.includes("--project-loading-only")){
    const errors=[];page.on("pageerror",error=>errors.push(error.message));
    const i18nTexts=require(root+"/i18n.js"),texts={en:i18nTexts.dictionaries.en,de:i18nTexts.dictionaries.de};
    // The loading status of the current skeleton: hidden or not, its label and elapsed text, and the cancel button's visibility and size.
    const loadingStatus=()=>page.evaluate(()=>{const status=document.querySelector(".skeleton-status"),cancel=document.querySelector("[data-loading-cancel]");if(!status)return null;const box=cancel&&cancel.getBoundingClientRect();return{hidden:status.hidden||getComputedStyle(status).display==="none",label:status.querySelector(".skeleton-label").textContent,elapsed:status.querySelector(".skeleton-elapsed").textContent,cancelVisible:Boolean(cancel&&!cancel.hidden&&box.width>0),cancelWidth:box?box.width:0,cancelHeight:box?box.height:0,visibleCancels:[...document.querySelectorAll("[data-loading-cancel]")].filter(node=>node.getClientRects().length>0).length};});
    const observeLoading=async(lang,overviewLabel)=>{
      const mapLabel=texts[lang]["ui.loading_project_map"];
      await page.evaluate(()=>{location.hash="projects/p1";});
      await page.waitForTimeout(500);
      let state=await loadingStatus();
      assert(state&&state.hidden,`${lang}: no loading status before one second`);
      await page.waitForTimeout(700);
      state=await loadingStatus();
      assert(state&&!state.hidden,`${lang}: the loading status shows after one second`);
      assert.strictEqual(state.label,overviewLabel,`${lang}: the overview names what loads`);
      assert(/1 s$/.test(state.elapsed),`${lang}: elapsed text ${state.elapsed}`);
      assert(state.cancelVisible&&state.cancelWidth>=47.5&&state.cancelHeight>=47.5,`${lang}: whole pages offer a 48 px cancel button ${JSON.stringify(state)}`);
      await page.waitForFunction(label=>{const node=document.querySelector(".skeleton-status .skeleton-label");return node&&node.textContent===label;},mapLabel,{timeout:15000});
      await page.waitForFunction(()=>/3 s$/.test(document.querySelector(".skeleton-status .skeleton-elapsed").textContent),null,{timeout:15000});
      state=await loadingStatus();
      assert.strictEqual(state.label,mapLabel,`${lang}: the map names its own loading`);
      assert(state.cancelVisible&&state.cancelWidth>=47.5&&state.cancelHeight>=47.5,`${lang}: the map load offers a 48 px cancel button`);
    };
    await page.setViewportSize({width:360,height:800});
    failLive=true;projectDelay=8500;mapDelay=8500;
    await observeLoading("en","Loading projects");
    await page.waitForSelector(".map-workspace");
    assert.strictEqual(await page.locator(".skeleton-status").count(),0,"the loading status is gone with the skeleton");
    assert.strictEqual(mapRequests,1,"slow project reads must complete without restarting");
    await page.waitForFunction(()=>document.querySelector(".quality-chip").textContent==="Live status unavailable");
    assert.strictEqual(await page.locator(".map-line").count(),1);
    assert.strictEqual(await page.locator(".map-point").count(),2);
    assert(await page.locator(".map-position").evaluate(node=>node.classList.contains("hidden")));
    await page.locator(".line-row").click();
    await page.waitForSelector(".line-quality");
    failLive=false;live.current_quality.accepted=true;live.current_fix.qual=4;
    await page.waitForFunction(()=>document.querySelector(".quality-chip").textContent==="RTK FIXED");
    // The chip shows the real fix, not the profile: a relaxed DGPS gate with a DGPS fix reads "DGPS".
    const savedLimits=live.current_quality.limits;
    live.current_quality.limits=Object.assign({},savedLimits,{fix_quality:"DGPS"});live.current_fix.qual=2;
    await page.waitForFunction(()=>document.querySelector(".quality-chip").textContent==="DGPS");
    live.current_quality.limits=savedLimits;live.current_fix.qual=4;
    await page.waitForFunction(()=>document.querySelector(".quality-chip").textContent==="RTK FIXED");
    // Leaving during the map read (#29b: a reload empties the route cache, so the map is read again).
    await page.evaluate(()=>{location.hash="measure";});await page.reload();await page.waitForSelector(".readiness-hero");
    projectDelay=0;mapDelay=1000;
    await go(page,"projects",".project-list");
    const pending=page.waitForRequest(request=>request.url().includes("/api/tracking/map"));
    await page.evaluate(()=>location.hash="projects/p1");await pending;
    await go(page,"device",".device-links");
    await page.waitForTimeout(1300);
    assert.strictEqual(await page.locator(".map-workspace").count(),0,"cancelled map must not replace the new page");
    assert.strictEqual(await page.locator(".device-links").count(),1);
    // #24-UI Task 7 (E27): leave the archive view while /api/tracking is held; the late answer shows nothing and no message.
    tracking.archives=mobileArchives;
    const held=holdTracking(),archiveAsked=page.waitForRequest(request=>new URL(request.url()).pathname==="/api/tracking");
    await page.evaluate(()=>{location.hash="archive";});await archiveAsked;
    await page.evaluate(()=>document.querySelectorAll("#feedback-dock .feedback-item").forEach(node=>node.remove()));
    await page.locator('[data-route="measure"]:visible').first().click();
    await page.waitForSelector(".readiness-hero");
    held.release();trackingHold=null;
    await page.waitForTimeout(600);
    assert.strictEqual(await page.locator(".archive-library").count(),0,"a late archive answer must not replace the new page");
    assert.strictEqual(await page.locator("#app-main .empty-state, #app-main .text-link[href='/#projects']").count(),0);
    assert(await page.locator(".readiness-hero").isVisible(),"Measure stays visible");
    assert.strictEqual(await page.locator("#feedback-dock .feedback-item").count(),0,"no message from the abandoned archive view");
    assert.strictEqual(await page.evaluate(()=>location.hash),"#measure");
    // The survey is not available yet (503 survey_loading) or storage is busy: a local error with a retry, never a blank page or a toast only.
    // Without a cached page (#29b: a reload empties the route cache; the cached case is in --router-cache-only).
    await page.reload();await page.waitForSelector(".readiness-hero");
    for(const [hash,ready,code] of[["archive",".archive-library .archive-card","survey_loading"],["projects",".project-list","storage_busy"]]){
      trackingFail={status:503,body:{error:code,message:code==="survey_loading"?"The survey is still loading; try again.":"Storage maintenance in progress. Please wait."}};
      await page.evaluate(value=>{location.hash=value;},hash);
      await page.waitForSelector("#app-main .notice.critical",{timeout:8000});
      assert((await page.locator("#app-main .notice.critical").innerText()).length>0,`${hash}: the error is named`);
      assert.strictEqual(await page.locator("#app-main").getByRole("button",{name:"Try again",exact:true}).count(),1,`${hash}: one retry button`);
      assert.strictEqual(await page.locator(ready).count(),0,`${hash}: no stale content next to the error`);
      assert.strictEqual(await page.locator("#feedback-dock .feedback-item.critical").count(),0,`${hash}: the error is local, not a toast`);
      trackingFail=null;
      await page.locator("#app-main").getByRole("button",{name:"Try again",exact:true}).click();
      await page.waitForSelector(ready);
      assert.strictEqual(await page.locator("#app-main .notice.critical").count(),0,`${hash}: the error is gone after the retry`);
      await go(page,"measure",".readiness-hero");
    }
    // Cancelling a whole page: the request ends, nothing is shown as loaded, the connection pill keeps its state, a retry loads.
    await page.reload();await page.waitForSelector(".readiness-hero");
    projectDelay=0;mapDelay=8500;
    const liveClass=await page.locator("#live-state").getAttribute("class");
    const mapFailed=page.waitForEvent("requestfailed",{predicate:request=>request.url().includes("/api/tracking/map"),timeout:10000});
    await page.evaluate(()=>{location.hash="projects/p1";});
    await page.waitForSelector("[data-loading-cancel]:not([hidden])",{timeout:5000});
    await page.waitForTimeout(1000);
    await page.getByRole("button",{name:"Cancel loading",exact:true}).click();
    await mapFailed;
    await page.waitForSelector('[data-notice="load-cancelled"]');
    assert.strictEqual(await page.locator('[data-notice="load-cancelled"]').innerText(),texts.en["ui.loading_cancelled"]);
    assert.strictEqual(await page.locator("#app-main").getByRole("button",{name:"Try again",exact:true}).count(),1);
    assert.strictEqual(await page.locator(".map-workspace").count(),0,"no map after cancelling");
    assert.strictEqual(await page.locator(".skeleton-status").count(),0,"the skeleton is gone");
    assert.strictEqual(await page.locator("#feedback-dock .feedback-item.ok").count(),0,"cancelling reports no success");
    assert.strictEqual(await page.locator("#live-state").getAttribute("class"),liveClass,"cancelling leaves the connection pill alone");
    await page.waitForTimeout(500);
    assert.strictEqual(await page.locator('[data-notice="load-cancelled"]').count(),1,"the cancel notice stays");
    mapDelay=0;
    await page.locator("#app-main").getByRole("button",{name:"Try again",exact:true}).click();
    await page.waitForSelector(".map-workspace");
    assert.strictEqual(await page.locator('[data-notice="load-cancelled"]').count(),0);
    // A cold 12.4 s map read (Review V15.10.3) counts up and finishes without an error or a restart (cold: the reload empties
    // the route cache, which holds the map from the retry above).
    await page.evaluate(()=>{location.hash="measure";});await page.reload();await page.waitForSelector(".readiness-hero");
    await go(page,"projects",".project-list");
    mapDelay=12400;
    const mapAsked=mapRequests;
    await page.evaluate(()=>{location.hash="projects/p1";});
    await page.waitForFunction(()=>/12 s$/.test((document.querySelector(".skeleton-status .skeleton-elapsed")||{textContent:""}).textContent),null,{timeout:20000});
    assert.strictEqual(await page.locator("#app-main .notice.critical").count(),0,"a slow read is no error");
    await page.waitForSelector(".map-workspace",{timeout:10000});
    assert.strictEqual(mapRequests,mapAsked+1,"the slow read is not restarted");
    mapDelay=0;
    // A section skeleton never offers to cancel: an edited form next to it stays, whole.
    accessDelay=6000;
    await page.evaluate(()=>{location.hash="device/connections";});
    await page.waitForSelector(".configuration-layout");
    await page.evaluate(()=>{document.querySelector(".configuration-layout").__probe="kept";});
    await page.locator('input[name="wifi_ssid"]').fill("Edited while loading");
    await page.waitForTimeout(1500);
    const section=await page.evaluate(()=>{const status=document.querySelector('[data-group="access"] .skeleton-status');return{shown:Boolean(status&&!status.hidden),label:status?status.querySelector(".skeleton-label").textContent:"",elapsed:status?status.querySelector(".skeleton-elapsed").textContent:"",cancels:[...document.querySelectorAll("[data-loading-cancel]")].filter(node=>node.getClientRects().length>0).length,dirty:document.querySelector(".configuration-layout").dataset.dirty};});
    assert(section.shown&&section.label.length>0&&/ s$/.test(section.elapsed),`the section names its loading ${JSON.stringify(section)}`);
    assert.strictEqual(section.cancels,0,"no visible cancel button next to the edited form");
    assert.strictEqual(section.dirty,"true");
    await page.waitForSelector('[data-group="access"] .skeleton-status',{state:"detached",timeout:10000});
    const after=await page.evaluate(()=>({kept:document.querySelector(".configuration-layout").__probe,ssid:document.querySelector('input[name="wifi_ssid"]').value,dirty:document.querySelector(".configuration-layout").dataset.dirty,filled:document.querySelectorAll('[data-group="access"] *').length>0,cancelled:document.querySelectorAll('[data-notice="load-cancelled"]').length}));
    assert.deepStrictEqual(after,{kept:"kept",ssid:"Edited while loading",dirty:"true",filled:true,cancelled:0});
    accessDelay=0;
    // The same wording in German.
    await setLanguage(page,"de");
    projectDelay=1500;mapDelay=3500;
    await observeLoading("de",require(root+"/i18n.js").v2de["Loading projects"]);
    await page.waitForSelector(".map-workspace");
    assert.deepStrictEqual(errors,[]);
    await browser.close();console.log("project-loading=ok (8.5s overview + 8.5s map, independent live failure/recovery, details, navigation cancellation)");return;
  }
  if(process.argv.includes("--feedback-only")){
    const errors=[];page.on("pageerror",error=>errors.push(error.message));
    const i18n=require(root+"/i18n.js"),en=i18n.dictionaries.en,deDict=i18n.dictionaries.de,v2de=i18n.v2de;
    // Test access (r3-5): only this served copy of app.js carries window.__rtkFeedbackTest; a missing name is a product failure.
    const hook=(name,...args)=>page.evaluate(([name,args])=>{const fn=window.__rtkFeedbackTest[name];if(typeof fn!=="function")throw new Error("product function "+name+" missing");return fn(...args);},[name,args]);
    const hookStoreSet=patch=>page.evaluate(value=>window.__rtkFeedbackTest.store.set(value),patch);
    const hookError=message=>page.evaluate(m=>window.__rtkFeedbackTest.showError(new Error(m)),message);
    assert.strictEqual(await page.evaluate(()=>typeof window.__rtkFeedbackTest),"object","harness: feedback test access missing");
    assert.strictEqual(typeof hookStoreSet,"function");
    // German dictionary text of an exact length (the long failure of the flow and dock checks).
    const germanText=length=>Object.values(deDict).filter(value=>typeof value==="string"&&!value.includes("{")).join(" ").slice(0,length-1)+".";
    const LONG_DE=germanText(240);
    assert.strictEqual(LONG_DE.length,240);
    const dockItems=()=>page.evaluate(()=>[...document.querySelectorAll("#feedback-dock .feedback-item")].map(item=>({key:item.dataset.key,kind:[...item.classList].find(name=>name!=="feedback-item"),text:item.querySelector(".feedback-text").textContent})));
    // An expired entry is gone; a loaded host may run the expiry timer late, so allow 2 s beyond the waited time.
    const expired=(selector,message)=>page.waitForFunction(selector=>!document.querySelector(selector),selector,{timeout:2000}).catch(()=>assert.fail(message));
    const dismissKey=key=>page.evaluate(key=>document.querySelector('#feedback-dock .feedback-item[data-key="'+key+'"] [data-feedback-dismiss]').click(),key);
    const dismissAll=async()=>{await page.evaluate(()=>{for(const button of document.querySelectorAll("#feedback-dock [data-feedback-dismiss]"))button.click();});await page.waitForFunction(()=>!document.querySelector("#feedback-dock .feedback-item"));};

    console.log("feedback: dock");
    await page.setViewportSize({width:360,height:800});
    assert.strictEqual(await page.locator("#feedback-dock").count(),1,"#feedback-dock missing");
    // (1) A success from a real action: shown in the dock, gone after 4.5 s.
    await go(page,"measure/setup",".field-setup-layout");
    await page.getByRole("button",{name:"Save field setup"}).click();
    await page.waitForSelector(".feedback-dock .feedback-item.ok .feedback-text");
    assert.strictEqual(await page.locator(".feedback-dock .feedback-item.ok .feedback-text").innerText(),"Field setup saved");
    await page.waitForTimeout(4500);
    await expired(".feedback-dock .feedback-item.ok","success must expire after 4 s");
    // (2) A warning stays until it is dismissed, also with the keyboard.
    fieldStatus.enabled=true;live.field_diagnostics=fieldStatus;
    await go(page,"device/diagnostics",".field-diagnostics-card");
    const card=page.locator(".field-diagnostics-card"),download=card.locator("a.btn"),warnText="Stop field diagnostics before downloading.";
    await download.dispatchEvent("click");
    await page.waitForSelector(".feedback-dock .feedback-item.warn");
    await page.waitForTimeout(11500);
    assert.deepStrictEqual(await dockItems(),[{key:warnText,kind:"warn",text:warnText}],"warning stays > 11 s");
    await page.locator(".feedback-dock .feedback-item.warn [data-feedback-dismiss]").focus();
    await page.keyboard.press("Enter");
    await page.waitForFunction(()=>!document.querySelector(".feedback-dock .feedback-item.warn"));
    // (3) POST failures: the server message, a 240 character German message verbatim; with a warning, the failure is on top.
    failFieldPost=true;
    await card.getByRole("button",{name:"Add marker",exact:true}).click();
    await page.waitForSelector(".feedback-dock .feedback-item.critical");
    assert((await page.locator(".feedback-dock .feedback-item.critical .feedback-text").innerText()).includes("storage busy"));
    failFieldPost=LONG_DE;
    await card.getByRole("button",{name:"Add marker",exact:true}).click();
    await page.waitForFunction(text=>{const node=document.querySelector(".feedback-dock .feedback-item.critical .feedback-text");return node&&node.textContent===text;},LONG_DE);
    failFieldPost=false;
    await download.dispatchEvent("click");
    await page.waitForFunction(()=>document.querySelectorAll(".feedback-dock .feedback-item").length===2);
    assert.deepStrictEqual((await dockItems()).map(item=>item.kind),["critical","warn"],"failure above the warning");
    const boxes=await page.evaluate(()=>[...document.querySelectorAll(".feedback-dock .feedback-item")].map(node=>{const r=node.getBoundingClientRect();return{x:r.x,y:r.y,width:r.width,height:r.height};}));
    assert(!intersects(boxes[0],boxes[1]),"dock entries overlap: "+JSON.stringify(boxes));
    // (4) The same warning three times is one entry.
    for(let index=0;index<3;index++)await download.dispatchEvent("click");
    assert.strictEqual((await dockItems()).filter(item=>item.kind==="warn").length,1,"one entry per warning");
    // (5) Entries carry no live role; the one polite region holds the latest text.
    assert.strictEqual(await page.evaluate(()=>document.querySelectorAll("#feedback-dock [role],#feedback-dock [aria-live]").length),0);
    assert.strictEqual(await page.locator("#rtk-feedback-live").textContent(),warnText);
    await dismissAll();
    fieldStatus.enabled=false;live.field_diagnostics=fieldStatus;

    console.log("feedback: flow");
    // In-flow dock (r2-5): it covers nothing, and showing, growing, shrinking or hiding it moves nothing above it.
    const flowGeometry=()=>page.evaluate(()=>{
      const box=node=>{const r=node.getBoundingClientRect();return{x:r.x,y:r.y,width:r.width,height:r.height,top:r.top,bottom:r.bottom};};
      const meet=(a,b)=>a.x<b.x+b.width&&b.x<a.x+a.width&&a.y<b.y+b.height&&b.y<a.y+a.height;
      const dock=document.getElementById("feedback-dock"),main=document.getElementById("app-main"),header=document.querySelector(".app-header"),nav=document.querySelector(".mobile-nav");
      const navShown=getComputedStyle(nav).display!=="none",d=box(dock),overlaps=[],covers=[];
      for(const el of main.querySelectorAll("*")){if(!el.checkVisibility())continue;const r=box(el);if(r.width>0&&r.height>0&&meet(d,r))overlaps.push(el.tagName.toLowerCase()+"."+el.className);}
      // The sticky header and the fixed navigation must stay painted wherever the dock meets them.
      for(const bar of[header,navShown?nav:null]){if(!bar)continue;const b=box(bar);if(!meet(d,b))continue;
        const x0=Math.max(d.x,b.x),x1=Math.min(d.x+d.width,b.x+b.width),y0=Math.max(d.top,b.top,0),y1=Math.min(d.bottom,b.bottom,innerHeight);
        for(const fx of[.1,.5,.9])for(const fy of[.1,.5,.9]){if(y1<=y0)continue;const hit=document.elementFromPoint(x0+(x1-x0)*fx,y0+(y1-y0)*fy);if(hit&&dock.contains(hit))covers.push(bar.className);}}
      return{dock:d,overlaps,covers,navTop:navShown?box(nav).top:innerHeight,headerBottom:box(header).bottom,navShown,scrollWidth:document.documentElement.scrollWidth,innerWidth,hidden:dock.hidden,position:getComputedStyle(dock).position,nextIsDock:main.nextElementSibling===dock,mainPadding:getComputedStyle(main).paddingBottom};
    });
    const anchorState=()=>page.evaluate(()=>{const action=window.__flowAnchor,r=action.getBoundingClientRect(),dock=document.getElementById("feedback-dock"),d=dock.getBoundingClientRect();
      const meet=(a,b)=>a.x<b.x+b.width&&b.x<a.x+a.width&&a.y<b.y+b.height&&b.y<a.y+a.height;
      return{scrollY,top:r.top,left:r.left,bottom:r.bottom,width:r.width,focused:document.activeElement===action,overlap:!dock.hidden&&meet(r,d)};});
    const sizes=[{width:320,height:480},{width:360,height:560},...[320,360,390,480,1280].map(width=>({width,height:800}))];
    // Eight more empty projects make the projects page scroll at every size, so an action can sit near the bottom bar.
    const savedProjects=tracking.projects.slice();
    tracking.projects.push(...Array.from({length:8},(_,index)=>({id:"pf"+index,name:"Flow "+index,line_count:0,point_count:0,created_at:600+index,updated_at:600+index})));
    for(const lang of["en","de"]){
      await setLanguage(page,lang);
      const okText=(lang==="de"?deDict:en)["ui.field_setup_saved"],warning=lang==="de"?v2de[warnText]:warnText;
      for(const size of sizes)for(const [hash,selector] of[["projects",".project-list"],["device",".device-links"]]){
        const label=`${lang} ${size.width}x${size.height} ${hash}`;
        await page.setViewportSize(size);
        await go(page,hash,selector);
        await page.evaluate(()=>window.scrollTo(0,0));
        const initial=await flowGeometry();
        // (a) a static block right behind <main>
        assert.strictEqual(initial.position,"static",label+": dock position");
        assert.strictEqual(initial.nextIsDock,true,label+": dock follows #app-main");
        assert.strictEqual(initial.hidden,true,label+": empty dock hidden");
        // (b) three entries at the top, the middle and the end of the page
        await hook("showFeedback",{key:"f-crit",kind:"critical",text:LONG_DE});
        await hook("showFeedback",{key:"f-warn",kind:"warn",text:warning});
        await hook("showFeedback",{key:"f-ok",kind:"ok",text:okText,ttl:30000});
        for(const where of["top","middle","end"]){
          await page.evaluate(where=>{const max=document.documentElement.scrollHeight-innerHeight;scrollTo(0,where==="top"?0:where==="middle"?max/2:document.documentElement.scrollHeight);},where);
          const geometry=await flowGeometry();
          assert.deepStrictEqual(geometry.overlaps,[],`${label} ${where}: dock overlaps content`);
          assert.deepStrictEqual(geometry.covers,[],`${label} ${where}: dock covers header or navigation`);
          assert(geometry.scrollWidth<=geometry.innerWidth,`${label} ${where}: horizontal overflow`);
          if(where==="end"){
            if(geometry.navShown)assert(geometry.dock.bottom<=geometry.navTop+0.5,`${label}: dock ends above the navigation (${geometry.dock.bottom} > ${geometry.navTop})`);
            const last=await page.evaluate(()=>{const buttons=document.querySelectorAll("#feedback-dock [data-feedback-dismiss]"),button=buttons[buttons.length-1];button.focus();const r=button.getBoundingClientRect();return{top:r.top,bottom:r.bottom};});
            const after=await flowGeometry();
            assert(last.top>=after.headerBottom-0.5&&last.bottom<=after.navTop+0.5,`${label}: last Dismiss visible ${JSON.stringify(last)} header ${after.headerBottom} nav ${after.navTop}`);
          }
        }
        await dismissAll();
        // (c) no jump: an action near the bottom keeps scroll position, rectangle and focus.
        await page.evaluate(()=>window.scrollTo(0,0));
        const placed=await page.evaluate(()=>{
          const main=document.getElementById("app-main"),nav=document.querySelector(".mobile-nav"),limit=getComputedStyle(nav).display!=="none"?nav.getBoundingClientRect().top:innerHeight;
          const maxScroll=document.documentElement.scrollHeight-innerHeight,visible=el=>el.checkVisibility()&&el.getBoundingClientRect().width>0;
          // The last visible primary action that can be scrolled to 16 px above the bottom bar, else the last such button,
          // else the last such link button (on short pages the buttons sit too high to reach the bottom bar), else the last
          // area link of the device overview (#24-UI: its restart button moved to Maintenance). A page that
          // cannot scroll that far (1280 x 800 device) keeps its last action as low as it gets.
          const reachable=el=>{const target=scrollY+el.getBoundingClientRect().bottom-(limit-16);return target>=0&&target<=maxScroll;};
          const pick=(list,test)=>list.filter(el=>visible(el)&&test(el)).pop(),groups=["button.primary","button","a.btn",".device-links a"].map(selector=>[...main.querySelectorAll(selector)]);
          let action=null;for(const group of groups)action=action||pick(group,reachable);
          const clamped=!action;for(const group of groups)action=action||pick(group,()=>true);
          if(!action)return null;
          window.__flowAnchor=action;
          scrollTo(0,Math.max(0,Math.min(maxScroll,scrollY+action.getBoundingClientRect().bottom-(limit-16))));
          action.focus();
          return{gap:limit-action.getBoundingClientRect().bottom,clamped,text:action.textContent.trim()};
        });
        assert(placed,label+": no visible action");
        if(placed.clamped)console.log(`feedback: ${label} cannot scroll its last action to the bottom bar (gap ${Math.round(placed.gap)} px)`);
        assert(placed.clamped?placed.gap>=0:placed.gap>=8&&placed.gap<=24,`${label}: action gap ${JSON.stringify(placed)}`);
        const before=await anchorState();
        assert.strictEqual(before.focused,true,label+": action focused");
        const steps=[
          ["success",()=>hook("showFeedback",{key:"f-ok",kind:"ok",text:okText,ttl:4000})],
          ["warning",()=>hook("showFeedback",{key:"f-warn",kind:"warn",text:warning})],
          ["failure",()=>hook("showFeedback",{key:"f-crit",kind:"critical",text:LONG_DE})],
          ["expiry",async()=>{await page.waitForTimeout(4500);await expired('#feedback-dock .feedback-item[data-key="f-ok"]',label+": success expired");}],
          ["dismiss",()=>dismissKey("f-warn")]];
        for(const [name,step] of steps){
          await step();
          const now=await anchorState();
          assert.strictEqual(now.scrollY,before.scrollY,`${label} ${name}: scrollY`);
          for(const key of["top","left","bottom","width"])assert(Math.abs(now[key]-before[key])<1,`${label} ${name}: action ${key} ${before[key]} -> ${now[key]}`);
          assert.strictEqual(now.focused,true,`${label} ${name}: focus kept`);
          assert.strictEqual(now.overlap,false,`${label} ${name}: action meets the dock`);
        }
        // (d) all closed: hidden again, #app-main keeps its own bottom padding; (e) no sideways overflow.
        await dismissAll();
        const closed=await flowGeometry();
        assert.strictEqual(closed.hidden,true,label+": dock hidden after closing");
        assert.strictEqual(closed.mainPadding,initial.mainPadding,label+": #app-main padding restored");
        assert(closed.scrollWidth<=closed.innerWidth,label+": horizontal overflow");
      }
    }

    tracking.projects=savedProjects;

    console.log("feedback: lifecycle");
    // One entry follows the page changes, also while every live poll hangs (Codex 5).
    await setLanguage(page,"en");
    await page.setViewportSize({width:360,height:800});
    await go(page,"projects",".project-list");
    await hookError("Project storage failed.");
    blockLive();
    try{
      for(let round=0;round<3;round++)for(const [hash,selector] of[["device",".device-links"],["projects",".project-list"]]){
        await go(page,hash,selector);
        assert.strictEqual(await page.evaluate(()=>[...document.querySelectorAll('.feedback-item[data-key="action"]')].filter(node=>node.checkVisibility()).length),1,`lifecycle round ${round} ${hash}`);
      }
    }finally{releaseLive();}
    await dismissAll();

    console.log("feedback: capture");
    // Capture strip (#21c Task 4): 360 x 800 EN, project active, gate open; the fixture tags each applied point with the POST's request_id.
    const capSaved={fix:structuredClone(live.current_fix),accepted:live.current_quality.accepted,project:structuredClone(live.active_project),line:live.active_line,
      undo:live.undo_candidate,capacity:structuredClone(live.capacity),progress:structuredClone(live.measurement_progress),projects:structuredClone(tracking.projects),startup:live.tracking_startup};
    const shotDir=process.env.RTK_SHOT_DIR;if(shotDir)fs.mkdirSync(shotDir,{recursive:true});
    const shot=async name=>{if(shotDir)await page.screenshot({path:`${shotDir}/capture-${name}.png`});};
    const nextPoll=async()=>{await page.waitForResponse(response=>response.url().endsWith("/api/ui/live")&&response.status()===200);await page.waitForTimeout(300);};
    const STRIP=".field-actions > .capture-strip";
    const captureItem=()=>page.evaluate(selector=>{const item=document.querySelector(selector+' .feedback-item[data-key="capture"]');if(!item)return null;
      return{kind:[...item.classList].find(name=>name!=="feedback-item"),text:item.querySelector(".feedback-text").textContent,reasons:[...item.querySelectorAll(".feedback-reasons li")].map(li=>li.textContent),
        actions:[...item.querySelectorAll(".feedback-actions button")].map(button=>({text:button.textContent,disabled:button.disabled,write:button.hasAttribute("data-feedback-write")}))};},STRIP);
    const waitCapture=(kind,text,timeout=8000)=>page.waitForFunction(([selector,kind,text])=>{const item=document.querySelector(selector+' .feedback-item[data-key="capture"]');
      return Boolean(item)&&item.classList.contains(kind)&&(text==null||item.querySelector(".feedback-text").textContent===text);},[STRIP,kind,text],{timeout}).catch(async()=>assert.fail(`capture entry ${kind} "${text}" expected, got ${JSON.stringify(await captureItem())}`));
    const undoOf=item=>item&&item.actions.find(action=>action.text==="Undo");
    const barButtons=()=>page.evaluate(()=>[...document.querySelectorAll(".field-actions button")].map(button=>({text:button.textContent,disabled:button.disabled,dismiss:button.hasAttribute("data-feedback-dismiss"),strip:Boolean(button.closest(".capture-strip"))})));
    const assertBarLocked=async label=>{const list=await barButtons();assert(list.some(button=>!button.strip),label+": bar has actions");for(const button of list)assert.strictEqual(button.disabled,!button.dismiss,`${label}: ${button.text} ${button.dismiss?"usable":"locked"}`);};
    const waitBarFree=(label,timeout=6000)=>page.waitForFunction(()=>{const own=[...document.querySelectorAll(".field-actions > button")];return own.length>0&&own.some(button=>!button.disabled)&&!document.querySelector(".capture-strip .feedback-item.running");},null,{timeout}).catch(()=>assert.fail(label+": bar must be free again"));
    const clickBar=name=>page.evaluate(name=>{const button=[...document.querySelectorAll(".field-actions > button")].find(node=>node.textContent===name);if(!button)throw new Error("bar button "+name+" missing");button.click();},name);
    const clickStrip=name=>page.evaluate(([selector,name])=>{const button=[...document.querySelectorAll(selector+" button")].find(node=>node.textContent===name);if(!button)throw new Error("strip button "+name+" missing");button.click();},[STRIP,name]);
    const posts=()=>trackingPostLog.map(post=>post.action);
    const clearPosts=()=>{trackingPostLog.length=0;};
    const lastPost=action=>[...trackingPostLog].reverse().find(post=>post.action===action);
    const waitPost=async action=>{for(let index=0;index<60&&!lastPost(action);index++)await page.waitForTimeout(50);assert(lastPost(action),action+" POST expected");return lastPost(action);};
    const sheet=page.locator("dialog.action-sheet");
    const dialogText=async()=>{await sheet.waitFor();return sheet.locator("p.muted").first().innerText();};
    const noDialog=async label=>{await page.waitForTimeout(300);assert.strictEqual(await page.locator("dialog.action-sheet").count(),0,label+": no dialog");};
    const liveFetches=()=>page.evaluate(()=>window.__liveFetches.slice());
    const recordFeedbackLive=()=>page.evaluate(()=>{window.__feedbackLive=[];const node=document.getElementById("rtk-feedback-live");if(window.__feedbackLiveObserver)window.__feedbackLiveObserver.disconnect();
      window.__feedbackLiveObserver=new MutationObserver(()=>{const list=window.__feedbackLive,text=node.textContent;if(list[list.length-1]!==text)list.push(text);});window.__feedbackLiveObserver.observe(node,{childList:true,characterData:true,subtree:true});});
    const feedbackLive=()=>page.evaluate(()=>window.__feedbackLive.slice());
    const hookCapturedPoint=candidate=>page.evaluate(c=>{const t=window.__rtkFeedbackTest;for(const n of ["showFeedback","capturedPointSpec"])if(typeof t[n]!=="function")throw new Error("product function "+n+" missing");t.showFeedback(Object.assign({key:"capture"},t.capturedPointSpec("Object point saved",c)));},candidate);
    const clearStrip=()=>page.evaluate(()=>{for(const button of document.querySelectorAll(".capture-strip [data-feedback-dismiss],#feedback-dock [data-feedback-dismiss]"))button.click();});
    const LINE={id:"l7",name:"Field route",state:"recording",recording_mode:"route",vertex_count:12,length_m:8.5,recording_sec:30};
    const resetCapture=async({line=false,points=31}={})=>{
      actionAnswers={};liveAnswers=[];checkAnswers=[];
      captureState={epoch:"a1b2c3d4",stack:[],order:40};
      live.measurement_progress={active:false,collected:0,total:5};delete live.tracking_startup;
      live.active_project={id:"p1",name:"Project",line_count:1,point_count:points};projectRow().point_count=points;
      live.active_line=line?Object.assign({},LINE):null;
      if(line)pushCandidate({kind:"vertex",feature_id:"l7",line_id:"l7",label:"Field route · V-12",index:12,record_order:++captureState.order});
      syncCandidate();
      await clearStrip();
      await nextPoll();
      await page.waitForFunction(()=>!document.querySelector(".capture-strip .feedback-item"),null,{timeout:3000}).catch(()=>assert.fail("strip must be empty after the reset"));
      clearPosts();
    };
    const openAsset=async name=>{await clickBar("Object point");await page.locator("dialog.asset-sheet").waitFor();await page.locator('dialog.asset-sheet input[name="name"]').fill(name);};
    // The asset sheet's history entry is popped after the measurement sheet opened, and the popstate handler closes that
    // sheet at once (src/20-navigation.js, outside #21c): object point cases follow the strip, not the sheet.
    const saveAsset=()=>page.locator("dialog.asset-sheet").getByRole("button",{name:"Save point"}).click();
    const addAssetFlow=async(name="Valve 21c")=>{await openAsset(name);await saveAsset();};
    // The request to cancel, 1 s into the measurement: the sheet's button for a line point; for an object point the same
    // POST from the page, because its sheet does not stay open (see saveAsset).
    const cancelMeasurement=async kind=>{if(kind==="vertex")await page.locator(".measurement-sheet[open]").waitFor();await page.waitForTimeout(1000);
      if(kind==="vertex")await page.locator(".measurement-sheet").getByRole("button",{name:"Cancel measurement"}).click();
      else await page.evaluate(()=>fetch("/api/tracking",{method:"POST",headers:{"Content-Type":"application/json","X-CSRF-Token":"csrf"},body:JSON.stringify({action:"cancel_measurement"})}).catch(()=>null));};
    const assertNoUndo=async label=>{const item=await captureItem();assert(!undoOf(item),label+": no Undo "+JSON.stringify(item));};
    try{
      await setLanguage(page,"en");
      await page.setViewportSize({width:360,height:800});
      live.current_fix=Object.assign({},capSaved.fix,{qual:4,fix_status_text:"RTK_FIXED",stale:false,fix_seq:(capSaved.fix.fix_seq||0)+50});
      live.current_quality.accepted=true;setVerdict([]);
      await resetCapture();
      await page.waitForFunction(selector=>Boolean(document.querySelector(selector)),STRIP,{timeout:4000}).catch(()=>assert.fail("capture strip missing"));
      assert.strictEqual(await page.evaluate(selector=>document.querySelector(selector).parentNode.lastElementChild===document.querySelector(selector),STRIP),true,"strip is the last child");

      console.log("feedback: capture U-07 start line");
      actionAnswers.start_line={delay:1500};
      await clickBar("Start line");
      await sheet.getByRole("button",{name:"Start recording"}).waitFor();
      await page.locator('dialog.action-sheet input[name="name"]').fill("Field route");
      await page.locator('dialog.action-sheet select[name="recording_mode"]').selectOption("route");
      const servedBefore=lastLiveServedAt;
      await sheet.getByRole("button",{name:"Start recording"}).click();
      await waitCapture("running","Starting line …",1400);
      await assertBarLocked("start line running");
      if(lastLiveServedAt!==servedBefore)console.log("feedback: a poll was answered before the running state was checked");
      await shot("start-running");
      assert.match((await waitPost("start_line")).request_id||"",/^r[a-z0-9]{6,20}$/,"start_line carries a request_id");
      await waitCapture("ok","Line recording started");
      await waitBarFree("start line done");
      await resetCapture();
      actionAnswers.start_line={status:400,body:{error:"invalid_tracking_request",message:"Select a project first."}};
      await clickBar("Start line");
      await page.locator('dialog.action-sheet input[name="name"]').fill("Field route");
      await page.locator('dialog.action-sheet select[name="recording_mode"]').selectOption("route");
      await sheet.getByRole("button",{name:"Start recording"}).click();
      await waitCapture("critical","Select a project first.");
      await shot("start-refused");
      await page.waitForTimeout(7200);
      assert.strictEqual((await captureItem()).kind,"critical","a refusal stays > 7 s");

      console.log("feedback: capture U-07 line point");
      await resetCapture({line:true});
      actionAnswers.add_vertex={delay:1500};
      await clickBar("Add line point");
      await page.locator(".measurement-sheet[open]").waitFor();
      await waitCapture("running","Saving line point …",1400);
      assert.strictEqual(await page.locator(".measurement-sheet[open]").count(),1,"measurement dialog in front of the running strip");
      assert.match((await waitPost("add_vertex")).request_id||"",/^r[a-z0-9]{6,20}$/);
      await waitCapture("ok","Line point saved · Field route · V-13");
      assert(undoOf(await captureItem()),"line point success offers Undo");
      await shot("vertex-ok");
      await resetCapture({line:true});
      actionAnswers.add_vertex={status:400,body:{error:"invalid_tracking_request",message:"Changed text.",reasons:["rtk_fixed_required","too_few_satellites"]}};
      await clickBar("Add line point");
      await waitCapture("critical","Measurement could not be completed.");
      assert.deepStrictEqual((await captureItem()).reasons,[en["quality.rtk_fixed_required"],en["quality.too_few_satellites"]]);
      await shot("vertex-refused");

      console.log("feedback: capture U-07 object point");
      await resetCapture();
      actionAnswers.add_asset={delay:1500};
      await addAssetFlow();
      await waitCapture("running","Saving object point …",1400);
      assert.match((await waitPost("add_asset")).request_id||"",/^r[a-z0-9]{6,20}$/);
      await waitCapture("ok","Object point saved · Valve 21c");
      assert(undoOf(await captureItem()),"object point success offers Undo");
      await resetCapture();
      actionAnswers.add_asset={status:400,body:{error:"invalid_tracking_request",message:"Changed text.",reasons:["gst_required"]}};
      await addAssetFlow();
      await waitCapture("critical","Measurement could not be completed.");
      assert.deepStrictEqual((await captureItem()).reasons,[en["quality.gst_required"]]);

      console.log("feedback: capture U-07 pause, resume, finish, undo");
      await resetCapture({line:true});
      for(const [action,button,running,done] of [["pause","Pause","Pausing line …","Line paused"],["resume","Resume","Resuming line …","Line resumed"],["finish","Finish line","Finishing line …","Line finished"]]){
        actionAnswers[action]={delay:1500};
        await clickBar(button);
        if(action==="finish")await sheet.getByRole("button",{name:"Finish line"}).click();
        await waitCapture("running",running,1400);
        await assertBarLocked(action+" running");
        assert.strictEqual("request_id" in await waitPost(action),false,action+" carries no request_id");
        await waitCapture("ok",done);
        await waitBarFree(action+" done");
      }
      for(const [action,button] of [["pause","Pause"],["resume","Resume"],["finish","Finish line"]]){
        await resetCapture({line:true});
        if(action==="resume"){live.active_line.state="paused";await nextPoll();}
        actionAnswers[action]={status:400,body:{error:"invalid_tracking_request",message:"No line is currently recording."}};
        await clickBar(button);
        if(action==="finish")await sheet.getByRole("button",{name:"Finish line"}).click();
        await waitCapture("critical","No line is currently recording.");
      }
      await resetCapture({line:true});
      await clickBar("Undo last point");
      assert.strictEqual(await dialogText(),"Remove line point “Field route · V-12”?");
      actionAnswers.undo={delay:1500};
      await sheet.getByRole("button",{name:"Undo last point"}).click();
      await waitCapture("running","Removing last point …",1400);
      await waitCapture("ok","Field route · V-12 removed");
      assert.deepStrictEqual(lastPost("undo").expect,{inventory_epoch:"a1b2c3d4",kind:"vertex",feature_id:"l7",index:12,record_order:41});
      await resetCapture({line:true});
      actionAnswers.undo={status:400,body:{error:"invalid_tracking_request",message:"No recorded point can be undone."}};
      await clickBar("Undo last point");
      await sheet.getByRole("button",{name:"Undo last point"}).click();
      await waitCapture("critical","No recorded point can be undone.");

      console.log("feedback: capture cancel");
      for(const kind of ["vertex","asset"]){
        const action=kind==="vertex"?"add_vertex":"add_asset",okText=kind==="vertex"?"Line point saved · Field route · V-13":"Object point saved · Valve 21c";
        const start=async()=>{if(kind==="vertex")await clickBar("Add line point");else await addAssetFlow();};
        const count=()=>kind==="vertex"?live.active_line.vertex_count:live.active_project.point_count;
        // (1) confirmed cancel
        await resetCapture({line:kind==="vertex"});
        let before=count();
        actionAnswers[action]={delay:3000,cancelable:true};
        await start();await cancelMeasurement(kind);
        await waitCapture("info","Measurement cancelled");
        await assertNoUndo(kind+" cancelled");
        assert.strictEqual(count(),before,kind+": nothing stored");
        assert.deepStrictEqual(posts(),[action,"cancel_measurement"]);
        await page.waitForTimeout(4500);
        await expired(STRIP+' .feedback-item[data-key="capture"]',kind+": cancel notice expires");
        // (2) success despite the request to cancel
        await resetCapture({line:kind==="vertex"});
        before=count();
        actionAnswers[action]={delay:3000,cancelable:false,apply:true};
        await start();await cancelMeasurement(kind);
        await waitCapture("ok",okText);
        assert(undoOf(await captureItem()),kind+": success after a cancel request offers Undo");
        assert.strictEqual(count(),before+1,kind+": stored");
        // (3) cancel request, answer lost
        for(const apply of [true,false]){
          await resetCapture({line:kind==="vertex"});
          actionAnswers[action]={delay:3000,cancelable:false,apply,abort:true};
          checkAnswers=[{delay:1200}];
          await start();await cancelMeasurement(kind);
          await waitCapture("running","Result unknown – checking the device …");
          if(apply){await waitCapture("ok","Saved – confirmed by the device · "+okText.split(" · ").slice(1).join(" · "));assert(undoOf(await captureItem()));}
          else{await waitCapture("warn","Result unclear – check the project");await assertNoUndo(kind+" lost, not applied");}
        }
        // (4) the answer to the cancel request decides nothing
        await resetCapture({line:kind==="vertex"});
        actionAnswers.cancel_measurement={abort:true};
        actionAnswers[action]={delay:3000,apply:true};
        await start();await cancelMeasurement(kind);
        await waitCapture("ok",okText);
      }
      // (5) the sheet closed with Escape, the page left and opened again: the bar's Cancel measurement stays usable
      // with fresh data while the own capture still runs; the rest of the bar stays locked.
      await resetCapture({line:true});
      const vertexCount5=live.active_line.vertex_count;
      actionAnswers.add_vertex={delay:12000,cancelable:true};
      await clickBar("Add line point");
      await page.locator(".measurement-sheet[open]").waitFor();
      live.measurement_progress={active:true,mode:"fixed",phase:"collecting",collected:1,total:5,elapsed_sec:2,rejected_samples:0};
      await page.keyboard.press("Escape");
      await page.waitForFunction(()=>!document.querySelector(".measurement-sheet[open]"));
      await page.waitForTimeout(300);
      await go(page,"projects",".project-list");
      await go(page,"status",".readiness-hero");
      const resumeCancel=page.locator(".field-actions .measurement-resume button",{hasText:"Cancel measurement"});
      try{
        await resumeCancel.waitFor();
        await waitCapture("running","Saving line point …");
        // The lock runs every second: two ticks after the bar was drawn.
        await page.waitForTimeout(2200);
        assert.strictEqual(await resumeCancel.isDisabled(),false,"Cancel measurement usable while the own capture runs");
        assert.strictEqual(await page.locator(".field-actions > button",{hasText:"Add line point"}).isDisabled(),true,"Add line point stays locked");
        clearPosts();
        await resumeCancel.click();
        await waitCapture("info","Measurement cancelled");
        assert.deepStrictEqual(posts(),["cancel_measurement"]);
        assert.strictEqual(live.active_line.vertex_count,vertexCount5,"nothing stored after the cancel");
      }finally{live.measurement_progress={active:false,collected:0,total:5};}
      await resetCapture({line:true});
      actionAnswers.pause={body:{}};
      await clickBar("Pause");
      await waitCapture("ok","Line paused");

      console.log("feedback: capture double click and a foreign message");
      await resetCapture({line:true});
      actionAnswers.pause={delay:1500};
      await page.evaluate(async()=>{const button=[...document.querySelectorAll(".field-actions > button")].find(node=>node.textContent==="Pause");button.click();await new Promise(resolve=>setTimeout(resolve,50));const again=[...document.querySelectorAll(".field-actions > button")].find(node=>node.textContent==="Pause");if(again)again.click();});
      await waitCapture("running","Pausing line …",1400);
      await hookError("Other");
      await page.waitForSelector(STRIP+' .feedback-item[data-key="action"]');
      await assertBarLocked("foreign message during the run");
      assert.strictEqual((await captureItem()).kind,"running","running stays visible");
      await waitCapture("ok","Line paused");
      await waitBarFree("after the double click");
      assert.deepStrictEqual(posts(),["pause"],"exactly one pause POST");

      console.log("feedback: capture undo");
      const vertexSuccess=async()=>{const label="Field route · V-"+(live.active_line.vertex_count+1);await clickBar("Add line point");await waitCapture("ok","Line point saved · "+label);return label;};
      // (1) quick undo without a dialog, with the server-checked identity
      await resetCapture({line:true});
      let label=await vertexSuccess();
      clearPosts();
      await clickStrip("Undo");
      await waitCapture("ok",label+" removed");
      assert.deepStrictEqual(trackingPostLog,[{action:"undo",expect:{inventory_epoch:"a1b2c3d4",kind:"vertex",feature_id:"l7",index:13,record_order:42}}]);
      assert.strictEqual(await page.locator("dialog.action-sheet").count(),0,"quick undo opens no dialog");
      // (2) Undo is gone after 6.5 s
      await resetCapture({line:true});
      await vertexSuccess();
      await page.waitForTimeout(6500);
      await page.waitForFunction(selector=>![...document.querySelectorAll(selector+" button")].some(node=>node.textContent==="Undo"),STRIP,{timeout:2000}).catch(()=>assert.fail("Undo must expire after 6 s"));
      // (3) server changed, browser cache unchanged: 409, nothing removed, the current candidate named
      await resetCapture({line:true});
      label=await vertexSuccess();
      await page.waitForResponse(response=>response.url().endsWith("/api/ui/live"));
      live.active_line.vertex_count+=1;
      pushCandidate({kind:"vertex",feature_id:"l7",line_id:"l7",label:"Field route · V-14",index:14,record_order:++captureState.order});
      const vertexBefore=live.active_line.vertex_count;
      clearPosts();
      await clickStrip("Undo");
      await waitCapture("warn","The last point changed – nothing was removed.");
      assert.deepStrictEqual((await captureItem()).reasons,["Field route · V-14"]);
      assert.deepStrictEqual(posts(),["undo"]);
      assert.strictEqual(lastPost("undo").expect.index,13);
      assert.strictEqual(live.active_line.vertex_count,vertexBefore,"nothing removed");
      await noDialog("409");
      // (4) browser cache already changed: the dialog names the current candidate; cancel sends nothing
      await resetCapture({line:true});
      await vertexSuccess();
      live.active_line.vertex_count+=1;
      pushCandidate({kind:"vertex",feature_id:"l7",line_id:"l7",label:"Field route · V-14",index:14,record_order:++captureState.order});
      await nextPoll();
      clearPosts();
      await clickStrip("Undo");
      assert.strictEqual(await dialogText(),"Remove line point “Field route · V-14”?");
      assert.strictEqual(await sheet.locator("h2").innerText(),"Undo last point");
      await sheet.getByRole("button",{name:"Cancel"}).click();
      await page.waitForFunction(()=>!document.querySelector("dialog.action-sheet"));
      assert.deepStrictEqual(posts(),[]);
      // (5) a success whose candidate carries another request id offers no Undo
      await resetCapture({line:true});
      actionAnswers.add_vertex={apply:true,tag:"other"};
      await clickBar("Add line point");
      await waitCapture("ok","Line point saved");
      await assertNoUndo("foreign tag");
      // (6) r2-1: no candidate in the cache, a second browser adds one; the dialog names exactly it
      await resetCapture();
      await page.waitForResponse(response=>response.url().endsWith("/api/ui/live"));
      const fetchesBefore=(await liveFetches()).length;
      addForeignPoint("B1");
      await clickBar("Undo last point");
      assert.strictEqual(await dialogText(),"Remove object point “B1”?");
      const undoFetches=(await liveFetches()).slice(fetchesBefore);
      assert.strictEqual(undoFetches.filter(item=>item.once).length,1,"one fetchLiveOnce request: "+JSON.stringify(undoFetches));
      await sheet.getByRole("button",{name:"Cancel"}).click();
      await page.waitForFunction(()=>!document.querySelector("dialog.action-sheet"));
      assert.deepStrictEqual(posts(),[]);
      const b1=serverUndoCandidate();
      await clickBar("Undo last point");
      assert.strictEqual(await dialogText(),"Remove object point “B1”?");
      await sheet.getByRole("button",{name:"Undo last point"}).click();
      await waitCapture("ok","B1 removed");
      assert.deepStrictEqual(trackingPostLog,[{action:"undo",expect:{inventory_epoch:b1.inventory_epoch,kind:"asset",feature_id:b1.feature_id,index:null,record_order:b1.record_order}}]);
      // (7) no candidate anywhere: a notice, no dialog, no POST
      await resetCapture();
      await clickBar("Undo last point");
      await waitCapture("info","Nothing to undo – no recorded point found");
      await noDialog("nothing to undo");
      assert.deepStrictEqual(posts(),[]);
      // (8) a point arrives while the dialog is open: 409, nothing removed, the new candidate named
      await resetCapture();
      addForeignPoint("X");
      await nextPoll();
      await clickBar("Undo last point");
      assert.strictEqual(await dialogText(),"Remove object point “X”?");
      addForeignPoint("Y");
      const pointsBefore=live.active_project.point_count;
      await sheet.getByRole("button",{name:"Undo last point"}).click();
      await waitCapture("warn","The last point changed – nothing was removed.");
      assert.deepStrictEqual((await captureItem()).reasons,["Y"]);
      assert.strictEqual(lastPost("undo").expect.feature_id,"f41");
      assert.strictEqual(live.active_project.point_count,pointsBefore);
      // (9) r3-1: the inventory was replaced (same ids, indices and orders, new epoch)
      await resetCapture();
      await addAssetFlow();
      await waitCapture("ok","Object point saved · Valve 21c");
      await page.waitForResponse(response=>response.url().endsWith("/api/ui/live"));
      replaceInventory("0badf00d");
      const assetsBefore=live.active_project.point_count;
      clearPosts();
      await clickStrip("Undo");
      await waitCapture("warn","The last point changed – nothing was removed.");
      assert.deepStrictEqual((await captureItem()).reasons,["Valve 21c"]);
      assert.deepStrictEqual(posts(),["undo"]);
      assert.strictEqual(lastPost("undo").expect.inventory_epoch,"a1b2c3d4");
      assert.strictEqual(live.active_project.point_count,assetsBefore);
      await nextPoll();
      await clickBar("Undo last point");
      assert.strictEqual(await dialogText(),"Remove object point “Valve 21c”?");
      await sheet.getByRole("button",{name:"Undo last point"}).click();
      await waitCapture("ok","Valve 21c removed");
      assert.strictEqual(lastPost("undo").expect.inventory_epoch,"0badf00d");
      assert.strictEqual(live.active_project.point_count,assetsBefore-1);

      console.log("feedback: capture unknown outcome");
      const checks=async()=>(await liveFetches()).filter(item=>item.check).length;
      // (a) lost answer after the mutation: checking, locked, then confirmed by the own request id
      await resetCapture();
      actionAnswers.add_asset={apply:true,abort:true};
      checkAnswers=[{delay:1500}];
      await addAssetFlow();
      await waitCapture("running","Result unknown – checking the device …");
      await assertBarLocked("checking");
      await clickBar("Object point");
      await page.waitForTimeout(200);
      assert.strictEqual(await page.locator("dialog.asset-sheet").count(),0,"a second Object point opens nothing while checking");
      await waitCapture("ok","Saved – confirmed by the device · Valve 21c");
      assert(undoOf(await captureItem()));
      await waitBarFree("confirmed");
      assert.deepStrictEqual(posts(),["add_asset"]);
      // (a2) Codex P2: the check confirms B while no ordinary poll has answered since A; the store takes the checked
      // snapshot, so B's Undo is the quick one with B's identity (never a dialog for A and a 409).
      await resetCapture();
      await addAssetFlow("A1");
      await waitCapture("ok","Object point saved · A1");
      await nextPoll();
      blockPolls();
      try{
        // A poll answered just before the block may still land; after this pause none does until the release.
        await page.waitForTimeout(400);
        assert.strictEqual(await page.evaluate(()=>window.__rtkFeedbackTest.store.state.live.undo_candidate.label),"A1");
        actionAnswers.add_asset={apply:true,abort:true};
        await addAssetFlow();
        await waitCapture("ok","Saved – confirmed by the device · Valve 21c");
        const confirmedB=serverUndoCandidate();
        assert.strictEqual(await page.evaluate(()=>window.__rtkFeedbackTest.store.state.live.undo_candidate.label),"Valve 21c","the store holds the confirmed candidate");
        clearPosts();
        await clickStrip("Undo");
        await waitCapture("ok","Valve 21c removed");
        assert.strictEqual(await page.locator("dialog.action-sheet").count(),0,"quick undo opens no dialog");
        assert.deepStrictEqual(trackingPostLog,[{action:"undo",expect:{inventory_epoch:confirmedB.inventory_epoch,kind:"asset",feature_id:confirmedB.feature_id,index:null,record_order:confirmedB.record_order}}]);
        assert.strictEqual(serverUndoCandidate().label,"A1","A1 stays");
      }finally{releasePolls();}
      // (b) foreign +1, own not stored: unclear with Open project, never Undo
      await resetCapture();
      actionAnswers.add_asset={apply:false,abort:true,delay:800};
      await addAssetFlow();
      addForeignPoint("B1");
      await waitCapture("warn","Result unclear – check the project");
      await assertNoUndo("foreign +1");
      assert.deepStrictEqual((await captureItem()).actions.map(action=>action.text),["Open project","Dismiss"]);
      await waitBarFree("unclear");
      await clickStrip("Open project");
      await page.waitForFunction(()=>location.hash==="#projects/p1");
      await go(page,"status",".readiness-hero");
      // (c) own and foreign: still unclear
      await resetCapture();
      actionAnswers.add_asset={apply:true,abort:true,afterApply:()=>addForeignPoint("B1")};
      await addAssetFlow();
      await waitCapture("warn","Result unclear – check the project");
      await assertNoUndo("own plus foreign");
      // (d) an old or merged store snapshot never counts
      for(const apply of [false,true]){
        await resetCapture();
        actionAnswers.add_asset={apply,abort:true};
        checkAnswers=[{delay:1500}];
        await addAssetFlow();
        await waitCapture("running","Result unknown – checking the device …");
        const requestId=lastPost("add_asset").request_id,cached=await page.evaluate(()=>window.__rtkFeedbackTest.store.state.live);
        await hookStoreSet({live:Object.assign({},cached,{undo_candidate:apply?Object.assign({},cached.undo_candidate||{},{request_id:null,label:"Merged"}):
          {kind:"asset",feature_id:"a99",line_id:null,label:"Valve 21c",index:null,record_order:99,request_id:requestId,inventory_epoch:"a1b2c3d4"}})});
        if(apply)await waitCapture("ok","Saved – confirmed by the device · Valve 21c");
        else await waitCapture("warn","Result unclear – check the project");
      }
      // (e) project change in between: unclear, Open project leads to the original project
      await resetCapture();
      actionAnswers.add_asset={apply:true,abort:true};
      checkAnswers=[{active_project:{id:"p2",name:"Archive",line_count:0,point_count:0},undo_candidate:null}];
      await addAssetFlow();
      await waitCapture("warn","Result unclear – check the project");
      await clickStrip("Open project");
      await page.waitForFunction(()=>location.hash==="#projects/p1");
      await go(page,"status",".readiness-hero");
      // (f) 507 tracking_storage_failed: unclear at once, announced once
      await resetCapture();
      await recordFeedbackLive();
      const checksBefore=await checks();
      actionAnswers.add_asset={status:507,body:{error:"tracking_storage_failed",message:"Could not store tracking data."}};
      await addAssetFlow();
      await waitCapture("warn","Result unclear – check the project");
      assert.deepStrictEqual(await feedbackLive(),["Saving object point …","Result unclear – check the project"]);
      assert.strictEqual(await checks(),checksBefore,"507 starts no check");
      // (g) deadline: the device keeps measuring; 46 s later the check ends unclear
      await resetCapture();
      actionAnswers.add_asset={apply:false,abort:true};
      checkAnswers=Array.from({length:40},()=>({measurement_progress:{active:true}}));
      await addAssetFlow();
      await waitCapture("running","Result unknown – checking the device …");
      await shiftClock(46000);
      await waitCapture("warn","Result unclear – check the project",4000);
      checkAnswers=[];
      await waitBarFree("after the deadline",8000);
      // (g2) a check fetch hangs right before the deadline: it is aborted, the check ends unclear, no further fetch
      await resetCapture();
      actionAnswers.add_asset={apply:false,abort:true};
      checkAnswers=[{measurement_progress:{active:true}},{hang:true}];
      // Playwright may report one aborted request twice: count each request once.
      const failed=new Set();const onFailed=request=>{if(request.url().endsWith("/api/ui/live")&&request.headers()["x-rtk-check"]==="1")failed.add(request.url()+"@"+request.timing().startTime);};
      page.on("requestfailed",onFailed);
      try{
        const checksAtStart=await checks();
        const firstAnswer=page.waitForResponse(response=>response.url().endsWith("/api/ui/live")&&response.request().headers()["x-rtk-check"]==="1");
        await addAssetFlow();
        await firstAnswer;
        const answeredAt=Date.now(),firstCheck=await checks(),checkStart=(await liveFetches()).filter(item=>item.check)[checksAtStart].at;
        assert.strictEqual(firstCheck,checksAtStart+1,"one check fetch answered");
        // The 2 s pause was scheduled before this shift (the page's timers run in real time): the shift leaves about
        // 400 ms of the 45 s for the next, hanging fetch.
        const shiftedAt=Date.now();
        await shiftClock(42600-(answeredAt-checkStart));
        await waitCapture("warn","Result unclear – check the project",2500);
        assert(Date.now()-shiftedAt<=2500+300,"unclear within 2.5 s after the shift");
        await page.waitForTimeout(300);
        assert.strictEqual(failed.size,1,"the hanging check fetch was aborted: "+JSON.stringify([...failed]));
        const afterChecks=await checks();
        assert.strictEqual(afterChecks,firstCheck+1,"two check fetches: "+JSON.stringify({shiftedAt,answeredAt,checkStart,checks:(await liveFetches()).filter(item=>item.check).slice(checksAtStart)}));
        await page.waitForTimeout(2500);
        assert.strictEqual(await checks(),afterChecks,"no further check fetch");
        await waitBarFree("after the hanging fetch",8000);
      }finally{page.off("requestfailed",onFailed);}
      // (j) 500 after the mutation with a tag: confirmed, never a refusal
      await resetCapture();
      actionAnswers.add_asset={apply:true,status:500,body:{error:"internal_error",message:"The device encountered an internal error."}};
      await addAssetFlow();
      await waitCapture("ok","Saved – confirmed by the device · Valve 21c");
      assert(undoOf(await captureItem()));
      assert.strictEqual(await page.locator(STRIP+" .feedback-item.critical").count(),0);
      assert.deepStrictEqual(posts(),["add_asset"]);
      // (k) 500 after the mutation without a tag: unclear, the next poll shows the stored point
      await resetCapture();
      actionAnswers.add_asset={apply:true,tag:false,status:500,body:{error:"internal_error",message:"The device encountered an internal error."}};
      await addAssetFlow();
      await waitCapture("warn","Result unclear – check the project");
      await assertNoUndo("500 without tag");
      await nextPoll();
      assert((await page.locator(".status-layout").innerText()).includes("32 object points"),"project row shows the stored point");
      // (l) 503 device_busy: checked, then unclear, never a refusal
      await resetCapture();
      await recordFeedbackLive();
      actionAnswers.add_asset={apply:false,status:503,body:{error:"device_busy",message:"Not enough memory on the device; try again."}};
      await addAssetFlow();
      await waitCapture("warn","Result unclear – check the project");
      assert((await feedbackLive()).includes("Result unknown – checking the device …"),"device_busy is checked");
      assert.strictEqual(await page.locator(STRIP+" .feedback-item.critical").count(),0);
      // (m) 503 survey_loading: answered before any mutation, a refusal without a check
      await resetCapture();
      await recordFeedbackLive();
      actionAnswers.add_asset={status:503,body:{error:"survey_loading",message:"Survey storage is loading or unavailable. Please wait."}};
      await addAssetFlow();
      await waitCapture("critical","Survey storage is loading or unavailable. Please wait.");
      assert(!(await feedbackLive()).includes("Result unknown – checking the device …"),"survey_loading starts no check");
      // (n) pause with 500: no request id, unclear at once
      await resetCapture({line:true});
      const pauseChecks=await checks();
      actionAnswers.pause={status:500,body:{error:"internal_error",message:"The device encountered an internal error."}};
      await clickBar("Pause");
      await waitCapture("warn","Result unclear – check the project");
      assert.strictEqual(await checks(),pauseChecks,"no check without a request id");
      assert.strictEqual(await page.locator(STRIP+" .feedback-item.critical").count(),0);
      // (i) dismissing the unclear notice only removes it
      assert.strictEqual(await page.locator(".feedback-item.running").count(),0);
      await waitBarFree("before dismiss");
      await clickStrip("Dismiss");
      await page.waitForFunction(selector=>!document.querySelector(selector+' .feedback-item[data-key="capture"]'),STRIP);
      assert.strictEqual(await page.locator(".feedback-item.running").count(),0);
      await waitBarFree("after dismiss");
      // (h) restart with a new sign-in: the new document knows nothing of the old request
      await resetCapture();
      // After 800 ms the asset sheet's history step has landed; the measurement sheet closes with the lost answer, and its
      // own history.back() is still in flight when the check answers 401 (the redirect must wait for it, #24).
      actionAnswers.add_asset={apply:true,abort:true,delay:800};
      const loading={tracking_startup:{state:"loading"},undo_candidate:null};
      checkAnswers=[{status:401,reboot:true,next:[loading,loading]}];
      await addAssetFlow();
      await page.waitForURL(/\?return=/);
      await page.waitForSelector(".readiness-hero");
      await page.waitForFunction(()=>[...document.querySelectorAll(".field-actions > button")].some(node=>node.textContent==="Object point"),null,{timeout:10000});
      await nextPoll();
      assert.strictEqual(await page.locator('.feedback-item[data-key="capture"]').count(),0,"no capture entry in the new document");
      assert(!(await barButtons()).some(button=>button.text==="Undo"),"no Undo in the new document");
      assert.deepStrictEqual(posts(),["add_asset"],"no POST from the new document");
      assert((await page.locator(".status-layout").innerText()).includes("32 object points"),"the stored point is listed");
      assert.strictEqual(serverUndoCandidate().request_id,null,"RAM tags empty after the restart");
      // (h2, h3) The sign-in redirect is not lost to a closing sheet's history step (#24): the redirect starts while the
      // sheets' own history.back() calls are still in flight. h2: the answer is lost at once and the check answers 401;
      // h3: the request itself answers 401, so the redirect starts before the measurement sheet closes. The sign-in page
      // answers after 300 ms like a busy device, so a traversal landing meanwhile would abort the redirect every time.
      const signInLands=async(label,answer,check)=>{
        await resetCapture();
        actionAnswers.add_asset=answer;
        checkAnswers=check?[check]:[];
        const aborted=[],onFailed=request=>{if(request.isNavigationRequest())aborted.push(request.url()+" "+(request.failure()||{}).errorText);};
        page.on("requestfailed",onFailed);
        try{
          await page.evaluate(()=>{window.__beforeSignIn=true;});
          const loaded=page.waitForEvent("load",{timeout:10000});
          signInPageDelay=300;
          await addAssetFlow();
          await loaded.catch(()=>assert.fail(label+": the sign-in redirect did not load: "+JSON.stringify(aborted)));
        }finally{page.off("requestfailed",onFailed);signInPageDelay=0;}
        assert.deepStrictEqual(aborted,[],label+": no aborted navigation");
        assert.strictEqual(await page.evaluate(()=>window.__beforeSignIn),undefined,label+": a new document");
        assert(new URL(page.url()).searchParams.get("return").startsWith("/ui-v2#"),label+": sign-in return target "+page.url());
        await page.waitForSelector(".readiness-hero");
        await page.waitForFunction(()=>[...document.querySelectorAll(".field-actions > button")].some(node=>node.textContent==="Object point"),null,{timeout:10000});
        assert.strictEqual(await page.locator("dialog[open]").count(),0,label+": no sheet open in the new document");
      };
      await signInLands("h2 lost answer at once",{apply:true,abort:true},{status:401,reboot:true,next:[loading,loading]});
      await signInLands("h3 401 on the request",{status:401,body:{error:"admin_session_required",message:"Sign in again."}},null);
      assert.deepStrictEqual(posts(),["add_asset"],"h3: one POST");

      console.log("feedback: capture focus");
      await resetCapture({line:true});
      await vertexSuccess();
      await recordFeedbackLive();
      await page.evaluate(selector=>{const button=[...document.querySelectorAll(selector+" button")].find(node=>node.textContent==="Undo");window.__focusTarget=button;button.focus();},STRIP);
      await page.waitForTimeout(4500);
      assert.strictEqual(await page.evaluate(()=>document.activeElement.isSameNode(window.__focusTarget)),true,"Undo keeps the focus over two polls");
      assert.deepStrictEqual(await feedbackLive(),[],"no second announcement");
      await resetCapture({line:true});
      actionAnswers.add_vertex={status:400,body:{error:"invalid_tracking_request",message:"Changed text.",reasons:["gst_required"]}};
      await clickBar("Add line point");
      await waitCapture("critical","Measurement could not be completed.");
      await recordFeedbackLive();
      await page.evaluate(selector=>{const button=document.querySelector(selector+' .feedback-item[data-key="capture"] [data-feedback-dismiss]');window.__focusTarget=button;button.focus();},STRIP);
      await page.waitForTimeout(4500);
      assert.strictEqual(await page.evaluate(()=>document.activeElement.isSameNode(window.__focusTarget)),true,"Dismiss keeps the focus over two polls");
      assert.deepStrictEqual(await feedbackLive(),[]);

      console.log("feedback: capture loss");
      const refusalCandidate={kind:"asset",feature_id:"a77",line_id:null,label:"Valve 21c",index:null,record_order:77,request_id:"r-x",inventory_epoch:"a1b2c3d4"};
      blockLive();
      try{
        await shiftClock(11000);
        await page.waitForFunction(()=>document.querySelector(".readiness-hero").classList.contains("stale-critical"),null,{timeout:2500});
        for(const button of await barButtons())assert.strictEqual(button.disabled,!button.dismiss,`loss: ${button.text}`);
        await clickStrip("Dismiss");
        await page.waitForFunction(selector=>!document.querySelector(selector+' .feedback-item[data-key="capture"]'),STRIP);
        await hookCapturedPoint(refusalCandidate);
        const undo=undoOf(await captureItem());
        assert(undo&&undo.disabled===true,"a new Undo is locked at once: "+JSON.stringify(undo));
      }finally{releaseLive();}
      // Dock (r2-7): the lock follows the transport on every page.
      await resetCapture({line:true});
      await vertexSuccess();
      await page.evaluate(()=>{location.hash="projects";});
      await page.waitForSelector("#feedback-dock .feedback-item[data-key=\"capture\"] button[data-feedback-write]");
      assert.strictEqual(await page.locator("#feedback-dock button[data-feedback-write]").isDisabled(),false,"dock Undo usable");
      // The 6 s Undo runs on real time (Date.now), the loss on the shifted clock; a long-lived entry keeps the lock check off that race.
      await page.evaluate(c=>{const t=window.__rtkFeedbackTest;t.showFeedback(Object.assign({key:"capture"},t.capturedPointSpec("Line point saved",c),{ttl:30000}));},refusalCandidate);
      assert.strictEqual(await page.locator("#feedback-dock button[data-feedback-write]").isDisabled(),false,"long-lived dock Undo usable");
      // The mutation answer renewed the data age too; a poll answered after it makes lastLiveServedAt the page's reference again.
      await page.waitForResponse(response=>response.url().endsWith("/api/ui/live")&&response.ok());
      blockLive();
      try{
        // The projects page polls every 5 s: the loss shows after 13 s without an answer.
        const waitMore=Math.max(0,2100-(Date.now()-lastLiveServedAt));
        if(waitMore)await page.waitForTimeout(waitMore);
        await shiftClock(11000);
        await page.waitForFunction(()=>{const b=document.querySelector("#feedback-dock button[data-feedback-write]");return b&&b.disabled;},null,{timeout:1500}).catch(async()=>assert.fail("dock Undo must lock with the loss "+JSON.stringify(await page.evaluate(()=>({hash:location.hash,link:(document.getElementById("live-state")||{dataset:{}}).dataset.link,pill:(document.getElementById("live-state")||{}).textContent,dock:[...document.querySelectorAll("#feedback-dock .feedback-item")].map(item=>[item.dataset.key,item.textContent,[...item.querySelectorAll("button")].map(b=>[b.textContent,b.disabled,b.hasAttribute("data-feedback-write")])])})).catch(e=>String(e)))+" sinceServed="+(Date.now()-lastLiveServedAt)));
        assert.strictEqual(await page.locator('#feedback-dock .feedback-item[data-key="capture"] [data-feedback-dismiss]').isDisabled(),false,"dock Dismiss usable");
        const postsBefore=posts().length;
        await hook("undoCaptured",serverUndoCandidate());
        await page.waitForTimeout(300);
        assert.strictEqual(posts().length,postsBefore,"a direct call sends nothing while the data is lost");
      }finally{releaseLive();}
      await page.waitForResponse(response=>response.url().endsWith("/api/ui/live")&&response.status()===200);
      await page.waitForTimeout(1200);
      await page.waitForFunction(()=>{const b=document.querySelector("#feedback-dock button[data-feedback-write]");return b&&!b.disabled;},null,{timeout:2500}).catch(()=>assert.fail("dock Undo usable again"));
      await page.evaluate(c=>{const t=window.__rtkFeedbackTest;t.showFeedback(Object.assign({key:"capture"},t.capturedPointSpec("Object point saved",c),{ttl:30000}));},refusalCandidate);
      assert.strictEqual(await page.locator("#feedback-dock button[data-feedback-write]").isDisabled(),false,"a fresh dock Undo is usable with fresh data");
      await clearStrip();
      await go(page,"status",".readiness-hero");

      console.log("feedback: capture first button");
      await resetCapture();
      live.current_fix=Object.assign({},live.current_fix,{qual:5,fix_status_text:"RTK_FLOAT",fix_seq:live.current_fix.fix_seq+1});
      live.current_quality.accepted=false;setVerdict(["rtk_fixed_required"]);
      await nextPoll();
      await hookError("Other");
      await hookCapturedPoint(refusalCandidate);
      await nextPoll();await nextPoll();
      const firstButton=await page.evaluate(()=>{const bar=document.querySelector(".field-actions"),button=bar.querySelector(":scope > button"),reason=bar.querySelector(".action-reason"),strip=bar.querySelector(":scope > .capture-strip");
        return{text:button.textContent,disabled:button.disabled,reason:reason&&reason.textContent,stripLast:bar.lastElementChild===strip};});
      assert.deepStrictEqual(firstButton,{text:"Start line",disabled:false,reason:en["ui.start_line_route_hint"],stripLast:true});
      assert.strictEqual(undoOf(await captureItem()).disabled,false,"Undo untouched by the wrapper");
      await page.evaluate(selector=>document.querySelector(selector+' .feedback-item[data-key="action"] [data-feedback-dismiss]').click(),STRIP);
      await page.waitForFunction(selector=>!document.querySelector(selector+' .feedback-item[data-key="action"]'),STRIP);
      await clickBar("Start line");
      await sheet.getByRole("button",{name:"Start recording"}).waitFor();
      await page.keyboard.press("Escape");
      await page.waitForFunction(()=>!document.querySelector("dialog.action-sheet"));
      await hookError("Other");
      await page.evaluate(selector=>{window.__stripNode=document.querySelector(selector);},STRIP);
      live.tracking_startup={state:"loading"};
      await nextPoll();
      const loadingBar=await page.evaluate(()=>{const bar=document.querySelector(".field-actions"),muted=bar.querySelector(":scope > p.muted");return{muted:muted&&muted.textContent,same:bar.querySelector(":scope > .capture-strip")===window.__stripNode,last:bar.lastElementChild===window.__stripNode};});
      assert.deepStrictEqual(loadingBar,{muted:"Measurement actions unlock when survey storage is ready.",same:true,last:true});
      delete live.tracking_startup;
      await nextPoll();
      assert(await page.evaluate(()=>[...document.querySelectorAll(".field-actions > button")].some(node=>node.textContent==="Start line")),"normal bar again");
      live.measurement_progress={active:true,mode:"fixed",elapsed_sec:3,collected:1,total:5};
      await nextPoll();
      assert.strictEqual(await page.evaluate(selector=>document.querySelector(selector+" [data-feedback-dismiss]").disabled,STRIP),false,"Dismiss usable during a measurement");
      live.measurement_progress={active:false,collected:0,total:5};
      live.current_quality.accepted=true;setVerdict([]);
      await nextPoll();

      console.log("feedback: capture lifecycle");
      await resetCapture();
      actionAnswers.add_asset={status:400,body:{error:"invalid_tracking_request",message:"Changed text.",reasons:["gst_required"]}};
      await addAssetFlow();
      await waitCapture("critical","Measurement could not be completed.");
      blockLive();
      try{
        for(let round=0;round<3;round++){
          for(const [hash,selector] of [["projects",".project-list"],["device",".device-links"],["status",".readiness-hero"]]){
            await go(page,hash,selector);
            const state=await page.evaluate(()=>({strip:document.querySelectorAll(".capture-strip .feedback-item").length,dock:document.querySelectorAll("#feedback-dock .feedback-item").length,
              dockHidden:document.getElementById("feedback-dock").hidden,stripInMain:document.querySelectorAll("#app-main .capture-strip").length}));
            if(hash==="status")assert.deepStrictEqual([state.strip,state.dockHidden],[1,true],`lifecycle ${round} status: `+JSON.stringify(state));
            else assert.deepStrictEqual([state.dock,state.stripInMain],[1,0],`lifecycle ${round} ${hash}: `+JSON.stringify(state));
          }
        }
      }finally{releaseLive();}

      console.log("feedback: capture widths");
      const countBefore=await page.evaluate(()=>[...document.querySelectorAll(".field-actions button")].filter(button=>!button.closest(".capture-strip")).length);
      for(const lang of ["en","de"]){
        await setLanguage(page,lang);
        const dict=lang==="de"?deDict:en;
        for(const size of [...[320,360,390,480,1280].map(width=>({width,height:800})),{width:320,height:480}]){
          const label=`${lang} ${size.width}x${size.height}`;
          await page.setViewportSize(size);
          await page.evaluate(()=>scrollTo(0,0));
          // Below 560 px the empty #21a bar alone covers the hero title at scroll 0 (134 px at 320 x 480), so the strip must add nothing while empty.
          const shortView=size.height<560;
          if(shortView){
            await page.waitForFunction(selector=>!document.querySelector(selector+" .feedback-item"),STRIP);
            const empty=await page.evaluate(selector=>{const strip=document.querySelector(selector),bar=strip.parentNode,withStrip=bar.getBoundingClientRect().height,display=strip.style.display;
              strip.style.display="none";const without=bar.getBoundingClientRect().height;strip.style.display=display;return{withStrip,without};},STRIP);
            assert(Math.abs(empty.withStrip-empty.without)<=0.5,label+": the empty strip leaves the #21a bar height unchanged "+JSON.stringify(empty));
          }
          await hookCapturedPoint(refusalCandidate);
          await hook("showFeedback",{key:"action",kind:"critical",text:"Measurement could not be completed.",reasons:[dict["quality.rtk_fixed_required"],dict["quality.too_few_satellites"]]});
          // A third entry: the strip keeps its cap of two.
          if(shortView)await hook("showFeedback",{key:"f-warn",kind:"warn",text:lang==="de"?v2de[warnText]:warnText});
          const geometry=await page.evaluate(selector=>{const strip=document.querySelector(selector),bar=strip.parentNode,nav=document.querySelector(".mobile-nav"),hero=document.querySelector(".readiness-hero"),title=hero.querySelector(".hero-state h2");
            const box=node=>{const r=node.getBoundingClientRect();return{top:r.top,bottom:r.bottom,left:r.left,right:r.right,height:r.height};};
            scrollTo(0,0);const heroBox=box(title),hit=document.elementFromPoint((heroBox.left+heroBox.right)/2,(heroBox.top+heroBox.bottom)/2),heroFree=Boolean(hit&&hero.contains(hit));
            // Scrolled into view: the hero title right below the sticky header.
            const header=document.querySelector(".app-header");scrollBy(0,box(title).top-box(header).bottom);
            const shown=box(title),shownHit=document.elementFromPoint((shown.left+shown.right)/2,(shown.top+shown.bottom)/2),heroShown={title:shown,headerBottom:box(header).bottom,barTop:box(bar).top,hit:Boolean(shownHit&&hero.contains(shownHit))};
            strip.scrollIntoView({block:"nearest"});
            return{strip:box(strip),bar:box(bar),nav:getComputedStyle(nav).display!=="none"?box(nav):null,innerHeight,innerWidth,scrollWidth:document.documentElement.scrollWidth,heroFree,heroShown,items:strip.querySelectorAll(".feedback-item").length};},STRIP);
          await shot("widths-"+label.replace(/\s/g,"-"));
          assert.strictEqual(geometry.items,2,label+": two entries");
          assert(geometry.strip.top>=geometry.bar.top-0.5&&geometry.strip.bottom<=geometry.bar.bottom+0.5,label+": strip inside the bar "+JSON.stringify(geometry));
          assert(geometry.strip.top>=-0.5&&geometry.strip.bottom<=geometry.innerHeight+0.5,label+": strip in the viewport "+JSON.stringify(geometry));
          assert(geometry.strip.height<=0.3*geometry.innerHeight+0.5,label+": strip height "+geometry.strip.height);
          if(geometry.nav&&size.width<900)assert(geometry.bar.bottom<=geometry.nav.top+0.5,label+": bar above the navigation "+JSON.stringify(geometry));
          if(!shortView)assert.strictEqual(geometry.heroFree,true,label+": hero not covered at scroll 0");
          else{
            const shown=geometry.heroShown;
            assert(shown.hit&&shown.title.top>=shown.headerBottom-0.5&&shown.title.bottom<=shown.barTop+0.5,label+": hero title fully visible above the action bar once scrolled into view "+JSON.stringify(shown));
            assert(geometry.items<=2,label+": at most two strip entries");
            await hook("dismissFeedback","f-warn");
          }
          assert(geometry.scrollWidth<=geometry.innerWidth,label+": horizontal overflow");
          await clearStrip();
        }
      }
      await setLanguage(page,"en");
      await page.setViewportSize({width:360,height:800});
      await page.waitForFunction(()=>!document.querySelector(".capture-strip .feedback-item"));
      assert.strictEqual(await page.evaluate(()=>[...document.querySelectorAll(".field-actions button")].filter(button=>!button.closest(".capture-strip")).length),countBefore,"bar buttons back to their count");

      console.log("feedback: capacity #45");
      // Issue reproduction (82 % of the limit, measurement store ready) and the guaranteed place for a capture under a critical limit.
      const setCapacity=used=>{live.capacity={used,limit:5000,remaining:5000-used,percent:Math.floor(used*100/5000),writable:true,
        level:used>=5000?"full":used>=4900?"critical":used>=4500?"warning":used>=4000?"notice":"ok"};};
      const CAP_ENTRY='.feedback-item[data-key="capacity"]',CAP_DOCK="#feedback-dock "+CAP_ENTRY;
      const capFree=async(selector,label)=>assert.deepStrictEqual(await paintProblems(page,selector),[],label+": "+selector);
      const capGeometry=async(label)=>{
        // The entry in the page flow at the end of the page: fully inside the viewport, between the header and the navigation.
        await page.evaluate(()=>new Promise(resolve=>{scrollTo({top:document.documentElement.scrollHeight,behavior:"instant"});requestAnimationFrame(()=>requestAnimationFrame(resolve));}));
        const end=await page.evaluate(selector=>{const r=document.querySelector(selector).getBoundingClientRect(),nav=document.querySelector(".mobile-nav"),
          navShown=getComputedStyle(nav).display!=="none";return{top:r.top,bottom:r.bottom,left:r.left,right:r.right,headerBottom:document.querySelector(".app-header").getBoundingClientRect().bottom,
            navTop:navShown?nav.getBoundingClientRect().top:innerHeight,innerWidth,scrollWidth:document.documentElement.scrollWidth,scrollY,scrollHeight:document.documentElement.scrollHeight,innerHeight,margin:getComputedStyle(document.querySelector("#feedback-dock")).marginBottom};},CAP_DOCK);
        assert(end.top>=end.headerBottom-0.5&&end.bottom<=end.navTop+0.5&&end.left>=0&&end.right<=end.innerWidth+0.5,label+": entry fully visible "+JSON.stringify(end));
        assert(end.scrollWidth<=end.innerWidth,label+": horizontal overflow");
        await capFree(CAP_DOCK,label+" at the end");
        if(end.navTop<await page.evaluate(()=>innerHeight))await capFree(".mobile-nav",label);
        // Every primary action stays free of the dock when it is focused.
        const actions=await page.evaluate(()=>[...document.querySelectorAll("#app-main button.primary,#app-main a.primary")].filter(node=>node.checkVisibility()&&!node.disabled).map((node,index)=>{node.dataset.capProbe=String(index);return index;}));
        for(const index of actions){
          const state=await page.evaluate(index=>{const node=document.querySelector('[data-cap-probe="'+index+'"]');node.focus();const r=node.getBoundingClientRect(),d=document.querySelector("#feedback-dock").getBoundingClientRect();
            const hit=document.elementFromPoint(r.left+r.width/2,r.top+r.height/2);return{overlap:r.left<d.right&&d.left<r.right&&r.top<d.bottom&&d.top<r.bottom,painted:Boolean(hit&&node.contains(hit)),inView:r.top>=0&&r.bottom<=innerHeight};},index);
          assert.deepStrictEqual(state,{overlap:false,painted:true,inView:true},`${label}: primary action ${index} focused`);
        }
        await page.evaluate(()=>new Promise(resolve=>{for(const node of document.querySelectorAll("[data-cap-probe]"))delete node.dataset.capProbe;scrollTo({top:0,behavior:"instant"});requestAnimationFrame(()=>requestAnimationFrame(resolve));}));
        await capFree("#app-main > :first-child",label+" at the top");
        return actions.length;
      };
      const capRepro=async(lang,size)=>{
        const label=`${lang} ${size.width}x${size.height}`,vt=text=>lang==="de"?v2de[text]:text;
        setCapacity(31);delete live.tracking_startup;
        await setLanguage(page,lang);await page.setViewportSize(size);
        setCapacity(4100);
        await page.reload();await page.waitForSelector(".readiness-hero");
        // (1) the measuring screen: no entry, the hero and the project stay free, the panel carries the message.
        await page.waitForFunction(()=>!document.querySelector(".recording-capacity").classList.contains("compact"));
        assert.strictEqual(await page.locator(CAP_ENTRY).count(),0,label+": no capacity entry on the measuring screen");
        assert((await page.locator(".recording-capacity").innerText()).includes(vt("Recording capacity is running low")),label+": panel message");
        await page.evaluate(()=>scrollTo(0,0));
        for(const selector of [".readiness-hero",".status-project"])await capFree(selector,label+" measuring");
        // (2) straight to the projects page, then the device page: the entry sits in the dock, covering nothing.
        let primaries=0;
        for(const [hash,selector] of [["projects",".project-list"],["device",".device-links"]]){
          await go(page,hash,selector);
          await page.waitForSelector(CAP_DOCK);
          assert.strictEqual(await page.locator(CAP_DOCK).count(),1,`${label} ${hash}: one capacity entry`);
          primaries+=await capGeometry(`${label} ${hash}`);
        }
        return primaries;
      };
      try{
        await capRepro("en",{width:390,height:844});
        // A further poll, one entry; keyboard dismissal; the automatic removal after the notice time (a fresh page: the entry lives 10 s).
        setCapacity(31);await nextPoll();setCapacity(4100);
        await page.evaluate(()=>{location.hash="projects";});await page.reload();await page.waitForSelector(".project-list");
        await page.waitForSelector(CAP_DOCK);
        // Off the measuring screen a poll takes 5 s and the notice lives 10 s: one more poll, still one entry.
        await nextPoll();
        assert.strictEqual(await page.locator(CAP_DOCK).count(),1,"one entry after a further poll");
        await page.locator(CAP_DOCK+" [data-feedback-dismiss]").focus();
        await page.keyboard.press("Enter");
        await page.waitForFunction(selector=>!document.querySelector(selector),CAP_DOCK);
        await page.reload();await page.waitForSelector(".project-list");await page.waitForSelector(CAP_DOCK);
        await page.waitForTimeout(10500);
        await expired(CAP_DOCK,"the capacity notice must leave after about 10 s");
        // (4) a critical limit and a long failure from the real field diagnostics, 320 px: both visible, the failure first, nothing overlaps.
        setCapacity(31);await nextPoll();
        await page.setViewportSize({width:320,height:640});
        await go(page,"device/diagnostics",".field-diagnostics-card");
        setCapacity(4900);await nextPoll();
        await page.waitForSelector(CAP_DOCK);
        // A critical limit has no notice time: three polls (15 s here), still exactly one entry.
        for(let index=0;index<3;index++){await nextPoll();assert.strictEqual(await page.locator(CAP_DOCK).count(),1,`one critical entry after poll ${index+1}`);}
        assert.strictEqual(fieldStatus.enabled,false);
        failFieldPost=LONG_DE;
        await page.locator(".field-diagnostics-card").getByRole("button",{name:"Start field diagnostics",exact:true}).click();
        await page.waitForFunction(text=>{const node=document.querySelector('#feedback-dock .feedback-item[data-key="action"] .feedback-text');return node&&node.textContent===text;},LONG_DE);
        failFieldPost=false;
        const pair=await page.evaluate(()=>{const items=[...document.querySelectorAll("#feedback-dock .feedback-item")].map(node=>{const r=node.getBoundingClientRect();return{key:node.dataset.key,kind:[...node.classList].find(name=>name!=="feedback-item"),x:r.x,y:r.y,width:r.width,height:r.height,shown:node.checkVisibility()};});
          return{items,scrollWidth:document.documentElement.scrollWidth,innerWidth};});
        assert.deepStrictEqual(pair.items.map(item=>[item.key,item.kind,item.shown]),[["action","critical",true],["capacity","critical",true]],"failure before the capacity message: "+JSON.stringify(pair));
        assert(!intersects(pair.items[0],pair.items[1]),"dock entries overlap "+JSON.stringify(pair.items));
        assert(pair.scrollWidth<=pair.innerWidth,"horizontal overflow at 320 px");
        await dismissAll();setCapacity(31);await nextPoll();
        // (5) the matrix.
        let primaryTotal=0;
        for(const lang of ["en","de"])for(const width of [320,360,390,480,1280])primaryTotal+=await capRepro(lang,{width,height:800});
        assert(primaryTotal>0,"the matrix must have focused at least one primary action");
        await setLanguage(page,"en");
      }finally{setCapacity(31);delete live.tracking_startup;await dismissAll().catch(()=>null);failFieldPost=false;}

      console.log("feedback: capacity #45 capture keeps its place");
      for(const [lang,size] of [["en",{width:360,height:800}],["de",{width:320,height:480}]]){
        const label=`${lang} ${size.width}x${size.height}`,vt=text=>lang==="de"?v2de[text]:text,dict=lang==="de"?deDict:en;
        const savedText=vt("Object point saved")+" · Valve 21c",savingText=dict["ui.capture_saving_asset"],undoLabel=dict["ui.undo_point"];
        const stripKeys=()=>page.evaluate(()=>[...document.querySelectorAll(".capture-strip .feedback-item")].map(node=>node.dataset.key+"."+[...node.classList].find(name=>name!=="feedback-item")));
        const waitKeys=async(expected,why)=>{await page.waitForFunction(expected=>JSON.stringify([...document.querySelectorAll(".capture-strip .feedback-item")].map(node=>node.dataset.key+"."+[...node.classList].find(name=>name!=="feedback-item")))===JSON.stringify(expected),expected,{timeout:8000})
          .catch(async()=>assert.fail(`${label}: ${why}: expected ${JSON.stringify(expected)} got ${JSON.stringify(await stripKeys())}`));};
        await setLanguage(page,lang);await page.setViewportSize(size);
        await resetCapture();
        setCapacity(4900);
        await nextPoll();
        await page.waitForSelector('.capture-strip .feedback-item.critical[data-key="capacity"]');
        await hookError("Other failure");
        await waitKeys(["action.critical","capacity.critical"],"two critical entries, newest first");
        const saveObjectPoint=async()=>{
          actionAnswers.add_asset={delay:1500};
          await clickBar(vt("Object point"));await page.locator("dialog.asset-sheet").waitFor();
          await page.locator('dialog.asset-sheet input[name="name"]').fill("Valve 21c");
          await page.locator("dialog.asset-sheet").getByRole("button",{name:vt("Save point")}).click();
        };
        const limitText=vt("Only {count} points available").replace("{count}","100");
        const capacityPanelCritical=async()=>{
          const panel=page.locator(".recording-capacity");
          assert(await panel.evaluate(node=>node.classList.contains("critical")&&!node.classList.contains("compact")),label+": panel shows the critical limit");
          assert((await panel.innerText()).includes(limitText),label+": panel text "+(await panel.innerText()));
        };
        // Round 1: the saved point shows with Undo, then the entries return after the undo time.
        await saveObjectPoint();
        await waitKeys(["capture.running","action.critical"],"saving comes first, two entries");
        assert.strictEqual((await captureItem()).text,savingText,label+": running text");
        await capacityPanelCritical();
        await waitCapture("ok",savedText);
        const saved=await page.evaluate(()=>{const item=document.querySelector('.capture-strip .feedback-item[data-key="capture"]'),first=document.querySelector(".capture-strip .feedback-item")===item,r=item.getBoundingClientRect(),
          nav=document.querySelector(".mobile-nav"),navTop=getComputedStyle(nav).display!=="none"?nav.getBoundingClientRect().top:innerHeight;
          return{first,top:r.top,bottom:r.bottom,innerHeight,navTop,undo:[...item.querySelectorAll("button")].filter(b=>!b.disabled).map(b=>b.textContent)};});
        assert(saved.first&&saved.undo.includes(undoLabel),label+": saved result first with an active Undo "+JSON.stringify(saved));
        assert(saved.top>=0&&saved.bottom<=saved.innerHeight&&saved.bottom<=saved.navTop+0.5,label+": saved result fully visible "+JSON.stringify(saved));
        await page.waitForTimeout(6500);
        await waitKeys(["action.critical","capacity.critical"],"entries back after the undo time");
        // Round 2: Undo within the time sends one checked POST.
        clearPosts();
        await saveObjectPoint();
        await waitCapture("ok",savedText);
        await clickStrip(undoLabel);
        await waitPost("undo");
        await page.waitForTimeout(400);
        assert.strictEqual(posts().filter(action=>action==="undo").length,1,label+": exactly one undo POST");
        assert(lastPost("undo").expect&&lastPost("undo").expect.feature_id,label+": the undo carries the expected point "+JSON.stringify(lastPost("undo")));
        await capacityPanelCritical();
        // Counter-probe: a refusal of the capture comes before the failure.
        await page.waitForTimeout(500);
        actionAnswers.add_asset={status:400,body:{error:"invalid_tracking_request",message:"Changed text.",reasons:["gst_required"]}};
        await clickBar(vt("Object point"));await page.locator("dialog.asset-sheet").waitFor();
        await page.locator('dialog.asset-sheet input[name="name"]').fill("Valve 21c");
        await page.locator("dialog.asset-sheet").getByRole("button",{name:vt("Save point")}).click();
        await waitCapture("critical",vt("Measurement could not be completed."));
        const refusedKeys=await stripKeys();
        assert.strictEqual(refusedKeys[0],"capture.critical",label+": the refusal comes first "+JSON.stringify(refusedKeys));
        assert.strictEqual(refusedKeys.length,2,label+": two entries "+JSON.stringify(refusedKeys));
        assert(refusedKeys.includes("action.critical"),label+": the failure keeps the second place "+JSON.stringify(refusedKeys));
        await clearStrip();
        setCapacity(31);await nextPoll();
      }
      await setLanguage(page,"en");
      await page.setViewportSize({width:360,height:800});
    }finally{
      captureState=null;actionAnswers={};liveAnswers=[];checkAnswers=[];failFieldPost=false;releaseLive();releasePolls();live.capacity=capSaved.capacity;
      live.current_fix=capSaved.fix;live.current_quality.accepted=capSaved.accepted;clearVerdict();live.active_project=capSaved.project;live.active_line=capSaved.line;
      if(capSaved.undo===undefined)delete live.undo_candidate;else live.undo_candidate=capSaved.undo;
      live.measurement_progress=capSaved.progress;tracking.projects=capSaved.projects;if(capSaved.startup===undefined)delete live.tracking_startup;else live.tracking_startup=capSaved.startup;
    }
    console.log("feedback: projects");
    // Restore and optimize (#21c Task 6): running, result and failure in the dock under the key "maintenance"; the pill stays live.
    await page.setViewportSize({width:360,height:800});
    await setLanguage(page,"en");
    const maintAnswers={restore:null,import:null,compact:null,calls:[]};
    // Answers one POST of the maintenance actions ({delay,status,body}); everything else falls through to the fixture.
    const maintRoute=async route=>{
      const url=new URL(route.request().url()),post=route.request().method()==="POST";
      const action=post&&url.pathname==="/api/tracking/import"?"import":post&&url.pathname==="/api/tracking"?route.request().postDataJSON().action:null;
      const answer=action&&maintAnswers[action];
      if(!answer)return route.fallback();
      maintAnswers[action]=null;maintAnswers.calls.push(action);
      if(answer.delay)await new Promise(resolve=>setTimeout(resolve,answer.delay));
      return route.fulfill({status:answer.status||200,contentType:"application/json",body:JSON.stringify(answer.body)});
    };
    await page.route("http://local.test/**",maintRoute);
    try{
      const waitMaint=(kind,text)=>page.waitForFunction(([kind,text])=>{const item=document.querySelector('#feedback-dock .feedback-item[data-key="maintenance"]');return Boolean(item)&&item.classList.contains(kind)&&item.querySelector(".feedback-text").textContent===text;},[kind,text],{timeout:5000});
      const link=()=>page.evaluate(()=>document.querySelector("#live-state").dataset.link);
      const label=text=>page.evaluate(()=>localStorage["rtk-language"]).then(language=>language==="de"&&v2de[text]||text);
      const openManagement=async()=>{await go(page,"projects",".project-list");await page.evaluate(()=>{document.querySelector("details.data-management").open=true;});};
      const sheetButton=async name=>page.locator("dialog.action-sheet").getByRole("button",{name:await label(name),exact:true});
      const managementButton=async name=>page.locator("details.data-management").getByRole("button",{name:await label(name),exact:true});
      // The projects route polls every 5 s and the pill turns "delayed" after 3 s; start each action right after an answer, so "live" is a product statement.
      const freshPoll=async()=>{await page.waitForResponse(response=>response.url().endsWith("/api/ui/live")&&response.status()===200);await page.waitForTimeout(200);};
      const confirm=async name=>{const button=await sheetButton(name);await button.waitFor();await freshPoll();await button.click();};
      const pickBackup=async file=>{const [chooser]=await Promise.all([page.waitForEvent("filechooser"),(await managementButton("Restore backup")).click()]);await chooser.setFiles(file);await confirm("Restore backup");};
      const ndjson={name:"backup.ndjson",mimeType:"application/x-ndjson",buffer:Buffer.from(JSON.stringify({record:"header",kind:"survey_backup",schema_version:5,profiles:[]})+"\n")};
      const jsonBackup={name:"backup.json",mimeType:"application/json",buffer:Buffer.from(JSON.stringify({kind:"survey_backup",schema_version:5,projects:[{id:"b1",name:"B1",lines:[],points:[]}]}))};

      // (1) NDJSON restore: running while the device works, then the summary and the legacy warning.
      await openManagement();
      maintAnswers.import={delay:1500,body:{projects:2,features:31,legacy_values_skipped:2}};
      await pickBackup(ndjson);
      await waitMaint("running","Restoring survey data …");
      assert.strictEqual(await link(),"live","the pill stays live while restoring");
      await waitMaint("ok","Survey backup restored: 2 projects, 31 points");
      assert.strictEqual(await link(),"live");
      await page.waitForSelector('#feedback-dock .feedback-item.warn[data-key="legacy"]');
      assert.deepStrictEqual(maintAnswers.calls,["import"]);
      await dismissAll();
      // (2) JSON restore: the project count and the sum of the project points.
      await openManagement();
      maintAnswers.restore={body:{projects:[{id:"b1",feature_count:20},{id:"b2",feature_count:11}],legacy_values_skipped:0}};
      await pickBackup(jsonBackup);
      await waitMaint("ok","Survey backup restored: 2 projects, 31 points");
      assert.strictEqual(await page.locator('#feedback-dock .feedback-item[data-key="legacy"]').count(),0,"no legacy warning without skipped values");
      assert.deepStrictEqual(maintAnswers.calls,["import","restore"]);
      await dismissAll();
      // (3) Failure: 400 invalid_backup, critical with the server text.
      await openManagement();
      maintAnswers.import={status:400,body:{error:"invalid_backup",message:"Backup upload is incomplete."}};
      await pickBackup(ndjson);
      await waitMaint("critical","Backup upload is incomplete.");
      assert.strictEqual(await link(),"live");
      assert.strictEqual(await page.locator('#feedback-dock .feedback-item[data-key="legacy"]').count(),0);
      await dismissAll();
      // (4) Optimize: running, then success that expires after 4 s.
      await openManagement();
      maintAnswers.compact={delay:1500,body:{ok:true}};
      await (await managementButton("Optimize storage")).click();
      await confirm("Optimize");
      await waitMaint("running","Optimizing recording storage …");
      assert.strictEqual(await link(),"live");
      await waitMaint("ok","Recording storage optimized");
      await page.waitForSelector(".project-list");
      await page.waitForTimeout(4500);
      await expired('#feedback-dock .feedback-item[data-key="maintenance"]',"the optimize success must expire after 4 s");
      // (5) Optimize failure: the server text, critical.
      await openManagement();
      maintAnswers.compact={status:400,body:{error:"invalid_tracking_request",message:"Wait for the current measurement to finish."}};
      await (await managementButton("Optimize storage")).click();
      await confirm("Optimize");
      await waitMaint("critical","Wait for the current measurement to finish.");
      assert.strictEqual(await link(),"live");
      await dismissAll();
      // (6) German texts of the running and result messages.
      await setLanguage(page,"de");
      await openManagement();
      maintAnswers.import={delay:1000,body:{projects:2,features:31,legacy_values_skipped:0}};
      await pickBackup(ndjson);
      await waitMaint("running",deDict["ui.restoring_survey"]);
      await waitMaint("ok",deDict["ui.restore_done"].replace("{projects}","2").replace("{points}","31"));
      await dismissAll();
    }finally{
      await page.unroute("http://local.test/**",maintRoute).catch(()=>null);
      await setLanguage(page,"en");
    }
    await testCorrectionsCause(page);
    await testWhyNot(page);
    await testJourney(page);
    assert.deepStrictEqual(errors,[]);
    await browser.close();console.log("feedback-browser=ok (dock, in-flow layout 320-1280 px EN/DE, lifecycle, capture strip U-07, cancel, undo, unknown outcome, focus, loss, widths, corrections cause #30, why-not sheet, connected journey)");return;
  }
  if(process.argv.includes("--field-diagnostics-only")){
    const errors=[];page.on("pageerror",error=>errors.push(error.message));
    await page.evaluate(()=>localStorage.setItem("rtk-language","de"));
    await page.reload();
    await go(page,"device/diagnostics",".field-diagnostics-card");
    const card=page.locator(".field-diagnostics-card");
    for(const width of [320,360,1280]){
      await page.setViewportSize({width,height:900});
      assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),`diagnostics overflow ${width}`);
    }
    await page.setViewportSize({width:360,height:900});
    await card.getByRole("button",{name:"Felddiagnose starten",exact:true}).click();
    await page.waitForFunction(()=>document.querySelector("#live-state small").textContent.includes("DIAG an"));
    const de=require(root + "/i18n.js").v2de;
    await page.waitForFunction(([start,stop])=>{const buttons=[...document.querySelectorAll(".field-diagnostics-card button")];const find=name=>buttons.find(b=>b.textContent.includes(name));return find(start)&&find(stop)&&find(start).disabled===true&&find(stop).disabled===false;},[de["Start field diagnostics"],de["Stop field diagnostics"]]);
    await page.evaluate(()=>{window.__exportClick=null;document.addEventListener("click",event=>{if(event.target.closest&&event.target.closest('a[href="/api/field-diagnostics/export"]'))window.__exportClick=event.defaultPrevented;});});
    await card.locator("a.btn").scrollIntoViewIfNeeded();
    await card.locator("a.btn").dispatchEvent("click"); // aria-disabled links are not actionable for click()
    await page.waitForSelector(".feedback-item.warn");
    assert((await page.locator(".feedback-item.warn .feedback-text").first().innerText()).includes(de["Stop field diagnostics before downloading."]));
    assert.strictEqual(await page.evaluate(()=>window.__exportClick),true,"export click must be default-prevented while diagnostics run");
    assert.strictEqual(new URL(page.url()).hash,"#device/diagnostics");
    await page.locator(".feedback-item.warn [data-feedback-dismiss]").first().click();
    await page.waitForFunction(()=>!document.querySelector(".feedback-item.warn"));
    await card.locator("select").selectOption("stationary");
    await card.getByRole("button",{name:"Markierung setzen",exact:true}).click();
    await page.waitForSelector(".feedback-item.ok");
    await page.locator(".feedback-item.ok .feedback-text",{hasText:de["Field marker saved"]}).waitFor();
    assert.deepStrictEqual(fieldMarkers,["stationary"]);
    failFieldPost=true;
    await card.getByRole("button",{name:"Markierung setzen",exact:true}).click();
    await page.waitForSelector(".feedback-item.critical");
    assert((await page.locator(".feedback-item.critical .feedback-text").first().innerText()).includes("storage busy"));
    failFieldPost=false;
    live.active_line={id:"test",name:"Route",state:"recording",recording_mode:"route",recording_sec:12};
    live.current_fix.qual=5;live.current_fix.fix_status_text="RTK_FLOAT";
    await page.reload();await page.waitForSelector(".field-diagnostics-card");
    await page.waitForFunction(()=>document.querySelector("#live-state b").textContent==="RTK FLOAT");
    assert(await page.locator("#live-state b").evaluate(node=>node.scrollWidth<=node.clientWidth));
    assert((await page.locator("#live-state small").innerText()).includes("DIAG an"));
    fieldStatus.error="storage_reserve";
    await page.reload();await page.waitForSelector(".field-diagnostics-card .critical");
    assert((await card.innerText()).includes("Speicherfehler"));
    fieldStatus.error=null;
    await card.getByRole("button",{name:"Felddiagnose stoppen",exact:true}).click();
    await page.waitForFunction(()=>!document.querySelector("#live-state small").textContent.includes("DIAG an"));
    await page.waitForFunction(()=>document.querySelector('.field-diagnostics-card a[aria-disabled]').getAttribute("aria-disabled")==="false");
    assert.deepStrictEqual(errors,[]);
    await page.screenshot({path:"/tmp/esp-rtk-field-diagnostics.png",fullPage:true});
    await browser.close();console.log("field-diagnostics-browser=ok (start/stop/marker, CSRF, storage error, recording fix, 320/360/1280px)");return;
  }
  live.active_line = { id: "l2", name: "Cold start", state: "recording", recording_mode: "route",
    recording_sec: 0, vertex_count: 0, length_m: 0 };
  for (const [state,label] of [["loading","Loading survey storage"],["error","Survey storage unavailable"]]) {
    live.tracking_startup={state};
    await page.reload();
    await page.waitForFunction(expected=>document.querySelector("#live-state small").textContent.endsWith(expected),label);
    assert((await page.locator("#live-state small").innerText()).startsWith("Live · "));
    assert.strictEqual(await page.locator(".hero-state h2").innerText(),label);
    assert.strictEqual(await page.locator(".field-actions button").count(),0);
  }
  delete live.tracking_startup;
  live.active_line = { id: "l2", name: "Cold start", state: "recording", recording_mode: "route",
    recording_sec: 0, vertex_count: 0, length_m: 0 };
  for (const [reason, label] of [["waiting_clock", "Waiting for GNSS time"],
    ["waiting_position", "Waiting for usable position"], ["settling", "Stabilizing position 4/10 s"]]) {
    live.route_startup = { ready: false, reason, stable_sec: 4, required_sec: 10 };
    await page.reload();
    await page.waitForFunction(expected => document.querySelector("#live-state small").textContent.endsWith(expected), label);
    assert((await page.locator("#live-state small").innerText()).startsWith("Live · "));
    assert.strictEqual(await page.locator("#live-state b").innerText(), "GPS FIX");
    assert.strictEqual(await page.locator(".hero-state h2").innerText(), label);
  }
  live.route_startup.ready = true;
  await page.reload();
  await page.waitForFunction(() => document.querySelector("#live-state small").textContent.includes("Recording"));
  live.active_line = null;
  delete live.route_startup;
  const initialLive = structuredClone(live);
  // Stale receiver position: grey, measuring locked, unlocked only by a larger fix_seq.
  {
    const savedFix = structuredClone(live.current_fix), savedAccepted = live.current_quality.accepted;
    let uptime = 500;
    const setFix = (stale, seq, ageMs, reboot) => {
      uptime = reboot ? 4 : uptime + 2;
      live.system = { boot_count: 0, uptime_sec: uptime };
      live.current_fix = Object.assign({}, savedFix, { qual: 4, fix_status_text: "RTK_FIXED", stale,
        fix_age_ms: ageMs, fix_seq: seq });
      live.current_quality.accepted = !stale;
    };
    const disabled = () => page.evaluate(() => ["Start line", "Object point"].map(name => {
      const button = [...document.querySelectorAll(".field-actions button")].find(node => node.textContent === name);
      return button ? button.disabled : null;
    }));
    const heroStale = () => page.evaluate(() => document.querySelector(".readiness-hero").classList.contains("stale"));
    const nextPoll = async () => { await page.waitForResponse(response => response.url().endsWith("/api/ui/live")); await page.waitForTimeout(300); };
    setFix(false, 8, 120);
    await page.reload();
    await page.waitForFunction(() => [...document.querySelectorAll(".field-actions button")].some(node => node.textContent === "Start line" && !node.disabled));
    setFix(true, 9, 4200);
    await page.waitForFunction(() => document.querySelector(".readiness-hero").classList.contains("stale"));
    assert.deepStrictEqual(await disabled(), [true, true]);
    assert.strictEqual(await page.locator(".readiness-hero h2").innerText(), "No current position");
    assert((await page.locator(".readiness-hero .hero-state p").innerText()).includes("4 s"));
    assert((await page.locator('[data-context="position"]').innerText()).includes("4 s"));
    assert(await page.locator('[data-context="position"]').evaluate(node => node.classList.contains("stale")));
    assert.strictEqual(await page.locator("#live-state b").innerText(), "Position outdated");
    assert((await page.locator("#live-state").getAttribute("class")).endsWith("error"));
    assert((await page.locator('.condition[data-gate="position"]').innerText()).includes("4 s"));
    assert.strictEqual(await page.locator(".condition.fail").count(), 6);
    await go(page, "projects/p1", ".map-workspace");
    await page.waitForFunction(() => document.querySelector(".map-position").classList.contains("stale"));
    await go(page, "status", ".readiness-hero");
    setFix(false, 9, 100);
    await nextPoll();
    assert.strictEqual(await heroStale(), true, "same fix_seq must stay stale");
    assert.deepStrictEqual(await disabled(), [true, true]);
    await go(page, "projects/p1", ".map-workspace");
    await nextPoll();
    assert.strictEqual(await page.locator(".map-workspace .quality-chip").first().innerText(), "Measurement blocked",
      "the map chip must not claim RTK FIXED while the latch holds");
    await go(page, "status", ".readiness-hero");
    setFix(false, 10, 100);
    await page.waitForFunction(() => !document.querySelector(".readiness-hero").classList.contains("stale"));
    assert.deepStrictEqual(await disabled(), [false, false]);
    // Open measurement dialogs follow the freshness while they are open.
    for (const [opener, dialogSelector, submitName] of [["Object point", "dialog.asset-sheet", "Save point"],
        ["Start line", "dialog.action-sheet", "Start recording"]]) {
      await page.locator(".field-actions button", { hasText: opener }).first().click();
      const submit = page.locator(dialogSelector).getByRole("button", { name: submitName });
      await submit.waitFor();
      if (opener === "Start line") await page.locator(dialogSelector + ' input[name="name"]').fill("Line A");
      assert.strictEqual(await submit.isDisabled(), false);
      setFix(true, 11, 4200);
      await page.waitForFunction(selector => document.querySelector(selector + " .fix-guard") && !document.querySelector(selector + " .fix-guard").hidden, dialogSelector);
      assert.strictEqual(await submit.isDisabled(), true, opener + " submit must lock on a stale position");
      assert((await page.locator(dialogSelector + " .fix-guard").innerText()).includes("4 s"));
      setFix(false, 12, 100);
      await page.waitForFunction(selector => document.querySelector(selector + " .fix-guard").hidden, dialogSelector);
      assert.strictEqual(await submit.isDisabled(), false);
      await page.keyboard.press("Escape");
      await page.waitForFunction(selector => !document.querySelector(selector), dialogSelector);
    }
    setFix(true, 10000, 5000);
    await page.waitForFunction(() => document.querySelector(".readiness-hero").classList.contains("stale"));
    // A delayed response with a lower fix_seq (and a slightly older uptime) must not unlock.
    setFix(false, 3, 100);
    live.system.uptime_sec = uptime - 1;
    await nextPoll();
    assert.strictEqual(await heroStale(), true, "a reordered lower fix_seq must stay stale");
    assert.deepStrictEqual(await disabled(), [true, true]);
    // A reboot (uptime drops) restarts fix_seq and unlocks.
    setFix(false, 3, 100, true);
    await page.waitForFunction(() => !document.querySelector(".readiness-hero").classList.contains("stale"));
    assert.deepStrictEqual(await disabled(), [false, false]);
    // Data age and connection loss (#21b): measured from the last answer the page really received.
    {
      const { en, de } = require(root + "/i18n.js").dictionaries;
      const lostStart = /^No status data from the device for 1\d s/;
      const startLineEnabled = () => page.waitForFunction(() => [...document.querySelectorAll(".field-actions button")].some(node => node.textContent === "Start line" && !node.disabled));
      const okLive = response => response.url().endsWith("/api/ui/live") && response.ok();
      const heroHas = (name, timeout) => page.waitForFunction(value => document.querySelector(".readiness-hero").classList.contains(value), name, { timeout });
      const setTabHidden = hidden => page.evaluate(value => {
        if (value) Object.defineProperty(document, "hidden", { configurable: true, get: () => true });
        else delete document.hidden;
        document.dispatchEvent(new Event("visibilitychange"));
      }, hidden);
      const freshness = page.locator(".hero-freshness");
      try {
        setFix(false, 20, 100);
        await page.reload();
        await page.waitForSelector(".readiness-hero");
        await startLineEnabled();
        // 1. Fresh data: ready hero with the age line.
        assert(await page.locator(".readiness-hero").evaluate(node => node.classList.contains("ready")), "hero must be ready");
        await freshness.waitFor({ state: "visible" });
        assert(/^● Updated [0-2] s ago$/.test(await freshness.innerText()), "fresh age line: " + await freshness.innerText());
        await settleAnnouncements();
        // 2. Hanging answers for 12 s (issue acceptance).
        await page.waitForResponse(okLive);
        blockLive();
        const served = lastLiveServedAt;
        await page.waitForFunction(() => document.querySelector(".hero-freshness.aging"));
        let since = Date.now() - served;
        assert(since >= 2850 && since <= 4500, `age marking ${since} ms after the last answer`);
        assert(await page.locator(".readiness-hero").evaluate(node => node.classList.contains("ready")), "delayed data keeps the hero ready");
        assert.strictEqual(await page.locator(".field-actions button", { hasText: "Start line" }).isDisabled(), false);
        assert((await freshness.innerText()).startsWith("◐ "), "delayed symbol: " + await freshness.innerText());
        await heroHas("stale-critical", 14000);
        since = Date.now() - served;
        assert(since >= 9850 && since <= 11500, `stale-critical ${since} ms after the last answer`);
        await page.waitForTimeout(Math.max(0, served + 12000 - Date.now()));
        const lostClasses = await page.locator(".readiness-hero").evaluate(node => node.className);
        assert(!/\b(ready|blocked|stale)\b/.test(lostClasses.replace("stale-critical", "")), "lost hero classes: " + lostClasses);
        assert(/^No status data from the device for 1\d s$/.test(await page.locator(".readiness-hero h2").innerText()));
        const lockedActions = await page.locator(".field-actions button").evaluateAll(nodes => nodes.map(node => node.disabled));
        assert(lockedActions.length >= 2 && lockedActions.every(Boolean), "every action is locked: " + JSON.stringify(lockedActions));
        assert.strictEqual(await page.locator(".field-actions .action-reason").innerText(), en["ui.live_lost_action"]);
        assert.strictEqual(await freshness.isHidden(), true, "the age line is hidden while the data is lost");
        assert(await page.locator(".conditions").evaluate(node => node.classList.contains("stale-critical")), "checklist is greyed");
        assert((await page.locator("#live-state").getAttribute("class")).endsWith("error"));
        let said = await announced();
        assert.strictEqual(said.length, 1, "one loss announcement: " + JSON.stringify(said));
        assert(lostStart.test(said[0]), said[0]);
        // 3. Recovery within one ticker second.
        releaseLive();
        await page.waitForResponse(okLive);
        await page.waitForFunction(() => document.querySelector(".readiness-hero").classList.contains("ready"), null, { timeout: 1000 });
        await startLineEnabled();
        assert(/^● Updated [01] s ago$/.test(await freshness.innerText()), "recovered age line: " + await freshness.innerText());
        assert.strictEqual(await page.locator(".conditions").evaluate(node => node.classList.contains("stale-critical")), false, "checklist colour returns");
        assert.strictEqual(await page.locator("#live-state").getAttribute("data-link"), "live");
        await page.waitForTimeout(1500);
        said = await announced();
        assert.strictEqual(said.length, 2, "loss and recovery announced once each: " + JSON.stringify(said));
        assert(said[1].startsWith("Ready to measure. "), said[1]);
        // 4. Repeated poll errors below 10 s never lock anything and never announce.
        await watchHeroCritical();
        failLive = true;
        await page.waitForTimeout(6000);
        assert.strictEqual(await page.locator("#live-state b").innerText(), "Device unreachable");
        assert(await page.evaluate(() => Boolean(document.querySelector(".hero-freshness.aging"))), "age line is marked during the poll errors");
        assert.strictEqual(await page.locator(".field-actions button", { hasText: "Start line" }).isDisabled(), false);
        failLive = false;
        await page.waitForFunction(() => document.querySelector(".readiness-hero").classList.contains("ready") && !document.querySelector(".hero-freshness.aging"));
        assert.strictEqual(await page.evaluate(() => window.__heroCritical), 0, "poll errors under 10 s must never grey the hero");
        assert.strictEqual((await announced()).length, 2);
        // 5. Returning to the tab from ready after minutes: no announcement.
        await setTabHidden(true);
        await shiftClock(60000);
        await setTabHidden(false);
        await page.waitForFunction(() => document.querySelector(".readiness-hero").classList.contains("ready"), null, { timeout: 3000 });
        await page.waitForTimeout(4000);
        assert.strictEqual((await announced()).length, 2, "tab return from ready is silent");
        // 6. Returning after an announced loss: silent as well.
        blockLive();
        await shiftClock(12000);
        await heroHas("stale-critical", 10000);
        await page.waitForTimeout(300);
        said = await announced();
        assert.strictEqual(said.length, 3, "third announcement is the loss: " + JSON.stringify(said));
        assert(lostStart.test(said[2]), said[2]);
        await setTabHidden(true);
        releaseLive();
        await shiftClock(60000);
        await setTabHidden(false);
        await page.waitForFunction(() => document.querySelector(".readiness-hero").classList.contains("ready"), null, { timeout: 3000 });
        await page.waitForTimeout(4500);
        assert.strictEqual((await announced()).length, 3, "tab return after a loss is silent");
        // 7. Navigation without a new answer: neither a restored nor a repeated loss announcement.
        blockLive();
        await shiftClock(11000);
        await heroHas("stale-critical", 10000);
        await page.waitForTimeout(300);
        said = await announced();
        assert.strictEqual(said.length, 4, "fourth announcement is the loss: " + JSON.stringify(said));
        assert(lostStart.test(said[3]), said[3]);
        // The device pages count the loss from 13 s (5 s poll); start beyond it so the first tick there is deterministic.
        await shiftClock(3000);
        await page.evaluate(() => { location.hash = "device"; });
        await page.waitForTimeout(2500);
        assert.strictEqual((await announced()).length, 4, "no announcement on the device page");
        await page.evaluate(() => { location.hash = "status"; });
        await page.waitForTimeout(2500);
        assert.strictEqual((await announced()).length, 4, "no announcement after navigating back");
        releaseLive();
        await page.waitForFunction(() => document.querySelector(".readiness-hero").classList.contains("ready"));
        await page.waitForTimeout(1500);
        said = await announced();
        assert.strictEqual(said.length, 5, "recovery is announced once: " + JSON.stringify(said));
        assert(said[4].startsWith("Ready to measure. "), said[4]);
        // 8. German without new data: title and reason follow the language, seconds keep counting.
        blockLive();
        await shiftClock(12000);
        await heroHas("stale-critical", 10000);
        await page.evaluate(() => RtkI18n.setLanguage("de"));
        await page.waitForFunction(prefix => document.querySelector(".readiness-hero h2").textContent.startsWith(prefix), de["ui.live_lost_title"].split("{seconds}")[0], { timeout: 2000 });
        assert.strictEqual(await page.locator(".field-actions .action-reason").innerText(), de["ui.live_lost_action"]);
        releaseLive();
        await page.evaluate(() => RtkI18n.setLanguage("en"));
        await page.waitForFunction(() => document.querySelector(".readiness-hero").classList.contains("ready"));
        // 9. A device that never answers: lost state without values after 10 s.
        blockLive();
        await page.reload();
        await page.waitForSelector(".readiness-hero");
        await page.waitForTimeout(1500);
        await recordAnnouncements();
        await shiftClock(11000);
        await heroHas("stale-critical", 10000);
        assert.strictEqual(await page.locator(".readiness-hero h2").innerText(), en["ui.live_never_title"]);
        assert.strictEqual(await page.locator(".readiness-hero .hero-state p").innerText(), en["ui.live_never_hint"]);
        assert.deepStrictEqual(await page.locator(".metric b").allInnerTexts(), ["—", "—", "—", "—"]);
        assert(await freshness.count() === 0 || await freshness.isHidden(), "no age line without data");
        assert(!(await page.locator("#live-state b").innerText()).includes("FIX"), "the pill must not invent a fix");
        await page.waitForTimeout(1500);
        said = await announced();
        assert.strictEqual(said.length, 1, "one announcement for the silent device: " + JSON.stringify(said));
        assert(said[0].startsWith(en["ui.live_never_title"]), said[0]);
        releaseLive();
        await page.waitForFunction(() => document.querySelector(".readiness-hero").classList.contains("ready"));
        assert.notStrictEqual((await page.locator(".metric b").allInnerTexts())[2], "—", "values appear once the device answers");
        await page.reload();
        await page.waitForSelector(".readiness-hero");
        await startLineEnabled();
        // 10. Unchanged states do not create live regions again (Wi-Fi error, running measurement from another session).
        const savedNetwork = structuredClone(live.network), savedProgress = live.measurement_progress;
        try {
          live.network = Object.assign({}, live.network, { last_wifi_error: "connection_lost" });
          live.measurement_progress = { active: true, mode: "fixed", elapsed_sec: 8, collected: 2, total: 5 };
          await page.evaluate(() => { document.querySelector(".status-diagnostics").open = true; });
          await page.waitForSelector(".measurement-resume");
          await page.waitForTimeout(2500);
          await recordLiveRegions();
          await page.waitForTimeout(6500);
          assert.deepStrictEqual(await page.evaluate(() => window.__liveRegionEvents), [], "unchanged data must not recreate live regions");
          assert.strictEqual(await page.locator(".measurement-resume").getAttribute("role"), null);
        } finally {
          live.network = savedNetwork;
          if (savedProgress === undefined) delete live.measurement_progress; else live.measurement_progress = savedProgress;
          await page.reload();
          await page.waitForSelector(".readiness-hero");
        }
        // 11. Task 4: open measuring dialogs lock with the loss, send nothing and keep their input (360 px EN, project active, no line, gate open).
        const lockedAfter = (dialog, name) => page.waitForFunction(([selector, label]) => [...document.querySelectorAll(selector + " button")].some(node => node.textContent === label && node.disabled), [dialog, name], { timeout: 1500 });
        const unlockedAfter = (dialog, name) => page.waitForFunction(([selector, label]) => [...document.querySelectorAll(selector + " button")].some(node => node.textContent === label && !node.disabled), [dialog, name], { timeout: 4000 });
        const closeDialog = async selector => { await page.locator(selector).getByRole("button", { name: "Cancel", exact: true }).click(); await page.waitForFunction(value => !document.querySelector(value), selector); };
        await startLineEnabled();
        const lineDialog = "dialog.action-sheet", lineSubmit = page.locator(lineDialog).getByRole("button", { name: "Start recording" }), lineName = page.locator(lineDialog + ' input[name="name"]');
        const openLineDialog = async () => { await page.locator(".field-actions button", { hasText: "Start line" }).first().click(); await lineSubmit.waitFor(); await lineName.fill("Dialog line 21b"); assert.strictEqual(await lineSubmit.isDisabled(), false, "line dialog starts submittable"); };
        // 11a. Line dialog open at the loss; Enter on the disabled submit sends nothing; fresh data release it with the name kept.
        const postsBefore = trackingPosts;
        await openLineDialog();
        blockLive();
        try {
          await shiftClock(11000);
          await lockedAfter(lineDialog, "Start recording");
          assert.strictEqual(await page.locator(lineDialog + " .fix-guard").isVisible(), true, "guard visible");
          assert.strictEqual(await page.locator(lineDialog + " .fix-guard").innerText(), en["ui.live_lost_action"]);
          await lineName.press("Enter");
          await page.waitForTimeout(300);
          assert.strictEqual(await page.locator(lineDialog).count(), 1, "dialog stays open");
          assert.strictEqual(trackingPosts, postsBefore, "nothing is sent while the data is lost");
        } finally { releaseLive(); }
        await unlockedAfter(lineDialog, "Start recording");
        assert.strictEqual(await page.locator(lineDialog + " .fix-guard").isHidden(), true, "guard hidden again");
        assert.strictEqual(await lineName.inputValue(), "Dialog line 21b", "input survives the lock");
        await closeDialog(lineDialog);
        // 11b. The check right before submitting: shift the clock and submit in one synchronous step, so no ticker second can run in between.
        await openLineDialog();
        blockLive();
        try {
          const enabledAtSubmit = await page.evaluate(() => {
            const dialog = document.querySelector("dialog.action-sheet"), button = [...dialog.querySelectorAll("button")].find(node => node.textContent === "Start recording"), before = !button.disabled;
            window.__rtkClockShift += 11000;
            dialog.querySelector("form").requestSubmit();
            return before;
          });
          assert.strictEqual(enabledAtSubmit, true, "the submit was still enabled when it was triggered");
          await page.waitForTimeout(100);
          assert.strictEqual(trackingPosts, postsBefore, "the pre-submit check blocks the request");
          assert.strictEqual(await page.locator(lineDialog).count(), 1, "dialog stays open after the blocked submit");
          await lockedAfter(lineDialog, "Start recording");
        } finally { releaseLive(); }
        await unlockedAfter(lineDialog, "Start recording");
        await closeDialog(lineDialog);
        // 11c. Object point dialog.
        const assetDialog = "dialog.asset-sheet", saveButton = page.locator(assetDialog).getByRole("button", { name: "Save point" }), valve = page.locator(assetDialog).getByRole("radio", { name: "Valve", exact: true });
        await page.locator(".field-actions button", { hasText: "Object point" }).first().click();
        await saveButton.waitFor();
        await page.locator(assetDialog + ' input[name="name"]').fill("Valve 21b");
        await valve.click();
        assert.strictEqual(await valve.getAttribute("aria-checked"), "true");
        blockLive();
        try {
          await shiftClock(11000);
          await lockedAfter(assetDialog, "Save point");
          assert.strictEqual(await page.locator(assetDialog + " .fix-guard").innerText(), en["ui.live_lost_action"]);
          await page.evaluate(() => document.querySelector("dialog.asset-sheet form").requestSubmit());
          await page.waitForTimeout(300);
          assert.strictEqual(trackingPosts, postsBefore, "object point sends nothing while the data is lost");
          assert.strictEqual(await page.locator(assetDialog).count(), 1);
        } finally { releaseLive(); }
        await unlockedAfter(assetDialog, "Save point");
        assert.strictEqual(await page.locator(assetDialog + ' input[name="name"]').inputValue(), "Valve 21b");
        assert.strictEqual(await valve.getAttribute("aria-checked"), "true", "the chosen type survives the lock");
        await closeDialog(assetDialog);
        // 11d. A mutation with the real partial shape renews metadata but neither the data age nor the lock.
        const savedRevision = live.revision;
        partialMutations = true;
        // Undo needs a concrete candidate (#21c); the poll before the loss carries it.
        live.undo_candidate = { kind: "asset", feature_id: "a1", line_id: null, label: "Valve 1", index: null, record_order: 1, request_id: null, inventory_epoch: "a1b2c3d4" };
        try {
          await page.waitForResponse(okLive);
          blockLive();
          const served = lastLiveServedAt;
          await shiftClock(Math.max(0, 8500 - (Date.now() - served)));
          await page.waitForFunction(() => /^\u25d0 Updated [89] s ago$/.test((document.querySelector(".hero-freshness") || {}).textContent || ""), null, { timeout: 1500 });
          const revisionBefore = live.revision;
          await page.locator(".field-actions button", { hasText: "Undo last point" }).first().click();
          const mutationAnswer = page.waitForResponse(response => response.url().endsWith("/api/tracking") && response.request().method() === "POST");
          await page.locator("dialog.action-sheet").getByRole("button", { name: "Undo last point" }).click();
          await mutationAnswer;
          await page.waitForFunction(() => !document.querySelector("dialog.action-sheet"));
          assert.notStrictEqual(live.revision, revisionBefore, "the mock answered the mutation with a new revision");
          await page.waitForTimeout(500);
          const afterMutation = await freshness.innerText();
          assert(/^\u25d0 Updated (8|9|10) s ago$/.test(afterMutation), "the mutation must not reset the age: " + afterMutation);
          await shiftClock(3000);
          await heroHas("stale-critical", 1500);
          assert.strictEqual(await page.locator(".field-actions button", { hasText: "Start line" }).first().isDisabled(), true, "lock stays after a mutation");
        } finally { partialMutations = false; releaseLive(); live.revision = savedRevision; if (savedRevision === undefined) delete live.revision; delete live.undo_candidate; }
        await page.waitForFunction(() => document.querySelector(".readiness-hero").classList.contains("ready"));
      } finally {
        failLive = false;
        releaseLive();
        await page.evaluate(() => { delete document.hidden; }).catch(() => {});
      }
    }
    live.current_fix = savedFix;
    live.current_quality.accepted = savedAccepted;
    delete live.system;
    await page.reload();
    await page.waitForSelector(".readiness-hero");
  }
  // A softened gate shows as a badge with its entries in the hero; a response without deviations clears it.
  {
    const badge = page.locator(".readiness-hero .gate-softened");
    assert.strictEqual(await badge.isVisible(), false, "no badge while the gate is at its defaults");
    setGateDeviations(softenedGate);
    await page.waitForFunction(() => !document.querySelector(".readiness-hero .gate-softened").hidden);
    assert.strictEqual(await badge.locator(".gate-entry").count(), 4);
    const badgeText = (await badge.innerText()).replace("GATE SOFTENED", "Gate softened");
    for (const entry of ["Gate softened", "required_fix DGPS > RTK_FIXED", "max_hdop 2 > 1.5", "quality_window_samples 3 < 5"])
      assert(badgeText.includes(entry), `hero badge misses ${entry}: ${badgeText}`);
    setGateDeviations([]);
    await page.waitForFunction(() => document.querySelector(".readiness-hero .gate-softened").hidden);
    // A fix whose GST is still pending shows the accuracy the device held its verdict on,
    // in the checklist rows and in the hero metric tiles.
    const nextPoll = async () => { await page.waitForResponse(response => response.url().endsWith("/api/ui/live")); await page.waitForTimeout(300); };
    const heroSigmas = () => page.locator(".readiness-hero .metric-grid .metric b").evaluateAll(nodes => nodes.slice(0, 2).map(node => node.textContent));
    const savedFix = structuredClone(live.current_fix);
    live.current_fix.receiver_accuracy = { horizontal_sigma_m: 0.31, altitude_sigma_m: 0.47 };
    await page.waitForFunction(() => document.querySelector(".condition-list").innerText.includes("H 0.310 m"));
    live.current_fix = Object.assign({}, savedFix, { receiver_accuracy: undefined, pending_gst: true,
      held_receiver_accuracy: { horizontal_sigma_m: 0.29, altitude_sigma_m: 0.43 } });
    await page.waitForFunction(() => document.querySelector(".condition-list").innerText.includes("H 0.290 m"));
    await nextPoll();
    const pendingText = await page.locator(".condition-list").innerText();
    assert(pendingText.includes("H 0.290 m") && pendingText.includes("V 0.430 m"), `pending GST lost the held accuracy rows: ${pendingText}`);
    assert.deepStrictEqual(await heroSigmas(), ["0.290 m", "0.430 m"], "hero tiles must show the held accuracy");
    live.current_fix = Object.assign({}, savedFix, { receiver_accuracy: undefined,
      held_receiver_accuracy: { horizontal_sigma_m: 0.29, altitude_sigma_m: 0.43 } });
    await page.waitForFunction(() => !document.querySelector(".condition-list").innerText.includes("H 0.290 m"));
    assert.deepStrictEqual(await heroSigmas(), ["— m", "— m"], "a bare fix that is not pending shows no accuracy");
    live.current_fix = savedFix;
    await nextPoll();
    // Correction link: the corrections row follows GGA field 13, the link row follows the
    // decoded RTCM epoch, and a 60 s old epoch stream with fresh bytes is red on the link only.
    const savedRtcm = structuredClone(live.rtcm);
    const corrections = page.locator('.condition[data-gate="corrections"]');
    const linkLine = corrections.locator('.condition-diagnostic[data-diagnostic="link"]');
    const heroClass = () => page.locator(".readiness-hero").getAttribute("class");
    const savedAccepted = live.current_quality.accepted;
    live.current_fix = Object.assign({}, savedFix, { qual: 4, fix_status_text: "RTK_FIXED", correction_age_sec: 1.2 });
    live.rtcm = Object.assign({}, savedRtcm, { arrival_age_sec: 0.2, last_msg_age_sec: 0.2, epoch_age_sec: 7, stale: false });
    await page.waitForFunction(() => document.querySelector(".condition-list").textContent.includes("Link latency 7.0 s \u00b7 arrival 0.2 s"));
    const heroBefore = await heroClass();
    assert.strictEqual(await linkLine.getAttribute("class"), "condition-diagnostic warn", "7 s epoch age must warn");
    assert.strictEqual(await linkLine.locator(".diagnostic-mark").innerText(), "!");
    assert((await corrections.textContent()).includes("corrections 1.2 s old"));
    assert.strictEqual(await corrections.getAttribute("class"), "condition pass");
    assert.strictEqual((await page.locator(".readiness-hero .metric b").nth(3).innerText()), "1.2 s", "hero tile shows GGA field 13");
    assert((await page.locator(".status-detail-rows").textContent()).includes("Link latency"));
    live.rtcm = Object.assign({}, savedRtcm, { arrival_age_sec: 0.2, last_msg_age_sec: 0.2, epoch_age_sec: 15, stale: true });
    await page.waitForFunction(() => document.querySelector(".condition-list").textContent.includes("Link latency 15.0 s"));
    assert.strictEqual(await linkLine.getAttribute("class"), "condition-diagnostic fail", "15 s epoch age must be red");
    assert.strictEqual(await linkLine.locator(".diagnostic-mark").innerText(), "\u00d7");
    live.rtcm = Object.assign({}, savedRtcm, { arrival_age_sec: 0.2, last_msg_age_sec: 0.2, epoch_age_sec: 60, stale: true });
    await page.waitForFunction(() => document.querySelector(".condition-list").textContent.includes("Link latency 60.0 s"));
    assert.strictEqual(await linkLine.getAttribute("class"), "condition-diagnostic fail");
    // The diagnostic never changes the gate: the row stays green and the hero keeps its state.
    assert.strictEqual(await corrections.getAttribute("class"), "condition pass", "corrections row follows the server verdict, not the epoch age");
    assert.strictEqual(await heroClass(), heroBefore, "a red link diagnostic must not move the hero");
    try {
      live.current_fix = Object.assign({}, savedFix, { qual: 4, fix_status_text: "RTK_FIXED", correction_age_sec: 6.5 });
      live.current_quality.accepted = false;
      setVerdict(["corrections_too_old"]);
      await page.waitForFunction(() => document.querySelector(".condition-list").textContent.includes("corrections 6.5 s old"));
      assert((await corrections.getAttribute("class")).endsWith("fail"));
    } finally {
      clearVerdict();
      live.current_quality.accepted = savedAccepted;
    }
    live.current_fix = Object.assign({}, savedFix, { qual: 4, fix_status_text: "RTK_FIXED", correction_age_sec: 1.2 });
    await page.waitForFunction(() => document.querySelector(".condition-list").textContent.includes("corrections 1.2 s old"));
    status.stats.ntrip_substate = "stale";
    rtcmOverride = { arrival_age_sec: 0.2, last_msg_age_sec: 0.2, epoch_age_sec: 60, stale: true };
    await go(page, "device/diagnostics/rtcm", ".stream-hero");
    assert.strictEqual(await page.locator(".stream-hero h2").innerText(), "Corrections stale: epochs 60.0 s old");
    assert((await page.locator(".stream-hero").getAttribute("class")).includes("critical"));
    const rtcmText = await page.locator(".message-list").innerText();
    assert(rtcmText.includes("Arrival age") && rtcmText.includes("Epoch age") && rtcmText.includes("60.0 s (GPS)"), rtcmText);
    status.stats.ntrip_substate = null;      // disconnected: the device cleared the substate
    rtcmOverride = { arrival_age_sec: 9, last_msg_age_sec: 9, epoch_age_sec: 69, stale: true };
    await go(page, "device", ".device-links");
    await go(page, "device/diagnostics/rtcm", ".stream-hero");
    assert.strictEqual(await page.locator(".stream-hero h2").innerText(), "Stream interrupted");
    rtcmOverride = {};
    await go(page, "device", ".device-links");
    await go(page, "device/diagnostics/rtcm", ".stream-hero");
    assert.strictEqual(await page.locator(".stream-hero h2").innerText(), "Stream is live");
    rtcmOverride = { arrival_age_sec: 9, last_msg_age_sec: 9, epoch_age_sec: null, stale: false };
    await go(page, "device", ".device-links");
    await go(page, "device/diagnostics/rtcm", ".stream-hero");
    assert.strictEqual(await page.locator(".stream-hero h2").innerText(), "Stream interrupted");
    rtcmOverride = {};
    await go(page, "status", ".readiness-hero");
    await page.evaluate(() => RtkI18n.setLanguage("de"));
    live.rtcm = Object.assign({}, savedRtcm, { arrival_age_sec: 0.4, last_msg_age_sec: 0.4, epoch_age_sec: 7, stale: false });
    await page.waitForFunction(() => document.querySelector(".condition-list").textContent.includes("Link-Latenz 7,0 s \u00b7 Ankunft 0,4 s"));
    assert((await page.locator(".condition-list").textContent()).includes("Korrekturen 1,2 s alt"));
    await page.evaluate(() => RtkI18n.setLanguage("en"));
    live.rtcm = savedRtcm;
    await page.waitForFunction(() => document.querySelector(".condition-list").textContent.includes("Link latency 1.1 s"));
    live.current_fix = savedFix;
    await nextPoll();
  }
  live.active_line = { id: "l2", name: "Field route with a long name", state: "recording",
    recording_sec: 125, vertex_count: 12, length_m: 5 };
  for (const width of [320, 360, 1280]) {
    await page.setViewportSize({ width, height: 900 });
    for (const [qual, label] of [[4, "RTK FIXED"], [5, "RTK FLOAT"], [2, "DGPS"], [1, "GPS FIX"], [0, "NO FIX"]]) {
      live.current_fix.qual = qual;
      live.current_quality.accepted = qual === 4;
      await page.reload();
      await page.waitForFunction(expected => document.querySelector("#live-state b").textContent === expected, label);
      const badge = page.locator("#live-state");
      assert((await badge.locator("small").innerText()).includes("02:05"));
      assert((await badge.getAttribute("aria-label")).includes(label));
      // The gate no longer colours the pill: every fix quality shows the live link as ok.
      assert((await badge.getAttribute("class")).endsWith("ok"));
      assert.strictEqual(await badge.getAttribute("data-link"), "live");
      assert.strictEqual(await badge.locator("span").first().innerText(), "\u25cf");
      assert(await badge.locator("b").evaluate(node => node.scrollWidth <= node.clientWidth), `${label} clips at ${width}px`);
      // With the gate closed the subtitle names the server reason and the recording facts move to .hero-activity.
      assert((await page.locator(".readiness-hero").innerText()).includes(label));
      assert.strictEqual(await page.locator(".hero-activity").isVisible(), qual !== 4);
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    }
  }
  live.current_fix.qual = 5;
  live.active_line.state = "paused";
  await page.reload();
  await page.waitForFunction(() => document.querySelector("#live-state small").textContent.endsWith("Recording paused"));
  assert((await page.locator("#live-state small").innerText()).startsWith("Live · "));
  assert.strictEqual(await page.locator("#live-state b").innerText(), "RTK FLOAT");
  live.measurement_progress = { active: true, elapsed_sec: 42, collected: 2, total: 5 };
  await page.reload();
  await page.waitForFunction(() => document.querySelector("#live-state small").textContent.includes("Measuring"));
  assert.strictEqual(await page.locator("#live-state b").innerText(), "RTK FLOAT");
  for (let outage = 0; outage < 3; outage++) {
    failLive = true;
    await page.evaluate(() => dispatchEvent(new Event("offline")));
    await page.waitForFunction(() => document.querySelector("#live-state b").textContent.includes("Offline"));
    assert.strictEqual(await page.locator("#live-state").getAttribute("data-link"), "error");
    assert.strictEqual(await page.locator("#live-state span").first().innerText(), "\u25cb");
    assert(!(await page.locator("#live-state").getAttribute("aria-label")).includes("RTK FLOAT"));
    assert(!(await page.locator("#live-state").getAttribute("title")).includes("Measuring"));
    failLive = false;
    await page.evaluate(() => dispatchEvent(new Event("online")));
    await page.waitForFunction(() => document.querySelector("#live-state b").textContent === "RTK FLOAT");
    assert.strictEqual(await page.locator("#live-state").getAttribute("data-link"), "live");
  }
  // The announcer is the only live region for state changes; the pill is not one.
  assert.strictEqual(await page.locator("#rtk-announcer").count(), 1);
  assert.strictEqual(await page.locator("#rtk-announcer").innerText(), "");
  assert.strictEqual(await page.locator("#live-state").getAttribute("aria-live"), null);
  live.active_line.state = "recording";
  live.current_fix.qual = 4;
  live.current_quality.accepted = true;
  live.measurement_progress.active = false;
  await page.reload();
  await page.getByRole("button", { name: "Add line point", exact: true }).click();
  await page.waitForSelector(".measurement-sheet[open]");
  live.current_fix.qual = 1;
  live.current_quality.accepted = false;
  live.measurement_progress.active = true;
  await page.waitForFunction(() => document.querySelector("#live-state b").textContent === "GPS FIX");
  assert((await page.locator("#live-state small").innerText()).includes("Measuring"));
  live.measurement_progress = { active: true, mode: "fixed", elapsed_sec: 8, collected: 2, total: 5 };
  await page.waitForFunction(() => document.querySelector(".measurement-state").textContent.includes("12 s left"));
  assert((await page.locator(".measurement-state").innerText()).includes(require(root + "/i18n.js").dictionaries.en["ui.measurement_time_left"].replace("{seconds}", "12")));
  // While the measurement runs only the progress poll delivers data: the age stays fresh, nothing is marked or greyed.
  await page.waitForTimeout(4000);
  assert.strictEqual(await page.locator(".readiness-hero").evaluate(node => node.classList.contains("stale-critical")), false);
  assert.strictEqual(await page.evaluate(() => Boolean(document.querySelector(".hero-freshness.aging"))), false);
  failProgress = true;
  await page.waitForFunction(() => document.querySelector("#live-state b").textContent === "Device unreachable");
  assert(!(await page.locator("#live-state").getAttribute("title")).includes("GPS FIX"));
  failProgress = false;
  live.current_fix.qual = 5;
  await page.waitForFunction(() => document.querySelector("#live-state b").textContent === "RTK FLOAT");
  // Progress poll outage beyond 10 s greys the hero behind the dialog; the first successful progress answer restores it at once (not at the next ticker second).
  failProgress = true;
  await page.evaluate(() => { window.__rtkClockShift += 11000; });
  await page.waitForFunction(() => document.querySelector(".readiness-hero").classList.contains("stale-critical"), null, { timeout: 3000 });
  await page.evaluate(() => {
    window.__progressAnswers = [];
    window.__criticalCleared = null;
    const hero = document.querySelector(".readiness-hero");
    new MutationObserver(() => { if (window.__criticalCleared == null && !hero.classList.contains("stale-critical")) window.__criticalCleared = Date.now(); }).observe(hero, { attributes: true, attributeFilter: ["class"] });
  });
  failProgress = false;
  await page.waitForFunction(() => !document.querySelector(".readiness-hero").classList.contains("stale-critical"), null, { timeout: 3000 });
  const recovery = await page.evaluate(() => ({ cleared: window.__criticalCleared, answers: window.__progressAnswers }));
  assert(recovery.answers.length > 0 && recovery.cleared != null, "recovery measured: " + JSON.stringify(recovery));
  assert(recovery.cleared - recovery.answers[0] <= 250, `the progress poll restores the hero ${recovery.cleared - recovery.answers[0]} ms after its first answer`);
  live.measurement_progress.active = false;
  assert(finishMeasurement, "measurement request must be in flight");
  finishMeasurement();
  await page.waitForSelector(".measurement-sheet", { state: "detached" });
  await page.setViewportSize({ width: 360, height: 900 });
  await page.evaluate(() => { localStorage.setItem("rtk-language", "de"); });
  await page.reload();
  await page.waitForFunction(() => document.querySelector("#live-state b").textContent === "RTK FLOAT");
  assert((await page.locator("#live-state small").innerText()).includes(require(root + "/i18n.js").v2de["Recording"]));
  assert(await page.locator("#live-state b").evaluate(node => node.scrollWidth <= node.clientWidth));
  assert(await page.locator(".readiness-hero").evaluate(node => getComputedStyle(node).color === "rgb(180, 35, 24)"));
  await page.screenshot({ path: "/tmp/esp-rtk-recording-fix.png" });
  await testStorageReserve(page);
  if (process.argv.includes("--status-only")) {
    await browser.close();
    console.log("app-status-acceptance=ok (fix transitions, pause, measurement progress, three reconnects, 320/360/1280px)");
    return;
  }
  Object.assign(live, initialLive);
  await page.evaluate(() => { localStorage.setItem("rtk-language", "en"); });
  await page.setViewportSize({ width: 360, height: 900 });
  await page.reload();
  await page.waitForFunction(() => document.querySelector("#live-state b").textContent === "GPS FIX");
  assert.strictEqual(await page.locator("#view-back").count(), 0);
  assert.strictEqual(await page.locator('#mobile-nav [data-route="measure"]').getAttribute("aria-current"), "page");
  assert.strictEqual(
    await page.locator('[data-context="storage"] b').innerText(),
    "0.6 / 1.0 MiB",
  );
  assert(await page.getByRole("button", { name: "Object point" }).isDisabled());
  await go(page, "projects", ".project-list");
  assert(!(await page.locator(".project-list").innerText()).includes("1970"));
  assert.strictEqual((await page.locator(".active-project").innerText()).toLowerCase(), "active for measurement");
  await go(page, "projects/p1", ".map-workspace");
  assert.strictEqual(
    await page.locator(".survey-map").evaluate(node => node.namespaceURI),
    "http://www.w3.org/2000/svg",
  );
  assert((await page.locator(".map-line").evaluate(node => node.getBBox().width)) > 0);
  assert.strictEqual(await page.locator(".map-point").count(), 2);
  const northBefore = await page.locator(".map-north").boundingBox();
  const scaleBefore = await page.locator(".map-label").textContent();
  await page.locator(".map-tools .btn").first().click();
  assert((await page.locator(".map-world").getAttribute("style")).includes("scale(0.5)"));
  assert.strictEqual(await page.locator(".survey-map").getAttribute("style"), null);
  assert.deepStrictEqual(await page.locator(".map-north").boundingBox(), northBefore);
  assert.notStrictEqual(await page.locator(".map-label").textContent(), scaleBefore);
  assert.strictEqual(await page.locator(".map-zoom").innerText(), "50 %");
  assert.strictEqual(await page.locator(".map-layers .btn").count(), 3);
  assert(await page.locator(".map-point").first().isVisible());
  assert((await page.locator(".map-point").first().boundingBox()).width >= 7);
  await page.getByRole("button", { name: "Fit project" }).click();
  // #24-UI Task 9: the tool row is four buttons plus the zoom display; layer state and zoom survive a visit to the library.
  await assertMapMenu(page, "app map menu");
  assert.strictEqual(await page.locator(".map-menu").evaluate(node => node.open), false, "menu starts folded");
  assert.strictEqual((await page.locator(".map-menu-state").innerText()).trim(), "3/3");
  await page.locator(".map-menu summary").click();
  await page.locator(".map-layers .btn", { hasText: "Points" }).click();
  assert.strictEqual((await page.locator(".map-menu-state").innerText()).trim(), "2/3", "one layer off");
  await page.locator(".map-menu summary").click();
  assert.strictEqual((await page.locator(".map-menu-state").innerText()).trim(), "2/3", "state shows while folded");
  assert.strictEqual(await page.locator(".map-menu .map-layers .btn[aria-pressed='false']").textContent(), "Points");
  await page.locator(".map-tools > .btn", { hasText: "+" }).click();
  assert.strictEqual(await page.locator(".map-zoom").innerText(), "150 %");
  await go(page, "projects", ".project-list");
  await go(page, "projects/p1", ".map-workspace");
  await page.waitForSelector(".map-tools");
  assert.strictEqual(await page.locator(".map-zoom").innerText(), "150 %", "zoom kept");
  assert.strictEqual((await page.locator(".map-menu-state").innerText()).trim(), "2/3", "layers kept");
  assert.strictEqual(await page.locator(".map-menu").evaluate(node => node.open), false, "folded menu stays folded");
  await page.locator(".map-menu summary").click();
  await go(page, "projects", ".project-list");
  await go(page, "projects/p1", ".map-workspace");
  await page.waitForSelector(".map-tools");
  assert.strictEqual(await page.locator(".map-menu").evaluate(node => node.open), true, "open menu stays open");
  assert.strictEqual((await page.locator(".map-menu-state").innerText()).trim(), "2/3");
  await page.locator(".map-layers .btn", { hasText: "Points" }).click();
  assert.strictEqual((await page.locator(".map-menu-state").innerText()).trim(), "3/3");
  await page.locator(".map-menu summary").click();
  await page.getByRole("button", { name: "Fit project" }).click();
  assert.strictEqual(await page.locator(".map-zoom").innerText(), "100 %");
  assert.strictEqual(await page.locator('#mobile-nav [data-route="projects"]').getAttribute("aria-current"), "page");
  assert.strictEqual(await page.locator('#mobile-nav [data-route="projects"]').getAttribute("aria-current"), "page");
  await page.locator(".line-row").click();
  await page.waitForSelector(".vertex-row");
  await page.getByRole("button", { name: "Load more line points" }).click();
  await page.waitForFunction(
    () => document.querySelectorAll(".vertex-row").length === 2,
  );
  const lineDetail = await page.locator(".line-detail").innerText();
  assert(lineDetail.includes("One line point recorded before V15.9 is quantised"), `line detail misses the legacy notice: ${lineDetail}`);
  assert.strictEqual(await page.locator(".vertex-row .precision-badge").count(), 1, "only the legacy vertex row carries the badge");
  // Line detail: the UTC text verbatim per vertex, the compact marker only on the unsynchronised one, never the raw seconds.
  const timeEn = require(root + "/i18n.js").dictionaries.en, timeDe = require(root + "/i18n.js").dictionaries.de;
  const vertexTimes = await page.locator(".vertex-row .vertex-time").allInnerTexts();
  assert(vertexTimes[0].includes(timeEn["ui.recorded_utc"] + ": 2026-08-26T11:09:24Z") && !vertexTimes[0].includes(timeEn["ui.clock_unsynced_short"]), `synced vertex time: ${vertexTimes[0]}`);
  assert(vertexTimes[1].includes(timeEn["ui.recorded_utc"] + ": 2000-01-01T00:00:12Z") && vertexTimes[1].includes(timeEn["ui.clock_unsynced_short"]), `unsynced vertex time: ${vertexTimes[1]}`);
  assert.strictEqual(await page.locator(".vertex-row .clock-badge").count(), 1);
  assert.strictEqual(await page.locator(".vertex-row .clock-badge").getAttribute("title"), timeEn["ui.clock_unsynced"]);
  assert(!(await page.locator(".line-detail").innerText()).includes("841057764"), "raw seconds must not be shown");
  await page.locator(".point-row").first().click();
  const pointDetail = await page.locator(".point-detail").innerText();
  for (const value of ["North side", "QUALITY ACCEPTED", "Horizontal 1σ", "0.012 m",
    "Vertical 1σ", "0.024 m", "RTK FIXED", "18", "0.70", "0.8 s", "5", "4.2 s", "0054", "NMEA_GST"])
    assert(pointDetail.includes(value), `object-point detail misses ${value}: ${pointDetail}`);
  assert(pointDetail.includes("50.00000000 / 8.00000000 · 100.000 m"), `exact point lacks e8 coordinates: ${pointDetail}`);
  assert(!pointDetail.includes("Legacy point"), "an exact point must not carry the legacy badge");
  assert(pointDetail.includes("Gate profile") && pointDetail.includes("1a2b3c4d") &&
    pointDetail.includes("RTK FIXED · ≥10 sat · HDOP ≤1.5 · age ≤5 s · H ≤0.05 m · V ≤0.10 m · spread ≤0.05 m · window 5"),
    `point detail misses the gate profile with its limits: ${pointDetail}`);
  for (const value of ["Height (MSL, phase centre)", "100.000 m", "Ellipsoid height", "147.100 m", "Geoid separation", "47.100 m",
    "Antenna height above the ground point", "1.800 m", "Ground height (MSL)", "98.200 m"])
    assert(pointDetail.includes(value), `height chain misses ${value}: ${pointDetail}`);
  assert(!pointDetail.includes("no ground height") && !pointDetail.includes("configured as 0"), "an exact point with an antenna height has no hint");
  // Time base: synchronised point shows the UTC text verbatim and no warning; the raw seconds never appear.
  assert(pointDetail.includes(timeEn["ui.recorded_utc"]) && pointDetail.includes("2026-08-26T11:09:24Z"), `UTC time missing: ${pointDetail}`);
  assert.strictEqual(await page.locator(".point-detail .recorded-row b").innerText(), "2026-08-26T11:09:24Z");
  assert.strictEqual(await page.locator('.point-detail [data-notice="clock-unsynced"]').count(), 0);
  assert(!pointDetail.includes("841057764"), "raw seconds must not be shown");
  await page.locator(".point-row").nth(1).click();
  const legacyDetail = await page.locator(".point-detail").innerText();
  assert(legacyDetail.includes("50.0005000 / 8.0005000 · 100.500 m"), `legacy point lacks degree text: ${legacyDetail}`);
  assert(legacyDetail.toLowerCase().includes("legacy point: position grid about 0.4 m"), `legacy badge missing: ${legacyDetail}`);
  assert(legacyDetail.includes("Gate profile") && legacyDetail.includes("deadbeef") && !legacyDetail.includes("sat"),
    `a profile missing from the table must show its id without limits: ${legacyDetail}`);
  assert(legacyDetail.includes("No antenna height was recorded with this point (older firmware); no ground height."),
    `a point without antenna height shows the missing hint: ${legacyDetail}`);
  assert.strictEqual(await page.locator(".point-detail .data-row", { hasText: "Ground height (MSL)" }).locator("b").innerText(), "\u2014");
  assert.strictEqual(await page.locator(".point-detail .data-row", { hasText: "Ellipsoid height" }).locator("b").innerText(), "\u2014");
  assert(!legacyDetail.includes(timeEn["ui.recorded_utc"]) && await page.locator(".point-detail .recorded-row").count() === 0, "a point without time fields shows no time row");
  // A point added through the map delta renders the same chain.
  assert.strictEqual(await page.locator(".map-point").count(), 2);
  mapRevision = 2;
  live.revision = 2;
  await page.waitForFunction(() => document.querySelectorAll(".map-point").length === 3, null, { timeout: 8000 });
  await page.locator('.map-point[aria-label$=": Valve 3"]').dispatchEvent("click");
  const deltaDetail = await page.locator(".point-detail").innerText();
  for (const value of ["Via delta", "120.000 m", "167.100 m", "47.100 m", "1.800 m", "118.200 m"])
    assert(deltaDetail.includes(value), `delta point detail misses ${value}: ${deltaDetail}`);
  // An unsynchronised point shows its (year 2000) UTC text plus the warning, not the raw seconds.
  assert.strictEqual(await page.locator(".point-detail .recorded-row b").innerText(), "2000-01-01T00:00:12Z");
  assert.strictEqual(await page.locator('.point-detail [data-notice="clock-unsynced"]').innerText(), timeEn["ui.clock_unsynced"]);
  // German labels (a full reload returns the delta point with the full map).
  await page.evaluate(() => RtkI18n.setLanguage("de"));
  await page.reload();
  await page.waitForSelector(".map-point");
  await page.locator(".map-point").nth(2).dispatchEvent("click");
  assert((await page.locator(".point-detail .recorded-row").innerText()).includes(timeDe["ui.recorded_utc"]));
  assert.strictEqual(await page.locator(".point-detail .recorded-row b").innerText(), "2000-01-01T00:00:12Z");
  assert.strictEqual(await page.locator('.point-detail [data-notice="clock-unsynced"]').innerText(), timeDe["ui.clock_unsynced"]);
  await page.locator(".map-point").nth(0).dispatchEvent("click");
  assert.strictEqual(await page.locator('.point-detail [data-notice="clock-unsynced"]').count(), 0);
  await page.evaluate(() => RtkI18n.setLanguage("en"));
  await page.reload();
  await page.waitForSelector(".map-point");
  mapRevision = 1;
  live.revision = 1;
  await go(page, "projects/p2", ".map-workspace");
  assert(await page.locator(".map-empty").isVisible());
  assert(await page.getByRole("button", { name: "Activate for measurement" }).isVisible());
  await go(page, "device", ".device-links");
  // #24-UI: the overview links the four device areas.
  assert.deepStrictEqual(await page.locator(".device-links a").evaluateAll(links => links.map(link => link.getAttribute("href"))),
    ["/#device/connections", "/#device/display", "/#device/diagnostics", "/#device/maintenance"]);
  assert.deepStrictEqual(await page.locator(".device-metric > span").allInnerTexts(), ["Network", "GNSS", "Corrections", "Storage"]);
  assert.strictEqual(await page.locator("#app-main button", { hasText: "Restart device" }).count(), 0, "restarting lives under Maintenance");
  const maintenanceEn = require(root + "/i18n.js").dictionaries.en;
  // #24-UI Task 6: Diagnostics leads to the expert pages (the console last); Maintenance carries backup and restart.
  await go(page, "device/diagnostics", ".diagnostic-links");
  assert.deepStrictEqual(await page.locator(".diagnostic-links a").evaluateAll(links => links.map(link => link.getAttribute("href"))),
    ["/#device/diagnostics/rtcm", "/#device/diagnostics/log", "/#device/diagnostics/console"]);
  assert.strictEqual((await page.locator(".diagnostic-links a").last().locator("small").innerText()).trim(), maintenanceEn["ui.expert_only"]);
  assert.strictEqual(await page.locator('a[href="/#device/connections/access"], a[href="/#device/label"]').count(), 0, "no label link on Diagnostics");
  await go(page, "device/maintenance", ".maintenance-layout");
  assert.deepStrictEqual(await page.locator(".maintenance-layout [data-card]").evaluateAll(cards => cards.map(card => card.getAttribute("data-card"))), ["config-backup", "restart"]);
  assert((await page.locator('[data-card="config-backup"]').innerText()).includes("measurement rules"), "the backup scope names the measurement rules");
  assert.strictEqual(await page.locator('[data-card="config-backup"] a[href="/api/config/backup"]').count(), 1);
  assert.deepStrictEqual(await page.locator('[data-card="restart"] .btn').allInnerTexts(), ["Restart device"]);
  // Old links: the log and the console land on their expert pages; the console has no restart button, only the Maintenance link.
  await go(page, "device/log", ".log-console");
  assert.strictEqual(await page.evaluate(() => location.hash), "#device/diagnostics/log");
  await go(page, "device/module", ".command-panel");
  assert.strictEqual(await page.evaluate(() => location.hash), "#device/diagnostics/console");
  assert.strictEqual(await page.locator("#app-main button", { hasText: "Restart device" }).count(), 0, "the console has no restart button");
  assert.strictEqual(await page.locator('#app-main a[href="/#device/maintenance"]').innerText(), maintenanceEn["ui.console_restart_link"]);
  await go(page, "device", ".device-identity");
  assert((await page.locator(".device-identity").innerText()).includes("HARD"));
  assert((await page.locator("#live-state").innerText()).includes("GPS FIX"));
  assert((await page.locator("#live-state").innerText()).includes("H 0.200 m"));
  // The header pill names the link latency (epoch age, falling back to the arrival age) above 5 s; this fixture's gate is
  // blocked (GPS FIX), so the blocked word follows the link word (#21d).
  const pillRtcm = live.rtcm;
  live.rtcm = Object.assign({}, pillRtcm, { arrival_age_sec: 0.2, last_msg_age_sec: 0.2, epoch_age_sec: 9 });
  await page.waitForFunction(() => document.querySelector("#live-state").textContent.includes("Link latency 9.0 s"));
  assert((await page.locator("#live-state small").innerText()).startsWith("Live \u00b7 blocked \u00b7 Link latency 9.0 s"));
  const liveLabelBox = await page.locator("#live-state > span:last-child").boundingBox();
  assert(liveLabelBox && liveLabelBox.width > 60, "global receiver label must not collapse to the dot width");
  live.rtcm = pillRtcm;
  await page.waitForFunction(() => !document.querySelector("#live-state").textContent.includes("Link latency"));
  // Every rejected sign-in counts: BLE 1 + TCP total 2 (the key-only subset tcp_auth_failures is 1).
  // The connected clients card of Connections → Device access is the visible place.
  await go(page, "device/connections/access", ".access-grid");
  const connectedClients = await page.locator(".tool-card", { hasText: "CONNECTED CLIENTS" }).innerText();
  assert(/Authentication errors\s*3/.test(connectedClients), connectedClients);
  // A transient /status failure on the overview is retried by the router (the display page itself needs no device request).
  failNextStatus = true;
  await go(page, "device", ".device-links");
  assert.strictEqual(failNextStatus, false, "the overview asked for /status once and was refused");
  await go(page, "device/display", ".settings-layout");
  assert.strictEqual(await page.getByRole("button", { name: "English" }).getAttribute("aria-pressed"), "true");
  // #24-UI: method, measurement rules and field setup moved to Measure → Field setup.
  assert.strictEqual(await page.locator('.settings-layout :is([data-card="method"], [data-card="measurement"], [data-card="gate"], [data-card="field-setup"])').count(), 0);
  await go(page, "measure/setup", ".field-setup-layout");
  assert.strictEqual(await page.locator(".measurement-choice button").count(), 4);
  assert.strictEqual(await page.locator(".setting-card .gate-softened").isVisible(), false);
  await page.getByRole("button", { name: "Configure acceptance rules" }).click();
  await page.waitForSelector("dialog[open]");
  await page.locator('input[name="max_hdop"]').fill("2.0");
  const gatePut = page.waitForRequest(request => request.url().endsWith("/api/measurement-gate") && request.method() === "PUT");
  await page.getByRole("button", { name: "Save acceptance rules" }).click();
  const gatePayload = (await gatePut).postDataJSON();
  await page.waitForFunction(() => !document.querySelector("dialog[open]"));
  assert.strictEqual(gatePayload.max_hdop, 2);
  // The save response carries the deviation list: the settings card shows the badge with it.
  await page.waitForFunction(() => { const node = document.querySelector(".setting-card .gate-softened"); return node && !node.hidden; });
  assert.strictEqual(await page.locator(".setting-card .gate-softened .gate-entry").count(), 1);
  assert((await page.locator(".setting-card .gate-softened").innerText()).includes("max_hdop 2 > 1.5"));
  setGateDeviations(softenedGate);
  await page.reload();
  await page.waitForSelector(".field-setup-layout");
  assert.strictEqual(await page.locator(".setting-card .gate-softened .gate-entry").count(), 4);
  assert((await page.locator(".setting-card .gate-softened").innerText()).toLowerCase().includes("gate softened"));
  await page.getByRole("button", { name: "Restore factory values (measurement rules)" }).click();
  await page.getByRole("button", { name: "Restore defaults" }).last().click();
  await page.waitForFunction(() => !document.querySelector("dialog[open]"));
  await page.waitForFunction(() => { const node = document.querySelector(".setting-card .gate-softened"); return node && node.hidden; });
  assert.strictEqual(await page.locator(".setting-card .gate-softened").isVisible(), false);
  // The field setup page addresses its cards by stable selectors; each kicker lands on its own card.
  const kickerOf = card => page.locator(`[data-card="${card}"] .kicker`).innerText();
  const setupEn = require(root + "/i18n.js").dictionaries.en;
  assert.strictEqual(await kickerOf("method"), setupEn["ui.method_title"].toUpperCase());
  assert.strictEqual(await kickerOf("gate"), setupEn["ui.rules_title"].toUpperCase());
  assert.strictEqual(await kickerOf("field-setup"), "FIELD SETUP");
  assert.strictEqual(await page.locator('[data-card="gate"] .measurement-gate-actions').count(), 1);
  assert.strictEqual(await page.locator('[data-card="method"] .measurement-gate-actions').count(), 0);
  assert.strictEqual(await page.locator('[data-card="field-setup"] .measurement-gate-actions').count(), 0);
  assert.strictEqual(await page.locator('[data-card="field-setup"] input[name="antenna_height_m"]').inputValue(), "2");
  assert((await page.locator('[data-card="field-setup"]').innerText()).includes("the raw receiver heights are never changed"));
  const frameInput = page.locator('[data-card="field-setup"] input[name="reference_frame"]'), epochInput = page.locator('[data-card="field-setup"] input[name="reference_frame_epoch"]');
  assert.strictEqual(await frameInput.inputValue(), "");
  assert.strictEqual(await frameInput.getAttribute("placeholder"), "ETRS89 (ReNEP)");
  assert.strictEqual(await frameInput.getAttribute("maxlength"), "40");
  assert.strictEqual(await epochInput.getAttribute("placeholder"), "2010.0");
  assert.strictEqual(await epochInput.getAttribute("maxlength"), "16");
  const fieldCardText = await page.locator('[data-card="field-setup"]').innerText();
  assert(fieldCardText.includes("Reference frame of the correction service") && fieldCardText.includes("Frame epoch (optional)")
    && fieldCardText.includes("Leave empty if unknown."), "English frame labels and hint");
  assert.strictEqual(await page.locator('[data-card="field-setup"] a[href="/#device/connections/internet"]').count(), 1);
  await frameInput.fill("ETRS89 (ReNEP)");
  await epochInput.fill("2010.0");
  await page.locator('[data-card="field-setup"] input[name="antenna_height_m"]').fill("1.8");
  const fieldPut = page.waitForRequest(request => request.url().endsWith("/api/field-setup") && request.method() === "PUT");
  await page.getByRole("button", { name: "Save field setup" }).click();
  const fieldBody = (await fieldPut).postDataJSON();
  assert.strictEqual(fieldBody.antenna_height_m, "1.8");
  assert.strictEqual(fieldBody.reference_frame, "ETRS89 (ReNEP)");
  assert.strictEqual(fieldBody.reference_frame_epoch, "2010.0");
  await page.waitForFunction(() => [...document.querySelectorAll(".feedback-text")].some(node => node.textContent.includes("Field setup saved")));
  assert.strictEqual(fieldSetup.antenna_height_m, 1.8);
  assert.strictEqual(await page.locator('[data-card="field-setup"] input[name="antenna_height_m"]').inputValue(), "1.8");
  assert.strictEqual(await frameInput.inputValue(), "ETRS89 (ReNEP)");
  assert.strictEqual(await epochInput.inputValue(), "2010.0");
  // An empty antenna field must not block saving the frame: the key is omitted from the PUT body.
  await page.locator('[data-card="field-setup"] input[name="antenna_height_m"]').fill("");
  await frameInput.fill("ETRS89 (DREF91)");
  const frameOnlyPut = page.waitForRequest(request => request.url().endsWith("/api/field-setup") && request.method() === "PUT");
  await page.getByRole("button", { name: "Save field setup" }).click();
  const frameOnlyBody = (await frameOnlyPut).postDataJSON();
  assert(!("antenna_height_m" in frameOnlyBody), "empty antenna field is omitted from the PUT body");
  assert.strictEqual(frameOnlyBody.reference_frame, "ETRS89 (DREF91)");
  await page.waitForFunction(() => fetch("/api/field-setup").then(r => r.json()).then(d => d.values.reference_frame === "ETRS89 (DREF91)"));
  assert.strictEqual(fieldSetup.reference_frame, "ETRS89 (DREF91)");
  assert.strictEqual(fieldSetup.antenna_height_m, 1.8);
  // Reference-frame warnings: overview and project header read the frame fields of /api/tracking.
  const frameEn = require(root + "/i18n.js").dictionaries.en, frameDe = require(root + "/i18n.js").dictionaries.de;
  const mixText = (dictionary, tally) => dictionary["ui.frame_mix_warning"].replace("{frames}", tally);
  const mixedTally = "ETRS89 (synthetic) 2 \u00b7 WGS84/ITRF 1";
  const frameFields = (project, frames, mixed, unknown) => Object.assign(project, { frames, mixed_frames: mixed, unknown_frames: unknown });
  frameFields(tracking.projects[0], { "ETRS89 (synthetic)": 2, "WGS84/ITRF": 1 }, true, false);
  frameFields(tracking.projects[1], { unknown: 3 }, false, true);
  await go(page, "projects", ".project-list");
  const overviewNotices = page.locator('.project-list [data-notice="frames"]');
  assert.strictEqual(await overviewNotices.count(), 2);
  assert.strictEqual(await overviewNotices.nth(0).innerText(), mixText(frameEn, mixedTally));
  assert.strictEqual(await overviewNotices.nth(0).getAttribute("data-kind"), "mix");
  assert.strictEqual(await overviewNotices.nth(1).innerText(), frameEn["ui.frame_unknown_warning"]);
  assert.strictEqual(await overviewNotices.nth(1).getAttribute("data-kind"), "unknown");
  assert(mixText(frameEn, mixedTally).includes("ETRS89 (synthetic) 2 \u00b7 WGS84/ITRF 1"));
  await go(page, "projects/p1", ".map-workspace");
  assert.strictEqual(await page.locator('.map-panel [data-notice="frames"]').count(), 1);
  assert.strictEqual(await page.locator('.map-panel [data-notice="frames"]').innerText(), mixText(frameEn, mixedTally));
  await go(page, "projects/p2", ".map-workspace");
  assert.strictEqual(await page.locator('.map-panel [data-notice="frames"]').innerText(), frameEn["ui.frame_unknown_warning"]);
  await page.evaluate(() => RtkI18n.setLanguage("de"));
  await page.reload();
  await page.waitForSelector(".map-workspace");
  assert.strictEqual(await page.locator('.map-panel [data-notice="frames"]').innerText(), frameDe["ui.frame_unknown_warning"]);
  await go(page, "projects/p1", ".map-workspace");
  assert.strictEqual(await page.locator('.map-panel [data-notice="frames"]').innerText(), mixText(frameDe, mixedTally));
  await page.evaluate(() => RtkI18n.setLanguage("en"));
  // A single known frame produces no warning.
  frameFields(tracking.projects[0], { "ETRS89 (synthetic)": 3 }, false, false);
  frameFields(tracking.projects[1], {}, false, false);
  await go(page, "projects", ".project-list");
  assert.strictEqual(await page.locator('[data-notice="frames"]').count(), 0);
  await go(page, "projects/p1", ".map-workspace");
  assert.strictEqual(await page.locator('[data-notice="frames"]').count(), 0);
  // The project page follows the live frame fields while points are recorded: warning appears, changes, disappears.
  live.active_project.frames = { "ETRS89 (synthetic)": 2, "WGS84/ITRF": 1 }; live.active_project.mixed_frames = true; live.active_project.unknown_frames = false;
  const liveNotice = page.locator('.map-panel [data-notice="frames"]');
  await page.waitForFunction(() => document.querySelectorAll('.map-panel [data-notice="frames"]').length === 1, null, { timeout: 8000 });
  assert.strictEqual(await liveNotice.innerText(), mixText(frameEn, mixedTally));
  assert.strictEqual(await liveNotice.getAttribute("data-kind"), "mix");
  live.active_project.frames = { "ETRS89 (synthetic)": 3, "WGS84/ITRF": 1, unknown: 2 };
  await page.waitForFunction(() => /unknown 2/.test(document.querySelector('.map-panel [data-notice="frames"]').textContent), null, { timeout: 8000 });
  assert.strictEqual(await page.locator('.map-panel [data-notice="frames"]').count(), 1);
  live.active_project.frames = { "ETRS89 (synthetic)": 4 }; live.active_project.mixed_frames = false;
  await page.waitForFunction(() => document.querySelectorAll('.map-panel [data-notice="frames"]').length === 0, null, { timeout: 8000 });
  // Another project's live fields never show on this page.
  live.active_project.id = "other"; live.active_project.mixed_frames = true; live.active_project.frames = { A: 1, B: 1 };
  await page.waitForTimeout(2600);
  assert.strictEqual(await page.locator('.map-panel [data-notice="frames"]').count(), 0);
  live.active_project.id = "p1"; delete live.active_project.frames; delete live.active_project.mixed_frames; delete live.active_project.unknown_frames;
  delete tracking.projects[0].frames; delete tracking.projects[0].mixed_frames; delete tracking.projects[0].unknown_frames;
  delete tracking.projects[1].frames; delete tracking.projects[1].mixed_frames; delete tracking.projects[1].unknown_frames;
  // DGPS mixing hint: the gate dialog shows it only while the select reads DGPS; the rules card follows the stored gate.
  await go(page, "measure/setup", ".field-setup-layout");
  assert.strictEqual(await page.locator('[data-card="gate"] [data-hint="frame-mix"]').count(), 0);
  await page.getByRole("button", { name: "Configure acceptance rules" }).click();
  await page.waitForSelector("dialog[open]");
  const dialogHint = page.locator('dialog [data-hint="frame-mix"]');
  assert.strictEqual(await dialogHint.count(), 1);
  assert.strictEqual(await dialogHint.isVisible(), false);
  await page.locator('dialog select[name="required_fix"]').selectOption("DGPS");
  assert.strictEqual(await dialogHint.isVisible(), true);
  assert.strictEqual(await dialogHint.innerText(), frameEn["ui.frame_mix_hint"]);
  await page.locator('dialog select[name="required_fix"]').selectOption("RTK_FLOAT");
  assert.strictEqual(await dialogHint.isVisible(), false);
  await page.locator('dialog select[name="required_fix"]').selectOption("DGPS");
  assert.strictEqual(await dialogHint.isVisible(), true);
  await page.getByRole("button", { name: "Cancel" }).click();
  // The closed sheet pops its history entry with history.back(), which lands asynchronously: wait for it, or the hash written next is undone.
  await page.waitForFunction(() => !document.querySelector("dialog[open]") && !(history.state && history.state.rtkSheet));
  tracking.current_quality.limits.fix_quality = "DGPS";
  gateRequiredFix = "DGPS";
  await go(page, "device", ".device-links");
  await go(page, "measure/setup", ".field-setup-layout");
  assert.strictEqual(await page.locator('[data-card="gate"] [data-hint="frame-mix"]').innerText(), frameEn["ui.frame_mix_hint"]);
  // /api/tracking judges like /api/ui/live (one verdict, E13) and only takes the stored limits from the tracking side.
  const served = await page.evaluate(async () => {
    const [trackingAnswer, liveAnswer] = await Promise.all([fetch("/api/tracking"), fetch("/api/ui/live")].map(request => request.then(response => response.json())));
    return { tracking: trackingAnswer.current_quality, live: liveAnswer.current_quality };
  });
  assert.strictEqual(served.tracking.accepted, served.live.accepted);
  assert.deepStrictEqual(served.tracking.reasons, served.live.reasons);
  assert.strictEqual(served.tracking.limits.fix_quality, "DGPS");
  // A stored gate that already reads DGPS shows the hint the moment the dialog opens.
  gateRequiredFix = "DGPS";
  await page.getByRole("button", { name: "Configure acceptance rules" }).click();
  await page.waitForSelector("dialog[open]");
  assert.strictEqual(await page.locator('dialog select[name="required_fix"]').inputValue(), "DGPS");
  assert.strictEqual(await dialogHint.isVisible(), true);
  assert.strictEqual(await dialogHint.innerText(), frameEn["ui.frame_mix_hint"]);
  await page.getByRole("button", { name: "Cancel" }).click();
  await page.waitForFunction(() => !document.querySelector("dialog[open]"));
  gateRequiredFix = "RTK_FIXED";
  tracking.current_quality.limits.fix_quality = "RTK_FIXED";
  await go(page, "device/display", ".settings-layout");
  const contrast = page.locator('.theme-choice [data-theme-choice="high-contrast"]');
  assert.strictEqual(await contrast.getAttribute("aria-pressed"), "false");
  await contrast.click();
  await page.waitForSelector('html[data-theme="high-contrast"]');
  assert.strictEqual(await contrast.getAttribute("aria-pressed"), "true");
  assert(
    await page.getByRole("button", { name: /Keep screen awake/ }).isEnabled(),
  );
  await go(page, "device/configuration", ".configuration-layout");
  await page.getByRole("button", { name: "Scan networks" }).click();
  await page.waitForSelector("dialog[open]");
  await page.getByRole("button", { name: "Use network" }).click();
  // The scan sheet resolves once its history entry is gone (#24-UI Task 8), a few milliseconds after the click.
  await page.waitForFunction(() => document.querySelector('input[name="wifi_ssid"]').value === "Visible");
  assert.strictEqual(await page.locator('input[name="wifi_ssid"]').inputValue(), "Visible");
  // The save, apply and "connection restored" steps and the #23b live-save notice moved to tests/guided_apply_acceptance.js (#23c).
  assert.strictEqual(await page.locator("form.configuration-layout").getAttribute("data-dirty"), "true");
  assert.strictEqual(await page.locator(".unsaved-marker").isVisible(), true);
  assert.strictEqual(applyCalls, 0);
  await go(page, "device/diagnostics/log", ".log-console");
  const download = page.waitForEvent("download");
  await page.getByRole("button", { name: "Share" }).click();
  assert.strictEqual((await download).suggestedFilename(), "rtk-log-filtered.txt");
  await go(page, "device/display", ".settings-layout");
  await page.getByRole("button", { name: "Deutsch" }).click();
  await page.waitForSelector(".settings-layout");
  assert.strictEqual(await page.getByRole("button", { name: "Deutsch" }).getAttribute("aria-pressed"), "true");
  assert.strictEqual(await page.locator('.theme-choice [data-theme-choice="high-contrast"]').innerText(), require(root + "/i18n.js").dictionaries.de["ui.high_contrast"]);
  assert.strictEqual(await page.locator('.theme-choice [data-theme-choice="high-contrast"]').getAttribute("aria-pressed"), "true");
  await go(page, "measure/setup", ".field-setup-layout");
  const setupDe = require(root + "/i18n.js").dictionaries.de;
  assert.strictEqual(await page.locator('[data-card="method"] .kicker').innerText(), setupDe["ui.method_title"].toUpperCase());
  assert.strictEqual(await page.locator('[data-card="gate"] .kicker').innerText(), setupDe["ui.rules_title"].toUpperCase());
  assert.strictEqual(await page.locator('[data-card="field-setup"] .kicker').innerText(), "FELDEINRICHTUNG");
  assert((await page.locator('[data-card="field-setup"]').innerText()).includes("Antennenh\u00f6he"));
  const fieldCardDe = await page.locator('[data-card="field-setup"]').innerText();
  const deDictionary = require(root + "/i18n.js").dictionaries.de;
  for (const key of ["ui.reference_frame", "ui.reference_frame_epoch", "ui.reference_frame_help"])
    assert(fieldCardDe.includes(deDictionary[key]) && deDictionary[key] !== key, `German text for ${key}`);
  await go(page, "status", ".readiness-hero");
  const compactHero = await page.locator(".readiness-hero").boundingBox();
  // The blocked hero now carries the full German action sentence (up to three lines at 360 px):
  // it stays a compact card, at most a third of the 900 px viewport. The data age line (#21b) adds about 20 px:
  // measured 296.6 px with the age line (was below 290), rounded up to 300 px.
  console.log(`compact German hero: ${compactHero.height}px`);
  assert(compactHero.height < 300, `status hero is ${compactHero.height}px high`);
  assert.strictEqual(await page.locator(".readiness-hero .metric").count(), 4);
  assert((await page.locator(".readiness-hero").innerText()).includes("Messung gesperrt"));
  await go(page, "device", ".device-links");
  const areasDe = require(root + "/i18n.js").dictionaries.de;
  assert.deepStrictEqual(await page.locator(".device-links a b").allInnerTexts(),
    ["ui.route_connections", "ui.route_display", "ui.route_diagnostics", "ui.route_maintenance"].map(key => areasDe[key]));
  assert((await page.locator(".device-identity").innerText()).includes("FELDEMPF\u00c4NGER"));
  await go(page, "device/connections/access", ".access-grid");
  assert((await page.locator("#app-main").innerText()).includes("Technische Verbindungsdetails"));
  assert.strictEqual(await page.locator(".tcp-auth-card .switch").getAttribute("aria-pressed"), "true");
  await page.locator(".tcp-auth-card .switch").click();
  await page.waitForSelector("dialog[open]");
  const accessRefresh = page.waitForResponse(response => response.url().endsWith("/api/access") && response.request().method() === "GET");
  const tcpPut = page.waitForRequest(request => request.url().endsWith("/api/tcp-auth") && request.method() === "PUT");
  await page.getByRole("button", { name: /Authentifizierung deaktivieren|Disable authentication/ }).click();
  assert.strictEqual((await tcpPut).postDataJSON().required, false);
  await accessRefresh;
  await page.waitForFunction(() => document.querySelector("#app-main").getAttribute("aria-busy") === "false");
  await go(page, "device/diagnostics/log", ".log-console");
  assert((await page.locator("#app-main").innerText()).includes("Was \u0069st passiert?"));
  await page.locator("#live-state").click();
  await page.waitForSelector(".readiness-hero");
  assert.strictEqual(await page.evaluate(() => location.hash), "#measure");
  const compact = await page.evaluate(() => ({ header: document.querySelector(".app-header").getBoundingClientRect().height, footer: document.querySelector(".mobile-nav").getBoundingClientRect().height, status: document.querySelector("#live-state").getBoundingClientRect().height }));
  assert(compact.header <= 65 && compact.footer <= 61 && compact.status >= 48);
  // The connectivity word stays fully visible at 320 px even with the longest detail; the detail may be clipped (#21b).
  await page.setViewportSize({ width: 320, height: 780 });
  const wordLive = { rtcm: live.rtcm, line: live.active_line };
  live.rtcm = Object.assign({}, live.rtcm, { arrival_age_sec: 0.2, last_msg_age_sec: 0.2, epoch_age_sec: 9 });
  live.active_line = { id: "l3", name: "Pill line", state: "recording", recording_mode: "route", recording_sec: 125, vertex_count: 2, length_m: 5 };
  for (const language of ["en", "de"]) {
    await page.evaluate(code => RtkI18n.setLanguage(code), language);
    const dictionary = require(root + "/i18n.js").dictionaries[language];
    // An active recording replaces the receiver detail in the subtitle; the full detail then lives in title and aria-label.
    await page.waitForFunction(prefix => document.querySelector("#live-state").title.includes(prefix) && document.querySelector("#live-state small").textContent.includes("\u00b7"), dictionary["ui.link_latency"].split("{")[0]);
    const pill = await page.evaluate(() => {
      const small = document.querySelector("#live-state small"), node = small.firstChild, text = small.textContent, word = text.split(" \u00b7 ")[0];
      const range = document.createRange();
      range.setStart(node, 0);
      range.setEnd(node, word.length);
      return { text, word, wordRight: range.getBoundingClientRect().right, smallRight: small.getBoundingClientRect().right,
        title: document.querySelector("#live-state").title, label: document.querySelector("#live-state").getAttribute("aria-label") };
    });
    assert.strictEqual(pill.word, dictionary["ui.link_live"], `${language}: pill word`);
    assert(pill.text.startsWith(pill.word + " \u00b7 "), `${language}: word then detail: ${pill.text}`);
    assert(pill.wordRight <= pill.smallRight + 0.5, `${language}: connectivity word clipped at 320 px (${pill.wordRight} > ${pill.smallRight})`);
    const latencyPrefix = dictionary["ui.link_latency"].split("{")[0];
    assert(pill.title.includes(pill.word) && pill.title.includes(latencyPrefix), `${language}: title carries the full detail: ${pill.title}`);
    assert(pill.label.includes(pill.word) && pill.label.includes(latencyPrefix), `${language}: aria-label carries the full detail: ${pill.label}`);
  }
  await page.evaluate(() => RtkI18n.setLanguage("en"));
  live.rtcm = wordLive.rtcm;
  live.active_line = wordLive.line;
  await page.setViewportSize({ width: 1280, height: 900 });
  for (const [hash, selector] of [
    ["status", ".readiness-hero"],
    ["projects", ".project-list"],
    ["projects/p1", ".map-workspace"],
    ["device", ".device-links"],
    ["device/display", ".settings-layout"],
    ["measure/setup", ".field-setup-layout"],
  ]) {
    await go(page, hash, selector);
    assert.strictEqual(
      await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),
      true,
      `${hash} overflows at desktop width`,
    );
  }
  await testFieldSetup(page);
  await testProjectView(page,browser);
  await testSheetHistory(page);
  await browser.close();
  console.log("app-v2-local-acceptance=ok");
})().catch((error) => {
  console.error(error);
  process.exit(1);
});

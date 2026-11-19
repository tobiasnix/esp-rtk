// SPDX-License-Identifier: AGPL-3.0-only
// Capacity estimates use confirmed points, never the configured point distance.
class CapacityEstimate {
  constructor(){this.reset();}
  reset(){this.samples=[];this.lineId=null;this.lastChange=0;this.used=null;this.limit=null;}
  observe(live,now=Date.now()){
    const line=live.active_line,capacity=live.capacity;
    if(!capacity||!line||line.state!=="recording"||!(line.auto_distance_m>0)||capacity.writable===false||
       (live.tracking_startup&&live.tracking_startup.state!=="ready")) {this.reset();return;}
    const count=Number(line.vertex_count),last=this.samples[this.samples.length-1];
    if(!Number.isFinite(count)){this.reset();return;}
    if(this.lineId!==line.id||this.limit!==capacity.limit||capacity.used<this.used||
       (last&&(count<last.count||now<last.at||now-last.at>15000||now-this.lastChange>15000)))this.reset();
    const previous=this.samples[this.samples.length-1];
    if(!previous||count>previous.count)this.lastChange=now;
    this.lineId=line.id;this.used=capacity.used;this.limit=capacity.limit;
    if(!previous||now>previous.at)this.samples.push({at:now,count});
    while(this.samples.length>1&&this.samples[0].at<now-120000)this.samples.shift();
  }
  seconds(remaining,now=Date.now()){
    const first=this.samples[0],last=this.samples[this.samples.length-1];
    if(!first||!last||now-this.lastChange>15000||now-last.at>15000||
       last.at-first.at<60000||last.count-first.count<10||remaining<=0)return null;
    return remaining*(last.at-first.at)/(last.count-first.count)/1000;
  }
}
const capacityEstimate=new CapacityEstimate();
let capacityWarningLevel=0;
const capacityNumber=value=>Number(value).toLocaleString(({de:"de-DE",pl:"pl-PL"})[globalThis.RtkI18n?RtkI18n.getLanguage():"en"]||"en-US");
function capacityText(message,values={}){return localText(message).replace(/\{(\w+)\}/g,(_,key)=>values[key]??"");}
function capacityLevel(capacity){
  if(!capacity||!Number.isFinite(capacity.used)||!(capacity.limit>0))return -1;
  const ratio=capacity.used/capacity.limit;
  return ratio>=1?4:ratio>=.98?3:ratio>=.9?2:ratio>=.8?1:0;
}
function capacityMessage(capacity){
  const level=capacityLevel(capacity),remaining=Math.max(0,capacity.limit-capacity.used);
  return level===4?localText("Point limit reached"):level===3?
    capacityText("Only {count} points available",{count:capacityNumber(remaining)}):
    level===2?localText("Archive a project soon"):level===1?localText("Recording capacity is running low"):"";
}
// The panel shrinks to title, count and meter only while nothing about capacity needs a sentence (#45).
function capacityCompact(live,unavailable){const line=live.active_line;return !unavailable&&capacityLevel(live.capacity)===0&&!storageBlocked(live)&&!(line&&line.state==="paused"&&line.pause_reason==="capacity_limit");}
function observeCapacity(live){
  capacityEstimate.observe(live);
  if(live.tracking_startup&&live.tracking_startup.state!=="ready")return;
  const level=capacityLevel(live.capacity);
  if(level<0)return;
  if(level>capacityWarningLevel){
    // Levels 1-2 stay quiet on the measuring screen, where the full panel carries the message; from level 3 the entry stays until closed.
    showFeedback({key:"capacity",kind:level>=3?"critical":"warn",text:capacityMessage(live.capacity),
      actions:[{label:localText("Manage projects"),run:()=>{dismissFeedback("capacity");return chooseCapacityProject();}}],
      ttl:level>=3?null:FEEDBACK_NOTICE_MS,quietOnMeasure:level<3});
  }else if(level<capacityWarningLevel)dismissFeedback("capacity");
  capacityWarningLevel=level;
}
function capacityPanel(){
  return h("section",{class:"tool-card recording-capacity","aria-label":localText("Recording capacity")},
    h("h2",{text:"Recording capacity"}),h("b",{class:"capacity-count",text:"—"}),
    h("progress",{class:"capacity-meter",max:1,value:0,"aria-label":localText("Point limit usage")}),
    h("strong",{class:"capacity-remaining",text:"Loading recording capacity"}),
    h("small",{class:"capacity-estimate",text:"Remaining time cannot currently be estimated"}),
    h("small",{class:"muted",text:"All non-archived projects combined"}),
    h("p",{class:"capacity-message",role:"status"}),
    iconButton("Manage projects",chooseCapacityProject,"secondary"));
}
function updateCapacityPanel(live){
  const panel=statusRefs&&statusRefs.capacity;if(!panel)return;
  const capacity=live.capacity||{},level=capacityLevel(capacity),line=live.active_line,
    unavailable=level<0||connectionState.state==="error"||Boolean(live.tracking_startup&&live.tracking_startup.state!=="ready"),
    remaining=unavailable?null:Math.max(0,capacity.limit-capacity.used),full=!unavailable&&level===4,
    storageLow=!unavailable&&storageBlocked(live);
  panel.className="tool-card recording-capacity"+(!unavailable&&level>=3?" critical":!unavailable&&level>0?" warn":"");
  panel.classList.toggle("compact",capacityCompact(live,unavailable));
  panel.querySelector(".capacity-count").textContent=unavailable?"—":
    `${capacityNumber(capacity.used)} / ${capacityNumber(capacity.limit)} ${localText("points")} · ${Math.floor(capacity.used*100/capacity.limit)} %`;
  const meter=panel.querySelector("progress");meter.hidden=unavailable;meter.max=capacity.limit||1;meter.value=capacity.used||0;
  panel.querySelector(".capacity-remaining").textContent=unavailable?localText("Recording capacity unavailable"):
    capacityText("{count} points available",{count:capacityNumber(remaining)});
  const seconds=unavailable||storageLow?null:capacityEstimate.seconds(remaining);
  panel.querySelector(".capacity-estimate").textContent=seconds===null?localText("Remaining time cannot currently be estimated"):
    capacityText("About {minutes} min until the point limit at the current recording rate",{minutes:capacityNumber(Math.max(1,Math.round(seconds/60)))});
  panel.querySelector(".capacity-estimate").hidden=full||!line;
  const message=panel.querySelector(".capacity-message");
  const messageText=unavailable?"":full?localText(line?"Your recorded points are saved. Archive another project to continue this line.":"Archive a project before recording more points."):
    storageLow?localText("Device storage reserve reached. Export and manage stored data before continuing."):
    line&&line.state==="paused"&&line.pause_reason==="capacity_limit"?localText("Capacity is available again. Resume the recording when ready."):capacityMessage(capacity);
  if(message.textContent!==messageText)message.textContent=messageText;
  message.hidden=!messageText;
  const manage=panel.querySelector("button");manage.textContent=localText(full?"Choose project to archive":"Manage projects");manage.disabled=unavailable||manage.getAttribute("aria-busy")==="true";
  if(full||storageLow){
    statusRefs.hero.classList.remove("ready");statusRefs.hero.classList.add("blocked");
    statusRefs.title.textContent=localText(full?(line&&line.state==="paused"?"Recording paused — point limit reached":"Point limit reached"):"Device storage reserve reached");
  }
}
async function chooseCapacityProject(){
  const tracking=await api.json("/api/tracking",{timeout:PROJECT_READ_TIMEOUT});
  const projects=tracking.projects.filter(project=>!(tracking.active_line&&project.id===tracking.active_project_id)&&project.feature_count>0)
    .sort((a,b)=>b.feature_count-a.feature_count);
  if(!projects.length){
    const accepted=await modal({title:"Manage projects",description:tracking.active_line?"No other project can be archived. Back up this project, finish its active line, then archive it and start a new project.":"There are no projects with recorded points to archive.",submit:"Open projects"});
    if(accepted)location.hash="projects";return;
  }
  const values=await modal({title:"Choose project to archive",description:"Archiving preserves a verified copy on the device and frees its points from the active limit. Download an additional backup from the project page.",
    fields:[{name:"project_id",label:"Project",type:"select",value:projects[0].id,options:projects.map(project=>({value:project.id,
      label:capacityText("{name} · free {count} points",{name:project.name,count:capacityNumber(project.feature_count)})}))}],submit:"Archive project"});
  if(values){await archiveProject(projects.find(project=>project.id===values.project_id),true,true);forgetTrackingCache();}
}


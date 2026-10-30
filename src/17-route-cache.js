// SPDX-License-Identifier: AGPL-3.0-only
// Route cache (#29b E1): the last load of a read-only project page, in memory only and keyed by the canonical route path.
// Only the listed views are kept (a new view stays uncached); the least recently used entry leaves first.
const ROUTE_CACHE_LIMIT=6;
const CACHED_VIEWS=new Set(["projects","archive","project"]);
const routeCache=new Map();
function cacheGet(route){if(!route||!CACHED_VIEWS.has(route.view)||!routeCache.has(route.path))return null;const entry=routeCache.get(route.path);routeCache.delete(route.path);routeCache.set(route.path,entry);return entry;}
function cachePut(route,data){if(!route||!CACHED_VIEWS.has(route.view))return null;const entry={data,savedAt:Date.now()};routeCache.delete(route.path);routeCache.set(route.path,entry);while(routeCache.size>ROUTE_CACHE_LIMIT)routeCache.delete(routeCache.keys().next().value);return entry;}
function cacheForget(path){routeCache.delete(path);}
// Own mutations (E4): with a project id both overviews and that project's page; without one every project page as well.
function forgetTrackingCache(projectId){cacheForget("projects");cacheForget("archive");if(projectId!==undefined){cacheForget(routeHash("project",{id:projectId}));return;}for(const path of [...routeCache.keys()])if(path.startsWith("projects/"))cacheForget(path);}
// Stale stamp (E3): the browser clock at the time of the load, then what the page is doing about it.
function staleStampText(savedAt,state){const time=new Date(savedAt).toLocaleTimeString(globalThis.RtkI18n&&RtkI18n.getLanguage()==="de"?"de-DE":"en-GB",{hour:"2-digit",minute:"2-digit",second:"2-digit"});return tr("ui.cached_as_of","As of {time}",{time})+" · "+(state==="failed"?tr("ui.cached_failed","update failed"):tr("ui.cached_refreshing","updating …"));}
function staleStamp(savedAt){const retry=iconButton("Try again",()=>renderRoute(),"secondary");retry.hidden=true;const stamp=h("p",{class:"stale-stamp","data-stamp":"refreshing",role:"status"},h("span",{class:"stale-stamp-text",textRaw:staleStampText(savedAt,"refreshing")}),retry);stamp.dataset.savedAt=String(savedAt);return stamp;}
// fresh keeps the stamp's box (invisible, no layout shift); failed names it and offers a retry.
function setStampState(stamp,state){if(!stamp)return;stamp.setAttribute("data-stamp",state);stamp.querySelector(".stale-stamp-text").textContent=staleStampText(Number(stamp.dataset.savedAt),state);stamp.querySelector("button").hidden=state!=="failed";}
// Stale-while-revalidate (E2): a cached page renders at once with one stamp it keeps across a re-render; the network
// confirms it (fresh) or replaces it. A failed load keeps the cached content (failed, no error page); without an entry
// the skeleton shows and an error goes to the router as before. Nothing changes after the route's signal aborted.
async function swrMount(route,signal,spec){const entry=cacheGet(route),stamp=entry?staleStamp(entry.savedAt):null;if(entry)spec.render(entry.data,stamp);else replace(main,skeleton(spec.label));let data;try{data=await spec.load(signal);}catch(error){if(signal.aborted||error.name==="AbortError")return null;if(!entry)throw error;setStampState(stamp,"failed");return null;}if(signal.aborted)return null;cachePut(route,data);if(spec.fresh)spec.fresh(data);if(!entry){spec.render(data,null);return data;}if(!spec.signature||spec.signature(data)!==spec.signature(entry.data))spec.render(data,stamp);setStampState(stamp,"fresh");return data;}

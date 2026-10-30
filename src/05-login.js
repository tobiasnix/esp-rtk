// SPDX-License-Identifier: AGPL-3.0-only
const $=id=>document.getElementById(id),main=$("app-main"),overlay=$("overlay-root");
const signInTarget=hash=>/^#[a-z][a-z0-9/_-]{0,63}$/.test(hash)?hash:"#measure";
// The sign-in target: the router hash, else the hash part of ?return= (set after a lost session), else Measure.
function signInHash(hash,search){if(hash&&signInTarget(hash)===hash)return hash;let back="";try{back=new URLSearchParams(search||"").get("return")||"";}catch(error){back="";}return signInTarget(/^\/(ui-v2)?#/.test(back)?back.slice(back.indexOf("#")):"");}
if(document.body.dataset.page==="app-v2-login"){
  const target=signInHash(location.hash,location.search);
  const form=$("v2-login-form"),input=$("v2-device-code"),state=$("v2-login-state"),show=$("v2-show-code"),d=value=>globalThis.RtkI18n&&RtkI18n.getLanguage()==="de"&&RtkI18n.v2de[value]||value;
  if(globalThis.RtkI18n&&RtkI18n.getLanguage()==="de"){document.documentElement.lang="de";document.title=d("Sign in · RTK Field App");document.querySelector(".login-intro h1").textContent=d("Ready when the receiver is.");document.querySelector(".login-intro p:last-child").textContent=d("Sign in locally with the device code printed on the receiver label.");document.querySelector(".login-panel .kicker").textContent=d("SECURE DEVICE ACCESS");document.querySelector(".login-panel h2").textContent=d("Sign in");document.querySelector(".login-panel .field>span").textContent=d("Device code");show.textContent=d("Show");form.querySelector("button[type=submit]").textContent=d("Open Field App");state.textContent=d("The code never leaves this device.");document.querySelector(".login-fallback a").textContent=d("Basic setup page (fallback)");}
  show.addEventListener("click",()=>{const visible=input.type==="text",label=visible?"Show":"Hide";input.type=visible?"password":"text";show.textContent=globalThis.RtkI18n&&RtkI18n.getLanguage()==="de"?RtkI18n.v2de[label]:label;show.setAttribute("aria-pressed",String(!visible));});
  const signIn=async code=>{state.className="muted";state.textContent=d("Checking device code …");const response=await fetch("/api/session",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({device_code:code,next:"/"})});if(!response.ok)throw new Error(await response.text()||d("Sign-in failed"));await response.json();location.replace("/?signed_in=1"+target);};
  form.addEventListener("submit",event=>{event.preventDefault();form.querySelector("button[type=submit]").disabled=true;signIn(input.value).catch(error=>{state.className="notice critical";state.textContent=d(error.message);form.querySelector("button[type=submit]").disabled=false;input.focus();});});
  const code=new URLSearchParams(location.hash.slice(1)).get("code");if(code){history.replaceState(null,"",location.pathname);input.value=code;signIn(code).catch(error=>{state.className="notice critical";state.textContent=d(error.message);});}
  return;
}

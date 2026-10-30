'use strict';
const task=location.pathname.match(/\/tasks\/([a-f0-9]{32})\/screen$/)?.[1];
const base='/api/tasks/'+task, image=document.getElementById('screen'), status=document.getElementById('status');
let token=null,seq=-1,active=true,pending=false;
document.getElementById('title').textContent='\u041f\u0440\u043e\u0432\u0435\u0440\u043a\u0430 CAPTCHA: '+task.slice(0,8);
document.getElementById('help').textContent='\u041d\u0430\u0436\u0438\u043c\u0430\u0439\u0442\u0435 \u043d\u0430 \u0438\u0437\u043e\u0431\u0440\u0430\u0436\u0435\u043d\u0438\u0435 \u0434\u043b\u044f \u0440\u0443\u0447\u043d\u043e\u0439 \u043f\u0440\u043e\u0432\u0435\u0440\u043a\u0438. \u042d\u0442\u043e \u044d\u043a\u0440\u0430\u043d \u0432\u0430\u0448\u0435\u0439 \u0437\u0430\u0434\u0430\u0447\u0438, \u043d\u0435 \u043d\u043e\u0432\u044b\u0439 \u043f\u043e\u0438\u0441\u043a.';
async function request(path,data){const r=await fetch(base+path,{method:data?'POST':'GET',cache:'no-store',headers:data?{'Content-Type':'application/json'}:{},body:data?JSON.stringify(data):undefined});const j=await r.json();if(!r.ok)throw Error(j.error||r.status);return j;}
let streamTimer=null, streamToken=null, retryTimer=null;
function disconnectStream(){clearTimeout(streamTimer);clearTimeout(retryTimer);image.removeAttribute('src');streamToken=null;}
function connectStream(){
 if(!active||document.hidden||!token)return;
 clearTimeout(streamTimer);clearTimeout(retryTimer);
 streamToken=token;
 image.src=base+'/screen/stream?token='+encodeURIComponent(token)+'&v='+Date.now();
 streamTimer=setTimeout(connectStream,25000);
}
image.onerror=()=>{if(active&&!document.hidden){clearTimeout(retryTimer);retryTimer=setTimeout(connectStream,1500);}};
document.addEventListener('visibilitychange',()=>{if(document.hidden)disconnectStream();else if(token)connectStream();});
async function poll(){
 if(!active)return;
 if(document.hidden){setTimeout(poll,1000);return;}
 try{
  const j=await request('/screen/frame?meta=1');
  token=j.token;
  if(streamToken!==token)connectStream();
  status.textContent=j.error||(!j.seq?'Ожидание потока экрана…':'');
 }catch(e){status.textContent=e.message;disconnectStream();}
 setTimeout(poll,1000);
}
async function input(data){if(!token||pending)return;pending=true;try{await request('/screen/input',{...data,token});}catch(e){status.textContent=e.message;}finally{pending=false;}}
image.onclick=e=>{const r=image.getBoundingClientRect();input({kind:'click',x:(e.clientX-r.left)/r.width,y:(e.clientY-r.top)/r.height});};
image.tabIndex=0;image.onkeydown=e=>{const key=e.shiftKey&&e.key==='Tab'?'Shift+Tab':e.key===' '?'Space':e.key;if(['Tab','Shift+Tab','Enter','Space','Escape','ArrowLeft','ArrowRight','ArrowUp','ArrowDown','Backspace'].includes(key)){e.preventDefault();input({kind:'key',key});}else if(e.key.length===1&&!e.ctrlKey&&!e.metaKey){e.preventDefault();input({kind:'text',text:e.key});}};
const refresh=document.getElementById('refresh');refresh.textContent='\u041e\u0431\u043d\u043e\u0432\u0438\u0442\u044c \u044d\u043a\u0440\u0430\u043d';refresh.onclick=()=>connectStream();
for(const [id,label,action] of [['continue','\u041f\u0440\u043e\u0434\u043e\u043b\u0436\u0438\u0442\u044c \u043f\u043e\u0438\u0441\u043a','continue'],['cancel','\u041e\u0441\u0442\u0430\u043d\u043e\u0432\u0438\u0442\u044c \u043f\u043e\u0438\u0441\u043a','cancel']]){const b=document.getElementById(id);b.textContent=label;b.onclick=async()=>{b.disabled=true;try{await request('/browser-'+action,{screen_token:token});status.textContent='\u041a\u043e\u043c\u0430\u043d\u0434\u0430 \u043f\u0440\u0438\u043d\u044f\u0442\u0430. \u0421\u0442\u0430\u0442\u0443\u0441 \u0437\u0430\u0434\u0430\u0447\u0438 \u0432 \u043e\u0441\u043d\u043e\u0432\u043d\u043e\u0439 \u0432\u043a\u043b\u0430\u0434\u043a\u0435.';}catch(e){status.textContent=e.message;}finally{b.disabled=false;}};}
window.addEventListener('pagehide',()=>{active=false;disconnectStream();});poll();



let wheelX=0,wheelY=0,wheelPoint={x:0.5,y:0.5},wheelTimer=null;
function flushWheel(){
 wheelTimer=null;
 if(!active||!token){wheelX=wheelY=0;return;}
 if(pending){wheelTimer=setTimeout(flushWheel,50);return;}
 const dx=wheelX,dy=wheelY;wheelX=wheelY=0;
 if(dx||dy)input({kind:'wheel',delta_x:dx,delta_y:dy,...wheelPoint});
}
image.addEventListener('wheel',e=>{
 e.preventDefault();image.focus({preventScroll:true});
 const r=image.getBoundingClientRect();if(!r.width||!r.height)return;
 wheelPoint={x:Math.max(0,Math.min(1,(e.clientX-r.left)/r.width)),y:Math.max(0,Math.min(1,(e.clientY-r.top)/r.height))};
 const factor=e.deltaMode===1?16:e.deltaMode===2?r.height:1;
 wheelX=Math.max(-2000,Math.min(2000,wheelX+e.deltaX*factor));
 wheelY=Math.max(-2000,Math.min(2000,wheelY+e.deltaY*factor));
 if(!wheelTimer)wheelTimer=setTimeout(flushWheel,50);
},{passive:false});


// The real controller runs against a minimal DOM and controlled fetch/timers.
const assert = require('node:assert/strict');
const {createController, formatPercent, formatSeconds} = require('../static/admin-metrics.js');
(async()=>{
 assert.equal(formatPercent(null),'Unavailable');
 assert.equal(formatSeconds(null),'Insufficient samples');
 let calls=0, active=0, release;
 const document={hidden:false};
 const views=[];let message='';let tick;
 const controller=createController({document,fetch:async()=>{calls++;active++;
   await new Promise(resolve=>{release=resolve});active--;
   return {status:200,ok:true,json:async()=>({metrics:{source:'jukes',windows:{'15m':{}},lifetime:{}}})};
 },render:(m,w)=>views.push(w),notice:t=>message=t,setInterval:f=>{tick=f;return 1},clearInterval:()=>{},now:()=>0});
 controller.start({windows:{'15m':{},'1h':{}}});
 controller.select('1h');assert.equal(views.at(-1),'1h');
 const pending=controller.refresh();await controller.refresh();assert.equal(calls,1);assert.equal(active,1);
 release();await pending;assert.equal(views.at(-1),'1h');
 document.hidden=true;await controller.refresh();assert.equal(calls,1);
 document.hidden=false;
 const unauthorized=createController({document,fetch:async()=>({status:401}),render:()=>{},notice:t=>message=t,setInterval:()=>1,clearInterval:()=>{},now:()=>0});
 await unauthorized.refresh();await unauthorized.refresh();assert.match(message,/Sign in/);
 const broken=createController({document,fetch:async()=>{throw Error('offline')},render:()=>{},notice:t=>message=t,setInterval:()=>1,clearInterval:()=>{},now:()=>0});
 await broken.refresh();assert.match(message,/Stale/);
 console.log('Admin metrics UI: refresh, visibility, windows, expiry and stale state passed');
})().catch(e=>{console.error(e);process.exitCode=1});

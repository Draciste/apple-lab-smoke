// Public synthetic fixture only. No project code, accounts, remote pages or secrets.
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import http from 'node:http';
import {spawn,execFileSync} from 'node:child_process';
import {createRequire} from 'node:module';
const root=path.join(os.homedir(),'AppleLab/browser-channels');
const require=createRequire(path.join(root,'automation/package.json'));
const {PNG}=require(path.join(root,'automation/node_modules/playwright-core/lib/utilsBundle.js'));
const out=process.env.APPLELAB_CANVAS_OUTPUT??path.join(root,'CANVAS-CONTROL.json');
const result={os:execFileSync('sw_vers',{encoding:'utf8'}),arch:process.arch,started:new Date().toISOString(),cases:[]};
const server=http.createServer((req,res)=>{res.setHeader('Content-Type','text/html');res.end('<!doctype html><style>body{margin:0}</style><div style="background:#00ff00;width:64px;height:64px" id="css"></div>');});
await new Promise(r=>server.listen(0,'127.0.0.1',r));
const socket=http.createServer();await new Promise(r=>socket.listen(0,'127.0.0.1',r));const port=socket.address().port;await new Promise(r=>socket.close(r));
const driver=spawn('/usr/bin/safaridriver',['--diagnose','--port',String(port)],{stdio:['ignore','ignore','pipe']});let errors='';driver.stderr.on('data',b=>errors=(errors+b).slice(-4000));driver.on('error',e=>{errors+=String(e);});
let session;
async function rpc(p,data,method){const r=await fetch('http://127.0.0.1:'+port+p,{method:method??(data?'POST':'GET'),...(data?{body:JSON.stringify(data),headers:{'Content-Type':'application/json'}}:{}),signal:AbortSignal.timeout(90000)});const j=await r.json();if(!r.ok)throw Error(JSON.stringify(j.value));return j.value;}
try{
 let ready=false;for(let i=0;i<40;i++){try{await rpc('/status');ready=true;break;}catch{await new Promise(r=>setTimeout(r,250));}}if(!ready)throw Error('Driver unavailable: '+errors);
 const s=await rpc('/session',{capabilities:{alwaysMatch:{browserName:'safari'}}});session=s.sessionId;result.capabilities=s.capabilities;
 const base='/session/'+session;
 const evaluate=(script,args=[])=>rpc(base+'/execute/sync',{script,args});
 for(const hint of [null,false,true,true,false,null]){
  await rpc(base+'/url',{url:'http://127.0.0.1:'+server.address().port});
  const c={hint};
  c.draw=await evaluate('const c=document.createElement("canvas");c.id="canvas";c.width=c.height=64;document.body.prepend(c);const ctx=arguments[0]===null?c.getContext("2d"):c.getContext("2d",{willReadFrequently:arguments[0]});ctx.fillStyle="#00ff00";ctx.fillRect(0,0,64,64);return {attrs:ctx.getContextAttributes?.(),visibility:document.visibilityState,secure:isSecureContext};',[hint]);
  c.paint=await rpc(base+'/execute/async',{script:'const done=arguments[arguments.length-1];let ended=false;setTimeout(()=>{if(!ended){ended=true;done(false)}},3000);requestAnimationFrame(()=>requestAnimationFrame(()=>{if(!ended){ended=true;done(true)}}));',args:[]});
  c.readback=await evaluate('try{return {pixel:[...document.querySelector("canvas").getContext("2d").getImageData(0,0,1,1).data]}}catch(e){return {error:e.name+": "+e.message}}');
  for(const id of ['canvas','css'])try{const e=await rpc(base+'/element',{using:'css selector',value:'#'+id});const eid=e['element-6066-11e4-a52e-4f735466cecf'];const bytes=Buffer.from(await rpc(base+'/element/'+encodeURIComponent(eid)+'/screenshot'),'base64');const png=PNG.sync.read(bytes);const offset=(Math.floor(png.height/2)*png.width+Math.floor(png.width/2))*4;c[id+'Screenshot']={width:png.width,height:png.height,pixel:[...png.data.subarray(offset,offset+4)]};}catch(e){c[id+'Screenshot']={error:String(e)};}
  c.passed=c.paint&&[c.readback.pixel,c.canvasScreenshot.pixel,c.cssScreenshot.pixel].every(p=>JSON.stringify(p)==='[0,255,0,255]');result.cases.push(c);await fs.writeFile(out,JSON.stringify(result,null,2));
 }
}catch(e){result.error=String(e);}finally{
 if(session)try{await rpc('/session/'+session,null,'DELETE');}catch(e){result.cleanupError=String(e);}
 if(driver.exitCode===null&&!driver.signalCode){driver.kill('SIGTERM');await new Promise(r=>{const t=setTimeout(()=>{driver.kill('SIGKILL');r()},5000);driver.once('exit',()=>{clearTimeout(t);r()});});}
 await new Promise(r=>server.close(r));result.driverStderr=errors;result.ended=new Date().toISOString();await fs.writeFile(out,JSON.stringify(result,null,2));console.log(JSON.stringify(result));
}
process.exitCode=result.error||result.cleanupError?1:0; // A measured unavailable backend is evidence, not harness failure.

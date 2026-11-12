// Public synthetic smoke: isolated own profiles, actual text and 2D pixels.
import fs from 'node:fs/promises';import path from 'node:path';import os from 'node:os';import http from 'node:http';
import {createRequire} from 'node:module';import {execFile,spawn} from 'node:child_process';import {promisify} from 'node:util';
const run=promisify(execFile),root=path.join(os.homedir(),'AppleLab/browser-channels');
const {chromium,firefox,webkit}=createRequire(path.join(root,'automation/package.json'))('playwright');
const install=JSON.parse(await fs.readFile(path.join(root,'INSTALLATION.json'),'utf8')),engines=JSON.parse(await fs.readFile(path.join(root,'ENGINES.json'),'utf8'));
const output=process.env.APPLELAB_CHANNEL_EVIDENCE??path.join(root,'SMOKE.json'),filter=process.env.APPLELAB_CHANNEL_FILTER?.split(',');
const text='Hello / Příliš žluťoučký kůň / 漢字',html='<!doctype html><meta charset=utf-8><h1 id=fixture>'+text+'</h1><canvas id=c width=32 height=32 style="position:fixed;left:0;top:0"></canvas><script>c.getContext("2d").fillStyle="#00ff00";c.getContext("2d").fillRect(0,0,32,32)</script>';
const server=http.createServer((req,res)=>{res.writeHead(200,{'Content-Type':'text/html; charset=utf-8'});res.end(html)});await new Promise(r=>server.listen(0,'127.0.0.1',r));const url='http://127.0.0.1:'+server.address().port;
const result={platform:process.platform,arch:process.arch,startedAt:new Date().toISOString(),results:[],scope:'Installation smoke only; not full DevAiAye, codecs, extension, WebGL/GPU or security certification'};
const temp=await fs.mkdtemp(path.join(os.tmpdir(),'browser-channel-smoke-'));async function save(){await fs.writeFile(output,JSON.stringify(result,null,2));}
const apps=Object.entries(install.channels).filter(([,r])=>r.path).map(([id,r])=>({id,path:r.path,version:r.version,executable:r.executable}));
for(const c of engines.cft)apps.push({id:'cft-'+c.channel.toLowerCase(),path:c.path,version:c.version,executable:'Google Chrome for Testing'});
try{
 for(const app of apps.filter(a=>!filter||filter.includes(a.id))){let browser;const entry={name:app.id,path:app.path,version:app.version};try{
  const executable=path.join(app.path,'Contents/MacOS',app.executable);entry.executable=executable;
  if(app.id.startsWith('firefox-')){
   const profile=path.join(temp,app.id);await fs.mkdir(profile);const shot=path.join(temp,app.id+'.png');
   await run(executable,['--headless','--no-remote','--profile',profile,'--screenshot',shot,url],{timeout:90000,maxBuffer:1048576});
   const data=await fs.readFile(shot);if(data.length<2000||!data.subarray(0,8).equals(Buffer.from([137,80,78,71,13,10,26,10])))throw Error('No actual PNG screenshot');
   const ffmpeg=process.env.APPLELAB_FFMPEG??'ffmpeg';const {stdout}=await run(ffmpeg,['-v','error','-i',shot,'-vf','crop=1:1:0:0','-frames:v','1','-f','rawvideo','-pix_fmt','rgba','pipe:1'],{encoding:'buffer',timeout:30000});
   if(!stdout.equals(Buffer.from([0,255,0,255])))throw Error('Native Firefox screenshot pixel mismatch');entry.pixel=[...stdout];entry.screenshotBytes=data.length;entry.method='Native Firefox headless screenshot + exact2D readback';
  }else{
   browser=await chromium.launch({executablePath:executable,headless:true,timeout:60000});const page=await browser.newPage();await page.goto(url,{timeout:30000});const actual=await page.locator('#fixture').textContent();const pixel=await page.evaluate(()=>[...document.querySelector('#c').getContext('2d').getImageData(0,0,1,1).data]);if(actual!==text||JSON.stringify(pixel)!=='[0,255,0,255]')throw Error('Actual text/pixel mismatch');entry.pixel=pixel;entry.text=actual;entry.engineVersion=browser.version();entry.method='Actual native browser DOM +2D pixel';
  }entry.state='passed';
 }catch(e){entry.state='failed';entry.error=String(e).slice(0,1500);}finally{if(browser)await browser.close().catch(e=>entry.cleanupError=String(e));}result.results.push(entry);await save();console.log(JSON.stringify(entry));}
 for(const [name,engine] of [['playwright-chromium',chromium],['playwright-firefox',firefox],['playwright-webkit',webkit]].filter(([n])=>!filter||filter.includes(n))){let browser;const entry={name};try{browser=await engine.launch({headless:true,timeout:60000});const page=await browser.newPage();await page.goto(url);const actual=await page.locator('#fixture').textContent(),pixel=await page.evaluate(()=>[...c.getContext('2d').getImageData(0,0,1,1).data]);if(actual!==text||JSON.stringify(pixel)!=='[0,255,0,255]')throw Error('Matching engine text/pixel mismatch');Object.assign(entry,{state:'passed',version:browser.version(),path:engine.executablePath(),pixel,text:actual});}catch(e){entry.state='failed';entry.error=String(e).slice(0,1500);}finally{if(browser)await browser.close().catch(e=>entry.cleanupError=String(e));}result.results.push(entry);await save();console.log(JSON.stringify(entry));}
 // Safari uses its actual supported WebDriver, independently from Playwright WebKit.
 for(const [name,driver,browserName] of [['safari','/usr/bin/safaridriver','safari'],['safari-technology-preview','/Applications/Safari Technology Preview.app/Contents/MacOS/safaridriver','Safari Technology Preview']].filter(([n])=>!filter||filter.includes(n))){
  let child,session,base;const entry={name,driver};const rpc=async(p,data,method)=>{const r=await fetch(base+p,{method:method??(data?'POST':'GET'),...(data?{body:JSON.stringify(data),headers:{'Content-Type':'application/json'}}:{}),signal:AbortSignal.timeout(90000)});const x=await r.json();if(x.value?.error)throw Error(JSON.stringify(x.value));return x.value;};
  try{await fs.access(driver);const s=http.createServer();await new Promise(r=>s.listen(0,'127.0.0.1',r));const port=s.address().port;await new Promise(r=>s.close(r));base='http://127.0.0.1:'+port;child=spawn(driver,['--port',String(port)],{stdio:'ignore'});
   let ready=false;for(let i=0;i<80;i++){try{await rpc('/status');ready=true;break;}catch{}if(child.exitCode!==null)break;await new Promise(r=>setTimeout(r,250));}if(!ready)throw Error('Safari driver unavailable');const created=await rpc('/session',{capabilities:{alwaysMatch:{browserName}}});session=created.sessionId;entry.version=created.capabilities?.browserVersion;
   await rpc('/session/'+session+'/url',{url});const actual=await rpc('/session/'+session+'/execute/sync',{script:'return document.querySelector("#fixture").textContent',args:[]});const pixel=await rpc('/session/'+session+'/execute/sync',{script:'return Array.from(document.querySelector("#c").getContext("2d").getImageData(0,0,1,1).data)',args:[]});if(actual!==text||JSON.stringify(pixel)!=='[0,255,0,255]')throw Error('Safari text/pixel mismatch');Object.assign(entry,{state:'passed',text:actual,pixel});
  }catch(e){entry.state='failed';entry.error=String(e).slice(0,1500);}finally{if(session)await rpc('/session/'+session,null,'DELETE').catch(e=>entry.cleanupError=String(e));if(child&&child.exitCode===null){child.kill('SIGTERM');await new Promise(r=>{const t=setTimeout(()=>{child.kill('SIGKILL');r();},5000);child.once('exit',()=>{clearTimeout(t);r();});});}}
  result.results.push(entry);await save();console.log(JSON.stringify(entry));
 }
}finally{await new Promise(r=>server.close(r));await fs.rm(temp,{recursive:true,force:true});result.ownProfilesRemoved=true;result.endedAt=new Date().toISOString();await save();}
process.exitCode=result.results.length>0&&result.results.every(r=>r.state==='passed'&&!r.cleanupError)?0:1;

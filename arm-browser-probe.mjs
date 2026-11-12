import { createRequire } from 'node:module';
import { createServer } from 'node:http';
import { mkdtemp, readFile, rm } from 'node:fs/promises';
import { join } from 'node:path';
import os from 'node:os';

if (process.arch !== 'arm64' || !['arm64', 'aarch64'].includes(os.machine().toLowerCase())) throw Error(`Expected ARM64 Node/OS: ${process.arch}/${os.machine()}`);
if (!process.env.CHROME_PATH || !process.env.PW_LAB_REQUIRE) throw Error('CHROME_PATH and PW_LAB_REQUIRE are required');
// Playwright's own browser diagnostics are retained only on failure, capped at 16 KiB.
let debugTail = '';
const stderrWrite = process.stderr.write.bind(process.stderr);
process.env.DEBUG = 'pw:browser';
process.stderr.write = function (chunk, ...args) { debugTail = (debugTail + String(chunk)).slice(-16384); const cb = args.find(x => typeof x === 'function'); if (cb) cb(); return true; };
const root = await mkdtemp(join(os.tmpdir(), 'arm-browser-probe-'));
process.env.TMPDIR = root; process.env.TEMP = root; process.env.TMP = root;
const expected = Buffer.from('ARM64 download fixture: Žluťoučký 🐎\n');
const server = createServer((req, res) => {
  if (req.url === '/fixture') { res.writeHead(200, { 'content-type':'application/octet-stream', 'content-disposition':'attachment; filename="fixture.txt"' }); res.end(expected); }
  else { res.writeHead(200, { 'content-type':'text/html; charset=utf-8' }); res.end('<!doctype html><title>ARM64 fixture</title><main id="target"></main><a id="download" href="/fixture" download="fixture.txt">download</a>'); }
});
let browser, context, browserPid = null, failed = false;
try {
  await new Promise((resolve, reject) => { server.once('error', reject); server.listen(0, '127.0.0.1', resolve); });
  console.log(JSON.stringify({node:process.version, arch:process.arch, machine:os.machine(), platform:process.platform, release:os.release()}));
  const { chromium } = createRequire(process.env.PW_LAB_REQUIRE)('playwright');
  browser = await chromium.launch({ executablePath:process.env.CHROME_PATH, headless:true, chromiumSandbox:true, ignoreDefaultArgs:['--enable-unsafe-swiftshader','--unsafely-disable-devtools-self-xss-warnings'], timeout:60000 });
  context = await browser.newContext({acceptDownloads:true});
  const cdp = await browser.newBrowserCDPSession();
  const version = await cdp.send('Browser.getVersion');
  let systemInfo;
  try { systemInfo = await cdp.send('SystemInfo.getInfo'); } catch (e) { systemInfo = {error:String(e)}; }

  try { const processes = await cdp.send('SystemInfo.getProcessInfo'); browserPid = processes.processInfo.find(p => p.type === 'browser')?.id ?? null; } catch { browserPid = null; }
  console.log(JSON.stringify({browserVersion:version.product,protocolVersion:version.protocolVersion,browserPid,gpu:systemInfo.gpu ? { devices:systemInfo.gpu.devices?.map(({vendorId,deviceId,vendorString,deviceString,driverVendor,driverVersion})=>({vendorId,deviceId,vendorString,deviceString,driverVendor,driverVersion})), featureStatus:systemInfo.gpu.featureStatus } : systemInfo}));
  const page = await context.newPage(); page.setDefaultTimeout(30000);
  const base = `http://127.0.0.1:${server.address().port}`;
  await page.goto(base, {waitUntil:'load'});
  const result = await page.evaluate(async () => {
    const unicode = 'Žluťoučký 🐎 漢字'; document.querySelector('#target').textContent = unicode;
    const c=document.createElement('canvas'); c.width=c.height=2; const x=c.getContext('2d'); x.fillStyle='#f00'; x.fillRect(0,0,2,2);
    const out={domUnicode:document.querySelector('#target').textContent===unicode,canvas:Array.from(x.getImageData(0,0,1,1).data)};
    function glTest(kind) {
      const gl=document.createElement('canvas').getContext(kind); if(!gl) return {status:'unavailable'};
      try {
        const is2=kind==='webgl2', vs=gl.createShader(gl.VERTEX_SHADER),fs=gl.createShader(gl.FRAGMENT_SHADER);
        gl.shaderSource(vs,is2?'#version 300 es\nin vec2 p; void main(){gl_Position=vec4(p,0.,1.);}':'attribute vec2 p; void main(){gl_Position=vec4(p,0.,1.);}');
        gl.shaderSource(fs,is2?'#version 300 es\nprecision mediump float; out vec4 color; void main(){color=vec4(1.,0.,0.,1.);}':'precision mediump float; void main(){gl_FragColor=vec4(1.,0.,0.,1.);}');
        gl.compileShader(vs); gl.compileShader(fs); if(!gl.getShaderParameter(vs,gl.COMPILE_STATUS)||!gl.getShaderParameter(fs,gl.COMPILE_STATUS)) throw Error('shader compile: '+gl.getShaderInfoLog(vs)+' '+gl.getShaderInfoLog(fs));
        const pr=gl.createProgram(); gl.attachShader(pr,vs);gl.attachShader(pr,fs);gl.linkProgram(pr);if(!gl.getProgramParameter(pr,gl.LINK_STATUS))throw Error('shader link');gl.useProgram(pr);
        gl.bindBuffer(gl.ARRAY_BUFFER,gl.createBuffer());gl.bufferData(gl.ARRAY_BUFFER,new Float32Array([-1,-1,3,-1,-1,3]),gl.STATIC_DRAW);const loc=gl.getAttribLocation(pr,'p');gl.enableVertexAttribArray(loc);gl.vertexAttribPointer(loc,2,gl.FLOAT,false,0,0);gl.drawArrays(gl.TRIANGLES,0,3);
        const px=new Uint8Array(4);gl.readPixels(0,0,1,1,gl.RGBA,gl.UNSIGNED_BYTE,px);const ext=gl.getExtension('WEBGL_debug_renderer_info');
        if(px[0]!==255||px[1]!==0||px[2]!==0||px[3]!==255)throw Error('pixel mismatch '+Array.from(px));
        return {status:'passed',pixel:Array.from(px),renderer:gl.getParameter(gl.RENDERER),unmaskedRenderer:ext?gl.getParameter(ext.UNMASKED_RENDERER_WEBGL):null};
      } catch(e) {return {status:'failed',error:String(e)};}
    }
    out.webgl1=glTest('webgl');out.webgl2=glTest('webgl2');
    async function gpuTest(){
      if(!navigator.gpu)return {status:'unavailable'};
      try {const a=await navigator.gpu.requestAdapter();if(!a)return {status:'no-adapter'};const d=await a.requestDevice();try {
        const input=d.createBuffer({size:4,usage:GPUBufferUsage.STORAGE|GPUBufferUsage.COPY_SRC}), read=d.createBuffer({size:4,usage:GPUBufferUsage.MAP_READ|GPUBufferUsage.COPY_DST});
        const p=d.createComputePipeline({layout:'auto',compute:{module:d.createShaderModule({code:'@group(0) @binding(0) var<storage,read_write> x: array<u32>; @compute @workgroup_size(1) fn main(){x[0]=42u;}'}),entryPoint:'main'}});
        const bind=d.createBindGroup({layout:p.getBindGroupLayout(0),entries:[{binding:0,resource:{buffer:input}}]});const enc=d.createCommandEncoder(),pass=enc.beginComputePass();pass.setPipeline(p);pass.setBindGroup(0,bind);pass.dispatchWorkgroups(1);pass.end();enc.copyBufferToBuffer(input,0,read,0,4);d.queue.submit([enc.finish()]);await read.mapAsync(GPUMapMode.READ);const value=new Uint32Array(read.getMappedRange())[0];read.unmap();if(value!==42)throw Error('compute mismatch '+value);return {status:'passed',value};
      }finally{d.destroy();}}catch(e){return {status:'failed',error:String(e)}}
    }
    out.webgpu=await Promise.race([gpuTest(),new Promise(resolve=>setTimeout(()=>resolve({status:'timeout'}),15000))]);
    return out;
  });
  console.log(JSON.stringify(result));
  if(!result.domUnicode || JSON.stringify(result.canvas)!=='[255,0,0,255]' || [result.webgl1,result.webgl2,result.webgpu].some(x=>x.status==='failed')) throw Error('Browser fixture failed');
  const [download] = await Promise.all([page.waitForEvent('download',{timeout:30000}),page.locator('#download').click()]);
  const file=join(root,'fixture.txt');await download.saveAs(file);if(!(await readFile(file)).equals(expected))throw Error('Downloaded bytes mismatch');
  console.log(JSON.stringify({download:'passed',bytes:expected.length}));
  try { const netPage=await context.newPage(); const response=await netPage.goto('https://www.google.com/robots.txt',{timeout:15000,waitUntil:'domcontentloaded'}); console.log(JSON.stringify({httpsStatus:response?.status() ?? null}));await netPage.close(); }
  catch(e){console.log(JSON.stringify({httpsStatus:'unavailable',error:String(e).slice(0,500)}));}
} catch(e) { failed=true; console.log(JSON.stringify({error:String(e),stack:String(e.stack ?? '').slice(0,3000)})); }
finally {
  if(context) { try {await context.close({reason:'probe complete'});} catch(e) {failed=true;console.log(JSON.stringify({closeError:String(e)}));} }
  if(browser) { try {await browser.close({reason:'probe complete'});} catch(e) {failed=true;console.log(JSON.stringify({browserCloseError:String(e)}));} }
  if(browserPid) {
    let alive=false;
    for(let i=0;i<20;i++) {
      try {process.kill(browserPid,0);alive=true;} catch(e) {if(e.code==='ESRCH')alive=false;else {alive=true;break;}}
      if(!alive)break;
      await new Promise(resolve=>setTimeout(resolve,250));
    }
    console.log(JSON.stringify({browserPid,browserProcessAfterClose:alive?'present-or-inaccessible':'absent'}));
    if(alive)failed=true;
  }
  await new Promise(resolve=>server.close(()=>resolve()));
  try {await rm(root,{recursive:true,force:true,maxRetries:8,retryDelay:250});} catch(e){failed=true;console.log(JSON.stringify({cleanupError:String(e)}));}
  process.stderr.write=stderrWrite;
  if(failed && debugTail) stderrWrite(`Playwright browser diagnostics (tail):\n${debugTail}\n`);
}
if(failed)process.exitCode=1;

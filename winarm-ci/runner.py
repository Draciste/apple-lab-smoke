"""Windows ARM candidate. Public output is a fixed scalar; detailed logs are sealed."""
import ctypes,hashlib,json,os,pathlib,re,shutil,subprocess,sys,tarfile,tempfile,threading,traceback,zipfile
from ctypes import wintypes
BASE=pathlib.Path(__file__).resolve().parent;TEMP=pathlib.Path(os.environ['RUNNER_TEMP'])
source_key=os.environ.pop('ARM_SOURCE_KEY','');result_key=os.environ.pop('ARM_RESULT_KEY','')
if not all(re.fullmatch(r'[0-9a-f]{64}',x or '') for x in (source_key,result_key)) or source_key==result_key:raise SystemExit('state=incomplete')
work=pathlib.Path(tempfile.mkdtemp(prefix='devaiaye-winarm-',dir=TEMP));evidence=work/'evidence';evidence.mkdir();log=work/'private.log';out=TEMP/'result.enc';state='incomplete';stage='preflight';failure=None
# KILL_ON_JOB_CLOSE contains the owned Node process and descendants once assigned.
class IO(ctypes.Structure):_fields_=[(n,ctypes.c_ulonglong) for n in ['ReadOperationCount','WriteOperationCount','OtherOperationCount','ReadTransferCount','WriteTransferCount','OtherTransferCount']]
class Basic(ctypes.Structure):_fields_=[('PerProcessUserTimeLimit',ctypes.c_longlong),('PerJobUserTimeLimit',ctypes.c_longlong),('LimitFlags',ctypes.c_uint32),('MinimumWorkingSetSize',ctypes.c_size_t),('MaximumWorkingSetSize',ctypes.c_size_t),('ActiveProcessLimit',ctypes.c_uint32),('Affinity',ctypes.c_size_t),('PriorityClass',ctypes.c_uint32),('SchedulingClass',ctypes.c_uint32)]
class Ext(ctypes.Structure):_fields_=[('BasicLimitInformation',Basic),('IoInfo',IO),('ProcessMemoryLimit',ctypes.c_size_t),('JobMemoryLimit',ctypes.c_size_t),('PeakProcessMemoryUsed',ctypes.c_size_t),('PeakJobMemoryUsed',ctypes.c_size_t)]
k=ctypes.WinDLL('kernel32',use_last_error=True);k.CreateJobObjectW.restype=ctypes.c_void_p;k.CreateJobObjectW.argtypes=[ctypes.c_void_p,ctypes.c_wchar_p];k.SetInformationJobObject.argtypes=[ctypes.c_void_p,ctypes.c_int,ctypes.c_void_p,ctypes.c_uint32];k.AssignProcessToJobObject.argtypes=[ctypes.c_void_p,ctypes.c_void_p];k.CloseHandle.argtypes=[ctypes.c_void_p]
def digest(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def environment(extra=None):
 names=('PATH','SystemRoot','WINDIR','COMSPEC','PATHEXT','TEMP','TMP','USERPROFILE','APPDATA','LOCALAPPDATA','PROGRAMFILES','ProgramFiles','ProgramFiles(x86)','ProgramData','PUBLIC')
 env={n:os.environ[n] for n in names if n in os.environ};env.update(PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD='1',CI='1');env.update(extra or {});return env
def run(args,*,cwd=None,env=None,timeout=1200):
 h=k.CreateJobObjectW(None,None)
 if not h:raise OSError('job creation failed')
 x=Ext();x.BasicLimitInformation.LimitFlags=0x2000
 if not k.SetInformationJobObject(h,9,ctypes.byref(x),ctypes.sizeof(x)):k.CloseHandle(h);raise OSError('job policy failed')
 p=None
 try:
  p=subprocess.Popen([str(a) for a in args],cwd=cwd,env=env or environment(),stdout=subprocess.PIPE,stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NO_WINDOW)
  if not k.AssignProcessToJobObject(h,ctypes.c_void_p(int(p._handle))):p.kill();p.wait();raise OSError('job assignment failed')
  def drain():
   count=0
   with log.open('ab') as f:
    while True:
     data=p.stdout.read(65536)
     if not data:break
     part=data[:max(0,2097152-count)];f.write(part);count+=len(part)
  thread=threading.Thread(target=drain,daemon=True);thread.start()
  try:p.wait(timeout=timeout)
  except subprocess.TimeoutExpired:raise RuntimeError('owned child timeout')
  if p.returncode:raise RuntimeError('owned child failed (exit '+str(p.returncode)+')')
 finally:
  k.CloseHandle(h)
  if p:
   try:p.wait(timeout=10)
   except subprocess.TimeoutExpired:pass
   if 'thread' in locals():thread.join(timeout=10)
def seal(op,src,dst,key):run(['node',BASE/'seal.mjs',op,src,dst],env=environment({'ARM_SEAL_KEY':key}),timeout=120)
def extract(src,dst):
 seen=set()
 with tarfile.open(src,'r:') as t:
  for m in t:
   p=pathlib.PurePosixPath(m.name)
   if p.is_absolute() or '..' in p.parts or not m.isfile() or m.name in seen or m.size>20_000_000 or ':' in m.name or '\\' in m.name:raise ValueError('unsafe input archive')
   seen.add(m.name);target=dst.joinpath(*p.parts);target.parent.mkdir(parents=True,exist_ok=True)
   with t.extractfile(m) as inp,target.open('xb') as f:shutil.copyfileobj(inp,f)
def prepare_media(root):
 archive=TEMP/'devaiaye-ffmpeg.zip'
 expected='60f467265b1e312373dbcd92200c2618a74850f98d3d078e94296bb3fa2047ba'
 if cfg.get('helper_sha256')!=expected or cfg.get('helper_arch')!='x64-emulated':raise ValueError('media helper pin mismatch')
 if not archive.is_file() or digest(archive)!=expected:raise ValueError('verified media archive unavailable')
 directory=work/'media';directory.mkdir();tools={}
 with zipfile.ZipFile(archive) as z:
  for name in ('ffmpeg.exe','ffprobe.exe'):
   found=[v for v in z.infolist() if v.filename=='ffmpeg-9.0.2-essentials_build/bin/'+name]
   if len(found)!=1 or found[0].file_size>300_000_000:raise ValueError('media archive members invalid')
   dst=directory/name
   with z.open(found[0]) as src,dst.open('xb') as outp:shutil.copyfileobj(src,outp)
   with dst.open('rb') as f:
    f.seek(0x3c);off=int.from_bytes(f.read(4),'little');f.seek(off)
    if f.read(4)!=b'PE\0\0' or int.from_bytes(f.read(2),'little')!=0x8664:raise ValueError('media helper is not x64 PE')
   probe=subprocess.run([str(dst),'-version'],env=environment(),stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=60,creationflags=subprocess.CREATE_NO_WINDOW)
   if probe.returncode:raise ValueError('media helper version probe failed')
   line=probe.stdout.decode('utf-8','replace').splitlines()[0][:200]
   tools[name]={'path':str(dst),'sha256':digest(dst),'version':line,'executableArchitecture':'x64','execution':'Windows ARM x64 emulation'}
 enc=subprocess.run([str(directory/'ffmpeg.exe'),'-hide_banner','-encoders'],env=environment(),stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=60,creationflags=subprocess.CREATE_NO_WINDOW)
 if enc.returncode or b'libx264' not in enc.stdout or b' aac ' not in enc.stdout:raise ValueError('required media encoders unavailable')
 (root/'devaiaye/runtime/media-tools.local.json').write_text(json.dumps({'platform':'windows-arm64','ffmpeg':str(directory/'ffmpeg.exe'),'ffprobe':str(directory/'ffprobe.exe'),'source':'Gyan 9.0.2 essentials; pinned SHA256; x64 via Windows emulation'}),encoding='utf-8')
 (evidence/'media-tools.json').write_text(json.dumps({'archive_sha256':expected,'source':'https://www.gyan.dev/ffmpeg/builds/packages/ffmpeg-9.0.2-essentials_build.zip','tools':tools,'encoder_probe':'libx264 and aac present','helper_architecture':'x64-emulated; Chrome and Node remain ARM64'}),encoding='utf-8')
 return directory
try:
 if os.environ.get('RUNNER_OS')!='Windows' or os.environ.get('RUNNER_ARCH')!='ARM64':raise ValueError('wrong runner')
 if out.exists():raise ValueError('result already exists')
 stage='read-pins';cfg=json.loads((BASE/'pins.json').read_text(encoding='utf-8-sig'));blob=BASE/'source.enc'
 if digest(blob)!=cfg['ciphertext_sha256']:raise ValueError('ciphertext digest mismatch')
 stage='decrypt-source';tar=work/'source.tar';seal('open',blob,tar,source_key);source_key=''
 if digest(tar)!=cfg['source_tar_sha256']:raise ValueError('source digest mismatch')
 stage='extract-source';root=work/'source';root.mkdir();extract(tar,root);tar.unlink()
 stage='media-helper';media=prepare_media(root)
 env=environment({'CHROME_PATH':os.environ['CHROME_PATH'],'LAB_PRODUCT_ROOT':str(root),'LAB_EVIDENCE':str(evidence),'TEMP':str(work),'TMP':str(work),'PATH':str(media)+os.pathsep+os.environ.get('PATH','')})
 node=pathlib.Path(shutil.which('node') or '')
 npm=node.parent/'node_modules'/'npm'/'bin'/'npm-cli.js'
 if not node.is_file() or not npm.is_file():raise ValueError('Node/npm unavailable')
 for folder in ('devaiaye/runtime','devaiaye/tests/vendor'):
  stage='npm-'+folder.replace('/','-');run([node,npm,'ci','--ignore-scripts','--no-audit','--no-fund'],cwd=root/folder,env=env,timeout=900)
 stage='product-entry';run(['node',root/'windows-entry.mjs'],cwd=root,env=env,timeout=2700)
 stage='verify-entry';entry=json.loads((evidence/'windows-entry.json').read_text(encoding='utf-8'))
 if entry.get('state')!='selected-tests-completed-sandbox-review-required':raise ValueError('selected tests incomplete')
 state='review-required'
except Exception as e:
 # Fixed stage and exception class are always safe. Child output may contain a
 # newly generated lab token, so it is never included as raw diagnostics.
 failure={'stage':stage,'exception':type(e).__name__,'message':str(e)[:300] if type(e) in (ValueError,RuntimeError) and str(e).startswith(('owned child','wrong runner','result already','ciphertext digest','source digest','Node/npm','selected tests','verified media','media archive','media helper','required media')) else 'details withheld'}
 if stage.startswith('npm-') and log.exists():
  # The dependency subprocess is started without secrets or lab tokens. Export
  # only a strict allowlisted npm error-code shape, never the raw log or URLs.
  with log.open('rb') as f:
   f.seek(max(0,log.stat().st_size-65536));tail=f.read(65536).decode('utf-8','replace')
  failure['npm_error_codes']=sorted(set(re.findall(r'(?im)^npm (?:ERR!|error) code (E[A-Z0-9_]{1,24})\s*$',tail)))[:12]
 elif stage=='product-entry' and log.exists():
  # A startup failure can occur before windows-entry can write its own result.
  # Preserve only a tightly bounded Node error-code shape, never raw output.
  with log.open('rb') as f:
   f.seek(max(0,log.stat().st_size-65536));tail=f.read(65536).decode('utf-8','replace')
  failure['node_error_codes']=sorted(set(re.findall(r'\b(?:ERR_[A-Z0-9_]{2,40}|MODULE_NOT_FOUND)\b',tail)))[:12]
finally:
 source_removed='not-created'
 if 'root' in locals() and root.exists():
  try:shutil.rmtree(root);source_removed='removed' if not root.exists() else 'failed'
  except Exception:source_removed='failed'
 if source_removed=='failed':state='incomplete'
 (evidence/'runner.json').write_text(json.dumps({'state':state,'failure':failure,'run_id':os.environ.get('GITHUB_RUN_ID'),'run_attempt':os.environ.get('GITHUB_RUN_ATTEMPT'),'commit':os.environ.get('GITHUB_SHA'),'source_sha256':locals().get('cfg',{}).get('source_tar_sha256'),'source_cleanup':source_removed,'descendant_cleanup':'job-object-closed; verify browser PID in entry','scope':'Windows ARM selected product tests; sandbox/cleanup and full evidence require review'}),encoding='utf-8')
 try:
  package=work/'result.tar'
  with tarfile.open(package,'w') as t:
   for item in evidence.rglob('*'):
    if item.is_symlink() or (not item.is_file() and not item.is_dir()):raise ValueError('invalid evidence item')
    if item.is_file():
     if item.stat().st_size>20_000_000:raise ValueError('evidence item too large')
     t.add(item,arcname='evidence/'+item.relative_to(evidence).as_posix(),recursive=False)
  if package.stat().st_size>200_000_000:raise ValueError('result too large')
  seal('seal',package,out,result_key);result_key=''
  # Upload only a newly authenticated result written by this invocation.
  with open(os.environ['GITHUB_OUTPUT'],'a',encoding='utf-8') as output:
   output.write('artifact_ready=true\n')
 except Exception:state='incomplete'
 shutil.rmtree(work,ignore_errors=True)
print('state='+state)
if state=='incomplete':raise SystemExit(1)

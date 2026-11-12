"""Disabled review candidate; configured only after approval and pin verification."""
import hashlib, json, os, pathlib, re, shutil, signal, subprocess, tarfile, tempfile, threading, traceback, urllib.request

BASE=pathlib.Path(__file__).resolve().parent
TEMP=pathlib.Path(os.environ['RUNNER_TEMP'])
SOURCE_KEY=os.environ.pop('ARM_SOURCE_KEY', '')
RESULT_KEY=os.environ.pop('ARM_RESULT_KEY', '')
for key in (SOURCE_KEY, RESULT_KEY):
    if not re.fullmatch(r'[0-9a-f]{64}',key): raise SystemExit('state=incomplete')
if SOURCE_KEY == RESULT_KEY: raise SystemExit('state=incomplete')
os.umask(0o077)
work=pathlib.Path(tempfile.mkdtemp(prefix='arm-qual-',dir=TEMP))
log=work/'private.log'; evidence=work/'evidence'; evidence.mkdir()
out=TEMP/'result.enc'
state='incomplete'
mode=os.environ.pop('ARM_MODE', '')
from_suite=os.environ.pop('ARM_FROM_SUITE', '')
include_unit=os.environ.pop('ARM_INCLUDE_UNIT', '')
current={'community030','rich040','repair041'}
extended={'audit021','documented-api','mining','scope022','ubo023','vnext'}
if mode not in {'all','current','extended','baseline','probe'} or include_unit not in {'true','false'}:
    raise SystemExit('state=incomplete')
allowed=(current|extended) if mode=='all' else current if mode=='current' else extended if mode=='extended' else set()
if from_suite!='none' and from_suite not in allowed:
    raise SystemExit('state=incomplete')
def run(argv, *, cwd=None, env=None, timeout=1200):
    # Drain continuously while retaining at most 2 MiB per process.
    p=subprocess.Popen([str(x) for x in argv],cwd=cwd,env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,start_new_session=True)
    def drain():
        count=0
        with log.open('ab') as f:
            while True:
                chunk=p.stdout.read(65536)
                if not chunk: break
                remaining=max(0,2097152-count)
                if remaining: f.write(chunk[:remaining]); count+=min(len(chunk),remaining)
    reader=threading.Thread(target=drain,daemon=True); reader.start()
    try: p.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(p.pid,signal.SIGKILL); p.wait(); reader.join(timeout=5)
        raise RuntimeError('child timed out')
    reader.join(timeout=5)
    if reader.is_alive(): raise RuntimeError('child output drain incomplete')
    if p.returncode: raise RuntimeError('child failed')
def digest(p):
    with p.open('rb') as f: return hashlib.file_digest(f,'sha256').hexdigest()
def seal(op,src,dst,key):
    env=dict(os.environ,ARM_SEAL_KEY=key)
    run(['node',BASE/'seal.mjs',op,src,dst],env=env,timeout=120)
def extract_safe(src,dst):
    with tarfile.open(src,'r:') as t:
        for m in t.getmembers():
            p=pathlib.PurePosixPath(m.name)
            if p.is_absolute() or '..' in p.parts or not m.isfile() or m.size>20000000:
                raise ValueError('unsafe source tar entry')
            target=dst.joinpath(*p.parts); target.parent.mkdir(parents=True,exist_ok=True)
            with t.extractfile(m) as inp, target.open('xb') as outp: shutil.copyfileobj(inp,outp)
def download(url,expected,dst):
    if not re.fullmatch(r'https://storage\.googleapis\.com/chrome-for-testing-public/[0-9.]+/mac-arm64/chrome-mac-arm64\.zip',url): raise ValueError('unexpected CfT URL')
    with urllib.request.urlopen(url,timeout=90) as r, dst.open('xb') as f:
        while True:
            chunk=r.read(1048576)
            if not chunk: break
            f.write(chunk)
            if f.tell()>500000000: raise ValueError('download too large')
    observed=digest(dst)
    if expected and observed!=expected: raise ValueError('CfT digest mismatch')
    return observed
def collect_baseline(root,evidence):
    tests=(root/'tests').resolve(); dest=evidence/'baseline'; dest.mkdir(exist_ok=True)
    for name in ('PEER_REPAIR_RUN.json','UNIFIED_RUN.json'):
        record=root/name
        if not record.exists(): continue
        if record.is_symlink() or not record.is_file(): raise ValueError('unexpected result record')
        value=json.loads(record.read_text()); directory=pathlib.Path(value['directory']).resolve()
        if directory.parent!=tests or not directory.is_dir(): raise ValueError('result directory outside tests')
        for item in directory.rglob('*'):
            if item.is_symlink() or (not item.is_file() and not item.is_dir()): raise ValueError('unexpected evidence type')
        shutil.copytree(directory,dest/directory.name)
        shutil.copyfile(record,dest/name)
try:
    if os.uname().sysname!='Darwin' or os.uname().machine!='arm64': raise ValueError('wrong platform')
    cfg=json.loads((BASE/'pins.json').read_text())
    blob=BASE/'source.enc'
    if digest(blob)!=cfg['ciphertext_sha256']: raise ValueError('ciphertext mismatch')
    tar=work/'source.tar'; seal('open',blob,tar,SOURCE_KEY); SOURCE_KEY=''
    if digest(tar)!=cfg['source_tar_sha256']: raise ValueError('source mismatch')
    root=work/'source'; root.mkdir(); extract_safe(tar,root); tar.unlink()
    # The owner checked the file manifest before sealing; the exact authenticated
    # archive digest binds that reviewed closure, including the three repairs.
    run(['sw_vers']);run(['uname','-m']);run(['node','--version']);run(['npm','--version'])
    zipfile=work/'chrome.zip'; cft_hash=download(cfg['cft_url'],cfg.get('cft_sha256'),zipfile)
    (evidence/'cft.json').write_text(json.dumps({'url':cfg['cft_url'],'sha256':cft_hash}))
    browser=work/'browser'; browser.mkdir(); run(['/usr/bin/ditto','-x','-k',zipfile,browser])
    app=browser/'chrome-mac-arm64/Google Chrome for Testing.app'
    # Official CfT URL is fixed; its actual digest and signature/Gatekeeper observations
    # are informational. Do not change Gatekeeper settings or re-sign the app.
    with log.open('ab') as f:
        for args in (['/usr/bin/codesign','--verify','--deep','--strict',str(app)], ['/usr/sbin/spctl','--assess','--type','execute',str(app)]):
            result=subprocess.run(args,stdout=f,stderr=subprocess.STDOUT,timeout=90)
            f.write(('signature-check-exit='+str(result.returncode)+'\n').encode())
    chrome=app/'Contents/MacOS/Google Chrome for Testing'
    # Prefer the image native tools; if absent use official Homebrew formula.
    ffmpeg=shutil.which('ffmpeg'); ffprobe=shutil.which('ffprobe')
    if not ffmpeg or not ffprobe:
        brew=shutil.which('brew')
        if not brew: raise ValueError('native media tools and Homebrew unavailable')
        run([brew,'install','ffmpeg'],timeout=1800)
        ffmpeg=shutil.which('ffmpeg'); ffprobe=shutil.which('ffprobe')
    if not ffmpeg or not ffprobe: raise ValueError('native media tools unavailable')
    ffmpeg=pathlib.Path(ffmpeg).resolve(); ffprobe=pathlib.Path(ffprobe).resolve()
    run(['lipo','-verify_arch','arm64',ffmpeg]); run(['lipo','-verify_arch','arm64',ffprobe]); run([ffmpeg,'-version']); run([ffprobe,'-version'])
    (evidence/'media-tools.json').write_text(json.dumps({'ffmpeg':str(ffmpeg),'ffprobe':str(ffprobe),'ffmpeg_sha256':digest(ffmpeg),'ffprobe_sha256':digest(ffprobe),'scope':'runner image or official Homebrew; see private log for versions'}))
    (root/'runtime/media-tools.local.json').write_text(json.dumps({'platform':'darwin-arm64','ffmpeg':str(ffmpeg),'ffprobe':str(ffprobe),'source':'runner image or official Homebrew'}))
    env=dict(os.environ,PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD='1',PATH=str(ffmpeg.parent)+os.pathsep+str(ffprobe.parent)+os.pathsep+os.environ.get('PATH',''))
    for sub in ('runtime','tests/vendor'): run(['npm','ci','--ignore-scripts','--no-audit','--no-fund'],cwd=root/sub,env=env)
    pwenv=dict(env); pwenv.pop('PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD',None)
    run(['node',root/'runtime/node_modules/playwright/cli.js','install','ffmpeg'],cwd=root,env=pwenv)
    env.update(DEVAIAYE_ROOT=str(root),LAB_EXTENSION=str(root/'extension'),LAB_CFT=str(chrome),DEVAIAYE_LAB_EVIDENCE=str(evidence),DEVAIAYE_LAB_MODE=mode,DEVAIAYE_LAB_INCLUDE_UNIT=('1' if include_unit=='true' else '0'))
    if from_suite!='none': env['DEVAIAYE_LAB_FROM_SUITE']=from_suite
    else: env.pop('DEVAIAYE_LAB_FROM_SUITE',None)
    run(['node',root/'apple/mac042-entry.mjs'],cwd=root,env=env,timeout=3300)
    result=json.loads((evidence/'apple-entry.json').read_text())
    if result.get('state')!='passed' or not result.get('chromeClosed') or not result.get('privateStateRemoved'): raise ValueError('qualification or cleanup incomplete')
    state='passed'
except Exception:
    with log.open('a',encoding='utf8') as f: traceback.print_exc(file=f)
finally:
    try:
        if 'root' in locals(): collect_baseline(root,evidence)
    except Exception:
        state='incomplete'
        with log.open('a',encoding='utf8') as f: traceback.print_exc(file=f)
    (evidence/'runner.json').write_text(json.dumps({'state':state,'scope':{'mode':mode,'fromSuite':None if from_suite=='none' else from_suite,'includeUnitRequested':include_unit=='true','unitApplicable':mode in {'all','current','extended'},'passMeaning':'selected scope only'},'run_id':os.environ.get('GITHUB_RUN_ID'),'source_sha256':locals().get('cfg',{}).get('source_tar_sha256')}))
    # Include only owned evidence and private diagnostics; never source or profile.
    package=work/'result.tar'
    try:
        with tarfile.open(package,'w') as t:
            t.add(evidence,arcname='evidence',recursive=True)
            if log.exists(): t.add(log,arcname='private.log')
        if package.stat().st_size>200000000: raise ValueError('result too large')
        seal('seal',package,out,RESULT_KEY); RESULT_KEY=''
    except Exception:
        state='incomplete'
    shutil.rmtree(work,ignore_errors=True)
print('state='+('finished' if state=='passed' else 'incomplete'))
if state!='passed': raise SystemExit(1)








"""Native CfT and matching Playwright engines, isolated from accepted product pins."""
from pathlib import Path
import argparse,hashlib,json,os,plistlib,shutil,subprocess,tempfile,urllib.request,zipfile
import importlib.util
spec=importlib.util.spec_from_file_location('mac_channels',Path(__file__).with_name('mac-channels.py'));module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);download=module.download

def run(args,timeout=600):return subprocess.run(args,check=True,timeout=timeout)
ap=argparse.ArgumentParser();ap.add_argument('--manifest',type=Path,required=True);ap.add_argument('--node',required=True);args=ap.parse_args()
manifest=json.loads(args.manifest.read_text());root=Path.home()/'AppleLab/browser-channels';root.mkdir(parents=True,exist_ok=True);results=[]
for channel in manifest['cft']:
 name=channel['channel'].lower();target=root/'cft'/name;app=target/('chrome-'+manifest['platform'])/'Google Chrome for Testing.app'
 if app.exists():
  info=plistlib.loads((app/'Contents/Info.plist').read_bytes())
  if info['CFBundleShortVersionString']!=channel['version']:raise RuntimeError('Different retained CfT version; review refresh before replacement')
  results.append({**channel,'path':str(app),'state':'retained'});continue
 if not channel['url'].startswith('https://storage.googleapis.com/chrome-for-testing-public/'):raise ValueError('Unexpected official CfT distribution')
 stage=Path(tempfile.mkdtemp(prefix='.cft-stage-',dir=root));archive=stage/'cft.zip'
 try:
  download(channel['url'],archive)
  with zipfile.ZipFile(archive) as z:
   if any(n.startswith('/') or '..' in Path(n).parts for n in z.namelist()):raise ValueError('Unsafe archive paths')
  expanded=stage/'expanded';run(['/usr/bin/ditto','-x','-k',str(archive),str(expanded)])
  candidate=expanded/('chrome-'+manifest['platform'])/'Google Chrome for Testing.app';info=plistlib.loads((candidate/'Contents/Info.plist').read_bytes())
  if info['CFBundleShortVersionString']!=channel['version']:raise RuntimeError('CfT metadata version mismatch')
  signature=subprocess.run(['/usr/bin/codesign','--verify','--deep','--strict',str(candidate)],capture_output=True,text=True,timeout=180)
  target.parent.mkdir(parents=True,exist_ok=True);os.rename(expanded,target)
  results.append({**channel,'path':str(app),'state':'installed','archiveSHA256':hashlib.file_digest(archive.open('rb'),'sha256').hexdigest(),'signatureExit':signature.returncode,'signatureObservation':signature.stderr[-1000:],'trust':'Official manifest HTTPS distribution and recorded archive hash; no valid publisher signature claim or security bypass'})
 finally:shutil.rmtree(stage)
automation=root/'automation';automation.mkdir(exist_ok=True)
package=automation/'package.json'
if not package.exists():package.write_text('{"name":"applelab-browser-engines","private":true,"version":"1.0.0"}\n')
env={**os.environ,'PATH':str(Path(args.node).parent)+':'+os.environ['PATH'],'PLAYWRIGHT_BROWSERS_PATH':str(root/'pw-cache'),'PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD':'1'}
subprocess.run([str(Path(args.node).parent/'npm'),'install','--ignore-scripts','--no-audit','--no-fund','--save-exact','playwright@1.65.0'],cwd=automation,env=env,check=True,timeout=600)
subprocess.run([args.node,str(automation/'node_modules/playwright/cli.js'),'install','chromium','firefox','webkit'],env=env,check=True,timeout=900)
(root/'ENGINES.json').write_text(json.dumps({'cft':results,'playwright':'1.65.0','automation':str(automation),'cache':str(root/'pw-cache')},indent=2)+'\n')

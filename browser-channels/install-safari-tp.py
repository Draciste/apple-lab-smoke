"""Supported native Apple installer, only inside the selected disposable Apple lab."""
from pathlib import Path
import argparse,hashlib,importlib.util,json,os,plistlib,shutil,subprocess,tempfile
spec=importlib.util.spec_from_file_location('channels',Path(__file__).with_name('mac-channels.py'));m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
ap=argparse.ArgumentParser();ap.add_argument('--manifest',type=Path,required=True);ap.add_argument('--receipt',type=Path,required=True);a=ap.parse_args()
if os.geteuid()!=0:raise SystemExit('Native package install requires existing authorized VM administrator route')
manifest=json.loads(a.manifest.read_text());major=int(subprocess.check_output(['/usr/bin/sw_vers','-productVersion'],text=True).split('.')[0])
if major!=manifest['osMajor']:raise RuntimeError('Manifest does not match native macOS major')
url=manifest['safariTechnologyPreview']['url']
if not url.startswith('https://secure-appldnld.apple.com/STP/'):raise RuntimeError('Unexpected Apple distribution')
stage=Path(tempfile.mkdtemp(prefix='applelab-stp-'));mount=stage/'mount';mount.mkdir();mounted=False;safe=True
try:
 archive=stage/'stp.dmg';m.download(url,archive)
 digest=hashlib.sha256()
 with archive.open('rb') as stream:
  for chunk in iter(lambda:stream.read(1048576),b''):digest.update(chunk)
 subprocess.run(['/usr/bin/hdiutil','attach','-readonly','-nobrowse','-noautoopen','-mountpoint',str(mount),str(archive)],check=True,timeout=180);mounted=True
 packages=list(mount.glob('*.pkg'))
 if len(packages)!=1:raise RuntimeError('Review changed Apple package shape')
 pkg=packages[0];sig=subprocess.run(['/usr/sbin/pkgutil','--check-signature',str(pkg)],capture_output=True,text=True,check=True,timeout=120)
 if not __import__('re').search(r'(?m)^\s*1\.\s+Software Update(?:\s|$)',sig.stdout):raise RuntimeError('Expected Apple Software Update leaf signer')
 subprocess.run(['/usr/sbin/spctl','--assess','--type','install','--verbose=2',str(pkg)],check=True,timeout=180)
 subprocess.run(['/usr/sbin/installer','-pkg',str(pkg),'-target','/'],check=True,timeout=600)
 app=Path('/Applications/Safari Technology Preview.app');info=plistlib.loads((app/'Contents/Info.plist').read_bytes())
 if info['CFBundleIdentifier']!='com.apple.SafariTechnologyPreview':raise RuntimeError('Unexpected installed STP identity')
 subprocess.run(['/usr/bin/codesign','--verify','--deep','--strict',str(app)],check=True,timeout=180)
 subprocess.run(['/usr/sbin/spctl','--assess','--type','execute',str(app)],check=True,timeout=180)
 driver=app/'Contents/MacOS/safaridriver';subprocess.run([str(driver),'--enable'],check=True,timeout=30)
 a.receipt.write_text(json.dumps({'state':'installed','url':url,'archiveSHA256':digest.hexdigest(),'path':str(app),'version':info['CFBundleShortVersionString'],'signature':sig.stdout,'update':'Native Apple Software Update; Safari stable unchanged'},indent=2)+'\n')
finally:
 if mounted:
  r=subprocess.run(['/usr/bin/hdiutil','detach',str(mount)],timeout=120);safe=r.returncode==0
 if safe:shutil.rmtree(stage)
 else:print('Retained owned mounted stage for reconciliation:',stage)

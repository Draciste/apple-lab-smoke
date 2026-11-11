#!/usr/bin/env python3
"""Small synthetic local-only browser fixture; no browser libraries required."""
import http.server
import hashlib
import json
import os
import platform
import queue
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import urllib.parse
import uuid
from pathlib import Path

DOWNLOAD = b'synthetic download: English and Czech UTF-8\n'
TEXT = 'Hello world | Příliš žluťoučký kůň | 漢字'
results = {}

def request(url, data=None, method=None, timeout=12):
    body = None if data is None else json.dumps(data, ensure_ascii=False).encode('utf-8')
    req = urllib.request.Request(url, body, {'Content-Type': 'application/json'}, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.load(response)

def port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]

def browser(name, command, capabilities, url, download_dir=None, download_name="synthetic-fixture.bin"):
    number = port()
    process = None
    session = None
    base = 'http://127.0.0.1:' + str(number)
    phase = 'launch driver'
    driver_log = tempfile.TemporaryFile(mode='w+b')
    try:
        port_args = ['--port=' + str(number)] if name == 'Chrome' else ['--port', str(number)]
        process = subprocess.Popen(command + port_args, stdout=driver_log, stderr=subprocess.STDOUT)
        phase = 'wait for driver'
        for _ in range(80):
            if process.poll() is not None:
                raise RuntimeError('driver exited before accepting connections')
            try:
                request(base + '/status')
                break
            except (OSError, ValueError):
                time.sleep(.25)
        else:
            raise RuntimeError('driver did not become ready')
        phase = 'create session'
        response = request(base + '/session', {'capabilities': {'alwaysMatch': capabilities}}, timeout=90)
        value = response['value']
        session = value['sessionId']
        caps = value.get('capabilities', {})
        phase = 'navigate fixture'
        request(base + '/session/' + session + '/url', {'url': url}, timeout=45)
        phase = 'read rendered fixture'
        value = request(base + '/session/' + session + '/execute/sync', {'script': 'return document.querySelector("#fixture").textContent', 'args': []})['value']
        if value != TEXT:
            raise AssertionError('rendered fixture text mismatch')
        download = {'status': 'unverified', 'detail': 'Safari download destination is not configured by this probe'}
        if download_dir is not None:
            phase = 'click and verify download'
            download = {'status': 'unverified' if name == 'Safari' else 'fail', 'detail': 'download not observed at expected location within 15 seconds'}
            element = request(base + '/session/' + session + '/element', {'using': 'css selector', 'value': '#download'})['value']
            element_id = element['element-6066-11e4-a52e-4f735466cecf']
            request(base + '/session/' + session + '/element/' + element_id + '/click', {})
            target = download_dir / download_name
            for _ in range(60):
                if target.is_file() and target.read_bytes() == DOWNLOAD:
                    download = {'status': 'pass', 'bytes': len(DOWNLOAD)}
                    break
                time.sleep(.25)
        if download['status'] == 'fail':
            raise RuntimeError(download['detail'])
        return {'status': 'pass', 'render': 'pass', 'download': download, 'browserName': caps.get('browserName'), 'browserVersion': caps.get('browserVersion')}
    except Exception as exc:
        driver_log.flush()
        driver_log.seek(0, 2)
        size = driver_log.tell()
        driver_log.seek(max(0, size - 6000))
        tail = driver_log.read().decode('utf-8', errors='replace')
        return {'status': 'fail', 'phase': phase, 'detail': str(exc)[:350], 'driver_exit': process.poll() if process else None, 'driver_log_tail': tail}
    finally:
        if session:
            try:
                request(base + '/session/' + session, method='DELETE')
            except Exception:
                pass
        if process:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        driver_log.close()

def chrome_cdp_version():
    executable = Path('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
    if not executable.is_file():
        return {'status': 'unverified', 'detail': 'Chrome application not found'}
    with tempfile.TemporaryDirectory(prefix='chrome-cdp-') as profile:
        number = port()
        process = subprocess.Popen([str(executable), '--headless', '--remote-debugging-address=127.0.0.1', '--remote-debugging-port=' + str(number), '--user-data-dir=' + profile, 'about:blank'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for _ in range(60):
                if process.poll() is not None:
                    return {'status': 'fail', 'detail': 'Chrome exited before CDP became ready'}
                try:
                    version = request('http://127.0.0.1:' + str(number) + '/json/version')
                    return {'status': 'pass', 'browser': version.get('Browser'), 'protocol_version': version.get('Protocol-Version')}
                except (OSError, ValueError):
                    time.sleep(.25)
            return {'status': 'fail', 'detail': 'CDP version endpoint did not become ready'}
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()

def opera_probe(url, temporary):
    if os.environ.get('GITHUB_ACTIONS') != 'true' or os.environ.get('GITHUB_SERVER_URL') != 'https://github.com':
        return {'status': 'unverified', 'detail': 'Opera acquisition only runs on the GitHub-hosted disposable job'}
    work = Path(temporary) / 'opera'
    work.mkdir()
    image, mount, app = work / 'opera.dmg', work / 'mount', work / 'Opera.app'
    mount.mkdir()
    mounted = False
    process = None
    phase = 'download official installer'
    try:
        opera_url = 'https://get.geo.opera.com/pub/opera/desktop/137.0.6036.63/mac/Opera_137.0.6036.63_Setup.dmg'
        download = subprocess.run(['/usr/bin/curl', '--fail', '--location', '--proto', '=https', '--proto-redir', '=https', '--max-time', '240', '--max-filesize', '800000000', '--output', str(image), '--write-out', '%{content_type}', opera_url], check=True, timeout=250, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        content_type = download.stdout.decode('ascii', errors='replace').strip().split(';', 1)[0].lower()
        if content_type not in ('application/octet-stream', 'application/x-apple-diskimage'):
            raise ValueError('Unexpected installer Content-Type: ' + repr(content_type))
        phase = 'verify pinned installer SHA-256'
        digest = hashlib.sha256()
        with image.open('rb') as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b''):
                digest.update(chunk)
        if digest.hexdigest() != '107729af53552633405ebf3536785fa7ade584e614064fc402c30d790f5de03a':
            raise ValueError('Installer SHA-256 mismatch')
        phase = 'mount read-only'
        subprocess.run(['/usr/bin/hdiutil', 'attach', '-readonly', '-nobrowse', '-noautoopen', '-mountpoint', str(mount), str(image)], check=True, timeout=90, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        mounted = True
        original = mount / 'Opera.app'
        phase = 'verify Opera signature and notarization'
        requirement = 'anchor apple generic and identifier "com.operasoftware.Opera" and certificate leaf[subject.OU] = "A2P9LX4JPN"'
        subprocess.run(['/usr/bin/codesign', '--verify', '--deep', '--strict', '-R', '=' + requirement, str(original)], check=True, timeout=90, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        subprocess.run(['/usr/sbin/spctl', '--assess', '--type', 'execute', str(original)], check=True, timeout=90, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        phase = 'copy verified app'
        subprocess.run(['/usr/bin/ditto', str(original), str(app)], check=True, timeout=90, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        subprocess.run(['/usr/bin/codesign', '--verify', '--deep', '--strict', '-R', '=' + requirement, str(app)], check=True, timeout=90, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        phase = 'launch isolated Opera CDP'
        number = port()
        profile = work / 'profile'
        binary = app / 'Contents' / 'MacOS' / 'Opera'
        log = open(work / 'opera.log', 'w+b')
        try:
            process = subprocess.Popen([str(binary), '--headless', '--no-first-run', '--remote-debugging-address=127.0.0.1', '--remote-debugging-port=' + str(number), '--user-data-dir=' + str(profile), 'about:blank'], stdout=log, stderr=subprocess.STDOUT)
            base = 'http://127.0.0.1:' + str(number)
            for _ in range(120):
                if process.poll() is not None:
                    raise RuntimeError('Opera exited before CDP endpoint became ready')
                try:
                    version = request(base + '/json/version', timeout=1)
                    break
                except (OSError, ValueError):
                    time.sleep(.25)
            else:
                raise TimeoutError('Opera CDP endpoint did not become ready')
            phase = 'render synthetic page over CDP'
            target = request(base + '/json/new?' + urllib.parse.quote(url, safe=''), method='PUT')
            js = r"""const ws = new WebSocket(process.argv[1]); let n = 0; const timer = setTimeout(() => { console.error('CDP timeout'); process.exit(2); }, 20000); ws.onopen = () => ws.send(JSON.stringify({id:1,method:'Runtime.evaluate',params:{expression:'JSON.stringify({text:document.querySelector(\"#fixture\")?.textContent,ua:navigator.userAgent})',returnByValue:true}})); ws.onmessage = event => { const m = JSON.parse(event.data); if (m.id !== 1) return; try { const x = JSON.parse(m.result.result.value); if (x.text && x.ua) { clearTimeout(timer); console.log(JSON.stringify(x)); ws.close(); return; } } catch (_) {} if (++n < 40) setTimeout(() => ws.send(JSON.stringify({id:1,method:'Runtime.evaluate',params:{expression:'JSON.stringify({text:document.querySelector(\"#fixture\")?.textContent,ua:navigator.userAgent})',returnByValue:true}})),250); };"""
            node = subprocess.run(['node', '-e', js, target['webSocketDebuggerUrl']], check=True, capture_output=True, text=True, timeout=25)
            actual = json.loads(node.stdout)
            if actual.get('text') != TEXT or 'OPR/' not in actual.get('ua', ''):
                raise ValueError('Opera brand or rendered text mismatch')
            phase = 'verify synthetic Opera download over CDP'
            download_dir = work / 'downloads'
            download_dir.mkdir()
            download_js = r"""const [browserURL,pageURL,dir,expectedURL]=process.argv.slice(1); let browser, page, guid; const timer=setTimeout(()=>{console.error('CDP download timeout');process.exit(2)},20000); function fail(x){console.error(x);process.exit(2)} browser=new WebSocket(browserURL); browser.onopen=()=>browser.send(JSON.stringify({id:1,method:'Browser.setDownloadBehavior',params:{behavior:'allow',downloadPath:dir,eventsEnabled:true}})); browser.onerror=()=>fail('browser websocket error'); browser.onmessage=e=>{let m=JSON.parse(e.data); if(m.id===1){if(m.error) return fail(JSON.stringify(m.error)); page=new WebSocket(pageURL);page.onerror=()=>fail('page websocket error'); page.onopen=()=>page.send(JSON.stringify({id:2,method:'Runtime.evaluate',params:{expression:'document.querySelector("#download").click()'}}));page.onmessage=e=>{let x=JSON.parse(e.data);if(x.id===2 && (x.error||x.result?.exceptionDetails)) fail(JSON.stringify(x));};} if(m.method==='Browser.downloadWillBegin' && m.params.url===expectedURL) guid=m.params.guid; if(m.method==='Browser.downloadProgress' && guid && m.params.guid===guid){if(m.params.state==='canceled') return fail('download canceled');if(m.params.state==='completed'){clearTimeout(timer); console.log(JSON.stringify({guid,state:m.params.state})); page.close();browser.close();}}};"""
            node = subprocess.run(['node', '-e', download_js, version['webSocketDebuggerUrl'], target['webSocketDebuggerUrl'], str(download_dir), url.rsplit('/', 1)[0] + '/synthetic-fixture.bin'], check=True, capture_output=True, text=True, timeout=25)
            event = json.loads(node.stdout)
            if event.get('state') != 'completed' or (download_dir / 'synthetic-fixture.bin').read_bytes() != DOWNLOAD:
                raise ValueError('Opera download event or bytes mismatch')
            return {'status': 'pass', 'render': 'pass', 'signature': 'Apple trust, Opera bundle and team verified', 'browser': version.get('Browser'), 'download': {'status': 'pass', 'bytes': len(DOWNLOAD), 'evidence': 'CDP completion and exact file bytes'}}
        finally:
            if process and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            log.close()
    except Exception as exc:
        detail = str(exc)[:300]
        if isinstance(exc, subprocess.CalledProcessError) and exc.stderr:
            detail += ': ' + exc.stderr.decode('utf-8', errors='replace')[-1000:]
        return {'status': 'unverified', 'phase': phase, 'detail': detail}
    finally:
        if mounted:
            try:
                subprocess.run(['/usr/bin/hdiutil', 'detach', str(mount)], timeout=30, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                print('Warning: could not detach temporary Opera image', file=sys.stderr)

def safari_mcp(url):
    executable = '/usr/bin/safaridriver'
    if not Path(executable).is_file():
        return {'status': 'unverified', 'detail': 'safaridriver unavailable'}
    process = subprocess.Popen([executable, '--mcp'], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding='utf-8', bufsize=1)
    lines = queue.Queue()
    def read_lines():
        for line in process.stdout:
            lines.put(line)
    threading.Thread(target=read_lines, daemon=True).start()
    def send(message):
        process.stdin.write(json.dumps(message) + '\n')
        process.stdin.flush()
    def response(identifier):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            line = lines.get(timeout=max(.01, deadline - time.monotonic()))
            message = json.loads(line)
            if message.get('id') == identifier:
                if 'error' in message:
                    raise RuntimeError('MCP returned an error: ' + str(message['error'])[:250])
                return message['result']
        raise TimeoutError('MCP response timeout')
    try:
        send({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-03-26', 'capabilities': {}, 'clientInfo': {'name': 'synthetic-probe', 'version': '1'}}})
        init = response(1)
        send({'jsonrpc': '2.0', 'method': 'notifications/initialized'})
        send({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list', 'params': {}})
        tools = response(2).get('tools')
        if not isinstance(tools, list):
            raise ValueError('tools/list did not return a list')
        catalog = [{'name': t.get('name'), 'inputSchema': t.get('inputSchema')} for t in tools if isinstance(t, dict)]
        result = {'status': 'pass', 'protocol_version': init.get('protocolVersion'), 'server': init.get('serverInfo'), 'tool_count': len(tools), 'tools': catalog, 'call': {'status': 'unverified', 'detail': 'documented navigate_to_url schema is not directly usable'}}
        candidates = [t for t in tools if isinstance(t, dict) and t.get('name') == 'navigate_to_url']
        if (len(candidates) == 1 and os.environ.get('GITHUB_ACTIONS') == 'true' and
            os.environ.get('GITHUB_SERVER_URL') == 'https://github.com'):
            schema = candidates[0].get('inputSchema') or {}
            props = schema.get('properties') or {}
            if (schema.get('type') == 'object' and isinstance(props, dict) and
                (props.get('url') or {}).get('type') == 'string' and
                set(schema.get('required') or []).issubset({'url'})):
                try:
                    send({'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call', 'params': {'name': 'navigate_to_url', 'arguments': {'url': url}}})
                    called = response(3)
                    serialized = json.dumps(called, ensure_ascii=False)
                    if called.get('isError') or TEXT not in serialized:
                        result['call'] = {'status': 'unverified', 'detail': 'tool returned an error or did not contain the expected fixture text'}
                    else:
                        result['call'] = {'status': 'pass', 'tool': 'navigate_to_url', 'evidence': 'fixture text returned'}
                except Exception as exc:
                    result['call'] = {'status': 'unverified', 'detail': str(exc)[:250] or type(exc).__name__}
        return result
    except Exception as exc:
        return {'status': 'unverified', 'detail': str(exc)[:250] or type(exc).__name__}
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()

def main():
    gaps_only = '--gaps-only' in sys.argv[1:]
    if any(arg != '--gaps-only' for arg in sys.argv[1:]):
        raise SystemExit('usage: probe.py [--gaps-only]')
    with tempfile.TemporaryDirectory(prefix='synthetic-probe-') as temporary:
        folder = Path(temporary) / 'Příliš žluťoučký' / '漢字'
        folder.mkdir(parents=True)
        payload = {'en': 'Hello world', 'cs': 'Příliš žluťoučký kůň', 'unicode_path': str(folder.name)}
        file = folder / 'příběh.json'
        file.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
        code = 'import json,sys; print(json.dumps(json.load(open(sys.argv[1],encoding="utf-8")),ensure_ascii=False))'
        out = subprocess.run([sys.executable, '-c', code, str(file)], check=True, capture_output=True, text=True, encoding='utf-8', timeout=10, env={**os.environ, 'PYTHONIOENCODING': 'utf-8'})
        assert json.loads(out.stdout) == payload
        results['unicode_subprocess_json'] = {'status': 'pass', 'cases': ['English', 'Czech', 'Unicode path']}
        safari_name = 'applelab-' + uuid.uuid4().hex + '.bin'
        html = '<!doctype html><meta charset="utf-8"><div id="fixture">' + TEXT + '</div><a id="download" href="/synthetic-fixture.bin">Download</a>'
        (folder / 'index.html').write_text(html, encoding='utf-8')
        class Handler(http.server.SimpleHTTPRequestHandler):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, directory=str(folder), **kwargs)
            def do_GET(self):
                if self.path in ('/synthetic-fixture.bin', '/' + safari_name):
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/octet-stream')
                    self.send_header('Content-Disposition', 'attachment; filename="' + self.path[1:] + '"')
                    self.send_header('Content-Length', str(len(DOWNLOAD)))
                    self.end_headers()
                    self.wfile.write(DOWNLOAD)
                else:
                    super().do_GET()
            def log_message(self, *args):
                pass
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = 'http://127.0.0.1:' + str(server.server_port) + '/index.html'
        try:
            chrome_download = Path(temporary) / 'chrome-download'
            firefox_download = Path(temporary) / 'firefox-download'
            chrome_download.mkdir()
            firefox_download.mkdir()
            drivers = [
                ('Safari', '/usr/bin/safaridriver', {'browserName': 'safari'}, None),
                ('Firefox', shutil.which('geckodriver'), {'browserName': 'firefox', 'moz:firefoxOptions': {'binary': '/Applications/Firefox.app/Contents/MacOS/firefox', 'prefs': {'browser.download.folderList': 2, 'browser.download.dir': str(firefox_download), 'browser.helperApps.neverAsk.saveToDisk': 'application/octet-stream', 'browser.download.useDownloadDir': True}}}, firefox_download),
                ('Chrome', shutil.which('chromedriver') or str(Path(os.environ.get('CHROMEWEBDRIVER', '/nonexistent')) / 'chromedriver'), {'browserName': 'chrome', 'goog:chromeOptions': {'prefs': {'download.default_directory': str(chrome_download), 'download.prompt_for_download': False}}}, chrome_download),
            ]
            if gaps_only:
                drivers = drivers[:1]
            for name, executable, caps, download_dir in drivers:
                if not executable or not Path(executable).is_file():
                    results[name] = {'status': 'unverified', 'detail': 'driver not found'}
                else:
                    if name == 'Safari' and os.environ.get('GITHUB_ACTIONS') == 'true' and os.environ.get('GITHUB_SERVER_URL') == 'https://github.com':
                        safari_download = Path.home() / 'Downloads'
                        # This random name belongs to this run; never replace an existing file.
                        safari_target = safari_download / safari_name
                        if safari_download.is_dir() and not safari_target.exists():
                            safari_url = url + '?download=' + safari_name
                            # The page uses this unique download link only for the Safari run.
                            (folder / 'index.html').write_text(html.replace('/synthetic-fixture.bin', '/' + safari_name), encoding='utf-8')
                            try:
                                results[name] = browser(name, [executable], caps, safari_url, safari_download, safari_name)
                            finally:
                                safari_target.unlink(missing_ok=True)
                                (folder / 'index.html').write_text(html, encoding='utf-8')
                        else:
                            results[name] = browser(name, [executable], caps, url)
                    else:
                        results[name] = browser(name, [executable], caps, url, download_dir)
            results['Opera'] = opera_probe(url, temporary)
            results['Safari_MCP'] = safari_mcp(url)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
    if not gaps_only:
        results['chrome_cdp_version'] = chrome_cdp_version()
    results['capabilities'] = {'protocol': 'W3C WebDriver over loopback HTTP; no Selenium client library', 'display': 'headed browser session attempted; no screen interaction or remote desktop', 'downloads': 'Safari attempts unique file in disposable CI Downloads; Chrome and Firefox use temporary directories', 'chrome_cdp': 'loopback version endpoint only', 'safari_mcp': 'tool schemas and conditional loopback navigate_to_url call', 'opera': 'official signed app acquisition, CDP render and download attempt', 'webkit': 'not a Safari substitute'}
    results['environment'] = {'system': platform.system(), 'release': platform.release(), 'macos_version': platform.mac_ver()[0], 'machine': platform.machine(), 'image_os': os.environ.get('ImageOS'), 'image_version': os.environ.get('ImageVersion')}
    output = json.dumps(results, ensure_ascii=False, indent=2)
    print(output)
    summary = os.environ.get('GITHUB_STEP_SUMMARY')
    if summary:
        with open(summary, 'a', encoding='utf-8') as file:
            file.write('## Synthetic browser probe\n\n```json\n' + output + '\n```\n')
    required = ('Safari', 'Opera') if gaps_only else ('unicode_subprocess_json', 'Safari', 'Firefox', 'Chrome', 'Opera')
    return 0 if all(results.get(name, {}).get('status') == 'pass' for name in required) else 1

if __name__ == '__main__':
    sys.exit(main())

#!/usr/bin/env python3
"""Small synthetic local-only browser fixture; no browser libraries required."""
import http.server
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
from pathlib import Path

DOWNLOAD = b'synthetic download: English and Czech UTF-8\n'
TEXT = 'Hello world | Příliš žluťoučký kůň | 漢字'
results = {}

def request(url, data=None, method=None):
    body = None if data is None else json.dumps(data, ensure_ascii=False).encode('utf-8')
    req = urllib.request.Request(url, body, {'Content-Type': 'application/json'}, method=method)
    with urllib.request.urlopen(req, timeout=12) as response:
        return json.load(response)

def port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]

def browser(name, command, capabilities, url, download_dir=None):
    number = port()
    process = None
    session = None
    base = 'http://127.0.0.1:' + str(number)
    try:
        process = subprocess.Popen(command + ['--port', str(number)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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
        response = request(base + '/session', {'capabilities': {'alwaysMatch': capabilities}})
        value = response['value']
        session = value['sessionId']
        caps = value.get('capabilities', {})
        request(base + '/session/' + session + '/url', {'url': url})
        value = request(base + '/session/' + session + '/execute/sync', {'script': 'return document.querySelector("#fixture").textContent', 'args': []})['value']
        if value != TEXT:
            raise AssertionError('rendered fixture text mismatch')
        download = {'status': 'unverified', 'detail': 'Safari download destination is not configured by this probe'}
        if download_dir is not None:
            download = {'status': 'fail', 'detail': 'download did not complete within 15 seconds'}
            element = request(base + '/session/' + session + '/element', {'using': 'css selector', 'value': '#download'})['value']
            element_id = element['element-6066-11e4-a52e-4f735466cecf']
            request(base + '/session/' + session + '/element/' + element_id + '/click', {})
            target = download_dir / 'synthetic-fixture.bin'
            for _ in range(60):
                if target.is_file() and target.read_bytes() == DOWNLOAD:
                    download = {'status': 'pass', 'bytes': len(DOWNLOAD)}
                    break
                time.sleep(.25)
        return {'status': 'pass' if download['status'] != 'fail' else 'fail', 'render': 'pass', 'download': download, 'browserName': caps.get('browserName'), 'browserVersion': caps.get('browserVersion')}
    except Exception as exc:
        return {'status': 'fail', 'detail': str(exc)[:350]}
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

def safari_mcp():
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
        return {'status': 'pass', 'protocol_version': init.get('protocolVersion'), 'server': init.get('serverInfo'), 'tool_count': len(tools), 'scope': 'initialize and tools/list only; no tool calls'}
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
        html = '<!doctype html><meta charset="utf-8"><div id="fixture">' + TEXT + '</div><a id="download" href="/synthetic-fixture.bin">Download</a>'
        (folder / 'index.html').write_text(html, encoding='utf-8')
        class Handler(http.server.SimpleHTTPRequestHandler):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, directory=str(folder), **kwargs)
            def do_GET(self):
                if self.path == '/synthetic-fixture.bin':
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/octet-stream')
                    self.send_header('Content-Disposition', 'attachment; filename="synthetic-fixture.bin"')
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
                ('Firefox', shutil.which('geckodriver'), {'browserName': 'firefox', 'moz:firefoxOptions': {'prefs': {'browser.download.folderList': 2, 'browser.download.dir': str(firefox_download), 'browser.helperApps.neverAsk.saveToDisk': 'application/octet-stream', 'browser.download.useDownloadDir': True}}}, firefox_download),
                ('Chrome', shutil.which('chromedriver') or str(Path(os.environ.get('CHROMEWEBDRIVER', '/nonexistent')) / 'chromedriver'), {'browserName': 'chrome', 'goog:chromeOptions': {'prefs': {'download.default_directory': str(chrome_download), 'download.prompt_for_download': False}}}, chrome_download),
            ]
            for name, executable, caps, download_dir in drivers:
                if not executable or not Path(executable).is_file():
                    results[name] = {'status': 'unverified', 'detail': 'driver not found'}
                else:
                    results[name] = browser(name, [executable], caps, url, download_dir)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
    results['Safari_MCP'] = safari_mcp()
    results['Opera'] = {'status': 'unverified', 'detail': 'No Opera acquisition or compatible driver configured; see README for official installation fallback'}
    results['chrome_cdp_version'] = chrome_cdp_version()
    results['capabilities'] = {'protocol': 'W3C WebDriver over loopback HTTP; no Selenium client library', 'display': 'headed browser session attempted; no screen interaction or remote desktop', 'downloads': 'attempted for Chrome and Firefox; Safari unverified', 'chrome_cdp': 'loopback version endpoint only', 'safari_mcp': 'initialize and tools/list only', 'opera': 'unverified; see installation fallback', 'webkit': 'not a Safari substitute'}
    results['environment'] = {'system': platform.system(), 'release': platform.release(), 'machine': platform.machine(), 'image_os': os.environ.get('ImageOS'), 'image_version': os.environ.get('ImageVersion')}
    output = json.dumps(results, ensure_ascii=False, indent=2)
    print(output)
    summary = os.environ.get('GITHUB_STEP_SUMMARY')
    if summary:
        with open(summary, 'a', encoding='utf-8') as file:
            file.write('## Synthetic browser probe\n\n```json\n' + output + '\n```\n')
    return 0 if all(results.get(name, {}).get('status') == 'pass' for name in ('unicode_subprocess_json', 'Safari', 'Firefox', 'Chrome')) else 1

if __name__ == '__main__':
    sys.exit(main())

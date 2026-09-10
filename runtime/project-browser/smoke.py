"""Real Linux/browser acceptance; no provider calls. Run as the service user."""
import json
from pathlib import Path
import tempfile
from factory.control.claude_terminal import TerminalSession
from factory.control.project_browser import BrowserSession

with tempfile.TemporaryDirectory(prefix='webuddy-browser-smoke-') as root:
    workspace = Path(root)
    (workspace / 'index.html').write_text('''<!doctype html><meta name="viewport" content="width=device-width"><title>Browser smoke</title>
<label>Name <input aria-label="Name"></label><button onclick="document.querySelector('p').textContent='Hello '+document.querySelector('input').value">Greet</button><p>Waiting</p>''')
    (workspace / 'preview.py').write_text('''import http.server
from pathlib import Path
server=http.server.ThreadingHTTPServer(('127.0.0.1',0),http.server.SimpleHTTPRequestHandler)
Path('preview-port').write_text(str(server.server_port))
server.serve_forever()
''')
    terminal = TerminalSession(workspace)
    browser = BrowserSession(workspace, terminal)
    try:
        result = terminal.run('python3 preview.py > /tmp/preview.log 2>&1 &', 10)
        assert result['exit_code'] == 0, result
        import time
        for _ in range(50):
            if (workspace / 'preview-port').exists(): break
            time.sleep(.1)
        port = int((workspace / 'preview-port').read_text())
        result = browser.call('open', url=f'http://127.0.0.1:{port}', width=390, height=844)
        assert result.get('ok'), result
        assert result['viewport']['width'] == 390, result
        field = next(item['ref'] for item in result['elements'] if item['tag'] == 'input')
        result = browser.call('fill', ref=field, text='webuddy')
        assert result.get('ok'), result
        button = next(item['ref'] for item in result['elements'] if item['tag'] == 'button')
        result = browser.call('click', ref=button)
        assert result.get('ok'), result
        result = browser.call('snapshot')
        assert 'Hello webuddy' in result['text'], result
        result = browser.call('screenshot')
        assert result.get('ok'), result
        size = Path(result['screenshot_path']).stat().st_size
        assert size > 1000, result
        denied = browser.call('open', url='http://127.0.0.1:8788')
        assert not denied.get('ok'), denied
        print(json.dumps({'passed': True, 'viewport': result['viewport'], 'rendered': 'Hello webuddy', 'screenshot_bytes': size, 'host_port_blocked': True}))
    finally:
        browser.close()
        terminal.close()

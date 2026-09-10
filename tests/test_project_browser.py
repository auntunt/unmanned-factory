import base64
import json
import shlex
from contextlib import contextmanager

import pytest
from factory.control.project_browser import BrowserSession, TOOL_NAMES
from factory.permission.rules import check_command, Decision

class Terminal:
    def __init__(self,workspace): self.workspace=workspace;self.commands=[]
    def run(self,command,timeout):
        self.commands.append((command,timeout))
        assert check_command(command).decision==Decision.ALLOW
        if '--serve' in command:return {'exit_code':0,'output':''}
        return {'exit_code':0,'output':json.dumps({'ok':True,'text':'page'})}

def test_reuses_session_quotes_input_and_holds_one_resource_slot(tmp_path,monkeypatch):
    active=[]
    @contextmanager
    def slot(timeout):
        assert not active
        active.append(True)
        try:yield 0.02
        finally:active.pop()
    monkeypatch.setattr('factory.control.resources.command_slot',slot)
    terminal=Terminal(tmp_path)
    original=terminal.run
    def execute(*args):
        assert active
        return original(*args)
    terminal.run=execute
    browser=BrowserSession(tmp_path,terminal,runtime='/opt/webuddy browser',chrome='/usr/bin/chrome')
    result=browser.call('open',url='http://127.0.0.1:5173',width=390,height=844)
    assert result['ok'] and result['wait_s']==.02
    browser.call('fill',ref='e1',text='$(touch /tmp/nope); `secret`')
    assert sum('--serve' in command for command,_ in terminal.commands)==1
    assert all('touch /tmp/nope' not in command for command,_ in terminal.commands)
    payload=json.loads(base64.b64decode(shlex.split(terminal.commands[-1][0])[-1]))
    assert payload['args']['text']=='$(touch /tmp/nope); `secret`'
    browser.close()

def test_requires_matching_terminal(tmp_path):
    with pytest.raises(ValueError):BrowserSession(tmp_path,None)
    with pytest.raises(ValueError):BrowserSession(tmp_path,Terminal(tmp_path/'other'))

def test_missing_runtime_never_falls_back_to_host(tmp_path):
    terminal=Terminal(tmp_path);terminal.run=lambda *_:{'exit_code':1,'output':'missing'}
    with pytest.raises(RuntimeError,match='administrator'):BrowserSession(tmp_path,terminal).call('snapshot')

def test_truncated_response_is_not_success(tmp_path):
    terminal=Terminal(tmp_path);browser=BrowserSession(tmp_path,terminal);browser.started=True
    terminal.run=lambda *_:{'exit_code':0,'truncated':True,'output':'{"ok":true}'}
    with pytest.raises(RuntimeError,match='complete'):browser.call('snapshot')
    assert len(TOOL_NAMES)==5

import json
import subprocess
from pathlib import Path

import httpx
import pytest

from factory.control.github import GitHubDelivery


def view(**changes):
    return {'id': 123, 'full_name':'owner/project', 'private':True,
            'default_branch':'main', 'permissions':{'push':True}, **changes}


def client(handler):
    return GitHubDelivery('test-not-a-real-token', httpx.Client(base_url='https://api.github.com',
        transport=httpx.MockTransport(handler), trust_env=False))


def test_account_list_and_repository_only_expose_writable_repos():
    def api(request):
        if request.url.path == '/user': return httpx.Response(200, json={'login':'owner','avatar_url':'https://avatars.githubusercontent.com/u/1','token':'hidden'})
        if request.url.path == '/user/repos':
            assert request.url.params['page'] == '2'
            return httpx.Response(200, json=[view(),view(id=2,permissions={'push':False}),view(id=3,archived=True)])
        return httpx.Response(200,json=view())
    publisher = client(api)
    assert set(publisher.account()) == {'login','html_url','avatar_url'}
    assert publisher.repositories(page=2)['repositories'] == [publisher.repository('owner/project')]


def test_create_private_uninitialized_repository_and_reject_existing():
    exists, calls = False, []
    def api(request):
        nonlocal exists
        calls.append(request)
        if request.url.path == '/user': return httpx.Response(200,json={'login':'owner'})
        if request.method == 'GET': return httpx.Response(200 if exists else 404,json=view() if exists else {})
        data = json.loads(request.content)
        assert data == {'name':'project','private':True,'description':'','auto_init':False}
        exists = True
        return httpx.Response(201,json=view())
    publisher = client(api)
    assert publisher.create_repository(name='project')['full_name'] == 'owner/project'
    with pytest.raises(ValueError,match='同名仓库已存在'):
        publisher.create_repository(name='project')
    assert sum(r.method == 'POST' for r in calls) == 1


def test_uncertain_create_requires_explicit_existing_selection():
    exists = False
    def api(request):
        nonlocal exists
        if request.url.path == '/user': return httpx.Response(200,json={'login':'owner'})
        if request.method == 'GET': return httpx.Response(200 if exists else 404,json=view() if exists else {})
        exists = True
        raise httpx.ReadTimeout('response lost', request=request)
    publisher = client(api)
    with pytest.raises(httpx.ReadTimeout): publisher.create_repository(name='project')
    with pytest.raises(ValueError,match='同名仓库'): publisher.create_repository(name='project')
    assert publisher.repository('owner/project')['id'] == 123


def git(root, *args):
    return subprocess.check_output(['git',*args],cwd=root,text=True,stderr=subprocess.DEVNULL).strip()


@pytest.fixture
def release(tmp_path, monkeypatch):
    root = tmp_path/'work'; root.mkdir()
    remote = tmp_path/'remote.git'
    git(root,'init','-q','-b','main'); git(root,'config','user.name','Test'); git(root,'config','user.email','test@example.com')
    git(root,'init','-q','--bare',str(remote))
    (root/'README.md').write_text('baseline'); git(root,'add','.'); git(root,'commit','-qm','baseline')
    base = git(root,'rev-parse','HEAD')
    git(root,'checkout','-qb','factory/release'); (root/'result.py').write_text('print(42)')
    git(root,'add','.'); git(root,'commit','-qm','verified'); sha = git(root,'rev-parse','HEAD')
    actual_run = subprocess.run; git_calls = []
    def local(args, **kwargs):
        if args[1] in ('push','fetch','ls-remote'):
            git_calls.append(list(args))
            assert not any(str(arg).startswith('--force') for arg in args)
            assert 'test-not-a-real-token' not in ' '.join(args)
            args = [str(remote) if arg == 'https://github.com/owner/project.git' else arg for arg in args]
        return actual_run(args,**kwargs)
    monkeypatch.setattr('factory.control.github.subprocess.run',local)
    project = {'repository':'owner/project','base_branch':'main'}
    run = {'id':'release','revision':1,'plan':{'title':'Release','summary':'Result'},
           'artifacts':{'branch':'factory/release','commit':sha,'worktree':str(root),'checks':[{'name':'check','exit':0}]}}
    return root, remote, project, run, base, git_calls


def test_initial_publish_exact_verified_commit_and_retry_without_pr(release):
    root,remote,project,run,base,calls = release
    def api(request): raise AssertionError('initial push requires no PR call')
    publisher = client(api)
    first = publisher.publish(project,run)
    assert first['publication_type'] == 'initial' and first['published_branch'] == 'main'
    assert first['repository_url'] == 'https://github.com/owner/project' and 'pr_url' not in first
    assert git(remote,'rev-parse','refs/heads/main') == run['artifacts']['commit']
    assert publisher.publish(project,run) == first
    assert sum(args[1] == 'push' for args in calls) == 1
    assert git(root,'rev-parse','main') == base


def test_nonempty_shared_history_uses_pr(release):
    root,remote,project,run,base,calls = release
    git(root,'push',str(remote),f'{base}:refs/heads/main')
    posted = []
    def api(request):
        if request.method == 'GET': return httpx.Response(200,json=[])
        posted.append(json.loads(request.content))
        return httpx.Response(201,json={'number':7,'html_url':'https://github.com/owner/project/pull/7'})
    result = client(api).publish(project,run)
    assert result['publication_type'] == 'pull_request' and result['pr_number'] == 7
    assert posted[0]['base'] == 'main' and posted[0]['head'] == 'factory/release'
    assert git(remote,'rev-parse','main') == base


def test_unrelated_existing_history_is_never_pushed_over(release, tmp_path):
    root,remote,project,run,base,calls = release
    other = tmp_path/'other';other.mkdir();git(other,'init','-q','-b','main')
    git(other,'config','user.name','Test');git(other,'config','user.email','test@example.com')
    (other/'unrelated').write_text('keep');git(other,'add','.');git(other,'commit','-qm','unrelated')
    original = git(other,'rev-parse','HEAD');git(other,'push',str(remote),'main')
    calls.clear()
    with pytest.raises(ValueError,match='没有共同历史'):
        client(lambda _:None).publish(project,run)
    assert not any(args[1] == 'push' for args in calls)
    assert git(remote,'rev-parse','main') == original


def test_branch_or_dirty_verified_workspace_rejected(release):
    root,remote,project,run,_,calls = release
    publisher=client(lambda _:None)
    git(root,'checkout','-qb','wrong')
    with pytest.raises(ValueError,match='分支已变化'): publisher.publish(project,run)
    assert not calls
    git(root,'checkout','factory/release'); (root/'result.py').write_text('changed')
    with pytest.raises(ValueError,match='工作区已变化'): publisher.publish(project,run)
    assert not calls


def test_remote_default_branch_can_differ_from_local_baseline(release):
    root,remote,project,run,base,_ = release
    project['github_base_branch'] = 'production'
    result = client(lambda _:None).publish(project,run)
    assert result['published_branch'] == 'production'
    assert git(remote,'rev-parse','production') == run['artifacts']['commit']
    assert git(root,'rev-parse','main') == base


def test_initial_upload_response_loss_is_reconciled_without_second_push(release, monkeypatch):
    root,remote,project,run,_,calls = release
    current_run = subprocess.run
    lost = False
    def timeout_after_push(args, **kwargs):
        nonlocal lost
        result = current_run(args, **kwargs)
        if args[1] == 'push' and not lost:
            lost = True
            raise subprocess.TimeoutExpired(args,120)
        return result
    monkeypatch.setattr('factory.control.github.subprocess.run',timeout_after_push)
    publisher=client(lambda _:None)
    with pytest.raises(subprocess.TimeoutExpired): publisher.publish(project,run)
    assert publisher.publish(project,run)['publication_type'] == 'initial'
    assert sum(args[1] == 'push' for args in calls) == 1


def test_remote_delivery_branch_divergence_rejected_without_force(release):
    root,remote,project,run,base,_ = release
    git(root,'push',str(remote),f'{base}:refs/heads/main')
    git(root,'checkout','-qb','different',base)
    (root/'different.txt').write_text('remote changes');git(root,'add','.');git(root,'commit','-qm','other')
    divergent=git(root,'rev-parse','HEAD')
    git(root,'push',str(remote),f'{divergent}:refs/heads/factory/release')
    git(root,'checkout','factory/release')
    with pytest.raises(ValueError,match='远端分支已经变化'):
        client(lambda _:None).publish(project,run)
    assert git(remote,'rev-parse','factory/release') == divergent


def test_existing_repository_selection_requires_write_permission():
    publisher=client(lambda _:httpx.Response(200,json=view(permissions={'pull':True,'push':False})))
    with pytest.raises(ValueError,match='写入权限'): publisher.repository('owner/project')


def test_create_preflight_access_failure_never_attempts_post():
    seen=[]
    def api(request):
        seen.append(request.method)
        return httpx.Response(200,json={'login':'owner'}) if request.url.path == '/user' else httpx.Response(403,json={})
    with pytest.raises(httpx.HTTPStatusError): client(api).create_repository(name='project')
    assert seen == ['GET','GET']

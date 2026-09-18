"""真实链路联测探针（可复用，Codex 服务器验收适配说明见 README.md）。

用途：临时数据目录起真实服务，走 创建工作区→写 checks（含单字符串触发格式）→
auto_spec_confirm→真实模型执行→running 期 follow-up→needs_human 后 90s 观察自动接续。
关键环境接线（踩过的坑，全部已内置）：
- FACTORY_PUBLIC_ORIGIN 必须等于请求 Origin（默认 8788，脚本用 18899）。
- 项目创建走 POST /api/v2/projects/create-workspace（直接 POST projects 需已有 git 目录）。
- 项目详情无单项 GET，用列表端点；PUT 是全量 ProjectUpdate（revision>=1）。
- 管理员用 AuthStore 直建（产品无 bootstrap 端点）。
- FACTORY_{PLANNER,CHEAP,STANDARD,STRONG}_{PROVIDER,MODEL} 必须配置。
已知局限：执行段真实模型耗时 20-45 分钟（账号节流时更久）；三次本机尝试
均未在窗口内到达 needs_human，自动接续现场判据见 handoff（needs_human 后无人工
操作出现 run.auto_resumed 且 followups 逐条 applied）。
"""
import json, os, subprocess, sys, tempfile, time
import urllib.request

BASE = 'http://127.0.0.1:18899'
def req(method, path, body=None, headers=None, raw=False):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method,
        headers={'Content-Type': 'application/json', 'Origin': BASE, **(headers or {})})
    try:
        with OPENER.open(r, timeout=30) as resp:
            return resp.status, json.loads(resp.read() or b'{}')
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b'{}')

import http.cookiejar, urllib.error
JAR = http.cookiejar.CookieJar()
OPENER = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(JAR))

def main():
    repo = '/Users/auntlee/workspace/.factory-worktrees/v3-skills-icons'
    data = tempfile.mkdtemp(prefix='r2-live-')
    env = {**os.environ, 'FACTORY_CONTROL_DATA': data, 'FACTORY_PUBLIC_ORIGIN': BASE, 'FACTORY_WORKSPACE_ROOT': data + '/ws',
           'FACTORY_PLANNER_PROVIDER': 'claude', 'FACTORY_PLANNER_MODEL': 'claude-haiku-4-5-20251001',
           'FACTORY_CHEAP_PROVIDER': 'claude', 'FACTORY_CHEAP_MODEL': 'claude-haiku-4-5-20251001',
           'FACTORY_STANDARD_PROVIDER': 'claude', 'FACTORY_STANDARD_MODEL': 'claude-haiku-4-5-20251001',
           'FACTORY_STRONG_PROVIDER': 'claude', 'FACTORY_STRONG_MODEL': 'claude-haiku-4-5-20251001'}
    srv = subprocess.Popen(['uv', 'run', 'factory-web', 'serve', '--port', '18899'],
                           cwd=repo, env=env, stdout=open(data + '/server.log', 'w'),
                           stderr=subprocess.STDOUT, start_new_session=True)
    try:
        for _ in range(60):
            time.sleep(1)
            try:
                s, _b = req('GET', '/api/auth/session'); break
            except Exception: continue
        sys.path.insert(0, repo)
        from factory.control.auth import AuthStore
        from pathlib import Path as _P
        AuthStore(_P(data) / 'users.db').create_user('chief', 'r2-live-checks-01')
        s, b = req('POST', '/api/auth/login', {'username': 'chief', 'password': 'r2-live-checks-01'})
        csrf = b.get('csrf_token'); H = {'X-CSRF-Token': csrf}
        print('login', s)
        os.makedirs(data + '/ws', exist_ok=True)
        s, b = req('POST', '/api/v2/projects/create-workspace', {'name': 'r2-live', 'idempotency_key': 'r2live-ws-1'}, H)
        print('project resp', s, json.dumps(b, ensure_ascii=False)[:300])
        pid = b.get('id') or (b.get('project') or {}).get('id')
        assert pid, 'no project id'
        s, b = req('GET', '/api/v2/projects', None, H)
        proj = next(x for x in b['projects'] if x['id'] == pid)
        print('project keys:', sorted(proj.keys()), 'revision=', proj.get('revision'))
        merged = {**(proj.get('checks') or {}), 'test': ['python3 -m pytest test_counter.py -v']}
        s2, b2 = req('PUT', f'/api/v2/projects/{pid}',
                     {'revision': proj.get('revision') or 1, 'name': proj['name'],
                      'base_branch': proj.get('base_branch', 'main'), 'checks': merged,
                      'auto_spec_confirm': True}, H)
        print('put checks', s2, json.dumps(b2, ensure_ascii=False)[:200] if s2 >= 400 else '')
        s, b = req('POST', '/api/v2/runs', {'project_id': pid,
            'request': '写一个 counter.py 模块：Counter 类支持 increment/decrement/value，并写 test_counter.py 用 pytest 覆盖。做成命令行工具风格的小库即可。',
            'idempotency_key': 'r2live-checks-followup-1'}, H)
        rid = b['id']; print('run', s, rid)
        followed = False; auto_seen = False; t0 = time.time(); last = ''
        while time.time() - t0 < 2700:
            time.sleep(6)
            s, run = req('GET', f'/api/v2/runs/{rid}', None, H)
            st = run.get('status'); 
            if st != last: print(f'[{int(time.time()-t0)}s]', st); last = st
            if st in ('running', 'planning') and not followed and time.time() - t0 > 30:
                s2, fb = req('POST', f'/api/v2/runs/{rid}/follow-up',
                             {'content': '补充要求：Counter 增加 reset() 方法并补对应测试。', 'idempotency_key': 'r2live-fu-1'}, H)
                print('follow-up', s2, json.dumps(fb, ensure_ascii=False)[:160]); followed = s2 == 200
            if st in ('ready_for_review', 'published', 'failed', 'cancelled'):
                break
            if st == 'needs_human':
                # 自动接续观察窗：90s 内轮询状态变化
                moved = False
                for _ in range(18):
                    time.sleep(5)
                    s, run2 = req('GET', f'/api/v2/runs/{rid}', None, H)
                    st2 = run2.get('status')
                    if st2 != 'needs_human':
                        print('AUTO-RESUMED ->', st2); last = st2; moved = True; break
                if not moved:
                    run = run2; print('no auto-resume within 90s'); break
        print('FINAL status', run.get('status'), '| error:', str(run.get('error'))[:200])
        print('followups:', json.dumps(run.get('followups'), ensure_ascii=False))
        s, ev = req('GET', f'/api/v2/runs/{rid}/events?limit=500', None, H)
        kinds = {}
        interesting = []
        for e in (ev if isinstance(ev, list) else ev.get('events') or []):
            k = e.get('type') or e.get('kind'); kinds[k] = kinds.get(k, 0) + 1
            if k in ('run.auto_resumed', 'followup.pending', 'followup.applied', 'followup.expired', 'check.result'):
                interesting.append({k: e.get('payload')})
        print('event kinds:', json.dumps(kinds, ensure_ascii=False))
        print('interesting:', json.dumps(interesting, ensure_ascii=False)[:2000])
        arts = run.get('artifacts') or {}
        print('final checks:', json.dumps(arts.get('final_checks') or arts.get('checks') or '', ensure_ascii=False)[:400])
        print('data dir:', data)
    finally:
        import signal as sg
        try: os.killpg(srv.pid, sg.SIGTERM)
        except Exception: pass

if __name__ == '__main__':
    main()

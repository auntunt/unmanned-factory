import sys,pathlib,json,subprocess,os,shlex
sys.argv=['closure-api.py','noop'];exec(pathlib.Path('/home/ubuntu/releases/acceptance-closure-20260920/closure-api.py').read_text())
rid=json.loads((BASE/'v2.json').read_text())['id'];old=api('GET','/api/v2/runs/'+rid);assert old['status'] in ('needs_human','cancelled');save('v2-final-stopped',old)
a=old['artifacts'];sha=a['commit'];repo='auntunt/webuddy-acceptance-counter-b0407128'
assert len(sha)==40 and all(ch in '0123456789abcdef' for ch in sha)
for line in pathlib.Path('/home/ubuntu/.factory/control.env').read_text().splitlines():
 if line.startswith('FACTORY_GITHUB_TOKEN='):token=' '.join(shlex.split(line.split('=',1)[1]))
helper=BASE/'git-askpass.py';helper.write_text('#!/usr/bin/python3\nimport os,sys\nprint("x-access-token" if "username" in sys.argv[1].lower() else os.environ["WEBUDDY_REVIEW_PUSH_TOKEN"])\n');helper.chmod(0o700)
env={**os.environ,'GIT_ASKPASS':str(helper),'GIT_TERMINAL_PROMPT':'0','WEBUDDY_REVIEW_PUSH_TOKEN':token}
review_ref='refs/heads/acceptance/closure-v2-review-20260920'
result=subprocess.run(['git','-C',a['worktree'],'push','https://github.com/'+repo+'.git',sha+':'+review_ref],env=env,capture_output=True,text=True,timeout=60)
if result.returncode:raise RuntimeError('review branch push failed (raw output withheld)')
save('v2-review-source-upload',{'repository':repo,'commit':sha,'ref':review_ref,'source_run_id':rid,'purpose':'candidate source upload; not passed release or platform publish'})
if old['status'] != 'cancelled': save('v2-cancelled',api('POST',f'/api/v2/runs/{rid}/cancel'))
p0=json.loads((BASE/'project.json').read_text());p=next(x for x in api('GET','/api/v2/projects')['projects'] if x['id']==p0['id'])
assert not subprocess.run(['git','-C',p['workspace'],'status','--porcelain'],capture_output=True,text=True,check=True).stdout.strip()
subprocess.run(['git','-C',p['workspace'],'checkout','--detach',sha],capture_output=True,text=True,check=True)
p=api('PUT','/api/v2/projects/'+p['id'],{'name':p['name'],'revision':p['revision'],'base_branch':a['branch'],'budget_usd':12,'auto_spec_confirm':True,'checks':{'test':['python3','-B','-m','unittest','discover','-v']},'requirement_analysis_budget_usd':1.5});save('project',p)
cfg={'repository':repo,'commit':sha,'run_id':rid};save('v2-pinned',cfg);subprocess.run(['sudo','-n','install','-m','644',str(BASE/'v2-pinned.json'),'/opt/webuddy-demo-counter/target.json'],check=True)
body={'project_id':p['id'],'operation':'release','execute_deploy':True,'interaction_mode':'automatic','idempotency_key':'closure-release-candidate-v2-20260920','request':'审核并更新同一地址上已部署的 Counter 网页至 V2：含翻倍与平方、标记 WB0920-FOLLOWUP。这是已有候选的独立部署验收，不是新增开发。不要改任何文件或重构，不新增文档/功能/测试。运行已有 unittest，启动现有 app.py，通过HTTP核验 /health 为 ok=true，/version 从 WEBUDDY_COMMIT 返回部署版本，POST /api/counter 的 increment/decrement/reset/double/square 正确，其中 square(-3)=9、square(0)=0、square(4)=16；核对现有 Dockerfile 非root、python:3.12-alpine、8080端口。候选源码已上传 GitHub 审核分支，固定部署提交 '+sha+'。本阶段不要求浏览器视觉验收，不要编写模拟代理或重复截图：公网反代页面由Codex在部署后实际操作验收。通过后由平台调用已绑定目标的受控 deploy/health_check/service_status；不要自行SSH或Docker。用户已授权替换同一示例容器并保留前一镜像，同一测试地址不变。只返回真实检查证据与本阶段验收结果，若缺项明确指出。'}
r=api('POST','/api/v2/runs',body);save('release-v2',r);print(json.dumps({'release_run_id':r['id'],'candidate_commit':sha,'review_ref':review_ref}))

import sys,pathlib,json,time,subprocess
sys.argv=['closure-api.py','noop'];exec(pathlib.Path('/home/ubuntu/releases/acceptance-closure-20260920/closure-api.py').read_text())
def watch(label,limit=1000):
 rid=json.loads((BASE/(label+'.json')).read_text())['id'];last=None;states=[];started=time.monotonic()
 while time.monotonic()-started<limit:
  r=api('GET','/api/v2/runs/'+rid);save(label+'-latest',r)
  if r['status']!=last:
   last=r['status'];states.append({'status':last,'at':time.time()});save(label+'-states',states);print(label,last,flush=True)
  if last=='awaiting_approval':
   api('POST',f'/api/v2/runs/{rid}/approve',{'revision':r['revision']});save(label+'-manual-approval',{'revision':r['revision'],'reason':'preauthorized automatic acceptance task'})
  if last in ('needs_human','needs_clarification','failed','cancelled'):
   time.sleep(3);r=api('GET','/api/v2/runs/'+rid)
   if r['status']==last: raise RuntimeError(label+' stopped: '+str(r.get('error'))[:500])
  if last in ('ready_for_review','published'):
   time.sleep(3);return api('GET','/api/v2/runs/'+rid)
  time.sleep(5)
 raise RuntimeError('bounded observation timeout; inspect running job before further work')
try:
 r=watch('v1');p=json.loads((BASE/'project.json').read_text());pid=p['id']
 p=next(x for x in api('GET','/api/v2/projects')['projects'] if x['id']==pid)
 pub=api('POST',f'/api/v3/runs/{r["id"]}/github-publish',{'mode':'bound','expected_project_revision':p['revision']});save('v1-publish',pub)
 assert pub['status']=='published',pub['status']
 a=pub['artifacts'];sha=a['commit'];print('v1 published',sha,flush=True)
 p=next(x for x in api('GET','/api/v2/projects')['projects'] if x['id']==pid)
 p=api('PUT','/api/v2/projects/'+pid,{'name':p['name'],'revision':p['revision'],'base_branch':a['branch'],'budget_usd':12,'auto_spec_confirm':True,'checks':p['checks'],'requirement_analysis_budget_usd':1.5});save('project',p)
 cfg={'repository':p['repository'],'commit':sha,'run_id':r['id']};save('v1-pinned',cfg)
 subprocess.run(['sudo','-n','install','-m','644',str(BASE/'v1-pinned.json'),'/opt/webuddy-demo-counter/target.json'],check=True)
 task={'project_id':pid,'request':'对已经完成并发布到GitHub的Counter网页执行部署准备。部署的应用源提交为 '+sha+' 。不修改任何代码、Dockerfile、README或历史文件，不新增功能。只运行现有unittest和必要HTTP检查（health/version及三个计数动作），确认现有Dockerfile的标准库非root启动契约。实际Docker构建和发布由平台绑定的管理员部署动作执行，不要自行运行Docker或SSH。当前明确授权：独立验收通过后平台执行已绑定目标的deploy、health_check、service_status。无需等待域名资料。交付形态为网页服务。','operation':'release','execute_deploy':True,'interaction_mode':'automatic','idempotency_key':'closure-release-v1-20260920'}
 rr=api('POST','/api/v2/runs',task);save('release-v1',rr);print('release submitted',rr['id'],flush=True)
 rr=watch('release-v1');save('release-v1-final',rr)
 remote=rr.get('artifacts',{}).get('remote_results',[])
 for _ in range(15):
  if len(remote)>=3:break
  time.sleep(3);rr=api('GET','/api/v2/runs/'+rr['id']);remote=rr.get('artifacts',{}).get('remote_results',[])
 save('release-v1-final',rr);print('remote',json.dumps(remote,ensure_ascii=False),flush=True)
 assert len(remote)>=3 and all(x['status']=='pass' for x in remote), 'Remote deployment did not pass'
 save('chain-v1-result',{'passed':True,'commit':sha,'run_id':r['id'],'release_run_id':rr['id']})
 print('V1_DEPLOYED',sha,flush=True)
except Exception as e:
 save('chain-v1-result',{'passed':False,'error':str(e)[:1200]});print('STOP',str(e)[:1200],flush=True);raise

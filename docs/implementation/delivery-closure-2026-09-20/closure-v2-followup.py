import sys,pathlib,json,time,sqlite3
sys.argv=['closure-api.py','noop'];exec(pathlib.Path('/home/ubuntu/releases/acceptance-closure-20260920/closure-api.py').read_text())
if (BASE/'v2.json').exists():
 r=json.loads((BASE/'v2.json').read_text());rid=r['id']
else:
 p0=json.loads((BASE/'project.json').read_text());pid=p0['id']
 p=next(x for x in api('GET','/api/v2/projects')['projects'] if x['id']==pid)
 checks={**p['checks'],'safe_node_acceptance':['/usr/bin/python3',str(BASE/'closure-safe-node-gate.py')]}
 p=api('PUT','/api/v2/projects/'+pid,{'name':p['name'],'revision':p['revision'],'base_branch':p['base_branch'],'budget_usd':12,'auto_spec_confirm':True,'checks':checks,'requirement_analysis_budget_usd':1.5});save('project',p)
 body={'project_id':pid,'request':'延续此项目已发布的 Counter 网页做 V2：保留原加一、减一、归零与相对路径 API；增加“翻倍”按钮和 action=double，返回 value*2；页面标记改为 webuddy 交付验收 V2。用少量 unittest 验证 double 的正数、负数、零，保留原测试覆盖；标题测试的 V1 预期应随需求改为 V2，其余既有行为断言不变。不要SSH或Docker或修改规格文件。不要编写模拟代理或反复截图，真实公网页面由Codex在部署后验收；本阶段用已有unittest和HTTP验证功能。本轮运维设置了名为 safe_node_acceptance 的外部检查作为可控恢复验收：第一次会故意失败，这是测试平台恢复行为，不是应用缺陷；不要尝试修改/读取/规避该检查，只运行仓库内部正常unittest，完成代码后交由平台执行外部检查。若外部检查触发中断，停止，让平台在安全节点接续用户后续补充。不要加入除此之外的功能，后续用户明确补充的需求除外。','operation':'general','interaction_mode':'automatic','idempotency_key':'closure-v2-followup-20260920'}
 r=api('POST','/api/v2/runs',body);save('v2',r);rid=r['id'];print('V2_SUBMITTED',rid,flush=True)
started=time.monotonic();sent=(BASE/'followup-receipt.json').exists();last=None;states=[];auto=[]
while time.monotonic()-started<1200:
 r=api('GET','/api/v2/runs/'+rid);save('v2-latest',r)
 if r['status']!=last:
  last=r['status'];states.append({'at':time.time(),'status':last});save('v2-states',states);print('V2',last,flush=True)
 if last=='awaiting_approval':
  try:
   api('POST',f'/api/v2/runs/{rid}/approve',{'revision':r['revision']});save('v2-manual-initial-approval',{'revision':r['revision']})
  except RuntimeError:
   if api('GET','/api/v2/runs/'+rid)['status'] not in ('running','queued','verifying'):raise
 if last=='running' and not sent:
  content='运行中补充：除了翻倍，再增加“平方”按钮及 action=square，返回 value*value；覆盖 -3 得 9、0 得 0、4 得 16，并在页面显示唯一标记 WB0920-FOLLOWUP。保留已有功能，最终仍交付同一个网页。此补充在本次安全节点继续落实，不需要我再按继续按钮。'
  receipt=api('POST',f'/api/v2/runs/{rid}/follow-up',{'content':content,'idempotency_key':'closure-v2-add-square-20260920'});save('followup-receipt',receipt);assert receipt.get('queued') and not receipt.get('applied'),receipt
  sent=True;print('FOLLOWUP_QUEUED',flush=True)
 with sqlite3.connect('file:'+str(BASE/'data/control.db')+'?mode=ro',uri=True) as db:
  rows=db.execute('select id,type,payload from events where run_id=? and type in (?,?,?,?) order by id',(rid,'run.auto_resumed','followup.pending','followup.applied','followup.expired')).fetchall()
 ev=[{'id':i,'type':t,'payload':json.loads(p)} for i,t,p in rows];save('followup-events',ev)
 auto=[x for x in ev if x['type']=='run.auto_resumed']
 if last in ('ready_for_review','published'):
  assert sent and auto and r['followups'][0]['applied'] and not r['followups'][0]['expired'], 'followup not consumed'
  save('followup-result',{'passed_control_flow':True,'run_id':rid,'manual_continue_calls_after_injection':1,'manual_resume_reason':'verification budget only; after automatic consumption','priming_manual_continue_calls':1,'auto_resume_event_ids':[x['id'] for x in auto],'final_status':last});print('FOLLOWUP_CONTROL_PASSED',flush=True);break
 if last in ('needs_human','failed','cancelled','needs_clarification'):
  time.sleep(8);rr=api('GET','/api/v2/runs/'+rid)
  if rr['status']==last:
   save('followup-result',{'passed_control_flow':False,'error':rr.get('error'),'run_id':rid,'status':last,'manual_continue_calls_after_injection':1,'manual_resume_reason':'verification budget only; after automatic consumption','priming_manual_continue_calls':1});raise RuntimeError(str(rr.get('error'))[:600])
 time.sleep(3)
else:raise RuntimeError('bounded v2 observation timeout')

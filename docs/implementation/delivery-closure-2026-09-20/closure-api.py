import json,pathlib,sys,time,sqlite3
import httpx
BASE=pathlib.Path('/home/ubuntu/releases/acceptance-closure-20260920')
c=httpx.Client(base_url='http://127.0.0.1:18926',headers={'Origin':'http://127.0.0.1:18926'},timeout=60)
def api(method,path,body=None):
 r=c.request(method,path,json=body)
 if not r.is_success: raise RuntimeError(str(r.status_code)+' '+r.text[:900])
 return r.json()
r=api('POST','/api/auth/login',json.loads((BASE/'operator.json').read_text()));c.headers['X-CSRF-Token']=r['csrf_token']
def save(name,value): (BASE/(name+'.json')).write_text(json.dumps(value,ensure_ascii=False,indent=2))
cmd=sys.argv[1]
if cmd=='init':
 p=api('POST','/api/v2/projects',{'name':'三项真实交付验收','workspace':str(BASE/'ws/counter'),'repository':'auntunt/webuddy-acceptance-counter-b0407128','base_branch':'acceptance/closure-seed-20260920','budget_usd':12,'checks':{'test':['python3','-B','-m','unittest','discover','-v']}})
 p=api('PUT','/api/v2/projects/'+p['id'],{'name':p['name'],'revision':p['revision'],'base_branch':p['base_branch'],'budget_usd':12,'auto_spec_confirm':True,'checks':p['checks'],'requirement_analysis_budget_usd':1.5});save('project',p)
 print(json.dumps({'id':p['id'],'budget':p['budget_usd']}))
elif cmd=='submit':
 p=json.loads((BASE/'project.json').read_text());body=json.loads(pathlib.Path(sys.argv[2]).read_text());body['project_id']=p['id'];r=api('POST','/api/v2/runs',body);save(sys.argv[3],r);print(json.dumps({'id':r['id'],'status':r['status']}))
elif cmd=='poll':
 label=sys.argv[2];rid=json.loads((BASE/(label+'.json')).read_text())['id'];r=api('GET','/api/v2/runs/'+rid);save(label+'-latest',r)
 art=r.get('artifacts',{});print(json.dumps({'id':rid,'status':r['status'],'revision':r['revision'],'error':r.get('error'),'tasks':[(t.get('id'),t.get('status')) for t in r.get('tasks',[])],'commit':art.get('commit'),'verification':art.get('verification'),'followups':r.get('followups'),'cost':art.get('total_known_cost_usd'),'remote':art.get('remote_results')},ensure_ascii=False))
elif cmd=='request':
 body=json.loads(pathlib.Path(sys.argv[4]).read_text()) if len(sys.argv)>4 else None;r=api(sys.argv[2],sys.argv[3],body);print(json.dumps(r,ensure_ascii=False))

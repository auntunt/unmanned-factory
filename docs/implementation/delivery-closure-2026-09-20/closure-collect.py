import sys,pathlib,json,sqlite3,subprocess,time
sys.argv=['closure-api.py','noop'];exec(pathlib.Path('/home/ubuntu/releases/acceptance-closure-20260920/closure-api.py').read_text())
p=json.loads((BASE/'project.json').read_text());runs=api('GET','/api/v2/runs?project_id='+p['id'])['runs'];summary=[]
for r in runs:
 rr=api('GET','/api/v2/runs/'+r['id']);a=rr.get('artifacts',{})
 summary.append({'id':r['id'],'status':rr['status'],'created_at':rr.get('created_at'),'updated_at':rr.get('updated_at'),'operation':rr.get('source',{}).get('operation'),'error':rr.get('error'),'commit':a.get('commit'),'base_sha':a.get('base_sha'),'known_cost_usd':a.get('total_known_cost_usd'),'verification':a.get('verification'),'remote_results':a.get('remote_results'),'followups':rr.get('followups')})
proof={'platform_code':'f0599bc','isolated_database':True,'project_id':p['id'],'runs':summary};save('platform-final-proof',proof)
rid=json.loads((BASE/'v2.json').read_text())['id']
with sqlite3.connect('file:'+str(BASE/'data/control.db')+'?mode=ro',uri=True) as db:
 rows=[{'id':i,'type':t,'payload':json.loads(raw)} for i,t,raw in db.execute('select id,type,payload from events where run_id=? order by id',(rid,))]
 pending=[x for x in rows if x['type']=='followup.pending'];assert len(pending)==1
 eid=pending[0]['id'];pid=pending[0]['payload']['id']
 auto=[x for x in rows if x['type']=='run.auto_resumed' and pid in x['payload'].get('pending_ids',[])];assert len(auto)==1 and auto[0]['payload']['actor']=='system/auto'
 applied=[x for x in rows if x['type']=='followup.applied' and x['payload'].get('pending_id')==pid];assert len(applied)==1
 human=[x for x in rows if x['id']>eid and x['type'] in ('human.continued','human.clarified','run.approved')];assert len(human)==1 and human[0]['type']=='human.continued',human
 assert human[0]['id']>applied[0]['id'],human
 expired=[x for x in rows if x['type']=='followup.expired'];assert not expired
 rr=api('GET','/api/v2/runs/'+rid);assert rr['status']=='cancelled' and rr['artifacts']['verification']['error_type']=='incomplete_coverage'
 save('followup-final-proof',{'passed':False,'automatic_consumption_passed':True,'end_to_end_unattended_passed':False,'scenario':'real model with controlled recoverable check fault','run_id':rid,'pending':pending[0],'auto_resumed':auto[0],'applied':applied[0],'manual_actions_after_pending':human,'priming_manual_continue_calls':1,'final_status':rr['status'],'verification':rr['artifacts']['verification'],'manual_release_required':True,'commit':rr['artifacts']['commit']})
v1=json.loads((BASE/'v1-pinned.json').read_text())['commit'];v2=json.loads((BASE/'v2-pinned.json').read_text())['commit']
deploy=json.loads(pathlib.Path('/opt/webuddy-demo-counter/current.json').read_text());assert deploy['commit']==v2 and deploy['previous_image']=='webuddy-demo-counter:'+v1
save('deployment-v2-proof',deploy)
print(json.dumps({'followup_consumption_passed':True,'followup_end_to_end_passed':False,'same_address_update_passed':True,'v1':v1,'v2':v2,'run_count':len(summary)},ensure_ascii=False))

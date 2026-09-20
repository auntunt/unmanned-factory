import sys,pathlib,json,time,subprocess
label=sys.argv[1]
src=pathlib.Path('/home/ubuntu/releases/acceptance-closure-20260920/closure-v1-chain.py').read_text();exec(src[:src.index('\ntry:\n')])
try:
 r=watch(label,1200);rid=r['id']
 for _ in range(20):
  r=api('GET','/api/v2/runs/'+rid);remote=r.get('artifacts',{}).get('remote_results',[])
  if len(remote)>=3:break
  time.sleep(3)
 save(label+'-final',r);assert len(remote)>=3 and all(x['status']=='pass' for x in remote),json.dumps(remote,ensure_ascii=False)
 p=next(x for x in api('GET','/api/v2/projects')['projects'] if x['id']==r['project_id'])
 pub=api('POST',f'/api/v3/runs/{rid}/github-publish',{'mode':'bound','expected_project_revision':p['revision']});save(label+'-publish',pub);assert pub['status']=='published'
 sha=json.loads((BASE/('v1-pinned.json' if label=='release-v1' else 'v2-pinned.json')).read_text())['commit']
 subprocess.run(['git','-C',r['artifacts']['worktree'],'diff','--exit-code',sha,r['artifacts']['commit'],'--','app.py','counter.py','Dockerfile'],check=True,capture_output=True)
 save(label+'-result',{'passed':True,'release_run_id':rid,'deployed_source_commit':sha,'release_commit':pub['artifacts']['commit'],'remote_results':remote});print('DEPLOYMENT_PASSED',sha,flush=True)
except Exception as e:
 save(label+'-result',{'passed':False,'error':str(e)[:1800]});print('STOP',str(e)[:1800],flush=True);raise

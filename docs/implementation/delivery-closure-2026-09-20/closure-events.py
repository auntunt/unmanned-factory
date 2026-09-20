import sqlite3,json,pathlib
p=pathlib.Path('/home/ubuntu/releases/acceptance-closure-20260920')
c=sqlite3.connect('file:'+str(p/'data/control.db')+'?mode=ro',uri=True)
for k,payload in c.execute('select type,payload from events where type in (?,?,?,?) order by id desc limit 5',('assistant.message','task.failed','run.needs_human','verification.completed')):
 d=json.loads(payload);print(k,str(d.get('text',d))[:1200])
for r, in c.execute('select data from runs'):
 r=json.loads(r);print('RUN',r['id'],r['status'],r.get('error'),r.get('artifacts',{}).get('total_known_cost_usd'))

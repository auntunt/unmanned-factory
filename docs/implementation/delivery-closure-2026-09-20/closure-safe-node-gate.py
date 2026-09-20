"""Acceptance-only controlled check fault; never installed in production or app source."""
import json,pathlib,sqlite3,time,sys
BASE=pathlib.Path('/home/ubuntu/releases/acceptance-closure-20260920')
rid=json.loads((BASE/'v2.json').read_text())['id']
with sqlite3.connect('file:'+str(BASE/'data/control.db')+'?mode=ro',uri=True) as c:
 rows=c.execute('select id,payload from events where run_id=? and type=? order by id',(rid,'run.auto_resumed')).fetchall()
released=any(json.loads(p).get('actor')=='system/auto' for _,p in rows)
with (BASE/'safe-node-gate.jsonl').open('a') as f:f.write(json.dumps({'at':time.time(),'run_id':rid,'released_by_auto_resume':released,'event_ids':[r[0] for r in rows]})+'\n')
print('Controlled acceptance checkpoint: '+('recovery event observed; pass' if released else 'intentional failure; stop and let platform consume queued follow-up; do not repair application for this check'))
sys.exit(0 if released else 17)

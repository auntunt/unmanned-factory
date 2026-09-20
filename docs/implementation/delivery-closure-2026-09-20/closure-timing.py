import sqlite3,json,time
c=sqlite3.connect('file:/home/ubuntu/releases/acceptance-closure-20260920/data/control.db?mode=ro',uri=True)
for raw, in c.execute('select data from runs'):
 r=json.loads(raw);print({k:r.get(k) for k in ('id','status','created_at','updated_at','error')});a=r.get('artifacts') or {};print({k:a.get(k) for k in ('commit','total_known_cost_usd')})
for k,raw in c.execute('select type,payload from events order by id desc limit 10'):
 p=json.loads(raw);print(k,{f:p.get(f) for f in ('phase','duration_s','status','error','error_type','cost_usd','stop_reason','subtype') if f in p})

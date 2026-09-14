"""Evidence-backed proposals, bounded auto-approval and weekly skill-use accounting."""
from __future__ import annotations
import hashlib
import json
import time
import uuid
from datetime import datetime, timezone, timedelta

from factory.control.agent_manifests import encoded
from factory.control.capabilities import CapabilityStore
from factory.control.store import Conflict, now, scrub

KINDS={'add_skill','remove_skill','upgrade_skill','add_assertion'}
FINAL={'ready_for_review','published','needs_human','inspection_completed','inspection_failed'}

class EvolutionStore:
    def __init__(self, store, manifests):
        self.store,self.manifests=store,manifests
        self.capabilities=CapabilityStore(store)
        with store.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS evolution_proposals(id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, project_id TEXT NOT NULL, dedupe_key TEXT UNIQUE NOT NULL, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS evolution_policy(project_id TEXT PRIMARY KEY, revision INTEGER NOT NULL, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS evolution_audit(id INTEGER PRIMARY KEY, proposal_id TEXT NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL, data TEXT NOT NULL, at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS evolution_weekly(week TEXT NOT NULL, agent_id TEXT NOT NULL, project_id TEXT NOT NULL, data TEXT NOT NULL, PRIMARY KEY(week,agent_id,project_id));
                CREATE TABLE IF NOT EXISTS evolution_ticks(week TEXT PRIMARY KEY, at TEXT NOT NULL);
                CREATE TRIGGER IF NOT EXISTS no_evolution_audit_update BEFORE UPDATE ON evolution_audit BEGIN SELECT RAISE(ABORT,'audit is immutable'); END;
                CREATE TRIGGER IF NOT EXISTS no_evolution_audit_delete BEFORE DELETE ON evolution_audit BEGIN SELECT RAISE(ABORT,'audit is immutable'); END;
            ''')

    def policy(self,pid,db=None):
        if db is None:
            self.store.project(pid)
            with self.store.connect() as conn:return self.policy(pid,conn)
        row=db.execute('SELECT data FROM evolution_policy WHERE project_id=?',(pid,)).fetchone()
        return json.loads(row[0]) if row else {'revision':0,'auto_evolve':False,'notifications':True}

    def configure(self,pid,revision,auto_evolve,notifications,actor):
        self.store.project(pid)
        if type(revision) is not int or type(auto_evolve) is not bool or type(notifications) is not bool:raise ValueError('进化策略参数无效')
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE');old=self.policy(pid,db)
            if old['revision']!=revision:raise Conflict('项目进化策略已更新')
            value={'revision':revision+1,'auto_evolve':auto_evolve,'notifications':notifications}
            db.execute('INSERT OR REPLACE INTO evolution_policy VALUES(?,?,?)',(pid,revision+1,encoded(value)))
            db.execute('INSERT INTO project_settings_audit(project_id,revision,actor,action,data,at) VALUES(?,?,?,?,?,?)',(pid,revision+1,str(actor),'evolution.policy',encoded(value),now()))
        return value

    def list(self,aid):
        self.manifests.get(aid)
        with self.store.connect() as db:
            return [json.loads(r[0]) for r in db.execute('SELECT data FROM evolution_proposals WHERE agent_id=? ORDER BY rowid DESC',(aid,))]

    def _evidence(self,db,pid,evidence):
        if (not isinstance(evidence,dict) or set(evidence)!={'run_ids','explanation'} or not isinstance(evidence['explanation'],str)
                or not evidence['explanation'].strip() or len(evidence['explanation'])>4000 or not isinstance(evidence['run_ids'],list)
                or not 1<=len(evidence['run_ids'])<=20 or any(not isinstance(x,str) for x in evidence['run_ids'])):
            raise ValueError('进化提案必须包含来源运行列表及证据说明')
        runs=[]
        for rid in dict.fromkeys(evidence['run_ids']):
            row=db.execute('SELECT data FROM runs WHERE id=?',(rid,)).fetchone()
            if not row:raise ValueError('证据运行不存在')
            run=json.loads(row[0]);ledger=run.get('artifacts',{}).get('acceptance_ledger',{})
            if run['project_id']!=pid:raise ValueError('证据必须来自所选项目')
            if run.get('status') not in FINAL or not any(isinstance(i,dict) and i.get('evidence') for i in ledger.get('items',[])):
                raise ValueError('来源运行没有可引用的独立验收证据')
            runs.append(run)
        return runs

    def _patch(self,current,kind,payload):
        if kind not in KINDS or not isinstance(payload,dict):raise ValueError('提案类型或变更无效')
        result={k:json.loads(encoded(current[k])) for k in ('identity','skills','assertions')}
        if kind=='add_assertion':
            if set(payload)!={'text'} or not isinstance(payload['text'],str) or not payload['text'].strip():raise ValueError('新增断言不能为空')
            if payload['text'] in result['assertions']:raise Conflict('断言已存在')
            result['assertions'].append(payload['text']);return result
        if set(payload)!={'id','version'} or not isinstance(payload['id'],str) or type(payload['version']) is not int or payload['version']<1:raise ValueError('skill 变更必须引用 id 和版本')
        ref=next((r for r in result['skills'] if r['id']==payload['id']),None)
        if kind=='add_skill':
            if ref:raise Conflict('清单已包含该 skill')
            result['skills'].append(payload)
        elif kind=='remove_skill':
            if ref!=payload:raise Conflict('待移除的 skill 版本已变化')
            result['skills'].remove(ref)
        else:
            if not ref or payload['version']<=ref['version']:raise Conflict('升级需引用更高版本')
            result['skills'][result['skills'].index(ref)]=payload
        return result

    def _audit(self,db,proposal,actor,action):
        db.execute('INSERT INTO evolution_audit(proposal_id,actor,action,data,at) VALUES(?,?,?,?,?)',(proposal['id'],str(actor),action,encoded(proposal),now()))

    def _create(self,db,aid,pid,kind,payload,evidence,actor,source,key):
        existing=db.execute('SELECT data FROM evolution_proposals WHERE dedupe_key=?',(key,)).fetchone()
        if existing:return json.loads(existing[0])
        self._evidence(db,pid,evidence)
        current=self.manifests._current(db,aid)
        self.manifests._validate(self._patch(current,kind,payload),db)
        p={'id':uuid.uuid4().hex,'agent_id':aid,'project_id':pid,'kind':kind,'change':payload,'evidence':scrub(evidence),
           'source':source,'status':'pending','revision':1,'manifest_revision':current['revision'],'created_at':now()}
        db.execute('INSERT INTO evolution_proposals VALUES(?,?,?,?,?)',(p['id'],aid,pid,key,encoded(p)))
        self._audit(db,p,actor,'proposed')
        return p

    def create(self,aid,pid,kind,payload,evidence,actor):
        self.store.project(pid)
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            # Public callers cannot impersonate an automatic source.
            return self._create(db,aid,pid,kind,payload,evidence,actor,'manual',uuid.uuid4().hex)

    def _auto_allowed(self,db,p):
        policy=self.policy(p['project_id'],db)
        if not policy['auto_evolve'] or (p['source'],p['kind']) not in {('capability','add_skill'),('usage','remove_skill')}:return False
        # A project's policy cannot evolve an unrelated job selected by a caller.
        row=db.execute('SELECT agent_id FROM project_assistants WHERE project_id=?',(p['project_id'],)).fetchone()
        return bool(row and row[0]==p['agent_id'])

    def _decide(self,db,p,approve,revision,actor,automatic=False):
        if p['revision']!=revision or p['status']!='pending':raise Conflict('提案已处理，请刷新')
        if automatic and (not approve or not self._auto_allowed(db,p)):raise ValueError('此变更必须人工审批')
        if approve:
            self._evidence(db,p['project_id'],p['evidence'])
            current=self.manifests._current(db,p['agent_id'])
            if current['revision']!=p['manifest_revision']:raise Conflict('提案基于旧清单，请重新提案或驳回')
            updated=self.manifests.save(p['agent_id'],self._patch(current,p['kind'],p['change']),current['revision'],actor,
                    human=not automatic,_db=db,action='evolution.approved:'+p['id'])
            p['result_revision']=updated['revision']
        p.update(status='approved' if approve else 'rejected',revision=revision+1,decided_at=now(),automatic=automatic)
        db.execute('UPDATE evolution_proposals SET data=? WHERE id=?',(encoded(p),p['id']))
        self._audit(db,p,actor,p['status'])
        if approve and self.policy(p['project_id'],db)['notifications']:
            notice={'id':'evolution:'+p['id'],'project_id':p['project_id'],'agent_id':p['agent_id'],'status':'evolution_approved','source':{'type':'evolution'}}
            reason=f"职能体进化已批准：{p['kind']} · 清单 revision {p['result_revision']} · {p['id'][:8]}"
            db.execute('INSERT INTO operations_outbox(run_id,data,reason) VALUES(?,?,?)',(notice['id'],encoded(notice),reason))
        return p

    def decide(self,proposal_id,approve,revision,actor):
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT data FROM evolution_proposals WHERE id=?',(proposal_id,)).fetchone()
            if not row:raise KeyError(proposal_id)
            return self._decide(db,json.loads(row[0]),approve,revision,actor)

    def promote(self,cid,cap_revision,aid,actor):
        c=self.capabilities.get(cid,cap_revision)
        rid=c.get('source_run_id')
        if not rid:raise ValueError('只有带来源运行证据的沉淀能力可升格')
        run=self.store.get(rid);pid=run['project_id']
        evidence={'run_ids':[rid],'explanation':f"从沉淀能力 {c['name']} v{cap_revision} 升格；来源运行 {rid} 的独立验收证据。"}
        key='capability:'+encoded([cid,cap_revision,aid])
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old=db.execute('SELECT data FROM evolution_proposals WHERE dedupe_key=?',(key,)).fetchone()
            if old:return json.loads(old[0])
            self._evidence(db,pid,evidence)
            mid=uuid.uuid4().hex
            skill={'id':mid,'version':1,'name':c['name'],'category':'workflow','description':c['description'][:1000],
                   'instructions':c['instructions'],'status':'draft','source':{'type':'capability','id':cid,'revision':cap_revision,'run_id':rid},'actor':str(actor),'updated_at':now()}
            db.execute('INSERT INTO instruction_modules VALUES(?,?,?)',(mid,1,encoded(skill)))
            p=self._create(db,aid,pid,'add_skill',{'id':mid,'version':1},evidence,actor,'capability',key)
            if self._auto_allowed(db,p):p=self._decide(db,p,True,p['revision'],'auto_evolve',automatic=True)
            return p

    def weekly(self,at=None):
        stamp=time.time() if at is None else at
        date=datetime.fromtimestamp(stamp,timezone.utc);iso=date.isocalendar();week=f'{iso.year}-W{iso.week:02}'
        previous=(date-timedelta(days=7)).isocalendar()
        report_week=f'{previous.year}-W{previous.week:02}'
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT 1 FROM evolution_ticks WHERE week=?',(week,)).fetchone():return []
            rows=[json.loads(r[0]) for r in db.execute('SELECT data FROM runs')]
            groups={}
            for run in rows:
                snapshot=run.get('agent_snapshot') or {};m=snapshot.get('manifest')
                if run.get('status') not in FINAL or not m:continue
                groups.setdefault((m['agent_id'],run['project_id']),[]).append(run)
            proposals=[]
            for (aid,pid),runs in groups.items():
                current=self.manifests._current(db,aid)
                runs.sort(key=lambda r:(r.get('created_at',''),r['id']))
                summary=[]
                for ref in current['skills']:
                    mounted=[r for r in runs if any(s['id']==ref['id'] for s in r['agent_snapshot']['manifest']['skills'])]
                    def cited(r):
                        return any(ref in item.get('skill_refs',[]) and item.get('evidence') for item in r.get('artifacts',{}).get('acceptance_ledger',{}).get('items',[]))
                    this_week=[r for r in mounted if (lambda d:d.isocalendar()[:2]==(previous.year,previous.week))(datetime.fromisoformat(r['created_at'].replace('Z','+00:00')))]
                    summary.append({**ref,'mounts':len(this_week),'evidence_runs':sum(bool(cited(r)) for r in this_week)})
                    last=mounted[-5:]
                    if len(last)!=5 or any(ref not in r['agent_snapshot']['manifest']['skills'] for r in last):continue
                    if any(cited(r) or not any(i.get('evidence') for i in r.get('artifacts',{}).get('acceptance_ledger',{}).get('items',[])) for r in last):continue
                    evidence={'run_ids':[r['id'] for r in last],'explanation':'连续 5 次运行挂载该 skill，但独立验收记录没有引用该版本；建议评审其适用性，不证明能力无用。'}
                    key='usage:'+hashlib.sha256(encoded([aid,pid,ref,evidence['run_ids']]).encode()).hexdigest()
                    p=self._create(db,aid,pid,'remove_skill',ref,evidence,'weekly','usage',key)
                    if p['status']=='pending' and p['manifest_revision']==self.manifests._current(db,aid)['revision'] and self._auto_allowed(db,p):
                        # Rebase only this freshly calculated proposal after another
                        # independent removal in the same transaction.
                        p=self._decide(db,p,True,p['revision'],'auto_evolve',automatic=True)
                    proposals.append(p)
                db.execute('INSERT INTO evolution_weekly VALUES(?,?,?,?)',(report_week,aid,pid,encoded(summary)))
            db.execute('INSERT INTO evolution_ticks VALUES(?,?)',(week,now()))
            return proposals

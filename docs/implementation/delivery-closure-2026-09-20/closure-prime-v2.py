import sys,pathlib,json,time
sys.argv=['closure-api.py','noop'];exec(pathlib.Path('/home/ubuntu/releases/acceptance-closure-20260920/closure-api.py').read_text())
rid=json.loads((BASE/'v2.json').read_text())['id'];r=api('GET','/api/v2/runs/'+rid);assert r['status']=='needs_human';save('v2-before-observer-repair',r)
f=BASE/'followup-result.json'
if f.exists():f.rename(BASE/'followup-observer-failed-attempt.json')
r=api('POST',f'/api/v2/runs/{rid}/continue',{'answer':'','revision':r['revision'],'resume_count':r.get('resume_count',0)});save('v2-priming-continue',{'run_id':rid,'reason':'observer approval race prevented injection in first attempt; resume same saved work then inject while ACTIVE','status':r['status'],'manual_continue_calls_before_test_segment':1})
for _ in range(40):
 r=api('GET','/api/v2/runs/'+rid)
 if r['status']=='running':break
 if r['status']=='needs_human':raise RuntimeError('resume failed before ACTIVE observation')
 time.sleep(.5)
assert r['status']=='running',r['status']
content='运行中补充：除了翻倍，再增加“平方”按钮及 action=square，返回 value*value；覆盖 -3 得 9、0 得 0、4 得 16，并在页面显示唯一标记 WB0920-FOLLOWUP。保留已有功能，最终仍交付同一个网页。此补充在本次安全节点继续落实，不需要我再按继续按钮。'
receipt=api('POST',f'/api/v2/runs/{rid}/follow-up',{'content':content,'idempotency_key':'closure-v2-add-square-20260920'})
save('followup-receipt',receipt);assert receipt.get('queued') and not receipt.get('applied'),receipt
save('followup-injection-state',{'run_id':rid,'observed_status_before_submit':r['status'],'at':time.time(),'manual_continue_calls_after_injection':0})
print(json.dumps({'observed_status':r['status'],'receipt':receipt},ensure_ascii=False))

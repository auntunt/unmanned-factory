"""Run from the operator's machine against the public application URL."""
import urllib.request,urllib.error,json,sys,pathlib,datetime
base='https://harness.cloudwaveai.cn/delivery-demo/counter/'
version=sys.argv[1];expected=sys.argv[2]
checks=[]
def get(path):
 with urllib.request.urlopen(base+path,timeout=20) as r:return r.status,r.read()
def post(value,action,expected_value=None,status=200):
 req=urllib.request.Request(base+'api/counter',data=json.dumps({'value':value,'action':action}).encode(),headers={'Content-Type':'application/json'})
 try:
  with urllib.request.urlopen(req,timeout=20) as r:code=r.status;body=r.read()
 except urllib.error.HTTPError as e:code=e.code;body=e.read()
 assert code==status,(action,code,body)
 data=json.loads(body)
 if status==200:assert data['value']==expected_value,(action,data,expected_value)
 checks.append({'action':action,'input':value,'status':code,'response':data})
http,html=get('');assert http==200
assert ('webuddy 交付验收 '+version) in html.decode()
_,b=get('health');assert json.loads(b)['ok'] is True
_,b=get('version');actual=json.loads(b);assert actual['commit']==expected,(actual,expected)
post(0,'increment',1);post(1,'decrement',0);post(9,'reset',0);post(1,'invalid',status=400)
if version=='V2':
 assert 'WB0920-FOLLOWUP' in html.decode()
 for v in (0,4,-3):post(v,'double',v*2);post(v,'square',v*v)
report={'passed':True,'at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'url':base,'version':version,'commit':actual['commit'],'checks':checks}
pathlib.Path(__file__).with_name('public-'+version+'.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
print(json.dumps(report,ensure_ascii=False))

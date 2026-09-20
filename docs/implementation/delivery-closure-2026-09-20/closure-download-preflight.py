import pathlib,shlex,urllib.request,urllib.parse,urllib.error,json
# Do not log credential or redirect query strings.
token=None
for line in pathlib.Path('/home/ubuntu/.factory/control.env').read_text().splitlines():
 if line.startswith('FACTORY_GITHUB_TOKEN='):token=' '.join(shlex.split(line.split('=',1)[1]))
headers={'Authorization':'Bearer '+token,'Accept':'application/vnd.github+json','User-Agent':'webuddy-acceptance'}
root='https://api.github.com/repos/auntunt/webuddy-acceptance-counter-b0407128'
with urllib.request.urlopen(urllib.request.Request(root,headers=headers),timeout=20) as r:meta=json.load(r)
print('repository_private',meta['private'])
class Redirect(urllib.request.HTTPRedirectHandler):
 def redirect_request(self,req,fp,code,msg,headers,newurl):
  assert urllib.parse.urlparse(newurl).hostname in ('api.github.com','codeload.github.com')
  print('redirect_host',urllib.parse.urlparse(newurl).hostname)
  result=super().redirect_request(req,fp,code,msg,headers,newurl);result.remove_header('Authorization');return result
try:
 with urllib.request.build_opener(Redirect()).open(urllib.request.Request(root+'/tarball/'+meta['default_branch'],headers=headers),timeout=20) as r:
  data=r.read(4*1024*1024+1);print(json.dumps({'download_status':r.status,'bytes':len(data),'bounded':len(data)<=4*1024*1024}))
except urllib.error.HTTPError as e:print('download_http_error',e.code)

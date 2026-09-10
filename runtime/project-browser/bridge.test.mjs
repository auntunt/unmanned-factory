import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import http from 'node:http';
import {previewURL,ownsListener,createPreviewProxy,screenshotPath,compactResponse} from './bridge.mjs';

test('only explicit loopback HTTP previews',()=>{
 assert.equal(previewURL('http://localhost:5173/path').href,'http://127.0.0.1:5173/path');
 for(const url of ['https://127.0.0.1:5173','http://example.com:5173','http://127.0.0.1','http://u:p@127.0.0.1:5173','file:///etc/passwd','http://169.254.169.254:8080','http://127.0.0.1:80']) assert.throws(()=>previewURL(url));
});
test('listener requires an owning FD visible in project PID namespace',async()=>{
 const root=await fs.mkdtemp(path.join(os.tmpdir(),'webuddy-proc-'));
 try {
  await fs.mkdir(path.join(root,'net'));await fs.mkdir(path.join(root,'42/fd'),{recursive:true});
  await fs.writeFile(path.join(root,'net/tcp'),'header\n0: 0100007F:1435 00000000:0000 0A 00000000:00000000 00:00000000 00000000 1000 0 123456 1\n');
  assert.equal(await ownsListener(5173,root),false);
  await fs.symlink('socket:[123456]',path.join(root,'42/fd/7'));
  assert.equal(await ownsListener(5173,root),true);assert.equal(await ownsListener(8788,root),false);
 }finally{await fs.rm(root,{recursive:true,force:true});}
});
test('proxy only forwards authorized preview and blocks remote/host endpoints',async()=>{
 const app=http.createServer((req,res)=>{if(req.url==='/redirect'){res.writeHead(302,{Location:'http://example.com/'});res.end();}else res.end('project page');});
 await new Promise(resolve=>app.listen(0,'127.0.0.1',resolve));
 const origin=`http://127.0.0.1:${app.address().port}`;
 const proxy=await createPreviewProxy(async target=>target.origin===origin);
 const get=url=>new Promise((resolve,reject)=>{http.get({host:'127.0.0.1',port:proxy.port,path:url},res=>{let body='';res.on('data',c=>body+=c);res.on('end',()=>resolve({status:res.statusCode,body,location:res.headers.location}));}).on('error',reject);});
 try{
  assert.equal((await get(origin+'/')).body,'project page');
  assert.equal((await get('http://127.0.0.1:8788/api/auth/me')).status,403);
  assert.equal((await get('http://example.com:8080/')).status,403);
  const redirect=await get(origin+'/redirect');assert.equal(redirect.status,302);assert.equal((await get(redirect.location)).status,403);
 }finally{proxy.close();app.closeAllConnections();await new Promise(resolve=>app.close(resolve));}
});
test('screenshot rejects folder symlink outside workspace',async()=>{
 const root=await fs.mkdtemp(path.join(os.tmpdir(),'webuddy-shot-')),outside=await fs.mkdtemp(path.join(os.tmpdir(),'webuddy-out-'));
 try{
  assert.ok((await screenshotPath(root)).startsWith((await fs.realpath(root))+path.sep));
  await fs.rm(path.join(root,'.webuddy'),{recursive:true});await fs.symlink(outside,path.join(root,'.webuddy'));
  await assert.rejects(()=>screenshotPath(root),/escapes project/);
 }finally{await fs.rm(root,{recursive:true,force:true});await fs.rm(outside,{recursive:true,force:true});}
});

test('Chinese DOM response stays below terminal UTF-8 budget and keeps usable refs',()=>{
 const result=compactResponse({ok:true,text:'中文测试'.repeat(1250),elements:Array.from({length:60},(_,i)=>({ref:`e${i}`,label:'中'.repeat(80),tag:'button'})),errors:Array(10).fill('错'.repeat(500))});
 assert.ok(Buffer.byteLength(JSON.stringify(result))<=24000);
 assert.equal(result.truncated,true);assert.ok(result.elements.length>0);assert.equal(result.elements[0].ref,'e0');
 const hugeURL=compactResponse({ok:true,url:'http://127.0.0.1:5173/'+ '中'.repeat(20000)});
 assert.ok(Buffer.byteLength(JSON.stringify(hugeURL))<=24000);
});

test('CONNECT forwards only the current authorized project listener',async()=>{
 const app=http.createServer((req,res)=>res.end('tunnel works'));
 await new Promise(resolve=>app.listen(0,'127.0.0.1',resolve));
 const authority=`127.0.0.1:${app.address().port}`;
 const proxy=await createPreviewProxy(async target=>target.host===authority);
 const connect=target=>new Promise((resolve,reject)=>{
  const req=http.request({host:'127.0.0.1',port:proxy.port,method:'CONNECT',path:target});
  req.on('connect',(res,socket)=>{resolve({status:res.statusCode,socket});});req.on('error',reject);req.end();
 });
 try{
  for(const target of ['example.com:443','127.0.0.1:8788','127.0.0.1:80','u:p@'+authority,authority+'/evil']) {
   const denied=await connect(target);assert.equal(denied.status,403);denied.socket.destroy();
  }
  const accepted=await connect(authority);assert.equal(accepted.status,200);
  const output=new Promise(resolve=>{let data='';accepted.socket.on('data',c=>data+=c);accepted.socket.on('end',()=>resolve(data));});
  accepted.socket.write(`GET / HTTP/1.1\r\nHost: ${authority}\r\nConnection: close\r\n\r\n`);
  assert.match(await output,/tunnel works/);
 }finally{proxy.close();app.closeAllConnections();await new Promise(resolve=>app.close(resolve));}
});

test('real Chromium WebSocket works through the scoped proxy',{skip:!process.env.WEBUDDY_TEST_CHROME},async()=>{
 const {default:puppeteer}=await import('puppeteer-core');const {createHash}=await import('node:crypto');
 const peers=new Set();
 const app=http.createServer((req,res)=>res.end('<html>preview</html>'));
 app.on('upgrade',(req,socket)=>{
  peers.add(socket);socket.on('error',()=>{});socket.on('close',()=>peers.delete(socket));
  const accept=createHash('sha1').update(req.headers['sec-websocket-key']+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').digest('base64');
  socket.write(`HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: ${accept}\r\n\r\n`);
 });
 await new Promise(resolve=>app.listen(0,'127.0.0.1',resolve));
 const origin=`http://127.0.0.1:${app.address().port}`;
 const proxy=await createPreviewProxy(async target=>target.origin===origin);let browser;
 try{
  browser=await puppeteer.launch({executablePath:process.env.WEBUDDY_TEST_CHROME,headless:true,args:['--no-sandbox',`--proxy-server=http://127.0.0.1:${proxy.port}`,'--proxy-bypass-list=<-loopback>']});
  const page=await browser.newPage();await page.goto(origin);
  assert.equal(await page.evaluate(url=>new Promise(resolve=>{const ws=new WebSocket(url.replace('http:','ws:'));const timeout=setTimeout(()=>resolve('timeout'),5000);ws.onopen=()=>{clearTimeout(timeout);resolve('opened');ws.close()};ws.onerror=()=>{clearTimeout(timeout);resolve('error')};}),origin),'opened');
 }finally{await browser?.close();proxy.close();for(const s of peers)s.destroy();app.closeAllConnections();await new Promise(resolve=>app.close(resolve));}
});

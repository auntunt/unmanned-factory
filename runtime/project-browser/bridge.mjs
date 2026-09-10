/** Persistent browser bridge. Only run inside the project's Linux PID sandbox. */
import fs from 'node:fs/promises';
import http from 'node:http';
import net from 'node:net';
import path from 'node:path';
import crypto from 'node:crypto';
import readline from 'node:readline';
import {fileURLToPath} from 'node:url';

export function previewURL(raw) {
  const url = new URL(raw);
  if (url.protocol !== 'http:' || !['127.0.0.1', 'localhost'].includes(url.hostname) ||
      url.username || url.password || !url.port || Number(url.port) < 1024) {
    throw new Error('Use an HTTP project preview bound to 127.0.0.1 on a port >= 1024');
  }
  url.hostname = '127.0.0.1';
  return url;
}

export async function ownsListener(port, procRoot = '/proc') {
  // Network namespace is shared, but /proc is the project PID namespace. A
  // host-only listener has no visible owning FD and is therefore rejected.
  const tcp = await fs.readFile(path.join(procRoot, 'net/tcp'), 'utf8');
  const inodes = new Set(tcp.split('\n').slice(1).map(line => line.trim().split(/\s+/))
    .filter(row => row.length > 9 && row[3] === '0A' &&
      ['0100007F', '00000000'].includes(row[1].split(':')[0]) &&
      parseInt(row[1].split(':')[1], 16) === port).map(row => row[9]));
  if (!inodes.size) return false;
  for (const pid of await fs.readdir(procRoot)) {
    if (!/^\d+$/.test(pid)) continue;
    const folder = path.join(procRoot, pid, 'fd');
    let fds;
    try { fds = await fs.readdir(folder); } catch { continue; }
    for (const fd of fds) {
      try {
        const match = /^socket:\[(\d+)\]$/.exec(await fs.readlink(path.join(folder, fd)));
        if (match && inodes.has(match[1])) return true;
      } catch { /* A process may exit while being inspected. */ }
    }
  }
  return false;
}

export async function createPreviewProxy(allowed) {
  const sockets = new Set();
  const server = http.createServer(async (request, response) => {
    try {
      const target = previewURL(request.url);
      if (!await allowed(target)) throw new Error('Project preview origin is not authorized');
      const headers = {...request.headers, host: target.host};
      delete headers['proxy-authorization']; delete headers['proxy-connection'];
      const upstream = http.request(target, {method: request.method, headers}, incoming => {
        response.writeHead(incoming.statusCode || 502, incoming.headers);
        incoming.pipe(response);
      });
      upstream.setTimeout(30000, () => upstream.destroy());
      upstream.on('error', () => { if (!response.headersSent) response.writeHead(502); response.end('Preview unavailable'); });
      request.pipe(upstream);
    } catch { response.writeHead(403); response.end('Only the authorized project preview is accessible'); }
  });
  server.on('connect', (_, socket) => { socket.end('HTTP/1.1 403 Forbidden\r\n\r\n'); });
  server.on('upgrade', async (request, socket, head) => {
    try {
      const target = previewURL(request.url.replace(/^ws:/, 'http:'));
      if (!await allowed(target)) throw new Error('blocked');
      const upstream = net.connect(Number(target.port), '127.0.0.1', () => {
        const headers = Object.entries(request.headers).filter(([key]) => !key.startsWith('proxy-'));
        upstream.write(`${request.method} ${target.pathname}${target.search} HTTP/1.1\r\n` + headers.map(([key,value]) => `${key}: ${value}\r\n`).join('') + '\r\n');
        if (head.length) upstream.write(head);
        socket.pipe(upstream); upstream.pipe(socket);
      });
      upstream.on('error', () => socket.destroy()); socket.on('error', () => upstream.destroy());
      socket.on('close', () => upstream.destroy());
    } catch { socket.destroy(); }
  });
  server.on('connection', socket => { sockets.add(socket); socket.on('close', () => sockets.delete(socket)); });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  return {port: server.address().port, close: () => { for (const socket of sockets) socket.destroy(); server.close(); }};
}

export function compactResponse(result) {
  while (Buffer.byteLength(JSON.stringify(result)) > 24000) {
    result.truncated = true;
    if (result.text?.length > 1000) result.text=result.text.slice(0,Math.floor(result.text.length/2));
    else if (result.elements?.length) result.elements.pop();
    else if (result.errors?.length) result.errors.shift();
    else {
      // Navigation URLs are project-controlled too, and can be arbitrarily long.
      const key = Object.keys(result).find(key => typeof result[key] === 'string' && result[key].length > 1000);
      if (key) result[key] = result[key].slice(0, 1000);
      else return {ok:false,error:'Browser response exceeds output budget',truncated:true};
    }
  }
  return result;
}

export async function screenshotPath(workspace) {
  const base = await fs.realpath(workspace);
  const parent = path.join(base, '.webuddy');
  await fs.mkdir(parent, {recursive: true});
  const parentRelative = path.relative(base, await fs.realpath(parent));
  if (parentRelative.startsWith('..') || path.isAbsolute(parentRelative)) throw new Error('Screenshot folder escapes project');
  const folder = path.join(parent, 'browser');
  await fs.mkdir(folder, {recursive: true});
  const actual = await fs.realpath(folder);
  const relative = path.relative(base, actual);
  if (relative.startsWith('..') || path.isAbsolute(relative)) throw new Error('Screenshot folder escapes project');
  return path.join(actual, `preview-${crypto.randomUUID()}.png`);
}

async function startBridge(socketPath, workspace, chrome) {
  if (process.platform !== 'linux') throw new Error('Browser bridge requires the isolated Linux project session');
  const procStat = await fs.readFile('/proc/1/cmdline', 'utf8');
  // The terminal supervisor is PID 1 (or bubblewrap's namespace init). Refuse
  // accidental deployment as an ordinary host daemon where all sockets are visible.
  if (!procStat.includes("request['command']") || !procStat.includes('start_new_session=True')) throw new Error('Project PID namespace is required');
  let activeOrigin = null, browser = null, page = null, nextRef = 1;
  const errors = [];
  const addError = value => { errors.push(String(value).slice(0,500)); if (errors.length > 10) errors.shift(); };
  const proxy = await createPreviewProxy(async target => target.origin === activeOrigin &&
    Number(target.port) !== proxy.port && await ownsListener(Number(target.port)));
  const {default: puppeteer} = await import('puppeteer-core');
  const directory = await fs.mkdtemp('/tmp/webuddy-chrome-');
  async function ensurePage() {
    if (page) return;
    browser = await puppeteer.launch({executablePath: chrome, headless: true, pipe: true,
      userDataDir: directory, timeout:30000, protocolTimeout:30000,
      args:['--no-sandbox','--disable-dev-shm-usage','--disable-background-networking',
        '--disable-component-update','--disable-sync','--disable-extensions',
        '--disable-quic','--force-webrtc-ip-handling-policy=disable_non_proxied_udp',
        `--proxy-server=http://127.0.0.1:${proxy.port}`, '--proxy-bypass-list=<-loopback>']});
    page = await browser.newPage();
    await page.setViewport({width:1280,height:900,deviceScaleFactor:1});
    await page.setBypassServiceWorker(true);
    await page.setRequestInterception(true);
    page.on('request', request => {
      const url = request.url();
      let allowed = false;
      try { allowed = previewURL(url).origin === activeOrigin; } catch { allowed = /^(?:data:|blob:)/.test(url) && !request.isNavigationRequest(); }
      if (allowed) void request.continue().catch(()=>{}); else { addError('Blocked external request: ' + url.slice(0,180)); void request.abort().catch(()=>{}); }
    });
    page.on('pageerror', error => addError(error.message));
    page.on('console', msg => { if (msg.type() === 'error') addError(msg.text()); });
    page.on('response', response => { if (response.status() >= 400) addError(`HTTP ${response.status()}: ${response.url()}`); });
    browser.on('targetcreated', async target => { if (target.type() === 'page') { const popup = await target.page(); if (popup && popup !== page) await popup.close().catch(()=>{}); } });
    // Close the initial blank tab; all later interactions use the one project page.
    for (const other of await browser.pages()) if (other !== page) await other.close();
  }
  async function snapshot() {
    const dom = await page.evaluate(startRef => {
      const elements = [];
      let counter = startRef;
      for (const item of document.querySelectorAll('a,button,input,textarea,select,[role="button"],[contenteditable="true"]')) {
        if (!item.getClientRects().length || elements.length >= 60) continue;
        const ref = `e${counter++}`; item.setAttribute('data-webuddy-ref', ref);
        elements.push({ref,tag:item.tagName.toLowerCase(),type:item.getAttribute('type'),
          label:(item.getAttribute('aria-label') || item.getAttribute('placeholder') || item.innerText || item.getAttribute('name') || '').trim().slice(0,80),
          disabled:Boolean(item.disabled)});
      }
      return {nextRef:counter,title:document.title.slice(0,200),text:(document.body?.innerText || '').slice(0,5000),elements};
    }, nextRef);
    nextRef = dom.nextRef; delete dom.nextRef;
    return {url:page.url(),viewport:page.viewport(),...dom,errors:[...errors]};
  }
  async function perform(request) {
    const {action, args = {}} = request;
    if (!['open','snapshot','click','fill','screenshot','close'].includes(action)) throw new Error('Unknown browser action');
    if (action === 'close') { await browser?.close(); browser=null;page=null;activeOrigin=null;return {closed:true}; }
    if (action === 'open') {
      const target = previewURL(args.url);
      if (Number(target.port) === proxy.port || !await ownsListener(Number(target.port))) throw new Error('Preview port is not owned by this project session; start the server here with --host 127.0.0.1');
      if (activeOrigin && activeOrigin !== target.origin) { await browser?.close(); browser=null;page=null; }
      const width=args.width ?? 1280, height=args.height ?? 900;
      if(!Number.isInteger(width)||width<320||width>1920||!Number.isInteger(height)||height<320||height>1600) throw new Error('Viewport must be width 320..1920 and height 320..1600');
      activeOrigin = target.origin; errors.length=0; await ensurePage();
      await page.setViewport({width,height,deviceScaleFactor:1,isMobile:width<=600,hasTouch:width<=600});
      await page.goto(target.href,{waitUntil:'domcontentloaded',timeout:20000});
    } else {
      if (!page || !activeOrigin) throw new Error('Open a project preview first');
      if (!await ownsListener(Number(new URL(activeOrigin).port))) throw new Error('Project preview has stopped');
      if (action === 'click' || action === 'fill') {
        if (!/^e[1-9][0-9]{0,9}$/.test(args.ref)) throw new Error('Use a ref from the latest snapshot');
        const element = await page.$(`[data-webuddy-ref="${args.ref}"]`);
        if (!element) throw new Error('Element changed; take a new snapshot');
        try {
          if (action === 'click') await element.click();
          else {
            if (typeof args.text !== 'string' || args.text.length > 10000) throw new Error('Fill text must be at most 10000 characters');
            const editable = await element.evaluate(node => node.matches('input:not([type="file"]),textarea,[contenteditable="true"]'));
            if (!editable) throw new Error('Element is not an editable text field');
            await element.focus(); await page.keyboard.down('Control'); await page.keyboard.press('A'); await page.keyboard.up('Control');
            await page.keyboard.sendCharacter(args.text);
          }
        } finally { await element.dispose(); }
      }
      if (action === 'screenshot') {
        const filename = await screenshotPath(workspace);
        await page.screenshot({path:filename,fullPage:false});
        return {...await snapshot(),screenshot_path:filename};
      }
    }
    return snapshot();
  }
  let queue = Promise.resolve();
  const server = net.createServer(socket => {
    socket.setTimeout(40000,()=>socket.destroy());
    let buffer='';
    socket.on('data', chunk => {
      buffer += chunk.toString();
      if (buffer.length > 64000) { socket.destroy(); return; }
      if (!buffer.includes('\n')) return;
      const line=buffer.split('\n')[0];buffer='';socket.pause();
      queue = queue.then(async()=>{
        let result;
        try { result={ok:true,...await perform(JSON.parse(line))}; }
        catch(error) { result={ok:false,error:String(error.message).slice(0,1000),errors:[...errors]}; }
        result = compactResponse(result);
        socket.end(JSON.stringify(result)+'\n');
      }).catch(()=>socket.destroy());
    });
  });
  server.listen(socketPath,()=>{void fs.chmod(socketPath,0o600);});
  const shutdown = async()=>{server.close();proxy.close();await browser?.close().catch(()=>{});await fs.rm(directory,{recursive:true,force:true});process.exit(0);};
  process.on('SIGTERM',shutdown);process.on('SIGINT',shutdown);
}

async function requestBridge(socketPath, encoded) {
  const payload=Buffer.from(encoded,'base64').toString();
  if (payload.length > 64000) throw new Error('Browser request too large');
  const socket=net.connect(socketPath);
  let response='';
  socket.setTimeout(35000,()=>{socket.destroy();process.exitCode=1;});
  socket.on('connect',()=>socket.write(payload+'\n'));
  socket.on('data',chunk=>{response+=chunk.toString();if(response.length>40000){socket.destroy();process.exitCode=1;}});
  socket.on('end',()=>process.stdout.write(response));
  socket.on('error',()=>{process.stderr.write('Project browser bridge unavailable\n');process.exitCode=1;});
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const [mode,socketPath,...rest]=process.argv.slice(2);
  if (mode === '--serve') await startBridge(socketPath,rest[0],rest[1]);
  else if (mode === '--request') await requestBridge(socketPath,rest[0]);
  else throw new Error('Expected --serve or --request');
}

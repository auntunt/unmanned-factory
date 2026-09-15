// Presentation-only fixtures. No backend or production requests; no imports are submitted.
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const puppeteer = require('../../runtime/project-browser/node_modules/puppeteer-core');
const output = path.resolve(__dirname, '../../docs/design/icons-2026-09-15');
const origin = process.env.UIUX_ORIGIN || 'http://127.0.0.1:5186';
const agents = [{ id: 'maintenance', name: '旧项目维护员', purpose: '梳理已有项目，基于回归证据修复问题。', active_version: 2 }, { id: 'cli', name: 'CLI 构建员', purpose: '将单一业务能力整理为可验证的命令行工具。', active_version: 1 }];
const modules = [{ id: 'maintenance-method', name: '项目代码梳理', description: '先了解项目边界，再制定可验证的修改步骤。', instructions: '读取入口与依赖，列出影响范围，并验证结果。', category: 'workflow', version: 2, agent_reference_count: 2 }, { id: 'style', name: '清晰界面规范', description: '统一层级、留白与状态表达。', instructions: '优先保证控件可辨识性与键盘操作。', category: 'style', version: 1, agent_reference_count: 1 }];
function payload(p) {
 if (p === '/api/auth/me') return { user: { id: 1, username: 'owner', role: 'admin' }, csrf_token: 'fixture' };
 if (p === '/api/v3/environment') return { mode: 'preview', label: '隔离界面演练' };
 if (p === '/api/v4/agents') return { agents };
 if (p === '/api/v2/projects') return { projects: [{ id: 'project', name: '团队工具项目' }] };
 if (p === '/api/v4/modules') return { modules };
 if (p.endsWith('/capabilities')) return { capabilities: [] };
 if (p.endsWith('/runs')) return { runs: [] };
 if (p.includes('skill-ingestions')) return { items: [] };
 return {};
}
(async () => {
 fs.mkdirSync(output, { recursive: true });
 const browser = await puppeteer.launch({ headless: true, executablePath: process.env.BROWSER_EXECUTABLE || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' });
 const errors = []; const report = [];
 try {
 for (const theme of ['light', 'dark']) {
  const page = await browser.newPage(); await page.setViewport({ width: 1440, height: 1080 });
  await page.evaluateOnNewDocument(t => localStorage.setItem('webuddy:theme', t), theme);
  await page.emulateMediaFeatures([{ name: 'prefers-color-scheme', value: theme }]);
  page.on('pageerror', e => errors.push(e.message)); await page.setRequestInterception(true);
  page.on('request', async req => {
   const url = new URL(req.url());
   if (url.pathname.startsWith('/api/')) { assert.equal(req.method(), 'GET'); await req.respond({ status: 200, contentType: 'application/json', body: JSON.stringify(payload(url.pathname)) }); }
   else if (url.origin === origin || url.protocol === 'data:') await req.continue();
   else await req.abort();
  });
  await page.goto(origin + '/agents', { waitUntil: 'networkidle0' }); await page.waitForSelector('.agent-catalog-card');
  await page.click('.wb-pack-import > summary');
  await page.screenshot({ path: path.join(output, `agents-native-${theme}.png`), fullPage: true });
  await page.click('#pack-tab-1');
  assert.equal(await page.$eval('#pack-panel-0', el => el.hidden), true);
  assert.equal(await page.$eval('#pack-panel-1', el => el.hidden), false);
  await page.screenshot({ path: path.join(output, `agents-external-${theme}.png`), fullPage: true });
  await page.goto(origin + '/ability-center?tab=modules', { waitUntil: 'networkidle0' }); await page.waitForSelector('.mod-card');
  await page.click('.mod-card summary');
  await new Promise(resolve => setTimeout(resolve, 200));
  const checks = await page.evaluate(() => ({
    overflow: document.documentElement.scrollWidth > innerWidth,
    expanded: document.querySelector('.mod-card details').open,
    triangle: Boolean(document.querySelector('.mod-card summary svg[data-icon=triangle]')),
    badge: Boolean(document.querySelector('.mod-card .wb-category-badge svg')),
    actionSizes: [getComputedStyle(document.querySelector('.mod-content-action')).fontSize, getComputedStyle(document.querySelector('.mod-card footer .wb-text-link')).fontSize],
    theme: document.documentElement.dataset.theme,
  }));
  assert.equal(checks.overflow, false); assert.equal(checks.expanded, true); assert.equal(checks.triangle, true); assert.equal(checks.badge, true); assert.equal(checks.actionSizes[0], checks.actionSizes[1]);
  report.push({ theme, ...checks });
  await page.screenshot({ path: path.join(output, `modules-${theme}.png`), fullPage: true });
  await page.setViewport({ width: 390, height: 844 });
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
  await page.close();
 }
 assert.deepEqual(errors, []);
 fs.writeFileSync(path.join(output, 'verification.json'), JSON.stringify({ fixture: true, report, errors }, null, 2));
 console.log('Light/dark screenshots and browser checks passed.');
 } finally { await browser.close(); }
})().catch(e => { console.error(e); process.exit(1); });

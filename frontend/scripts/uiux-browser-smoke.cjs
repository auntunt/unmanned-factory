// Isolated presentation fixtures: no backend, credentials, model calls, or production writes.
const path = require('node:path'); const fs = require('node:fs'); const assert = require('node:assert/strict');
const puppeteer = require('../../runtime/project-browser/node_modules/puppeteer-core');
const axePath = require.resolve('axe-core/axe.min.js', { paths: [process.env.UIUX_TOOLS || '/tmp/webuddy-uiux-tools'] });
const output = process.env.UIUX_OUTPUT || '/tmp/webuddy-uiux-audit'; fs.mkdirSync(output, { recursive: true });
const origin = process.env.UIUX_ORIGIN || 'http://127.0.0.1:5175';
const run = { id: '1234567890abcdef1234567890abcdef', project_id: 'abcdef1234567890abcdef1234567890', request: '修复工时导出的日期格式\n兼容已有 CSV 输入，保留原始记录。', status: 'needs_human', execution_mode: 'continuous', revision: 10, created_at: '2026-09-14T01:00:00Z', updated_at: '2026-09-14T02:00:00Z', plan: { title: '持续编码', summary: '补齐导出兼容性和回归验证。', tasks: [], questions: [] }, artifacts: { commit: 'a'.repeat(40), branch: 'factory/1234567890abcdef1234567890abcdef' }, source: { actor_id: 1 }, policy: { revision: 1, mode: 'autonomous' } };
const definitions = [['intake', '需求澄清'], ['plan', '方案规划'], ['build', '开发执行'], ['verify', '质量验证'], ['deliver', '交付发布'], ['reuse', '能力沉淀（可选）']];
const engineering = { stages: definitions.map(([id, label]) => ({ id, label, count: 3, unit: '条记录', items: [{ id: run.id, title: '持续编码', status: run.status, updated_at: run.updated_at, href: `/runs/${run.id}?view=${id === 'deliver' ? 'delivery' : 'execution'}` }] })), verified_runs: 1, published_runs: 0, distilled_runs: 0, reused_runs: 0, draft_capabilities: 0, ready_capabilities: 0 };
const project = { id: run.project_id, name: '工时 CSV 工具', repository: 'local/hours', workspace: '/workspace/hours', base_branch: 'main', checks: {}, revision: 1, managed_workspace: true, budget_usd: 10 };
const overview = { snapshot_at: run.updated_at, projects: 1, runs: 3, active_runs: 1, attention_runs: 1, engineering, run_snapshots: [run], project_summaries: [{ ...project, next_run: run, active_runs: 1, attention_runs: 1, engineering }], attention: [{ id: run.id, title: '持续编码', project_id: project.id, project_name: project.name, status: run.status, updated_at: run.updated_at, run_snapshot: run }], recent_events: [], model_usage: [{ profile: 'standard', provider: 'claude', model: 'opus', calls: 3, token_usage_calls: 0, cache_usage_calls: 0, cached_input_tokens: 0 }] };
const presets = ['general', 'bugfix', 'startup', 'release', 'dependencies'].map((id, index) => ({ id, label: ['新需求', '修复 Bug', '启动排错', '部署准备', '依赖维护'][index], hint: '描述目标与验收方式', version: 1, fields: [] }));
const mutations = []; let delayRuntime = false;
function payload(url, method) {
  const p = new URL(url).pathname;
  if (method !== 'GET') { mutations.push(p); return run }
  if (p === '/api/auth/me') return { user: { id: 1, username: 'owner', role: 'admin' }, csrf_token: 'isolated-fixture' };
  if (p === '/api/v3/environment') return { mode: 'preview', label: '隔离界面演练' };
  if (p === '/api/v2/projects') return { projects: [project] };
  if (p.includes('/overview')) return { ...overview, project_id: new URL(url).searchParams.get('project_id') };
  if (p === '/api/v2/operation-presets') return { presets };
  if (p.endsWith('/policy')) return { revision: 1, mode: 'supervised', max_risk: 'low', max_attempts: 2, auto_escalate: false, resume_on_restart: false };
  if (p.endsWith('/readiness')) return { ready: true, checks: [] };
  if (p === '/api/v2/runs') return { runs: [run] };
  if (p === `/api/v2/runs/${run.id}`) return run;
  if (p.endsWith('/inspection')) return { enabled: true, interval_s: 3600, revision: 1, last_at: run.updated_at, last_status: 'inspection_failed', last_run_id: run.id, consecutive_failures: 3, history: Array.from({ length: 20 }, (_, i) => ({ run_id: `inspection-${i}`, at: new Date(Date.UTC(2026, 8, 14, i)).toISOString(), verdict: i < 3 ? 'fail' : i % 4 === 0 ? 'unverified' : 'pass', duration_s: 3.2 })) };
  if (p.endsWith('/deploy-targets')) return { targets: [], available: [], revision: 0 };
  if (p.endsWith('/knowledge')) return { entries: [] };
  if (p.endsWith('/capabilities')) return { capabilities: [], bindings: [] };
  if (p.endsWith('/deliverables')) return { items: [], count: 0 };
  if (p.endsWith('/runtime')) return { revision: 1, profiles: {}, limits: {}, blockers: [], tools: {}, host: {}, last_probes: [] };
  if (p.endsWith('/operations')) return { revision: 0, knowledge_enabled: false, webhook_configured: false };
  return {};
}
(async () => {
 const browser = await puppeteer.launch({ headless: true, executablePath: process.env.BROWSER_EXECUTABLE || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' });
 const report = { fixture: true, viewport: { width: 1440, height: 1000 }, pages: [], errors: [], mutations };
 try {
 const page = await browser.newPage(); await page.setViewport(report.viewport);
 page.on('pageerror', error => report.errors.push(error.message));
 await page.setRequestInterception(true);
 page.on('request', async req => { if (new URL(req.url()).pathname.startsWith('/api/')) { if (delayRuntime && req.url().endsWith('/runtime')) await new Promise(r => setTimeout(r, 2000)); await req.respond({ status: 200, contentType: 'application/json', body: JSON.stringify(payload(req.url(), req.method())) }); } else await req.continue(); });
 async function audit(name, url, wait) {
   await page.goto(origin + url, { waitUntil: 'networkidle0' }); await page.waitForSelector(wait);
   await page.addScriptTag({ path: axePath });
   const result = await page.evaluate(async () => { const a = await axe.run(document, { runOnly: { type: 'tag', values: ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'best-practice'] } }); return { violations: a.violations.map(v => ({ id: v.id, impact: v.impact, nodes: v.nodes.map(n => ({ target: n.target, summary: n.failureSummary })) })), incomplete: a.incomplete.map(v => ({ id: v.id, count: v.nodes.length })), overflow: document.documentElement.scrollWidth > innerWidth }; });
   report.pages.push({ name, url, ...result }); await page.screenshot({ path: path.join(output, name + '.png'), fullPage: true });
 }
 await audit('overview', '/', '.wb-stage-strip');
 await audit('project', `/projects/${project.id}?stage=deliver`, '[role="tabpanel"]');
 assert.equal(await page.$$eval('.wb-stage-strip [role="tab"]', els => els.length), 6);
 assert.equal(await page.$eval('.pw-cycle', e => e.querySelector('.el-loop') === null), true);
 assert.equal(await page.$$eval('h2', els => els.some(e => e.textContent === '定时巡检')), false);
 await page.focus('#stage-deliver'); await page.keyboard.press('ArrowLeft'); await page.waitForFunction(() => location.search.includes('stage=verify'));
 assert.equal(await page.$eval('#stage-verify', e => e.getAttribute('aria-selected')), 'true');

 await audit('project-settings', `/projects/${project.id}?tab=settings`, '.wb-inspection-trend a');
 await audit('runs', '/runs', '.wb-runs-table');
 await audit('run-detail', `/runs/${run.id}?view=requirements`, '#run-view-panel');
 await page.focus('#run-tab-requirements'); await page.keyboard.press('ArrowRight'); await page.waitForFunction(() => location.search.includes('view=plan'));
 await page.goto(origin + `/runs/${run.id}?view=requirements`, { waitUntil: 'networkidle0' });
 await page.click('[aria-label="更多运行操作"]');
 const trigger = await page.waitForSelector('.wb-overflow button.wb-button-danger:last-child'); await trigger.click();
 await page.waitForSelector('dialog[open]'); assert.equal(mutations.length, 0);
 assert.equal(await page.evaluate(() => document.activeElement.textContent), '保留运行');
 await page.keyboard.press('Escape'); assert.equal(await page.$('dialog[open]'), null);
 assert.equal(await page.evaluate(() => document.activeElement.textContent), '取消运行');
 await audit('capabilities-empty', '/capabilities', '.wb-empty-state');
 await audit('costs', '/costs', '.wb-cost-summary');
 await audit('runtime', '/settings/runtime', '[aria-label="服务器连接"]');
 delayRuntime = true; await page.goto(origin + '/settings/runtime', { waitUntil: 'domcontentloaded' }); await page.waitForSelector('.wb-skeleton'); await page.screenshot({ path: path.join(output, 'runtime-loading.png') });
 assert.deepEqual(report.errors, []); assert.deepEqual(mutations, []);
 assert.equal(report.pages.every(p => !p.overflow && p.violations.length === 0), true, 'Axe violations or horizontal overflow: see report');
 } finally { await browser.close(); fs.writeFileSync(path.join(output, 'axe-report.json'), JSON.stringify(report, null, 2)); }
 console.log(JSON.stringify(report));
})().catch(e => { console.error(e); process.exit(1) });

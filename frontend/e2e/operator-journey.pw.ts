import { expect, test, type Page, type TestInfo, type Route } from '@playwright/test'
import { readFile } from 'node:fs/promises'

// Public disposable credentials defined by scripts/preview_v3.py. No real
// provider credentials, external publication or production data are involved.
async function login(page: Page) {
  await page.goto('/')
  await page.getByLabel('用户名', { exact: true }).fill('preview')
  await page.getByLabel('密码', { exact: true }).fill('factory-preview-only')
  await page.getByRole('button', { name: '进入工作台' }).click()
  await expect(page.getByRole('heading', { name: '你想做什么？' })).toBeVisible()
  await expect(page.locator('.as-env')).toBeVisible()
}
async function capture(page: Page, info: TestInfo, name: string) {
  const path = info.outputPath(name + '.png')
  await page.screenshot({ path, fullPage: true })
  await info.attach(name + ' (synthetic rehearsal)', { path, contentType: 'image/png' })
}

test('desktop: real login, overview, task filtering, execution evidence and reload', async ({ page }, info) => {
  const errors: string[] = []
  page.on('pageerror', error => errors.push(error.message))
  await login(page)
  await capture(page, info, 'desktop-start')
  await page.getByRole('link', { name: '工程总览', exact: true }).click()
  await expect(page.getByRole('heading', { name: '工程总览', exact: true })).toBeVisible()
  await page.getByRole('link', { name: '历史作品', exact: true }).click()
  await expect(page.getByRole('heading', { name: '历史作品', exact: true })).toBeVisible()
  await page.getByRole('searchbox', { name: '搜索任务' }).fill('不存在的合成目标')
  await expect(page.getByRole('heading', { name: '没有符合条件的任务' })).toBeVisible()
  await page.getByRole('button', { name: '清除筛选' }).click()
  await page.getByRole('searchbox', { name: '搜索任务' }).fill('请完善工单服务')
  await expect(page.locator('.cv-list-item')).toHaveCount(1)
  await page.reload()
  await expect(page.getByRole('searchbox', { name: '搜索任务' })).toHaveValue('请完善工单服务')
  await capture(page, info, 'desktop-history')
  await page.locator('.cv-list-item').click()
  await expect(page.locator('.ew-run-context')).toBeVisible()
  // The scripted provider writes a failing file then repairs it; checks and
  // checkpoint persistence are real. Never replace a failed lifecycle with mock success.
  await expect(async () => {
    const id = page.url().split('/runs/')[1]?.split('?')[0]
    const response = await page.request.get('/api/v2/runs/' + id)
    expect(response.ok()).toBeTruthy()
    expect((await response.json()).status).toBe('ready_for_review')
  }).toPass({ timeout: 90_000, intervals: [1000, 2000] })
  await page.reload()
  await page.getByText('查看验证记录', { exact: true }).click()
  await expect(page.getByText('结果来自真实检查记录。', { exact: false })).toBeVisible()
  // This legacy rehearsal has real command checks, not an acceptance ledger.
  // Require the recorded successful check AND the failed repair attempt.
  const finalCheck = page.locator('.cv-recorded-check').filter({ hasText: '集成检查 · welcome' })
  await expect(finalCheck.locator('summary')).toContainText('通过')
  await finalCheck.locator('summary').click()
  await expect(finalCheck).toContainText('退出码：0')
  await expect(finalCheck.getByLabel('检查命令参数')).toContainText("Path('welcome.txt').read_text()")
  const firstAttempt = page.locator('.cv-recorded-check').filter({ hasText: '任务 welcome · 第 1 次尝试' })
  await expect(firstAttempt.locator('summary')).toContainText('未通过')
  await firstAttempt.locator('summary').click()
  await expect(firstAttempt).toContainText('退出码：1')
  await expect(firstAttempt).toContainText('AssertionError: 欢迎提示未达到验收条件')
  await capture(page, info, 'desktop-task-evidence')
  await page.reload()
  await expect(page.locator('.ew-run-context')).toBeVisible()
  expect(errors).toEqual([])
})

test('failed refresh preserves real records and repeated retry returns to real HTTP', async ({ page }, info) => {
  await login(page)
  await page.goto('/history')
  await expect(page.locator('.cv-list-item').first()).toBeVisible()
  const before = await page.locator('.cv-list-item').count()
  // Explicit transport fault injection; records and the successful retry come
  // from the real rehearsal backend. This is not a production outage claim.
  await page.route('**/api/v2/runs', route => route.fulfill({
    status: 503, contentType: 'application/json',
    body: JSON.stringify({ detail: '合成网络故障：验证重试体验' }),
  }), { times: 1 })
  await page.getByRole('button', { name: '刷新列表', exact: true }).click()
  await expect(page.getByRole('alert')).toContainText('同步失败，以下为上次成功读取的记录')
  await expect(page.locator('.cv-list-item')).toHaveCount(before)
  await capture(page, info, 'desktop-refresh-failure')
  await page.getByRole('button', { name: '重新读取', exact: true }).click()
  await expect(page.getByRole('alert')).toHaveCount(0)
  await page.getByRole('button', { name: '刷新列表', exact: true }).click()
  await expect(page.getByRole('button', { name: '刷新列表', exact: true })).toBeEnabled()
  await expect(page.locator('.cv-list-item')).toHaveCount(before)
})

test('mobile: drawer dismissal, task filters and draft survive reload without submitting', async ({ page }, info) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await login(page)
  await page.getByLabel('需求', { exact: true }).fill('合成草稿：建立工单管理工具，支持预约和导出')
  await page.reload()
  await expect(page.getByLabel('需求', { exact: true })).toHaveValue('合成草稿：建立工单管理工具，支持预约和导出')
  await capture(page, info, 'mobile-start')
  await page.getByRole('button', { name: '打开导航', exact: true }).click()
  await expect(page.getByRole('dialog', { name: '工作区导航' })).toBeVisible()
  await page.keyboard.press('Escape')
  await expect(page.getByRole('dialog', { name: '工作区导航' })).toHaveCount(0)
  await page.getByRole('button', { name: '打开导航', exact: true }).click()
  await page.getByRole('dialog', { name: '工作区导航' }).getByRole('link', { name: '历史作品', exact: true }).click()
  await expect(page.getByRole('dialog', { name: '工作区导航' })).toHaveCount(0)
  await page.getByRole('button', { name: /^待处理/ }).click()
  await expect(page.getByRole('button', { name: /^待处理/ })).toHaveAttribute('aria-pressed', 'true')
  await page.getByRole('button', { name: /^最近任务/ }).click()
  await expect(page.locator('.cv-list-item').first()).toBeVisible()
  await expect(page.locator('body')).not.toHaveCSS('overflow', 'hidden')
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBeTruthy()
  await capture(page, info, 'mobile-history')
})

test('homepage submission survives a lost response and delivers one independently verified CLI task', async ({ page }, info) => {
  const goal = '创建一个命令行工具，运行 python hello.py 输出：工单服务已就绪'
  type EntryRun = {
    id: string; project_id: string; status: string; revision: number
    spec_confirmation?: { automatic: boolean; policy: string }
    artifacts?: {
      checks?: Array<{ name: string; exit: number }>
      acceptance_ledger?: { complete: boolean; counts: { pass: number; fail: number; unverified: number } }
    }
  }
  const first: { value?: EntryRun } = {}
  const submissions: Array<Record<string, unknown>> = []
  let workspaceCreates = 0
  page.on('request', request => {
    if (request.method() !== 'POST') return
    const path = new URL(request.url()).pathname
    if (path === '/api/v2/projects/create-workspace') workspaceCreates += 1
    if (path === '/api/v2/runs') submissions.push(request.postDataJSON() as Record<string, unknown>)
  })
  await login(page)
  // Let the real server commit the task, then simulate loss of only its response.
  // The retry must recover that same task, not create another project or run.
  let responseLossInjected = false
  const loseCreateResponse = async (route: Route) => {
    // A background list GET must neither consume nor trigger this fault.
    if (route.request().method() !== 'POST' || responseLossInjected) {
      await route.continue()
      return
    }
    responseLossInjected = true
    const response = await route.fetch()
    await page.unroute('**/api/v2/runs', loseCreateResponse)
    expect(response.status()).toBe(201)
    first.value = await response.json() as EntryRun
    await route.abort('failed')
  }
  await page.route('**/api/v2/runs', loseCreateResponse)
  await page.getByLabel('需求', { exact: true }).fill(goal)
  await page.getByRole('button', { name: '开始制作', exact: true }).click()
  // The actual router asks for the mode for this wording. Choose through the
  // real UI; do not stub routing or silently rewrite the user's fixed goal.
  const routeChoice = page.getByRole('group', { name: '选择处理方式', exact: true })
  await expect(routeChoice).toBeVisible()
  expect(workspaceCreates).toBe(0)
  expect(submissions).toHaveLength(0)
  await capture(page, info, 'homepage-routing-choice')
  await routeChoice.getByRole('button', { name: '按开发处理', exact: true }).click()
  await expect(page.getByRole('alert')).toContainText('未能确认启动状态')
  expect(first.value?.id).toBeTruthy()
  await expect(page.getByLabel('需求', { exact: true })).toHaveValue(goal)
  await capture(page, info, 'homepage-lost-response')
  await page.reload()
  await expect(page.getByLabel('需求', { exact: true })).toHaveValue(goal)
  const initial = first.value!
  const recovered = page.waitForResponse(response =>
    new URL(response.url()).pathname === '/api/v2/runs' && response.request().method() === 'POST')
  await page.getByRole('button', { name: '开始制作', exact: true }).click()
  const response = await recovered
  expect(response.status()).toBe(201)
  const recoveredRun = await response.json() as EntryRun
  expect(recoveredRun.id).toBe(initial.id)
  await expect(page).toHaveURL(new RegExp('/runs/' + initial.id + '$'))
  expect(workspaceCreates).toBe(1)
  expect(submissions).toHaveLength(2)
  expect(submissions[0]).toMatchObject({
    request: goal, project_id: initial.project_id, operation: 'general', interaction_mode: 'automatic',
  })
  expect(submissions[0].idempotency_key).toBeTruthy()
  expect(submissions[1].idempotency_key).toBe(submissions[0].idempotency_key)
  const sameProject = await page.request.get('/api/v2/runs?project_id=' + initial.project_id)
  expect(sameProject.ok()).toBeTruthy()
  expect((await sameProject.json()).runs.map((run: EntryRun) => run.id)).toEqual([initial.id])

  let delivered: EntryRun | undefined
  await expect(async () => {
    const result = await page.request.get('/api/v2/runs/' + initial.id)
    expect(result.ok()).toBeTruthy()
    delivered = await result.json() as EntryRun
    expect(delivered.status).toBe('ready_for_review')
  }).toPass({ timeout: 90_000, intervals: [1000, 2000] })
  expect(delivered?.spec_confirmation).toMatchObject({ automatic: true, policy: 'submission' })
  expect(delivered?.artifacts?.checks).toEqual(expect.arrayContaining([
    expect.objectContaining({ name: 'workspace-integrity', exit: 0 }),
  ]))
  expect(delivered?.artifacts?.acceptance_ledger?.complete).toBe(true)
  expect(delivered?.artifacts?.acceptance_ledger?.counts.pass).toBeGreaterThan(0)
  expect(delivered?.artifacts?.acceptance_ledger?.counts).toMatchObject({ fail: 0, unverified: 0 })
  const eventResponse = await page.request.get('/api/v2/runs/' + initial.id + '/events')
  expect(eventResponse.ok()).toBeTruthy()
  const eventRows = (await eventResponse.json()).events as Array<{
    id: number; type: string; task_id?: string
    payload: {
      actor?: string; revision?: number; configuration_revision?: number
      name?: string; exit?: number; stdout?: string; argv?: string[]
      files?: Array<{ path: string; accepted: boolean; late: boolean }>
    }
  }>
  const events = eventRows.map(event => event.type)
  expect(events).toEqual(expect.arrayContaining(['requirement_analysis.completed', 'spec.auto_confirmed', 'policy.authorized', 'run.started', 'verification.completed']))
  // Specification delegation and execution permission are different records.
  // Assert the real automatic project-policy authorization precedes execution;
  // never race the planner's transient awaiting_approval state with a fake click.
  const authorization = eventRows.find(event => event.type === 'policy.authorized')
  const execution = eventRows.find(event => event.type === 'run.started')
  expect(authorization?.payload.actor).toBe('project-policy')
  expect(authorization?.payload.revision).toBe(delivered?.revision)
  expect(authorization?.payload.revision).toBeGreaterThan(0)
  expect(authorization?.id).toBeLessThan(execution!.id)
  expect(events).not.toContain('human.approved')
  const functionalCheck = eventRows.find(event => event.type === 'check.result'
    && event.task_id === 'verification' && event.payload.name === 'synthetic-cli-functional-check')
  expect(functionalCheck?.payload).toMatchObject({ exit: 0, stdout: '工单服务已就绪\n' })
  expect(functionalCheck?.payload.argv?.slice(-2)).toEqual(['-I', 'hello.py'])
  const scope = eventRows.find(event => event.type === 'scope_declaration')
  expect(scope?.payload.files).toEqual(expect.arrayContaining([
    expect.objectContaining({ path: 'hello.py', accepted: true, late: false }),
  ]))
  await page.reload()
  await page.getByText('查看验证记录', { exact: true }).click()
  await expect(page.locator('.cv-verify-counts')).toBeVisible()
  const integrated = page.locator('.cv-recorded-check').filter({ hasText: '集成检查 · workspace-integrity' })
  await expect(integrated.locator('summary')).toContainText('通过')
  await integrated.locator('summary').click()
  await expect(integrated).toContainText('退出码：0')
  await page.getByText(/^全部文件 ·/).click()
  const file = page.locator('.cv-file').filter({ has: page.getByText('hello.py', { exact: true }) })
  await expect(file).toBeVisible()
  const downloading = page.waitForEvent('download')
  await file.getByRole('link', { name: '下载', exact: true }).click()
  const download = await downloading
  const output = info.outputPath('delivered-hello.py')
  await download.saveAs(output)
  expect(download.suggestedFilename()).toBe('hello.py')
  expect(await readFile(output, 'utf8')).toBe('print("工单服务已就绪")\n')
  await info.attach('downloaded synthetic CLI source', { path: output, contentType: 'text/plain' })
  await info.attach('new-task verification (synthetic provider)', {
    body: JSON.stringify({
      run_id: initial.id, project_id: initial.project_id,
      workspace_create_requests: workspaceCreates, run_create_requests: submissions.length,
      recovered_same_run: recoveredRun.id === initial.id, status: delivered?.status,
      spec_confirmation: delivered?.spec_confirmation,
      checks: delivered?.artifacts?.checks,
      acceptance_ledger: delivered?.artifacts?.acceptance_ledger,
      events, execution_authorization: authorization, execution_started: execution,
      functional_check: functionalCheck, scope_declaration: scope,
    }, null, 2),
    contentType: 'application/json',
  })
  await capture(page, info, 'homepage-created-delivery')
})

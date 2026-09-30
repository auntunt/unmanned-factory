import { expect, test, type Page, type TestInfo } from '@playwright/test'

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
  await expect(page.locator('.cv-verify-counts')).toBeVisible()
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

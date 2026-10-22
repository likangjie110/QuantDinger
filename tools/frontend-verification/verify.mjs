#!/usr/bin/env node
import { createHash } from 'node:crypto'
import { execFileSync, spawn } from 'node:child_process'
import { existsSync } from 'node:fs'
import { mkdir, readFile, writeFile } from 'node:fs/promises'
import { createServer } from 'node:net'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { parseArgs, routeFor, safePath, expectedApiError, readAllowed, outcome, sourceProvenance, readResult, STATES, VIEWPORTS } from './core.mjs'
import { MOCK_JOB_ID, mockConfig, mockResponse } from './fixtures.mjs'

const submittedConfig = { ...mockConfig, sourceId: 7, startDate: '2024-01-01', endDate: '2024-12-31' }

const help = `Usage: node verify.mjs --target main|standalone --source-revision <full-git-sha>
  --mode mock|integration [--base-url <http-origin>] [--source <checkout>]
  [--output-dir <directory>] [--browser-channel chromium|msedge|chrome]
  [--test-environment test|staging]

Mock uses the real application and intercepts API requests. Without --base-url it
starts the existing checkout's installed Vite (no dependency install or build).
Integration requires explicit --base-url and --test-environment. It is read only,
uses an isolated browser without personal sessions, and blocks API mutations.
Exit codes: 0 passed; 1 observed failure; 2 blocked/invalid invocation.
`
if (process.argv.includes('--help')) {
  process.stdout.write(help)
  process.exit(0)
}

let options
try {
  options = parseArgs(process.argv.slice(2))
} catch (error) {
  process.stderr.write(`${error.message}\n${help}`)
  process.exit(2)
}

const output = options.outputDir
await mkdir(output, { recursive: true })
const report = {
  schemaVersion: 1, target: options.target, sourceRevision: options.sourceRevision,
  mode: options.mode, route: routeFor(options.target), status: 'blocked',
  generatedAt: new Date().toISOString(), screenshots: [], checks: [],
  states: Object.fromEntries(STATES.map(state => [state, { status: 'blocked', reason: 'Not executed' }])),
  console: { status: 'blocked', path: 'console.json', errorCount: 0 },
  network: { status: 'blocked', path: 'network.json', unexpectedErrorCount: 0 },
  axe: { status: 'blocked', path: 'axe.json', criticalCount: 0 },
  integration: options.mode === 'mock' ? { status: 'unverified' } : { status: 'unverified', scope: 'read-only', readOnly: { status: 'blocked' } },
  deployment: { status: 'unverified' },
  limitations: [
    'This report proves only the named target and source revision; standalone evidence cannot validate the main product.',
    'Screenshots are evidence for human visual review; screenshot capture alone does not assert responsive layout quality.',
    'Axe checks are automated checks, not complete keyboard, focus, or accessibility verification.',
    options.mode === 'mock' ? 'Mock API responses never establish backend integration or deployment readiness.' : 'Read-only integration does not verify submission, mutation, trading, or five-state coverage.'
  ]
}
const consoleEntries = []
const networkEntries = []
const axeEntries = []
const blockedOperations = []
const unhandledMockApis = new Set()
let browser
let server
let baseUrl = options.baseUrl
let interrupted = false

// Release only this run's child process on interruption; never discover/kill peers.
for (const signal of ['SIGINT', 'SIGTERM']) {
  process.once(signal, () => {
    interrupted = true
    server?.kill()
    void browser?.close().catch(() => undefined)
  })
}

const writeJson = (filename, value) => writeFile(join(output, filename), `${JSON.stringify(value, null, 2)}\n`)
const addCheck = (id, status, detail) => report.checks.push({ id, status, detail })

function digest(value) {
  return createHash('sha256').update(String(value)).digest('hex')
}

async function unusedPort() {
  return new Promise((accept, reject) => {
    const socket = createServer()
    socket.once('error', reject)
    socket.listen(0, '127.0.0.1', () => {
      const port = socket.address().port
      socket.close(() => accept(port))
    })
  })
}

async function prepareApplication() {
  const defaultStandalone = resolve(dirname(fileURLToPath(import.meta.url)), '../../frontend-factor-mining')
  const source = options.source || (options.target === 'standalone' ? defaultStandalone : undefined)
  if (source) {
    const packageJson = JSON.parse(await readFile(join(source, 'package.json'), 'utf8'))
    const expectedName = options.target === 'main' ? 'quantdinger-vue' : 'quantdinger-factor-mining-ui'
    if (packageJson.name !== expectedName) throw new Error('SOURCE_TARGET_MISMATCH')
    const actualRevision = execFileSync('git', ['-C', source, 'rev-parse', 'HEAD'], { encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'] }).trim()
    if (actualRevision.toLowerCase() !== options.sourceRevision) throw new Error('SOURCE_REVISION_MISMATCH')
    const dirty = execFileSync('git', ['-C', source, 'status', '--porcelain', '--untracked-files=no', '--', '.'], { encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'] }).trim().length > 0
    report.source = { ...sourceProvenance(true, false), dirty }
    if (dirty) report.limitations.push('The checkout has tracked working-tree changes. sourceRevision names HEAD, not a clean immutable bundle.')
  } else {
    report.source = sourceProvenance(false, false)
    report.limitations.push('No source checkout supplied: sourceRevision is caller-declared and cannot be matched to the served bundle by this tool.')
  }
  if (baseUrl) {
    addCheck('source-provenance', 'blocked', 'Explicit base URL is not bound to the checked-out revision by independent bundle metadata')
    report.limitations.push('An existing service can serve an older bundle. Checking local HEAD does not establish the served bundle revision.')
    return
  }
  if (!source) throw new Error('SOURCE_REQUIRED_TO_START_APPLICATION')
  const vite = join(source, 'node_modules', 'vite', 'bin', 'vite.js')
  if (!existsSync(vite)) throw new Error('SOURCE_VITE_DEPENDENCY_MISSING')
  const port = await unusedPort()
  baseUrl = `http://127.0.0.1:${port}`
  server = spawn(process.execPath, [vite, '--host', '127.0.0.1', '--port', String(port), '--strictPort'], { cwd: source, stdio: 'ignore', windowsHide: true })
  let startupError = false
  server.on('error', () => { startupError = true })
  const deadline = Date.now() + 30000
  while (Date.now() < deadline) {
    if (interrupted) throw new Error('VERIFICATION_INTERRUPTED')
    if (startupError || server.exitCode !== null) throw new Error('APPLICATION_START_FAILED')
    try {
      const response = await fetch(baseUrl, { signal: AbortSignal.timeout(1000) })
      if (response.ok) {
        report.source = { ...report.source, ...sourceProvenance(true, true) }
        addCheck('source-provenance', 'passed', 'The harness started installed Vite from the verified source checkout and observed its own origin')
        return
      }
    } catch { /* Keep probing until the bounded startup deadline. */ }
    await new Promise(accept => setTimeout(accept, 200))
  }
  throw new Error('APPLICATION_START_TIMEOUT')
}

function attachEvidence(page, state, viewport) {
  let active = true
  page.on('close', () => { active = false })
  page.on('console', message => {
    if (!active) return
    const type = message.type()
    const path = safePath(message.location().url || '')
    const raw = message.text()
    const injectedStatus = raw.match(/^Failed to load resource: the server responded with a status of (403|500)/)?.[1]
    const expected = options.mode === 'mock' && injectedStatus && expectedApiError(state, path, Number(injectedStatus))
    consoleEntries.push({ state, viewport, type, path, expected: Boolean(expected), messageHash: digest(raw) })
  })
  page.on('pageerror', error => {
    if (active) consoleEntries.push({ state, viewport, type: 'pageerror', expected: false, messageHash: digest(error.message) })
  })
  page.on('response', response => {
    if (!active) return
    const pathname = new URL(response.url()).pathname
    const status = response.status()
    networkEntries.push({
      state, viewport, path: safePath(response.url()), method: response.request().method(), status,
      expected: options.mode === 'mock' && expectedApiError(state, pathname, status)
    })
  })
  page.on('requestfailed', request => {
    if (active) networkEntries.push({ state, viewport, path: safePath(request.url()), method: request.method(), status: 0, failed: true, expected: false })
  })
}

async function contextFor(state, viewport) {
  const context = await browser.newContext({ viewport, locale: 'zh-CN', serviceWorkers: 'block' })
  context.setDefaultTimeout(6000)
  context.setDefaultNavigationTimeout(20000)
  const control = { state, config: { ...mockConfig }, submitted: false, jobsRequests: 0, jobStatus: 'succeeded', releaseLoading: null }
  if (options.mode === 'mock' && options.target === 'main') {
    // Fictitious state is used only with intercepted APIs; never reuse a real account.
    await context.addInitScript(() => {
      localStorage.setItem('Access-Token', JSON.stringify('frontend-verification-fictitious-token'))
      localStorage.setItem('User-Info', JSON.stringify({ id: 0, username: 'frontend-verification', nickname: 'Verification', is_demo: true }))
      localStorage.setItem('User-Roles', JSON.stringify([{ id: 'default', permissionList: [] }]))
      localStorage.setItem('lang', JSON.stringify('zh-CN'))
    })
  }
  await context.route('**/*', async route => {
    const request = route.request()
    const url = new URL(request.url())
    if (url.origin !== new URL(baseUrl).origin) {
      blockedOperations.push({ state, type: 'external-resource', method: request.method() })
      await route.abort('blockedbyclient')
      return
    }
    if (!url.pathname.startsWith('/api/')) {
      if (['GET', 'HEAD'].includes(request.method())) return route.continue()
      blockedOperations.push({ state, type: 'non-api-mutation', method: request.method() })
      return route.abort('blockedbyclient')
    }
    if (options.mode === 'integration') {
      if (readAllowed(request.method(), url.pathname)) return route.continue()
      blockedOperations.push({ state, type: 'integration-api-denied', method: request.method(), path: safePath(request.url()) })
      return route.fulfill({ status: 403, json: { code: 0, msg: 'Verification is read only', data: null } })
    }
    if (url.pathname === '/api/factor-mining/jobs') {
      control.jobsRequests += 1
      if (state === 'loading') await new Promise(accept => { control.releaseLoading = accept })
    }
    if (url.pathname === '/api/factor-mining/run' && request.method() === 'POST') {
      control.config = request.postDataJSON() || {}
      control.submitted = true
      control.jobStatus = 'queued'
    }
    const response = mockResponse(url.pathname, state, control.config, control.jobStatus)
    if (response) return route.fulfill({ status: response.status, json: response.body })
    unhandledMockApis.add(safePath(request.url()))
    return route.fulfill({ status: 501, json: { code: 0, msg: 'No verification fixture for this endpoint', data: null } })
  })
  const page = await context.newPage()
  attachEvidence(page, state, viewport)
  return { context, page, control }
}

async function screenshot(page, state, viewport) {
  const path = `${state}-${viewport.width}x${viewport.height}.png`
  await page.screenshot({ path: join(output, path), fullPage: false, animations: 'disabled' })
  report.screenshots.push({ state, viewport, path })
}

async function visible(locator, timeout = 6000) {
  try { await locator.first().waitFor({ state: 'visible', timeout }); return true } catch { return false }
}

async function jobTab(page) {
  const named = page.getByRole('tab', { name: /作业列表|Jobs/i })
  if (!await visible(named)) return false
  await named.first().click()
  return true
}

async function targetReached(page) {
  if (options.target === 'main') {
    const correctHash = new URL(page.url()).hash.split('?')[0] === '#/factor-mining'
    return correctHash && visible(page.locator('.factor-mining.qd-workspace-page'))
  }
  return visible(page.locator('#app > .el-container h1').filter({ hasText: /因子挖掘|Factor/i }))
}

async function fillForm(page) {
  const form = page.locator('.submit-job')
  const source = form.locator('input[role="spinbutton"], .ant-input-number-input').first()
  await source.fill('7')
  await source.press('Tab')
  const dates = options.target === 'main'
    ? form.locator('.ant-calendar-picker input')
    : form.locator('.el-date-editor input')
  await dates.nth(0).fill(submittedConfig.startDate)
  await dates.nth(0).press('Enter')
  await dates.nth(0).press('Tab')
  await dates.nth(1).fill(submittedConfig.endDate)
  await dates.nth(1).press('Enter')
  await dates.nth(1).press('Tab')
  await form.getByRole('button', { name: /^估价$|^Estimate$/i }).click()
  return visible(form.getByRole('button', { name: /^提交作业$|^Submit( job)?$/i }).and(page.locator(':enabled')))
}

async function runAxe(page, state, viewport) {
  try {
    const { default: AxeBuilder } = await import('@axe-core/playwright')
    const result = await new AxeBuilder({ page }).analyze()
    const violations = result.violations.map(violation => ({
      id: violation.id, impact: violation.impact, helpUrl: violation.helpUrl,
      nodeCount: violation.nodes.length
    }))
    axeEntries.push({ state, viewport, status: 'passed', violations, incompleteCount: result.incomplete.length })
  } catch {
    axeEntries.push({ state, viewport, status: 'blocked', reason: 'AXE_DEPENDENCY_OR_ANALYSIS_UNAVAILABLE' })
  }
}

async function mockScenario(state, viewport) {
  const { context, page, control } = await contextFor(state, viewport)
  let passed = false
  let reason = 'Expected UI state was not observed'
  try {
    await page.goto(`${baseUrl}${report.route}`, { waitUntil: 'domcontentloaded' })
    if (!await targetReached(page)) {
      reason = 'Target factor-mining page was not reached (authentication, route, or bundle mismatch)'
    } else if (!await jobTab(page)) {
      reason = 'Jobs tab was not found in the actual application'
    } else {
      if (state === 'loading') {
        passed = await visible(page.locator('.el-loading-mask, .ant-spin-spinning'))
      } else if (state === 'empty') {
        passed = await visible(page.getByText(/暂无作业记录|暂无.*作业|No jobs|No mining jobs/i))
      } else if (state === 'error') {
        passed = await visible(page.locator('.el-message--error, .ant-message-error, .ant-notification-notice-error'))
        const text = await page.locator('.el-message--error, .ant-message-error, .ant-notification-notice-error').first().innerText().catch(() => '')
        addCheck(`error-readable-${viewport.width}`, text && !text.includes('factorMining.errors.') ? 'passed' : 'failed', 'Errors must not expose a raw factorMining.errors translation key')
      } else if (state === 'unauthorized') {
        const notice = page.locator('.el-message--error, .ant-message-error, .ant-notification-notice-error')
        await visible(notice)
        const text = await notice.first().innerText().catch(() => '')
        passed = /无权限|没有权限|权限不足|禁止访问|不允许|Forbidden|permission denied|Operation not allowed/i.test(text)
        reason = '403 response did not produce a clear permission-denied state in the actual UI'
      } else {
        passed = await visible(page.getByText(MOCK_JOB_ID, { exact: true }))
        if (passed) {
          const detail = page.getByRole('button', { name: /查看详情/ }).or(page.getByText(/^查看$|^View$/))
          await detail.first().click()
          passed = await visible(page.getByRole('dialog').getByText(mockConfig.startDate, { exact: false }))
          addCheck(`job-detail-${viewport.width}`, passed ? 'passed' : 'failed', 'Open the actual job detail and display fixture configuration')
          const close = page.locator('.el-dialog__headerbtn, .ant-modal-close')
          await close.first().click()
        }
      }
    }
    await screenshot(page, state, viewport)
    if (state !== 'loading') await runAxe(page, state, viewport)
    addCheck(`${state}-${viewport.width}`, passed ? 'passed' : 'failed', passed ? 'Expected state observed in the actual application' : reason)
    if (state === 'success' && passed) await mockSubmit(page, control, viewport)
  } catch {
    addCheck(`${state}-${viewport.width}`, 'blocked', 'Browser automation could not complete; inspect captured console/network evidence before attributing a UI defect')
    try { await screenshot(page, `${state}-failure`, viewport) } catch { /* Preserve other available evidence. */ }
  } finally {
    control.releaseLoading?.()
    await context.close()
  }
}

async function mockSubmit(page, control, viewport) {
  try {
    await page.getByRole('tab', { name: /^提交作业$|^Submit job$/i }).click()
    const ready = await fillForm(page)
    if (!ready) throw new Error('MOCK_ESTIMATE_NOT_READY')
    const [receiptResponse] = await Promise.all([
      page.waitForResponse(response => new URL(response.url()).pathname === '/api/factor-mining/run' && response.request().method() === 'POST', { timeout: 6000 }),
      page.locator('.submit-job').getByRole('button', { name: /^提交作业$|^Submit( job)?$/i }).click()
    ])
    addCheck(`mock-submit-http-${viewport.width}`, receiptResponse.status() === 202 ? 'passed' : 'failed', 'The mocked run receipt must preserve the backend HTTP 202 contract')
    const expectedConfig = submittedConfig
    const configMatches = ['sourceId', 'market', 'timeframe', 'startDate', 'endDate'].every(key => control.config[key] === expectedConfig[key])
    const receipt = await visible(page.getByText(/作业已提交|提交成功|Job submitted/i))
    const submitPassed = control.submitted && configMatches && receipt
    addCheck(`mock-submit-${viewport.width}`, submitPassed ? 'passed' : 'failed', 'UI estimate and mock submission must preserve the displayed request fields and show a receipt')
    await jobTab(page)
    await Promise.all([
      page.waitForResponse(response => new URL(response.url()).pathname === '/api/factor-mining/jobs', { timeout: 6000 }),
      page.getByRole('button', { name: /^刷新$|^Refresh$/i }).first().click()
    ])
    const listed = await visible(page.getByText(MOCK_JOB_ID, { exact: true }))
    addCheck(`submitted-job-visible-${viewport.width}`, listed ? 'passed' : 'failed', 'The submitted fixture job is visible after the actual list refresh')
    const requestCount = control.jobsRequests
    control.jobStatus = 'running'
    const running = await visible(page.getByText(/^运行中$|^Running$/i), 8000)
    addCheck(`job-polling-${viewport.width}`, running && control.jobsRequests > requestCount ? 'passed' : 'failed', 'The actual page must poll the queued mock job and display running status')
    control.jobStatus = 'succeeded'
    const succeeded = await visible(page.getByText(/^成功$|^Succeeded$/i), 8000)
    addCheck(`job-completed-${viewport.width}`, succeeded ? 'passed' : 'failed', 'The actual page must poll the running mock job and display its terminal succeeded result')
    await screenshot(page, 'mock-submitted', viewport)
    const reloadRequests = control.jobsRequests
    await page.reload({ waitUntil: 'domcontentloaded' })
    await jobTab(page)
    const reloaded = await visible(page.getByText(MOCK_JOB_ID, { exact: true }))
    addCheck(`mock-refresh-${viewport.width}`, reloaded && control.jobsRequests > reloadRequests ? 'passed' : 'failed', 'Reload the actual page and fetch the same submitted mock job from the retained in-memory fixture (not durable persistence)')
    const detail = page.getByRole('button', { name: /查看详情/ }).or(page.getByText(/^查看$|^View$/))
    await detail.first().click()
    const shownConfig = await visible(page.getByRole('dialog').getByText(expectedConfig.startDate, { exact: false }))
    addCheck(`mock-refresh-detail-${viewport.width}`, shownConfig ? 'passed' : 'failed', 'After reload, job detail must display the changed date configuration entered in this mock submission')
    await screenshot(page, 'mock-refreshed-detail', viewport)
  } catch {
    addCheck(`mock-submit-${viewport.width}`, 'blocked', 'Browser automation could not complete the actual form flow; no real backend request was sent')
  }
}

async function integrationRead(page, viewport, phase) {
  const prefix = `integration-${phase}-${viewport.width}`
  const [response] = await Promise.all([
    page.waitForResponse(response => new URL(response.url()).pathname === '/api/factor-mining/jobs', { timeout: 6000 }),
    page.getByRole('button', { name: /^刷新$|^Refresh$/i }).first().click()
  ])
  let body
  try { body = await response.json() } catch { /* A non-JSON response is a failed contract assertion. */ }
  const result = readResult(body)
  const validEnvelope = response.status() === 200 && result.status === 'passed'
  addCheck(`${prefix}-envelope`, validEnvelope ? 'passed' : 'failed', 'A real jobs read must return HTTP 200 and code=1 with data.items array')
  if (!validEnvelope) return null
  const items = body.data.items
  const uiVisible = items.length
    ? await visible(page.locator('.job-list').getByText(String(items[0].jobId), { exact: true }))
    : await visible(page.locator('.job-list .el-empty, .job-list .ant-empty'))
  addCheck(`${prefix}-ui-result`, uiVisible ? 'passed' : 'failed', 'The actual jobs UI must display the returned record or an explicit empty result')
  return items.map(item => String(item.jobId))
}

async function integrationScenario(viewport) {
  const { context, page } = await contextFor('integration', viewport)
  try {
    await page.goto(`${baseUrl}${report.route}`, { waitUntil: 'domcontentloaded' })
    const reached = await targetReached(page)
    addCheck(`integration-route-${viewport.width}`, reached ? 'passed' : 'blocked', reached ? 'Actual factor-mining page reached' : 'Isolated browser did not reach the authenticated factor-mining page')
    if (reached) {
      const shown = await jobTab(page)
      addCheck(`integration-job-list-${viewport.width}`, shown ? 'passed' : 'failed', 'Read-only actual job-list navigation')
      const first = await integrationRead(page, viewport, 'initial')
      await page.reload({ waitUntil: 'domcontentloaded' })
      const reloaded = await targetReached(page) && await jobTab(page)
      const second = reloaded ? await integrationRead(page, viewport, 'reload') : null
      const retainedResult = first && second && first.every(id => second.includes(id))
      addCheck(`integration-reload-consistency-${viewport.width}`, retainedResult ? 'passed' : 'failed', 'Reload the test page and verify previously returned jobs remain represented in the real read result')
      await runAxe(page, 'integration', viewport)
    }
    await screenshot(page, 'integration', viewport)
  } catch {
    addCheck(`integration-${viewport.width}`, 'blocked', 'Test application could not be reached or inspected')
  } finally {
    await context.close()
  }
}

try {
  await prepareApplication()
  if (interrupted) throw new Error('VERIFICATION_INTERRUPTED')
  const { chromium } = await import('playwright')
  browser = await chromium.launch({ headless: true, ...(options.browserChannel === 'chromium' ? {} : { channel: options.browserChannel }) })
  for (const viewport of VIEWPORTS) {
    if (interrupted) throw new Error('VERIFICATION_INTERRUPTED')
    if (options.mode === 'mock') {
      for (const state of STATES) await mockScenario(state, viewport)
    } else {
      await integrationScenario(viewport)
    }
  }
  report.console.errorCount = consoleEntries.filter(entry => ['error', 'pageerror'].includes(entry.type) && !entry.expected).length
  report.console.status = report.console.errorCount ? 'failed' : 'passed'
  report.network.unexpectedErrorCount = networkEntries.filter(entry => (entry.failed || entry.status >= 400) && !entry.expected).length
  report.network.status = report.network.unexpectedErrorCount ? 'failed' : 'passed'
  report.axe.criticalCount = axeEntries.reduce((count, entry) => count + (entry.violations || []).filter(violation => violation.impact === 'critical').length, 0)
  report.axe.status = report.axe.criticalCount ? 'failed' : outcome(axeEntries.map(entry => entry.status))
  for (const state of STATES) {
    const checks = report.checks.filter(check => check.id.startsWith(`${state}-`))
    report.states[state] = { status: outcome(checks.map(check => check.status)), checks: checks.map(check => check.id) }
  }
  if (unhandledMockApis.size) {
    addCheck('mock-api-fixtures', 'failed', 'Actual application requested an API without a fixture; see network evidence')
    report.limitations.push('Some requested APIs have no fixture; their 501 response is a failed scope coverage check.')
  }
  if (blockedOperations.length) {
    addCheck('network-boundary', 'failed', 'The application attempted a disallowed request; the harness blocked it')
  }
  const allStatuses = [...report.checks.map(check => check.status), report.console.status, report.network.status, report.axe.status]
  report.status = outcome(allStatuses)
  if (options.mode === 'integration') report.integration.readOnly.status = report.status
} catch (error) {
  const safeReasons = new Set(['SOURCE_TARGET_MISMATCH', 'SOURCE_REVISION_MISMATCH', 'SOURCE_REQUIRED_TO_START_APPLICATION', 'SOURCE_VITE_DEPENDENCY_MISSING', 'APPLICATION_START_FAILED', 'APPLICATION_START_TIMEOUT', 'VERIFICATION_INTERRUPTED'])
  report.limitations.push(safeReasons.has(error.message) ? error.message : 'VERIFICATION_DEPENDENCY_OR_APPLICATION_UNAVAILABLE')
} finally {
  await browser?.close()
  server?.kill()
  await writeJson('console.json', { schemaVersion: 1, entries: consoleEntries })
  await writeJson('network.json', { schemaVersion: 1, entries: networkEntries, blockedOperations })
  await writeJson('axe.json', { schemaVersion: 1, status: report.axe.status, entries: axeEntries })
  await writeJson('browser-report.json', report)
}
process.stdout.write(`${JSON.stringify({ status: report.status, target: report.target, mode: report.mode, report: 'browser-report.json', screenshotCount: report.screenshots.length, failedChecks: report.checks.filter(check => check.status === 'failed').map(check => check.id) })}\n`)
process.exitCode = report.status === 'passed' ? 0 : report.status === 'failed' ? 1 : 2

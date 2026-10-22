#!/usr/bin/env node
import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { mkdir, readFile, writeFile } from 'node:fs/promises'
import { createServer } from 'node:net'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { AI_JOB_ID, createP2Fixture, shellResponse } from './p2-fixtures.mjs'
import { digest, inspectSource, isP2Target, requestAllowed, safeP2Path, sourceProvenance, validateTestBackend } from './p2-support.mjs'
import { VIEWPORTS } from './core.mjs'

const root = dirname(fileURLToPath(import.meta.url))
const args = process.argv.slice(2)
if (args.includes('--help')) {
  process.stdout.write('node tools/frontend-verification/verify-p2.mjs --source frontend-vue --mode mock|test-backend --ui-ready true [--backend-url http://127.0.0.1:PORT] [--browser-channel msedge] [--output-dir tools/frontend-verification/artifacts/p2]\n')
  process.exit(0)
}
const options = {}
for (let index = 0; index < args.length; index += 2) {
  const key = args[index].replace(/^--/, '')
  if (!['source', 'mode', 'ui-ready', 'backend-url', 'browser-channel', 'output-dir'].includes(key) || !args[index + 1]) throw new Error('INVALID_ARGUMENTS')
  options[key] = args[index + 1]
}
if (options['ui-ready'] !== 'true') throw new Error('UI_OWNER_HANDOFF_REQUIRED')
if (!['mock', 'test-backend'].includes(options.mode)) throw new Error('EXPLICIT_MODE_REQUIRED')
if (options.mode === 'mock' && options['backend-url']) throw new Error('MOCK_MUST_NOT_HAVE_A_BACKEND')
if (options.mode === 'test-backend' && !options['backend-url']) throw new Error('TEST_BACKEND_URL_REQUIRED')
const source = resolve(options.source || join(root, '../../frontend-vue'))
const output = resolve(options['output-dir'] || join(root, `artifacts/p2-${options.mode}`))
if (!output.startsWith(`${root}\\artifacts\\`) && !output.startsWith(`${root}/artifacts/`)) throw new Error('OUTPUT_MUST_STAY_IN_TOOL_ARTIFACTS')
await mkdir(output, { recursive: true })
const report = {
  schemaVersion: 2, target: 'main', route: '/#/factor-mining', feature: 'P2', mode: options.mode,
  status: 'blocked', startedAt: new Date().toISOString(), checks: [], screenshots: [], shellApiMode: 'fixture',
  integration: { status: 'unverified', scope: options.mode === 'mock' ? 'mock' : 'isolated-fixture-backend' },
  businessPostgres: { status: 'unverified' }, realModel: { status: 'not-executed' }, deployment: { status: 'unverified' },
  limitations: [
    'The actual main SPA is served from the recorded working tree. An uncommitted tree is not an immutable HEAD bundle.',
    'Mock submissions and reloads prove retained fixture state, not business PostgreSQL durability or worker execution.',
    'Screenshots require visual review. Automated axe checks do not replace a complete accessibility audit.'
  ]
}
let browser
let server
let before
let baseUrl
let scenarioId = 'setup'
let backendUrl
const logs = { console: [], network: [], axe: [], blocked: [], api: [] }
const add = (id, status = 'passed', detail = '') => {
  report.checks.push({ id, status, detail })
  process.stdout.write(`${status}: ${id}${status === 'failed' ? ` ${detail.split('\n')[0]}` : ''}\n`)
}
const json = (name, body) => writeFile(join(output, name), JSON.stringify(body, null, 2) + '\n')
const cssInput = (page, id) => page.locator(`input#${id}, #${id} input`)
const labels = {
  jobs: /^(作业列表|Jobs)$/,
  estimate: /^(估价|Estimate)$/,
  submit: /^(提交作业|Submit job)$/
}

async function waitFor(check, code, timeout = 10000) {
  const end = Date.now() + timeout
  while (Date.now() < end) {
    if (await check()) return
    await new Promise(accept => setTimeout(accept, 100))
  }
  throw new Error(code)
}

async function portNumber() {
  return new Promise((accept, reject) => {
    const socket = createServer()
    socket.once('error', reject)
    socket.listen(0, '127.0.0.1', () => { const port = socket.address().port; socket.close(() => accept(port)) })
  })
}

async function startApp() {
  const pkg = JSON.parse(await readFile(join(source, 'package.json'), 'utf8'))
  assert.equal(pkg.name, 'quantdinger-vue', 'SOURCE_TARGET_MISMATCH')
  before = await inspectSource(source)
  report.source = sourceProvenance({ ...before, autoStarted: false })
  report.source.runtimeFileCount = before.runtimeFileCount
  const port = await portNumber()
  baseUrl = `http://127.0.0.1:${port}`
  const cache = join(output, 'vite-cache')
  server = spawn(process.execPath, [join(root, 'p2-vite-server.mjs'), source, cache, String(port)], { cwd: root, stdio: ['ignore', 'ignore', 'pipe'], windowsHide: true })
  let startError = false
  const serverErrors = []
  server.on('error', () => { startError = true })
  server.stderr.on('data', chunk => serverErrors.push(digest(chunk)))
  const end = Date.now() + 30000
  while (Date.now() < end) {
    if (startError || server.exitCode !== null) throw new Error('APPLICATION_START_FAILED')
    try {
      const response = await fetch(baseUrl, { signal: AbortSignal.timeout(1500) })
      if (response.ok) {
        report.source = { ...report.source, ...sourceProvenance({ ...before, autoStarted: true }) }
        add('source-worktree-served', 'passed', 'Runner started the installed Vite against the recorded source tree, with cache inside tools')
        report.serverStderrHashes = serverErrors
        return
      }
    } catch { /* Bounded readiness polling. */ }
    await new Promise(accept => setTimeout(accept, 150))
  }
  throw new Error('APPLICATION_START_TIMEOUT')
}

async function screenshot(page, state, viewport) {
  const capture = async name => {
    const path = `${name}-${viewport.width}x${viewport.height}.png`
    await page.screenshot({ path: join(output, path), fullPage: true, animations: 'disabled' })
    report.screenshots.push({ state: name, viewport, path })
  }
  if (state === 'evolution-input') {
    await page.locator('#fm-population').scrollIntoViewIfNeeded()
    await capture('evolution-parameters')
    await page.locator('#fm-holdout-ratio').scrollIntoViewIfNeeded()
  }
  if (state === 'evolution-research') await page.locator('#fm-research-summary').scrollIntoViewIfNeeded()
  await capture(state)
  if (state === 'replay-preview') {
    await page.locator('#fm-submit-replay').scrollIntoViewIfNeeded()
    await capture('replay-submit')
  }
}

async function axe(page, state, viewport) {
  await page.waitForLoadState('networkidle')
  const { default: AxeBuilder } = await import('@axe-core/playwright')
  const result = await new AxeBuilder({ page }).analyze()
  const violations = result.violations.map(row => ({
    id: row.id, impact: row.impact, helpUrl: row.helpUrl, nodeCount: row.nodes.length,
    targets: row.nodes.map(node => node.target),
    failures: row.nodes.map(node => ({ target: node.target, summary: node.failureSummary })),
    p2Controls: row.nodes.filter(node => isP2Target(node.target)).length
  }))
  logs.axe.push({ state, viewport, violations })
  const critical = violations.filter(row => row.impact === 'critical').length
  const p2Serious = violations.filter(row => ['critical', 'serious'].includes(row.impact) && row.p2Controls > 0).length
  add(`${state}-axe-${viewport.width}`, critical || p2Serious ? 'failed' : 'passed', `${violations.length} violations; ${critical} critical; ${p2Serious} serious/critical violations targeting P2 controls`)
}

async function measureLayout(page, state, viewport) {
  const measurements = await page.evaluate(() => {
    const dialog = document.querySelector('.ant-modal-content')
    return { viewport: innerWidth, documentWidth: document.documentElement.scrollWidth, dialogWidth: dialog?.getBoundingClientRect().width || null }
  })
  add(`${state}-responsive-${viewport.width}`, measurements.documentWidth <= measurements.viewport + 2 && (!measurements.dialogWidth || measurements.dialogWidth <= measurements.viewport) ? 'passed' : 'failed', JSON.stringify(measurements))
}

async function contextFor(viewport) {
  const fixture = createP2Fixture()
  const requests = []
  const context = await browser.newContext({ viewport, locale: 'zh-CN', serviceWorkers: 'block' })
  context.setDefaultTimeout(10000)
  context.setDefaultNavigationTimeout(30000)
  await context.addInitScript(() => {
    if (!['http:', 'https:'].includes(location.protocol)) return
    localStorage.setItem('Access-Token', JSON.stringify('p2-fixture-only-no-business-auth'))
    localStorage.setItem('User-Info', JSON.stringify({ id: 0, username: 'p2-verification', nickname: 'P2 Fixture', is_demo: true }))
    localStorage.setItem('User-Roles', JSON.stringify([{ id: 'default', permissionList: [] }]))
    localStorage.setItem('lang', JSON.stringify('zh-CN'))
  })
  await context.route('**/*', async route => {
    const request = route.request()
    const url = new URL(request.url())
    const method = request.method()
    if (url.origin !== baseUrl || (!url.pathname.startsWith('/api/') && !['GET', 'HEAD'].includes(method))) {
      logs.blocked.push({ scenarioId, path: safeP2Path(url.href), method, resourceType: request.resourceType(), hostHash: digest(url.hostname), kind: url.origin !== baseUrl ? 'external' : 'operation' })
      return route.abort('blockedbyclient')
    }
    if (!url.pathname.startsWith('/api/')) return route.continue()
    if (!requestAllowed(method, url.pathname)) {
      logs.blocked.push({ scenarioId, path: safeP2Path(url.href), operation: /^\/api\/[a-zA-Z/-]+$/.test(url.pathname) ? url.pathname : '[redacted]', method, kind: 'unhandled-api' })
      return route.fulfill({ status: 501, json: { code: 0, msg: 'No P2 fixture', data: null } })
    }
    const payload = method === 'POST' ? request.postDataJSON() : undefined
    const row = { scenarioId, method, path: url.pathname, query: Object.fromEntries(url.searchParams), payload }
    requests.push(row)
    const shell = shellResponse(method, url.pathname)
    if (shell) {
      row.response = shell.body
      return route.fulfill({ status: shell.status, json: shell.body })
    }
    if (options.mode === 'mock') {
      const response = fixture.respond(method, url.pathname + url.search, payload)
      row.response = response?.body
      if (!response) throw new Error('UNHANDLED_P2_API')
      return route.fulfill({ status: response.status, json: response.body })
    }
    // Drop all browser authentication before contacting the explicitly isolated test service.
    const response = await fetch(new URL(url.pathname + url.search, backendUrl), {
      method, headers: method === 'POST' ? { 'content-type': 'application/json' } : {},
      body: method === 'POST' ? JSON.stringify(payload) : undefined,
      signal: AbortSignal.timeout(15000), redirect: 'error'
    })
    const body = await response.text()
    try { row.response = JSON.parse(body) } catch { /* The UI will report the invalid response. */ }
    return route.fulfill({ status: response.status, contentType: response.headers.get('content-type') || 'application/json', body })
  })
  const page = await context.newPage()
  page.on('console', message => logs.console.push({ scenarioId, viewport, type: message.type(), path: safeP2Path(message.location().url || '/'), messageHash: digest(message.text()), message: message.text().replace(/https?:\/\/[^\s"')]+/g, '[url]').replaceAll(source, '[source]').slice(0, 800) }))
  page.on('pageerror', error => logs.console.push({ scenarioId, viewport, type: 'pageerror', messageHash: digest(error.message), message: error.message.replace(/https?:\/\/[^\s"')]+/g, '[url]').replaceAll(source, '[source]').slice(0, 800) }))
  page.on('response', response => logs.network.push({ scenarioId, viewport, method: response.request().method(), path: safeP2Path(response.url()), status: response.status() }))
  page.on('requestfailed', request => logs.network.push({ scenarioId, viewport, method: request.method(), path: safeP2Path(request.url()), status: 0, failed: true }))
  return { context, page, requests }
}

async function goto(page, query = '') {
  await page.goto('about:blank')
  await page.goto(`${baseUrl}/#/factor-mining${query}`, { waitUntil: 'domcontentloaded' })
  await page.locator('.factor-mining.qd-workspace-page').waitFor({ state: 'visible' })
  await closeNavigation(page)
}

async function closeNavigation(page) {
  const mask = page.locator('.ant-pro-sider-menu .ant-drawer-open .ant-drawer-mask')
  if (await mask.isVisible()) {
    await mask.click({ position: { x: (await mask.boundingBox()).width - 8, y: 100 } })
    await mask.waitFor({ state: 'hidden' })
  }
}

async function reload(page) {
  await page.reload({ waitUntil: 'domcontentloaded' })
  await page.locator('.factor-mining.qd-workspace-page').waitFor({ state: 'visible' })
  await closeNavigation(page)
}

async function tab(page, name) {
  await page.getByRole('tab', { name }).click()
}

async function selectMode(page, label) {
  await page.locator('#fm-search-mode').click()
  await page.locator('.ant-select-dropdown:visible').getByRole('option', { name: label, exact: true }).click()
}

async function setDatesAndSource(page) {
  const form = page.locator('.submit-job')
  await form.getByPlaceholder(/^(portfolio 策略源 ID|Portfolio script source id)$/).fill('7')
  const dates = form.locator('.ant-calendar-picker input')
  for (const [index, value] of ['2024-01-01', '2024-12-31'].entries()) {
    await dates.nth(index).click()
    const placeholder = await dates.nth(index).getAttribute('placeholder')
    const popupInput = page.locator(`input.ant-calendar-input[placeholder="${placeholder}"]:visible`)
    await popupInput.fill(value)
    await popupInput.press('Enter')
    await popupInput.waitFor({ state: 'hidden' })
    assert.equal(await dates.nth(index).inputValue(), value)
  }
}

async function openJob(page, jobId) {
  const row = page.locator('.job-list tbody tr').filter({ hasText: jobId })
  await row.waitFor({ state: 'visible' })
  await row.getByText(/^(查看|View)$/).click()
  const dialog = page.getByRole('dialog')
  await dialog.getByText(jobId, { exact: true }).waitFor({ state: 'visible' })
  return dialog
}

async function researchVisible(dialog, research) {
  const summary = dialog.locator('#fm-research-summary')
  await summary.waitFor({ state: 'visible' })
  for (const label of [/^(研究组|Research group)$/, /^(本次试验数|Trials in this run)$/, /^(累计试验数|Cumulative trials)$/, /^(上下文指纹|Context fingerprint)$/, /^(冻结版本|Freeze version)$/, /^(冻结数据指纹|Frozen data fingerprint)$/, /^(保留段状态|Holdout status)$/, /^(保留段评估|Holdout evaluation)$/, /^(保留段区间|Holdout window)$/]) {
    await summary.getByText(label).waitFor({ state: 'visible' })
  }
  for (const value of [research.contextHash, research.freezeSnapshotId, research.freezeVersion]) {
    await summary.getByText(String(value), { exact: true }).waitFor({ state: 'visible' })
  }
  const text = await summary.innerText()
  assert.match(text, /已预留|\bReserved\b/)
  assert.match(text, /未评估|\bNot evaluated\b/)
  for (const value of [research.experimentId, research.runTrials, research.cumulativeTrials, research.holdout.startDate, research.holdout.endDate]) assert.ok(text.includes(String(value)), 'RESEARCH_VALUE_NOT_RENDERED')
}

async function evolution(page, requests, viewport) {
  scenarioId = `evolution-${viewport.width}`
  await goto(page)
  await selectMode(page, /^(进化搜索|Evolution search)$/)
  for (const [id, value] of [['fm-population', '32'], ['fm-generations', '20'], ['fm-elite-ratio', '0.25']]) assert.equal(await cssInput(page, id).inputValue(), value)
  add(`${scenarioId}-default-parameters`)
  await setDatesAndSource(page)
  await page.locator('.ant-collapse-header').filter({ hasText: /高级选项|Advanced/ }).click()
  assert.equal(Number(await cssInput(page, 'fm-holdout-ratio').inputValue()), 0.1)
  for (const id of ['fm-population', 'fm-generations', 'fm-elite-ratio', 'fm-holdout-ratio']) {
    assert.ok(await cssInput(page, id).evaluate(node => node.labels?.length > 0 || Boolean(node.getAttribute('aria-label'))), `P2_LABEL_MISSING_${id}`)
  }
  add(`${scenarioId}-labels`)
  await cssInput(page, 'fm-holdout-ratio').fill('0.2')
  await cssInput(page, 'fm-holdout-ratio').press('Tab')
  add(`${scenarioId}-keyboard-focus`, await page.evaluate(() => document.activeElement?.tagName !== 'BODY') ? 'passed' : 'failed')
  await screenshot(page, 'evolution-input', viewport)
  await axe(page, 'evolution-input', viewport)
  await measureLayout(page, 'evolution-input', viewport)
  const form = page.locator('.submit-job')
  await form.getByRole('button', { name: labels.estimate }).click()
  const submit = form.getByRole('button', { name: labels.submit })
  await waitFor(() => submit.isEnabled(), 'EVOLUTION_ESTIMATE_FAILED')
  const estimate = requests.filter(row => row.path === '/api/factor-mining/estimate').at(-1)
  assert.equal(estimate.payload.holdoutRatio, 0.2)
  assert.equal(estimate.payload.searchMode, 'evolution')
  await submit.click()
  await waitFor(() => requests.some(row => row.path === '/api/factor-mining/run'), 'EVOLUTION_NOT_SUBMITTED')
  const submitted = requests.filter(row => row.path === '/api/factor-mining/run').at(-1)
  for (const [key, value] of Object.entries({ sourceId: 7, searchMode: 'evolution', population: 32, generations: 20, eliteRatio: 0.25, holdoutRatio: 0.2, startDate: '2024-01-01', endDate: '2024-12-31' })) assert.equal(submitted.payload[key], value, `PAYLOAD_${key}`)
  for (const key of ['aiRounds', 'aiProposalsPerRound', 'researchNotes', 'replayJobId']) assert.equal(key in submitted.payload, false, `INACTIVE_${key}`)
  add(`${scenarioId}-payload`, 'passed', 'Actual DOM form submitted the evolution defaults and changed holdout; inactive AI fields absent')
  await waitFor(() => Boolean(submitted.response?.data?.jobId), 'SUBMISSION_RECEIPT_MISSING')
  const jobId = submitted.response.data.jobId
  let dialog = await openJob(page, jobId)
  const detail = requests.filter(row => row.path === `/api/factor-mining/jobs/${jobId}`).at(-1).response.data
  await researchVisible(dialog, detail.result.summary.research)
  add(`${scenarioId}-research-summary`)
  await screenshot(page, 'evolution-research', viewport)
  await axe(page, 'evolution-research', viewport)
  await measureLayout(page, 'evolution-research', viewport)
  await dialog.getByRole('button', { name: 'Close', exact: true }).click()
  await reload(page)
  await page.locator('.job-list').waitFor({ state: 'visible' })
  dialog = await openJob(page, jobId)
  await researchVisible(dialog, detail.result.summary.research)
  add(`${scenarioId}-result-after-reload`, 'passed', 'Fresh read returned the same submitted fixture job and research evidence')
  await dialog.locator('#fm-view-candidates').click()
  await assertCandidateFilter(page, requests, detail.result.runId, viewport, 'submitted-candidates')
}

async function assertCandidateFilter(page, requests, runId, viewport, state) {
  await waitFor(() => page.url().includes(`runId=${runId}`) && page.url().includes('tab=candidates'), 'URL_FILTER_NOT_SET')
  await waitFor(() => Boolean(requests.filter(row => row.path === '/api/factor-mining/candidates').at(-1)?.response?.data?.items?.length), 'FILTERED_CANDIDATES_MISSING')
  const candidateRead = requests.filter(row => row.path === '/api/factor-mining/candidates').at(-1)
  assert.equal(candidateRead.query.runId, String(runId))
  assert.ok(candidateRead.response.data.items.every(row => row.runId === runId))
  for (const candidate of candidateRead.response.data.items) await page.locator(`.candidate-list tbody tr[data-row-key="${candidate.id}"]`).waitFor({ state: 'visible' })
  const count = requests.filter(row => row.path === '/api/factor-mining/candidates').length
  await reload(page)
  await page.locator('.candidate-list').waitFor({ state: 'visible' })
  await waitFor(() => requests.filter(row => row.path === '/api/factor-mining/candidates').length > count, 'CANDIDATE_RELOAD_DID_NOT_READ')
  assert.equal(requests.filter(row => row.path === '/api/factor-mining/candidates').at(-1).query.runId, String(runId))
  assert.equal(await cssInput(page, 'fm-candidate-run-id').inputValue(), String(runId))
  for (const candidate of candidateRead.response.data.items) await page.locator(`.candidate-list tbody tr[data-row-key="${candidate.id}"]`).waitFor({ state: 'visible' })
  await screenshot(page, state, viewport)
  await axe(page, state, viewport)
  await measureLayout(page, state, viewport)
  add(`${state}-url-filter-reload-${viewport.width}`, 'passed', 'The candidate API was re-read with the same numeric run filter after a full page reload')
}

async function replay(page, requests, viewport) {
  scenarioId = `replay-${viewport.width}`
  await goto(page, '?tab=jobs')
  let dialog = await openJob(page, AI_JOB_ID)
  await dialog.getByText('Fixture proposal only', { exact: true }).waitFor({ state: 'visible' })
  const detail = requests.filter(row => row.path === `/api/factor-mining/jobs/${AI_JOB_ID}`).at(-1).response.data
  await researchVisible(dialog, detail.result.summary.research)
  await dialog.locator('#fm-replay-job').click()
  const replayForm = page.locator('.fm-replay')
  await replayForm.waitFor({ state: 'visible' })
  assert.equal(await page.locator('.submit-job form').count(), 0)
  assert.equal(await replayForm.getByRole('button', { name: labels.estimate }).count(), 0)
  await screenshot(page, 'replay-preview', viewport)
  await axe(page, 'replay-preview', viewport)
  await measureLayout(page, 'replay-preview', viewport)
  await reload(page)
  await page.locator('.submit-job form').waitFor({ state: 'visible' })
  assert.equal(await page.locator('.fm-replay').count(), 0)
  add(`${scenarioId}-transient-selection-reset`)
  await tab(page, labels.jobs)
  dialog = await openJob(page, AI_JOB_ID)
  await dialog.locator('#fm-replay-job').click()
  const estimateCount = requests.filter(row => row.path === '/api/factor-mining/estimate').length
  const submitCount = requests.filter(row => row.path === '/api/factor-mining/run').length
  await page.locator('#fm-submit-replay').click()
  await waitFor(() => requests.filter(row => row.path === '/api/factor-mining/run').length > submitCount, 'REPLAY_NOT_SUBMITTED')
  const request = requests.filter(row => row.path === '/api/factor-mining/run').at(-1)
  assert.deepEqual(request.payload, { searchMode: 'ai', replayJobId: AI_JOB_ID })
  assert.equal(requests.filter(row => row.path === '/api/factor-mining/estimate').length, estimateCount)
  add(`${scenarioId}-minimal-payload`, 'passed', 'Actual replay submitted exactly searchMode + replayJobId without estimating')
  await waitFor(() => Boolean(request.response?.data?.jobId), 'REPLAY_RECEIPT_MISSING')
  dialog = await openJob(page, request.response.data.jobId)
  await dialog.getByText(AI_JOB_ID, { exact: true }).waitFor({ state: 'visible' })
  await screenshot(page, 'replay-result', viewport)
  await dialog.locator('#fm-view-candidates').click()
  const replayDetail = requests.filter(row => row.path === `/api/factor-mining/jobs/${request.response.data.jobId}`).at(-1).response.data
  await assertCandidateFilter(page, requests, replayDetail.result.runId, viewport, 'replay-candidates')
  await reload(page)
  await tab(page, labels.jobs)
  await openJob(page, request.response.data.jobId)
  add(`${scenarioId}-new-job-after-reload`)
}

async function enumeration(page, requests, viewport) {
  scenarioId = `enumeration-${viewport.width}`
  await goto(page, '?tab=submit')
  await setDatesAndSource(page)
  const form = page.locator('.submit-job')
  await form.getByRole('button', { name: labels.estimate }).click()
  await waitFor(() => form.getByRole('button', { name: labels.submit }).isEnabled(), 'ENUMERATION_ESTIMATE_FAILED')
  const payload = requests.filter(row => row.path === '/api/factor-mining/estimate').at(-1).payload
  assert.equal(payload.searchMode, 'enumeration')
  assert.equal(payload.holdoutRatio, 0.1)
  for (const key of ['population', 'generations', 'eliteRatio', 'aiRounds', 'aiProposalsPerRound', 'researchNotes', 'replayJobId']) assert.equal(key in payload, false)
  add(`${scenarioId}-inactive-fields-trimmed`)
}

try {
  if (options.mode === 'test-backend') {
    backendUrl = options['backend-url']
    validateTestBackend(backendUrl, { schemaVersion: 1, environment: 'test', businessDatabaseWrites: false, realModelCalls: false, mutationScope: 'factor-mining-fixtures' })
    const response = await fetch(new URL('/__p2_verification_capabilities', backendUrl), { signal: AbortSignal.timeout(3000), redirect: 'error' })
    validateTestBackend(backendUrl, await response.json())
    report.testBackendCapabilitiesVerified = true
  }
  await startApp()
  const { chromium } = await import('playwright')
  browser = await chromium.launch({ headless: true, channel: options['browser-channel'] || 'msedge' })
  report.browser = { channel: options['browser-channel'] || 'msedge', version: browser.version() }
  for (const viewport of VIEWPORTS) {
    const { context, page, requests } = await contextFor(viewport)
    try {
      for (const scenario of [evolution, replay, enumeration]) {
        try { await scenario(page, requests, viewport) } catch (error) {
          add(scenarioId, 'failed', String(error.message).replaceAll(source, '[source]').replaceAll(baseUrl, '[app]'))
          await screenshot(page, `${scenarioId}-failure`, viewport).catch(() => undefined)
        }
      }
    } finally {
      logs.api.push(...requests.filter(row => row.path.startsWith('/api/factor-mining/')).map(row => ({ viewport, scenarioId: row.scenarioId, method: row.method, path: row.path, query: row.query, payload: row.payload, result: { code: row.response?.code, jobId: row.response?.data?.jobId, runId: row.response?.data?.runId } })))
      await context.close()
    }
  }
} catch (error) {
  add(scenarioId, 'blocked', error.code || String(error.message).split('\n')[0])
} finally {
  await browser?.close().catch(() => undefined)
  server?.kill()
  if (before) {
    const after = await inspectSource(source)
    report.source.worktreeHashAfter = after.treeHash
    report.source.unchangedDuringRun = before.treeHash === after.treeHash
    add('source-unchanged-during-run', before.treeHash === after.treeHash ? 'passed' : 'failed', 'A changed runtime tree invalidates attribution of the combined evidence')
  }
  const consoleErrors = logs.console.filter(row => ['error', 'pageerror'].includes(row.type)).length
  const networkErrors = logs.network.filter(row => row.failed || row.status >= 400).length
  report.console = { path: 'console.json', errorCount: consoleErrors, status: consoleErrors ? 'failed' : 'passed' }
  report.network = { path: 'network.json', unexpectedErrorCount: networkErrors, blockedOperationCount: logs.blocked.length, status: networkErrors || logs.blocked.length ? 'failed' : 'passed' }
  report.axe = { path: 'axe.json', status: logs.axe.length ? (report.checks.some(row => row.id.includes('-axe-') && row.status === 'failed') ? 'failed' : 'passed') : 'blocked', violationCount: logs.axe.reduce((n, row) => n + row.violations.length, 0) }
  add('console-errors', report.console.status, `${consoleErrors} errors; raw messages omitted`)
  add('network-errors', report.network.status, `${networkErrors} unexpected failures; ${logs.blocked.length} disallowed operations`)
  const requiredChecks = VIEWPORTS.every(({ width }) => [`evolution-${width}-result-after-reload`, `submitted-candidates-url-filter-reload-${width}`, `replay-${width}-minimal-payload`, `replay-${width}-new-job-after-reload`, `replay-candidates-url-filter-reload-${width}`, `enumeration-${width}-inactive-fields-trimmed`].every(id => report.checks.some(row => row.id === id && row.status === 'passed')))
  report.businessFlow = { status: requiredChecks ? 'passed' : 'blocked', requiredTerminalChecks: 12, evidence: 'api-evidence.json', persistenceScope: options.mode === 'mock' ? 'in-memory-fixture-only' : 'isolated-test-backend' }
  report.status = report.checks.some(row => row.status === 'failed') ? 'failed' : report.checks.some(row => row.status === 'blocked') || !requiredChecks ? 'blocked' : 'passed'
  if (options.mode === 'test-backend') report.integration.status = report.status
  report.finishedAt = new Date().toISOString()
  await Promise.all([json('browser-report.json', report), json('console.json', logs.console), json('network.json', { requests: logs.network, blocked: logs.blocked }), json('axe.json', logs.axe), json('api-evidence.json', logs.api)])
  process.stdout.write(JSON.stringify({ status: report.status, mode: report.mode, checks: report.checks.length, failures: report.checks.filter(row => row.status !== 'passed'), screenshots: report.screenshots.length, console: report.console, network: report.network, axe: report.axe, source: report.source }) + '\n')
  process.exitCode = report.status === 'passed' ? 0 : report.status === 'failed' ? 1 : 2
}

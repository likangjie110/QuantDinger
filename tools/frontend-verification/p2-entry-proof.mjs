import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { mkdir, readFile, writeFile } from 'node:fs/promises'
import { createServer } from 'node:net'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { chromium } from 'playwright'
import { createP2Fixture } from './p2-fixtures.mjs'
import { digest, inspectSource, requestAllowed } from './p2-support.mjs'

const root = dirname(fileURLToPath(import.meta.url))
const source = resolve(root, '../../frontend-vue')
const output = join(root, 'artifacts/p2-entry-proof')
await mkdir(output, { recursive: true })
const required = ['/src/main.js', '/src/App.vue', '/src/router/index.js', '/src/config/router.config.js', '/src/permission.js', '/src/views/factor-mining/index.vue', '/src/api/factor-mining.js', '/src/utils/request.js']
const before = await inspectSource(source)
const pkg = JSON.parse(await readFile(join(source, 'package.json'), 'utf8'))
assert.equal(pkg.scripts.dev, 'vite')
assert.equal(pkg.scripts.build, 'vite build')
assert.match(await readFile(join(source, 'index.html'), 'utf8'), /src="\/src\/main\.js"/)
const hashes = {}
for (const path of ['index.html', 'vite.config.js', 'package.json', ...required.map(path => path.slice(1))]) hashes[path] = digest(await readFile(join(source, path)))
const port = await new Promise((accept, reject) => {
  const socket = createServer()
  socket.on('error', reject)
  socket.listen(0, '127.0.0.1', () => { const number = socket.address().port; socket.close(() => accept(number)) })
})
const origin = `http://127.0.0.1:${port}`
const server = spawn(process.execPath, [join(root, 'p2-vite-server.mjs'), source, join(output, 'vite-cache'), String(port)], { cwd: root, windowsHide: true, stdio: 'ignore' })
const result = { status: 'blocked', mode: 'repository-native-vite-development', source: before, packageScripts: { dev: pkg.scripts.dev, build: pkg.scripts.build }, hashes, loadedModules: [], apiReads: [], consoleErrors: [], serverOverrides: ['loopback host', 'random strict port', 'cache directory inside tools', 'open=false'], productionBuildVerified: false, headBundleVerified: false, backendMode: 'mock' }
let browser
try {
  const end = Date.now() + 30000
  while (true) {
    if (server.exitCode !== null) throw new Error('SOURCE_SERVER_EXITED')
    try { if ((await fetch(origin, { signal: AbortSignal.timeout(1000) })).ok) break } catch { /* Bounded readiness polling. */ }
    assert.ok(Date.now() < end, 'SOURCE_SERVER_TIMEOUT')
    await new Promise(accept => setTimeout(accept, 100))
  }
  browser = await chromium.launch({ headless: true, channel: 'msedge' })
  const context = await browser.newContext({ serviceWorkers: 'block', viewport: { width: 1440, height: 900 } })
  context.setDefaultTimeout(15000)
  await context.addInitScript(() => {
    if (location.protocol !== 'http:') return
    localStorage.setItem('Access-Token', JSON.stringify('p2-fixture-only-no-business-auth'))
    localStorage.setItem('User-Info', JSON.stringify({ id: 0, username: 'p2-verification', nickname: 'P2 Fixture', is_demo: true }))
    localStorage.setItem('User-Roles', JSON.stringify([{ id: 'default', permissionList: [] }]))
  })
  const fixture = createP2Fixture()
  await context.route('**/*', async route => {
    const request = route.request()
    const url = new URL(request.url())
    if (url.origin !== origin || request.method() !== 'GET') return route.abort('blockedbyclient')
    if (!url.pathname.startsWith('/api/')) return route.continue()
    assert.ok(requestAllowed('GET', url.pathname), 'UNKNOWN_API_READ')
    const response = fixture.respond('GET', url.pathname + url.search)
    assert.ok(response, 'MISSING_READ_FIXTURE')
    if (url.pathname.startsWith('/api/factor-mining/')) result.apiReads.push(url.pathname)
    return route.fulfill({ status: response.status, json: response.body })
  })
  const page = await context.newPage()
  page.on('response', response => {
    const path = new URL(response.url()).pathname
    if (required.includes(path)) result.loadedModules.push({ path, status: response.status() })
  })
  page.on('pageerror', error => result.consoleErrors.push(error.message.replace(/https?:\/\/[^\s]+/g, '[url]')))
  await page.goto(`${origin}/#/factor-mining`)
  await page.locator('.factor-mining.qd-workspace-page').waitFor({ state: 'visible' })
  await page.getByRole('tab', { name: /^(Jobs|作业列表)$/ }).click()
  await page.locator('.job-list tbody tr').first().waitFor({ state: 'visible' })
  await page.waitForLoadState('networkidle')
  result.mounted = await page.evaluate(() => {
    const app = document.querySelector('#app')?.__vue__
    return { appElement: Boolean(app), route: app?.$router?.currentRoute?.path, routerMode: app?.$router?.mode, factorWorkspace: Boolean(document.querySelector('.factor-mining.qd-workspace-page')) }
  })
  assert.equal(result.mounted.appElement, true)
  assert.equal(result.mounted.route, '/factor-mining')
  assert.equal(result.mounted.routerMode, 'hash')
  for (const path of required) assert.ok(result.loadedModules.some(row => row.path === path && row.status === 200), `MODULE_NOT_LOADED_${path}`)
  assert.ok(result.apiReads.includes('/api/factor-mining/jobs'))
  assert.equal(result.consoleErrors.length, 0)
  result.status = 'passed'
} catch (error) {
  result.status = 'failed'
  result.error = error.message.split('\n')[0]
} finally {
  await browser?.close()
  server.kill()
  const after = await inspectSource(source)
  result.sourceUnchanged = before.treeHash === after.treeHash
  if (!result.sourceUnchanged) result.status = 'failed'
  await writeFile(join(output, 'browser-entry.json'), JSON.stringify(result, null, 2) + '\n')
  process.stdout.write(JSON.stringify(result) + '\n')
  process.exitCode = result.status === 'passed' ? 0 : 1
}

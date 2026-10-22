import { resolve } from 'node:path'

export const VIEWPORTS = [{ width: 1440, height: 900 }, { width: 390, height: 844 }]
export const STATES = ['loading', 'empty', 'error', 'success', 'unauthorized']

export function routeFor(target) {
  return target === 'main' ? '/#/factor-mining' : '/'
}

export function parseArgs(argv) {
  const allowed = new Set(['target', 'base-url', 'source-revision', 'output-dir', 'mode', 'source', 'test-environment', 'browser-channel'])
  const values = {}
  for (let index = 0; index < argv.length; index += 2) {
    const name = argv[index]?.replace(/^--/, '')
    if (!allowed.has(name)) throw new Error(`Unknown option: ${argv[index]}`)
    if (!argv[index + 1] || argv[index + 1].startsWith('--')) throw new Error(`Missing value for --${name}`)
    values[name] = argv[index + 1]
  }
  if (!['main', 'standalone'].includes(values.target)) throw new Error('--target must be main or standalone')
  if (!/^[a-f0-9]{40,64}$/i.test(values['source-revision'] || '')) throw new Error('--source-revision must be the full source Git revision')
  if (!['mock', 'integration'].includes(values.mode)) throw new Error('--mode must be mock or integration')
  if (values.mode === 'integration' && !values['base-url']) throw new Error('Integration requires explicit --base-url')
  if (values.mode === 'integration' && !['test', 'staging'].includes(values['test-environment'])) throw new Error('Integration requires --test-environment test|staging')
  if (values['base-url']) {
    const parsed = new URL(values['base-url'])
    if (parsed.username || parsed.password) throw new Error('--base-url must not contain credentials')
    if (!['http:', 'https:'].includes(parsed.protocol) || parsed.pathname !== '/' || parsed.search || parsed.hash) throw new Error('--base-url must be an HTTP(S) origin, without path, query, or hash')
  }
  if (values['browser-channel'] && !['chromium', 'msedge', 'chrome'].includes(values['browser-channel'])) throw new Error('--browser-channel must be chromium, msedge, or chrome')
  return {
    target: values.target,
    sourceRevision: values['source-revision'].toLowerCase(),
    mode: values.mode,
    baseUrl: values['base-url'] ? new URL(values['base-url']).origin : undefined,
    outputDir: resolve(values['output-dir'] || 'artifacts/frontend-verification'),
    source: values.source ? resolve(values.source) : undefined,
    testEnvironment: values['test-environment'],
    browserChannel: values['browser-channel'] || 'chromium'
  }
}

export function safePath(value) {
  try {
    const pathname = new URL(value, 'http://verification.invalid').pathname
    if (/^\/api\/factor-mining\/(jobs|candidates)\/[^/]+$/.test(pathname)) return pathname.replace(/\/[^/]+$/, '/:id')
    if (/^\/api\/factor-mining\/(catalog|estimate|run|jobs|candidates)$/.test(pathname)) return pathname
    if (pathname === '/api/auth/info' || pathname === '/api/auth/logout') return pathname
    if (pathname === '/') return '/'
    return '[other-resource]'
  } catch {
    return '[invalid-url]'
  }
}

export function readAllowed(method, pathname) {
  return ['GET', 'HEAD'].includes(method) && (
    /^\/api\/factor-mining\/(catalog|jobs|candidates)(\/[^/]+)?$/.test(pathname) || pathname === '/api/auth/info'
  )
}

export function expectedApiError(state, pathname, status) {
  return pathname === '/api/factor-mining/jobs' && ((state === 'error' && status === 500) || (state === 'unauthorized' && status === 403))
}

export function outcome(statuses) {
  if (statuses.includes('failed')) return 'failed'
  if (!statuses.length || statuses.some(status => status !== 'passed')) return 'blocked'
  return 'passed'
}

export function sourceProvenance(revisionVerified, autoStarted) {
  const bundleVerified = Boolean(revisionVerified && autoStarted)
  return { status: bundleVerified ? 'passed' : 'unverified', revisionVerified: Boolean(revisionVerified), bundleVerified }
}

export function readResult(body) {
  if (body?.code !== 1 || !Array.isArray(body.data?.items)) return { status: 'failed', itemCount: null }
  return { status: 'passed', itemCount: body.data.items.length }
}

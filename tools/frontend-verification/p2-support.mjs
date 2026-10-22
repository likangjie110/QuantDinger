import { createHash } from 'node:crypto'
import { execFileSync } from 'node:child_process'
import { readFile } from 'node:fs/promises'
import { join } from 'node:path'
import { safePath } from './core.mjs'

export const digest = value => createHash('sha256').update(String(value)).digest('hex')

export function isP2Target(target) {
  return /fm-(population|generations|elite-ratio|holdout-ratio|submit-replay|view-candidates|research-summary|research-fingerprint)|fm-replay|fm-detail-actions/.test(JSON.stringify(target))
}

export function sourceProvenance({ head, dirty, treeHash, autoStarted }) {
  return {
    headRevision: head, dirty, worktreeHash: treeHash, servedSource: autoStarted ? 'working-tree' : 'unverified',
    worktreeBundleVerified: Boolean(autoStarted), headBundleVerified: Boolean(autoStarted && !dirty)
  }
}

export async function inspectSource(source) {
  const git = args => execFileSync('git', ['-C', source, ...args], { encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'] })
  const head = git(['rev-parse', 'HEAD']).trim()
  const dirty = Boolean(git(['status', '--porcelain', '--untracked-files=normal', '--', '.']).trim())
  const files = [...new Set(git(['ls-files', '-z', '--cached', '--others', '--exclude-standard', '--', 'src', 'public', 'index.html', 'vite.config.js', 'package.json', 'pnpm-lock.yaml']).split('\0').filter(Boolean))].sort()
  const hash = createHash('sha256')
  for (const name of files) {
    hash.update(name).update('\0')
    try { hash.update(await readFile(join(source, name))) } catch (error) {
      if (error.code !== 'ENOENT') throw error
      hash.update('[deleted]')
    }
    hash.update('\0')
  }
  return { head, dirty, treeHash: hash.digest('hex'), runtimeFileCount: files.length }
}

export function requestAllowed(method, path) {
  if (method === 'POST') return ['/api/factor-mining/estimate', '/api/factor-mining/run'].includes(path)
  return method === 'GET' && (['/api/auth/info', '/api/settings/public-config', '/api/settings/brand-config', '/api/policy/broker-market', '/api/strategies/notifications/unread-count'].includes(path) || /^\/api\/factor-mining\/(catalog|jobs|candidates)(\/[^/]+)?$/.test(path) || /^\/api\/factor-mining\/jobs\/[^/]+\/proposals$/.test(path))
}

export function safeP2Path(value) {
  const path = new URL(value, 'http://verification.invalid').pathname
  return /^\/api\/factor-mining\/jobs\/[^/]+\/proposals$/.test(path) ? '/api/factor-mining/jobs/:id/proposals' : safePath(value)
}

export function validateTestBackend(value, body) {
  const url = new URL(value)
  if (url.protocol !== 'http:' || !['127.0.0.1', 'localhost', '[::1]'].includes(url.hostname) || url.username || url.password || url.pathname !== '/' || url.search || url.hash) throw new Error('TEST_BACKEND_MUST_BE_LOOPBACK_ORIGIN')
  if (body?.schemaVersion !== 1 || body.environment !== 'test' || body.businessDatabaseWrites !== false || body.realModelCalls !== false || body.mutationScope !== 'factor-mining-fixtures') throw new Error('TEST_BACKEND_ISOLATION_NOT_ATTESTED')
}

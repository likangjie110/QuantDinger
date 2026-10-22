import test from 'node:test'
import assert from 'node:assert/strict'
import { parseArgs, routeFor, safePath, expectedApiError, readAllowed, outcome, sourceProvenance, readResult } from '../core.mjs'
import { MOCK_JOB_ID, mockResponse } from '../fixtures.mjs'

const revision = 'a'.repeat(40)
const args = ['--target', 'standalone', '--source-revision', revision, '--mode', 'mock']

test('mock has a fixed standalone identity and can start the existing source', () => {
  const result = parseArgs(args)
  assert.equal(result.target, 'standalone')
  assert.equal(result.sourceRevision, revision)
  assert.equal(result.mode, 'mock')
  assert.equal(routeFor('main'), '/#/factor-mining')
  assert.equal(routeFor('standalone'), '/')
})

test('integration requires an explicit URL and a named test environment', () => {
  const base = ['--target', 'main', '--source-revision', revision, '--mode', 'integration']
  assert.throws(() => parseArgs(base), /base-url/)
  assert.throws(() => parseArgs([...base, '--base-url', 'http://127.0.0.1:8000']), /test-environment/)
  const parsed = parseArgs([...base, '--base-url', 'http://127.0.0.1:8000', '--test-environment', 'test'])
  assert.equal(parsed.testEnvironment, 'test')
})

test('rejects ambiguous target, source revision, URL secrets, and arbitrary routes', () => {
  assert.throws(() => parseArgs([...args, '--target', 'other']), /target/)
  assert.throws(() => parseArgs(args.map(value => value === revision ? 'HEAD' : value)), /revision/)
  assert.throws(() => parseArgs([...args, '--base-url', 'https://user:password@example.test']), /credentials/)
  assert.throws(() => parseArgs([...args, '--base-url', 'https://example.test/?token=secret']), /origin/)
  assert.throws(() => parseArgs([...args, '--route', '/live-trading']), /Unknown/)
})

test('safe network records omit host, query, credentials, and arbitrary endpoints', () => {
  assert.equal(safePath('https://user:secret@example.test/api/factor-mining/jobs?token=secret'), '/api/factor-mining/jobs')
  assert.equal(safePath('https://example.test/api/factor-mining/jobs/private-id?token=secret'), '/api/factor-mining/jobs/:id')
  assert.equal(safePath('https://example.test/private/account/secret-key'), '[other-resource]')
})

test('integration only passes explicitly allowed reads and cannot submit or cancel', () => {
  assert.equal(readAllowed('GET', '/api/factor-mining/jobs'), true)
  assert.equal(readAllowed('GET', '/api/factor-mining/catalog'), true)
  assert.equal(readAllowed('GET', '/api/auth/info'), true)
  assert.equal(readAllowed('POST', '/api/factor-mining/run'), false)
  assert.equal(readAllowed('POST', '/api/factor-mining/jobs/test/cancel'), false)
  assert.equal(readAllowed('GET', '/api/trades'), false)
})

test('only the exact injected failure endpoint is an expected network error', () => {
  assert.equal(expectedApiError('error', '/api/factor-mining/jobs', 500), true)
  assert.equal(expectedApiError('unauthorized', '/api/factor-mining/jobs', 403), true)
  assert.equal(expectedApiError('success', '/api/factor-mining/jobs', 500), false)
  assert.equal(expectedApiError('error', '/api/auth/info', 500), false)
})

test('missing evidence and observed failures cannot become passed', () => {
  assert.equal(outcome(['passed', 'passed']), 'passed')
  assert.equal(outcome(['passed', 'blocked']), 'blocked')
  assert.equal(outcome(['passed', 'failed', 'blocked']), 'failed')
  assert.equal(outcome([]), 'blocked')
})

test('job list and detail retain the exact submitted mock configuration', () => {
  const config = { sourceId: 7, startDate: '2024-01-01', endDate: '2024-12-31', searchMode: 'enumeration' }
  const list = mockResponse('/api/factor-mining/jobs', 'success', config)
  const detail = mockResponse(`/api/factor-mining/jobs/${MOCK_JOB_ID}`, 'success', config)
  assert.deepEqual(list.body.data.items[0].config, config)
  assert.deepEqual(detail.body.data.config, config)
  assert.equal(detail.body.data.jobId, list.body.data.items[0].jobId)
})

test('run receipt preserves the real HTTP 202 contract and candidate list does not invent total', () => {
  assert.equal(mockResponse('/api/factor-mining/run', 'success').status, 202)
  assert.deepEqual(mockResponse('/api/factor-mining/candidates', 'success').body.data, { items: [] })
})

test('a checked-out revision cannot prove an externally served bundle', () => {
  assert.deepEqual(sourceProvenance(true, false), { status: 'unverified', revisionVerified: true, bundleVerified: false })
  assert.deepEqual(sourceProvenance(true, true), { status: 'passed', revisionVerified: true, bundleVerified: true })
  assert.equal(sourceProvenance(false, true).bundleVerified, false)
})

test('HTTP 200 alone cannot establish a successful jobs read', () => {
  assert.equal(readResult({ code: 0, msg: 'failed', data: null }).status, 'failed')
  assert.equal(readResult({ code: 1, data: {} }).status, 'failed')
  assert.deepEqual(readResult({ code: 1, data: { items: [] } }), { status: 'passed', itemCount: 0 })
  assert.deepEqual(readResult({ code: 1, data: { items: [{ jobId: 'test' }] } }), { status: 'passed', itemCount: 1 })
})

test('list and detail agree throughout the queued/running/completed mock lifecycle', () => {
  for (const status of ['queued', 'running', 'succeeded']) {
    const list = mockResponse('/api/factor-mining/jobs', 'success', undefined, status).body.data.items[0]
    const detail = mockResponse(`/api/factor-mining/jobs/${MOCK_JOB_ID}`, 'success', undefined, status).body.data
    assert.deepEqual(detail, list)
    assert.equal(detail.status, status)
    assert.equal(detail.hasResult, status === 'succeeded')
    assert.equal(Boolean(detail.result), status === 'succeeded')
  }
})

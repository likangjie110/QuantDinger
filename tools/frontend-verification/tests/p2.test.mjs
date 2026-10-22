import test from 'node:test'
import assert from 'node:assert/strict'

test('P2 submission retains evolution and holdout settings in a retrievable run', async () => {
  const { createP2Fixture } = await import('../p2-fixtures.mjs')
  const api = createP2Fixture()
  const payload = { sourceId: 7, market: 'USStock', timeframe: '1D', startDate: '2024-01-01', endDate: '2024-12-31', searchMode: 'evolution', population: 32, generations: 20, eliteRatio: 0.25, holdoutRatio: 0.2, budget: 40, seed: 42 }
  const receipt = api.respond('POST', '/api/factor-mining/run', payload)
  assert.equal(receipt.status, 202)
  const job = api.respond('GET', `/api/factor-mining/jobs/${receipt.body.data.jobId}`).body.data
  assert.deepEqual(job.config, payload)
  assert.equal(Number.isSafeInteger(job.result.runId), true)
  assert.equal(job.result.summary.research.holdout.evaluated, false)
  assert.equal(job.result.summary.research.runTrials, 40)
  assert.ok(job.result.summary.research.cumulativeTrials > 40)
  assert.notEqual(job.result.summary.research.freezeSnapshotId, job.result.summary.snapshotId)
  assert.equal(api.respond('GET', '/api/factor-mining/jobs').body.data.items[0].jobId, job.jobId)
})

test('replay accepts the minimal payload, inherits the original config and never estimates', async () => {
  const { createP2Fixture, AI_JOB_ID } = await import('../p2-fixtures.mjs')
  const api = createP2Fixture()
  const original = api.respond('GET', `/api/factor-mining/jobs/${AI_JOB_ID}`).body.data
  const receipt = api.respond('POST', '/api/factor-mining/run', { searchMode: 'ai', replayJobId: AI_JOB_ID })
  assert.equal(receipt.status, 202)
  const replay = api.respond('GET', `/api/factor-mining/jobs/${receipt.body.data.jobId}`).body.data
  assert.deepEqual(replay.config, { ...original.config, replayJobId: AI_JOB_ID })
  assert.equal(replay.result.summary.ai.replayOf, AI_JOB_ID)
  assert.equal(api.requests.filter(row => row.path === '/api/factor-mining/estimate').length, 0)
  const polluted = api.respond('POST', '/api/factor-mining/run', { searchMode: 'ai', replayJobId: AI_JOB_ID, holdoutRatio: 0.1 })
  assert.equal(polluted.status, 400)
  assert.equal(api.respond('GET', `/api/factor-mining/jobs/${AI_JOB_ID}/proposals?limit=200`).body.data.items.length, 1)
})

test('candidate fixtures respect numeric runId filtering across independent API reads', async () => {
  const { createP2Fixture, AI_RUN_ID } = await import('../p2-fixtures.mjs')
  const api = createP2Fixture()
  const path = `/api/factor-mining/candidates?runId=${AI_RUN_ID}&limit=50&offset=0`
  const first = api.respond('GET', path)
  assert.ok(first.body.data.items.length > 0)
  assert.ok(first.body.data.items.every(row => row.runId === AI_RUN_ID))
  assert.deepEqual(api.respond('GET', path), first)
  assert.deepEqual(api.respond('GET', '/api/factor-mining/candidates?runId=999999').body.data.items, [])
  assert.equal(api.respond('GET', '/api/factor-mining/candidates?runId=invalid').status, 400)
})

test('unknown APIs and cancellation never receive success fixtures', async () => {
  const { createP2Fixture, AI_JOB_ID } = await import('../p2-fixtures.mjs')
  const api = createP2Fixture()
  assert.equal(api.respond('POST', `/api/factor-mining/jobs/${AI_JOB_ID}/cancel`), null)
  assert.equal(api.respond('POST', '/api/live-trading/order', {}), null)
  assert.equal(api.respond('GET', '/api/private-data'), null)
})

test('the actual SPA shell receives only explicit read-only fixture responses', async () => {
  const { createP2Fixture } = await import('../p2-fixtures.mjs')
  const { requestAllowed } = await import('../p2-support.mjs')
  const api = createP2Fixture()
  for (const path of ['/api/settings/public-config', '/api/settings/brand-config', '/api/policy/broker-market', '/api/strategies/notifications/unread-count']) {
    assert.equal(requestAllowed('GET', path), true, path)
    assert.equal(requestAllowed('POST', path), false, path)
    assert.equal(api.respond('GET', path).status, 200, path)
  }
  assert.equal(api.respond('GET', '/api/strategies/notifications/unread-count').body.data.unread, 0)
  assert.equal(api.respond('GET', '/api/settings/values'), null)
})

test('a dirty worktree served by this runner can never claim the HEAD bundle', async () => {
  const { sourceProvenance } = await import('../p2-support.mjs')
  const provenance = sourceProvenance({ head: 'a'.repeat(40), dirty: true, treeHash: 'b'.repeat(64), autoStarted: true })
  assert.equal(provenance.servedSource, 'working-tree')
  assert.equal(provenance.headBundleVerified, false)
  assert.equal(provenance.worktreeBundleVerified, true)
  assert.equal(provenance.worktreeHash, 'b'.repeat(64))
  assert.equal(sourceProvenance({ head: 'a'.repeat(40), dirty: false, treeHash: 'b'.repeat(64), autoStarted: false }).headBundleVerified, false)
})

test('P2 network boundaries allow only named mining APIs and prohibit external services', async () => {
  const { requestAllowed, safeP2Path } = await import('../p2-support.mjs')
  assert.equal(requestAllowed('GET', '/api/factor-mining/jobs/test/proposals'), true)
  assert.equal(requestAllowed('POST', '/api/factor-mining/run'), true)
  assert.equal(requestAllowed('POST', '/api/factor-mining/estimate'), true)
  assert.equal(requestAllowed('POST', '/api/factor-mining/jobs/test/cancel'), false)
  assert.equal(requestAllowed('POST', '/api/orders'), false)
  assert.equal(safeP2Path('http://local.invalid/api/factor-mining/jobs/private/proposals?token=secret'), '/api/factor-mining/jobs/:id/proposals')
})

test('a test backend must explicitly attest to fixture isolation before any writes', async () => {
  const { validateTestBackend } = await import('../p2-support.mjs')
  const capabilities = { schemaVersion: 1, environment: 'test', businessDatabaseWrites: false, realModelCalls: false, mutationScope: 'factor-mining-fixtures' }
  assert.doesNotThrow(() => validateTestBackend('http://127.0.0.1:5123', capabilities))
  for (const body of [null, {}, { ...capabilities, businessDatabaseWrites: true }, { ...capabilities, realModelCalls: true }]) {
    assert.throws(() => validateTestBackend('http://127.0.0.1:5123', body))
  }
  assert.throws(() => validateTestBackend('https://example.com', capabilities))
})

test('P2 axe target detection includes research summary, fingerprint and replay submit controls', async () => {
  const { isP2Target } = await import('../p2-support.mjs')
  assert.equal(isP2Target(['#fm-research-summary']), true)
  assert.equal(isP2Target([['#fm-research-fingerprint']]), true)
  assert.equal(isP2Target(['#fm-submit-replay > span']), true)
  assert.equal(isP2Target(['.fm-replay .ant-descriptions-item-content']), true)
  assert.equal(isP2Target(['.ant-table-wrapper']), false)
})

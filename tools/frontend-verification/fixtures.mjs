export const MOCK_JOB_ID = 'frontend-verification-job'
export const mockConfig = {
  sourceId: 1, market: 'USStock', timeframe: '1D', startDate: '2025-01-01',
  endDate: '2025-12-31', budget: 400, seed: 42, searchMode: 'enumeration'
}

export function mockJob(config = mockConfig, status = 'succeeded') {
  const completed = status === 'succeeded'
  return {
    jobId: MOCK_JOB_ID, runId: 'frontend-verification-run', status,
    config, createdAt: '2026-01-01T08:00:00Z', startedAt: status === 'queued' ? null : '2026-01-01T08:00:01Z',
    finishedAt: completed ? '2026-01-01T08:01:00Z' : null, hasResult: completed,
    progress: { stage: completed ? 'done' : 'search', evaluated: completed ? 400 : status === 'running' ? 10 : 0, total: 400 },
    result: completed ? {
      summary: { trials: 400, candidates: 12, frozen: 3, snapshotId: 'verification-fixture' },
      split: {
        searchStart: '2025-01-01', searchEnd: '2025-08-31',
        frozenStart: '2025-09-01', frozenEnd: '2025-12-31',
        freezeStart: '2025-09-01', freezeEnd: '2025-12-31'
      }
    } : null
  }
}

export const envelope = data => ({ code: 1, msg: 'common.success', data })

export function mockResponse(pathname, state, config, jobStatus = 'succeeded') {
  if (pathname === '/api/factor-mining/jobs') {
    // Inject generic transport failures. The jobs route does not document a stable
    // 500/403 envelope; these scenarios verify UI resilience, not backend behavior.
    if (state === 'error') return { status: 500, body: { code: 0, msg: 'factorMining.runFailed', data: null } }
    if (state === 'unauthorized') return { status: 403, body: { code: 403, msg: 'Permission denied: factor-mining', data: null } }
    return { status: 200, body: envelope({ items: state === 'empty' || state === 'loading' ? [] : [mockJob(config, jobStatus)] }) }
  }
  if (pathname === `/api/factor-mining/jobs/${MOCK_JOB_ID}`) return { status: 200, body: envelope(mockJob(config, jobStatus)) }
  if (pathname === '/api/factor-mining/candidates') return { status: 200, body: envelope({ items: [] }) }
  if (pathname === '/api/factor-mining/catalog') return { status: 200, body: envelope({ fields: [], operators: [], templates: [] }) }
  if (pathname === '/api/factor-mining/estimate') return { status: 200, body: envelope({ templateCount: 8, candidateBudget: 400 }) }
  if (pathname === '/api/factor-mining/run') return { status: 202, body: envelope({ jobId: MOCK_JOB_ID, runId: 'frontend-verification-run', status: 'queued' }) }
  if (pathname === '/api/auth/info') return { status: 200, body: envelope({ userinfo: { id: 0, username: 'frontend-verification', is_demo: true } }) }
  return null
}

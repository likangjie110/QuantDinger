export const AI_JOB_ID = 'p2-recorded-ai'
export const AI_RUN_ID = 701
export const defaultConfig = {
  sourceId: 7, market: 'USStock', timeframe: '1D', startDate: '2024-01-01', endDate: '2024-12-31',
  budget: 40, seed: 42, searchMode: 'enumeration', holdoutRatio: 0.1, freezeRatio: 0.3
}

const copy = value => structuredClone(value)
const success = (data, status = 200) => ({ status, body: { code: 1, msg: 'common.success', data } })
const invalid = () => ({ status: 400, body: { code: 0, msg: 'factorMining.invalidParameter', data: null } })

export function shellResponse(method, path) {
  if (method !== 'GET') return null
  if (path === '/api/auth/info') return success({ userinfo: { id: 0, username: 'p2-verification', is_demo: true } })
  if (path === '/api/settings/public-config') return success({ billing_enabled: false, marketplace_enabled: false, registration_enabled: false })
  if (path === '/api/settings/brand-config' || path === '/api/policy/broker-market') return success({})
  if (path === '/api/strategies/notifications/unread-count') return success({ unread: 0 })
  return null
}

export function researchFor(config) {
  const start = new Date(`${config.startDate}T00:00:00Z`).getTime()
  const end = new Date(`${config.endDate}T00:00:00Z`).getTime()
  const ratio = config.holdoutRatio ?? 0
  const cutoff = end - (end - start) * ratio
  const date = timestamp => new Date(timestamp).toISOString().slice(0, 10)
  return {
    experimentId: 71, contextHash: 'c'.repeat(64), runTrials: config.budget,
    cumulativeTrials: 120 + config.budget, freezeVersion: 'd'.repeat(64), freezeSnapshotId: 'f'.repeat(64),
    holdout: { reserved: ratio > 0, startDate: ratio > 0 ? date(cutoff) : null, endDate: ratio > 0 ? config.endDate : null, evaluated: false }
  }
}

function jobFor(jobId, runId, config) {
  const research = researchFor(config)
  return {
    jobId, runId, status: 'succeeded', config: copy(config), hasResult: true,
    createdAt: '2026-10-19T08:00:00Z', startedAt: '2026-10-19T08:00:01Z', finishedAt: '2026-10-19T08:00:02Z',
    progress: { stage: 'done', evaluated: config.budget, total: config.budget }, error: null,
    result: {
      runId, config: copy(config), candidates: [], trials: [],
      summary: { trials: config.budget, candidates: 1, frozen: 1, snapshotId: 'e'.repeat(64), research },
      split: { searchStart: config.startDate, searchEnd: '2024-07-31', freezeStart: '2024-08-01', freezeEnd: research.holdout.startDate || config.endDate }
    }
  }
}

function candidateFor(runId, id = runId) {
  return {
    id, runId, expressionHash: 'a'.repeat(64), expression: { op: 'ts_mean', args: ['close'], params: { n: 5 } },
    origin: 'template', templateId: 'momentum', market: 'USStock', timeframe: '1D', status: 'frozen', reason: '',
    createdAt: '2026-10-19T08:00:02Z', metrics: { coverage: 1, rankIc: 0.08, icir: 1.2, averageTurnover: 0.1, foldIcSigns: [1, 1, 1] },
    outOfSample: { coverage: 1, rankIc: 0.06, icir: 0.9 }
  }
}

export function createP2Fixture() {
  const original = jobFor(AI_JOB_ID, AI_RUN_ID, { ...defaultConfig, searchMode: 'ai', holdoutRatio: 0.2, aiRounds: 1, aiProposalsPerRound: 2, researchNotes: '' })
  original.result.summary.ai = { rounds: 1, proposals: 2, l1Rejected: 0, duplicates: 0, evaluated: 2, model: ['fixture-only'], provider: ['fixture-only'], replayOf: null, snapshotChanged: false }
  const jobs = [original]
  const candidates = [candidateFor(AI_RUN_ID)]
  const requests = []
  let nextRun = 702
  return {
    requests,
    respond(method, value, payload) {
      const url = new URL(value, 'http://fixture.invalid')
      const path = url.pathname
      requests.push({ method, path, payload: copy(payload) })
      const shell = shellResponse(method, path)
      if (shell) return shell
      if (method === 'GET' && path === '/api/factor-mining/catalog') return success({ fields: [], operators: [], templates: [] })
      if (method === 'POST' && path === '/api/factor-mining/estimate') return success({ templateCount: 8, candidateBudget: payload?.budget || 40 })
      if (method === 'POST' && path === '/api/factor-mining/run') {
        let config = copy(payload || {})
        if (config.replayJobId) {
          if (Object.keys(config).sort().join(',') !== 'replayJobId,searchMode' || config.searchMode !== 'ai') return invalid()
          const source = jobs.find(row => row.jobId === config.replayJobId)
          if (!source) return { status: 404, body: { code: 0, msg: 'factorMining.aiReplayNotFound', data: null } }
          config = { ...copy(source.config), replayJobId: source.jobId }
        }
        if (config.searchMode !== 'evolution' && ['population', 'generations', 'eliteRatio'].some(key => key in config)) return invalid()
        if (config.searchMode !== 'ai' && ['aiRounds', 'aiProposalsPerRound', 'researchNotes', 'replayJobId'].some(key => key in config)) return invalid()
        const runId = nextRun++
        const job = jobFor(`p2-job-${runId}`, runId, config)
        if (config.searchMode === 'ai') job.result.summary.ai = { ...copy(original.result.summary.ai), replayOf: config.replayJobId || null }
        jobs.unshift(job)
        candidates.push(candidateFor(runId))
        return success({ jobId: job.jobId, runId, status: 'queued' }, 202)
      }
      if (method === 'GET' && path === '/api/factor-mining/jobs') return success({ items: copy(jobs) })
      const detail = path.match(/^\/api\/factor-mining\/jobs\/([^/]+)(\/proposals)?$/)
      if (method === 'GET' && detail) {
        const job = jobs.find(row => row.jobId === detail[1])
        if (!job) return { status: 404, body: { code: 0, msg: 'factorMining.jobNotFound', data: null } }
        if (detail[2]) return success({ items: [{ id: 1, runId: job.runId, round: 1, ordinal: 1, hypothesis: 'Fixture proposal only', status: 'evaluated', reason: '', expressionHash: 'a'.repeat(64), model: 'fixture-only', provider: 'fixture-only' }] })
        return success(copy(job))
      }
      if (method === 'GET' && path === '/api/factor-mining/candidates') {
        const runId = url.searchParams.get('runId')
        if (runId !== null && !/^[1-9]\d*$/.test(runId)) return invalid()
        let items = candidates.filter(row => runId === null || row.runId === Number(runId))
        for (const key of ['status', 'market']) if (url.searchParams.get(key)) items = items.filter(row => row[key] === url.searchParams.get(key))
        const offset = Number(url.searchParams.get('offset') || 0)
        const limit = Number(url.searchParams.get('limit') || 50)
        return success({ items: copy(items.slice(offset, offset + limit)) })
      }
      const candidate = path.match(/^\/api\/factor-mining\/candidates\/(\d+)$/)
      if (method === 'GET' && candidate) {
        const row = candidates.find(item => item.id === Number(candidate[1]))
        return row ? success(copy(row)) : { status: 404, body: { code: 0, msg: 'factorMining.candidateNotFound', data: null } }
      }
      return null
    }
  }
}

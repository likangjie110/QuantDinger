import assert from 'node:assert/strict'
import { test } from 'node:test'
import { AxiosError } from 'axios'
import client, { factorMiningApi } from '../../src/api/factorMining.js'

function setupTransport(t, envelope, token = null) {
  const calls = []
  const previousAdapter = client.defaults.adapter
  const storageDescriptor = Object.getOwnPropertyDescriptor(globalThis, 'localStorage')
  Object.defineProperty(globalThis, 'localStorage', {
    configurable: true,
    value: { getItem: key => key === 'token' ? token : null }
  })
  client.defaults.adapter = async config => {
    calls.push(config)
    return {
      config,
      data: envelope,
      headers: {},
      status: 200,
      statusText: 'OK'
    }
  }
  t.after(() => {
    client.defaults.adapter = previousAdapter
    if (storageDescriptor) {
      Object.defineProperty(globalThis, 'localStorage', storageDescriptor)
    } else {
      delete globalThis.localStorage
    }
  })
  return calls
}

test('submits a run with the bearer token and expected request contract', async (t) => {
  const calls = setupTransport(t, {
    code: 1,
    msg: 'common.success',
    data: { runId: 123, jobId: 'job-123', status: 'queued' }
  }, 'unit-test-token')
  const payload = { market: 'USStock', timeframe: '1d', budget: 10 }

  const result = await factorMiningApi.submitRun(payload)

  assert.deepEqual(result, { runId: 123, jobId: 'job-123', status: 'queued' })
  assert.equal(calls.length, 1)
  assert.equal(calls[0].method, 'post')
  assert.equal(calls[0].url, '/factor-mining/run')
  assert.equal(calls[0].baseURL, '/api')
  assert.deepEqual(JSON.parse(calls[0].data), payload)
  assert.equal(calls[0].headers.get('Authorization'), 'Bearer unit-test-token')
})

test('unwraps a successful response envelope and preserves endpoint defaults', async (t) => {
  const calls = setupTransport(t, {
    code: 1,
    msg: 'common.success',
    data: { items: [{ id: 7, status: 'frozen' }] }
  })

  const result = await factorMiningApi.getCandidates({ status: 'frozen' })

  assert.deepEqual(result, { items: [{ id: 7, status: 'frozen' }] })
  assert.equal(calls[0].url, '/factor-mining/candidates')
  assert.deepEqual(calls[0].params, {
    runId: undefined,
    status: 'frozen',
    market: undefined,
    limit: 50,
    offset: 0
  })
  assert.equal(calls[0].headers.has('Authorization'), false)
})

test('keeps zero values and explicit filters in candidate pagination', async (t) => {
  const calls = setupTransport(t, { code: 1, msg: 'common.success', data: { items: [] } })

  await factorMiningApi.getCandidates({
    runId: 42, market: 'Crypto', status: 'search', limit: 0, offset: 0
  })

  assert.deepEqual(calls[0].params, {
    runId: 42, market: 'Crypto', status: 'search', limit: 0, offset: 0
  })
})

test('uses the default jobs limit without altering the list envelope', async (t) => {
  const calls = setupTransport(t, { code: 1, msg: 'common.success', data: { items: [] } })

  assert.deepEqual(await factorMiningApi.getJobs(), { items: [] })
  assert.equal(calls[0].url, '/factor-mining/jobs')
  assert.deepEqual(calls[0].params, { limit: 20 })
})

test('rejects a failed response envelope with the server message', async (t) => {
  setupTransport(t, {
    code: 0,
    msg: 'factorMining.invalidConfig',
    data: null
  })

  await assert.rejects(
    factorMiningApi.getCatalog(),
    (error) => error instanceof Error && error.message === 'factorMining.invalidConfig'
  )
})

test('rejects a failed envelope even if its data looks successful', async (t) => {
  setupTransport(t, { code: 0, msg: '', data: { items: [] } })

  await assert.rejects(factorMiningApi.getJobs(), { message: '请求失败' })
})

test('preserves transport authentication errors for the UI to handle', async (t) => {
  setupTransport(t, null)
  const response = {
    data: { code: 0, msg: 'common.unauthorized', data: null },
    status: 401,
    statusText: 'Unauthorized',
    headers: {},
    config: { headers: {} }
  }
  const error = new AxiosError('Request failed with status code 401', 'ERR_BAD_REQUEST', {}, null, response)
  client.defaults.adapter = async () => { throw error }

  await assert.rejects(factorMiningApi.getCatalog(), received => {
    assert.equal(received, error)
    assert.equal(received.response.status, 401)
    assert.equal(received.response.data.msg, 'common.unauthorized')
    return true
  })
})

import axios from 'axios'

/**
 * These signatures describe the data returned after the response interceptor
 * unwraps the API envelope. They do not change Axios transport behavior.
 * @typedef {{items: Array<Record<string, unknown>>}} MiningItems
 * @typedef {{templateCount: number, candidateBudget: number}} MiningEstimate
 * @typedef {{jobId: string, status: string, progress: Record<string, unknown>, config: Record<string, unknown>, result: unknown, hasResult: boolean, error: string | null, createdAt: string | null, startedAt: string | null, finishedAt: string | null}} MiningJob
 * @typedef {{runId?: number, status?: string, market?: string, limit?: number, offset?: number}} CandidateFilters
 * @typedef {Omit<import('axios').AxiosInstance, 'get' | 'post'> & {
 *   get<T>(url: string, config?: import('axios').AxiosRequestConfig): Promise<T>,
 *   post<T>(url: string, data?: unknown, config?: import('axios').AxiosRequestConfig): Promise<T>
 * }} UnwrappedApiClient
 */

const api = /** @type {UnwrappedApiClient} */ (/** @type {unknown} */ (axios.create({
  baseURL: '/api',
  timeout: 60000,
  headers: {
    'Content-Type': 'application/json'
  }
})))

// 请求拦截器 - 添加 token
api.interceptors.request.use(
  config => {
    const token = localStorage.getItem('token')
    if (token) {
      config.headers.Authorization = `Bearer ${token}`
    }
    return config
  },
  error => Promise.reject(error)
)

// 响应拦截器 - 统一处理响应
api.interceptors.response.use(
  response => {
    const { data } = response
    // QuantDinger 统一信封格式: {code: 1, msg: "common.success", data: ...}
    if (data.code === 1) {
      return data.data
    } else {
      return Promise.reject(new Error(data.msg || '请求失败'))
    }
  },
  error => {
    return Promise.reject(error)
  }
)

// 因子挖掘 API
export const factorMiningApi = {
  // 获取 DSL 词表
  /** @returns {Promise<{fields: string[], operators: string[], templates: Array<Record<string, unknown>>}>} */
  getCatalog() {
    return api.get('/factor-mining/catalog')
  },

  // 静态估价
  /** @param {Record<string, unknown>} params @returns {Promise<MiningEstimate>} */
  estimate(params) {
    return api.post('/factor-mining/estimate', params)
  },

  // 提交挖掘作业
  /** @param {Record<string, unknown>} params @returns {Promise<MiningJob>} */
  submitRun(params) {
    return api.post('/factor-mining/run', params)
  },

  // 获取作业列表
  /** @param {number} limit @returns {Promise<{items: MiningJob[]}>} */
  getJobs(limit = 20) {
    return api.get('/factor-mining/jobs', { params: { limit } })
  },

  // 获取作业详情
  /** @param {string} jobId @returns {Promise<MiningJob>} */
  getJobDetail(jobId) {
    return api.get(`/factor-mining/jobs/${jobId}`)
  },

  // 取消作业
  /** @param {string} jobId @returns {Promise<MiningJob>} */
  cancelJob(jobId) {
    return api.post(`/factor-mining/jobs/${jobId}/cancel`)
  },

  // 获取候选库列表
  /** @param {CandidateFilters} params @returns {Promise<MiningItems>} */
  getCandidates(params = {}) {
    const { runId, status, market, limit = 50, offset = 0 } = params
    return api.get('/factor-mining/candidates', {
      params: { runId, status, market, limit, offset }
    })
  },

  // 获取候选详情
  /** @param {number} candidateId @returns {Promise<Record<string, unknown>>} */
  getCandidateDetail(candidateId) {
    return api.get(`/factor-mining/candidates/${candidateId}`)
  }
}

export default api

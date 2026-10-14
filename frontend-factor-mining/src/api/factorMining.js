import axios from 'axios'

const api = axios.create({
  baseURL: '/api',
  timeout: 60000,
  headers: {
    'Content-Type': 'application/json'
  }
})

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
  getCatalog() {
    return api.get('/factor-mining/catalog')
  },

  // 静态估价
  estimate(params) {
    return api.post('/factor-mining/estimate', params)
  },

  // 提交挖掘作业
  submitRun(params) {
    return api.post('/factor-mining/run', params)
  },

  // 获取作业列表
  getJobs(limit = 20) {
    return api.get('/factor-mining/jobs', { params: { limit } })
  },

  // 获取作业详情
  getJobDetail(jobId) {
    return api.get(`/factor-mining/jobs/${jobId}`)
  },

  // 取消作业
  cancelJob(jobId) {
    return api.post(`/factor-mining/jobs/${jobId}/cancel`)
  },

  // 获取候选库列表
  getCandidates(params = {}) {
    const { runId, status, market, limit = 50, offset = 0 } = params
    return api.get('/factor-mining/candidates', {
      params: { runId, status, market, limit, offset }
    })
  },

  // 获取候选详情
  getCandidateDetail(candidateId) {
    return api.get(`/factor-mining/candidates/${candidateId}`)
  }
}

export default api

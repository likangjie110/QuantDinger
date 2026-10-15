<template>
  <div class="job-list">
    <el-card>
      <template #header>
        <div class="card-header">
          <span>{{ t('factorMining.jobs.title') }}</span>
          <el-button type="primary" size="small" @click="loadJobs">
            {{ t('factorMining.jobs.refresh') }}
          </el-button>
        </div>
      </template>

      <el-table :data="jobs" v-loading="loading" stripe>
        <el-table-column prop="jobId" :label="t('factorMining.jobs.jobId')" width="280" />

        <el-table-column :label="t('factorMining.jobs.status')" width="100">
          <template #default="{ row }">
            <el-tag :type="getStatusType(row.status)">
              {{ t(`factorMining.jobStatus.${row.status}`) }}
            </el-tag>
          </template>
        </el-table-column>

        <el-table-column :label="t('factorMining.jobs.market')" width="100">
          <template #default="{ row }">
            {{ row.config?.market || '-' }}
          </template>
        </el-table-column>

        <el-table-column :label="t('factorMining.jobs.dateRange')" width="220">
          <template #default="{ row }">
            {{ formatDateRange(row) }}
          </template>
        </el-table-column>

        <el-table-column :label="t('factorMining.jobs.progress')" width="150">
          <template #default="{ row }">
            <div v-if="row.progress && row.status === 'running'">
              <el-progress
                :percentage="calculateProgress(row.progress)"
                :status="row.progress.stage === 'review' ? 'success' : ''"
              />
              <div style="font-size: 12px; color: #909399; margin-top: 4px">
                {{ row.progress.stage }}: {{ row.progress.evaluated }} / {{ row.progress.total }}
              </div>
            </div>
            <span v-else-if="row.status === 'succeeded' && row.hasResult">
              {{ t('factorMining.jobs.seeDetail') }}
            </span>
            <span v-else>-</span>
          </template>
        </el-table-column>

        <el-table-column :label="t('factorMining.jobs.createdAt')" width="180">
          <template #default="{ row }">
            {{ formatDateTime(row.createdAt) }}
          </template>
        </el-table-column>

        <el-table-column :label="t('factorMining.jobs.actions')" fixed="right" width="180">
          <template #default="{ row }">
            <el-button
              type="primary"
              size="small"
              @click="viewDetail(row)"
            >
              {{ t('factorMining.jobs.viewDetail') }}
            </el-button>
            <el-button
              v-if="row.status === 'queued' || row.status === 'running'"
              type="danger"
              size="small"
              @click="cancelJob(row.jobId)"
            >
              {{ t('factorMining.jobs.cancel') }}
            </el-button>
          </template>
        </el-table-column>
      </el-table>

      <el-empty v-if="!loading && jobs.length === 0" :description="t('factorMining.jobs.noJobs')" />
    </el-card>

    <!-- 作业详情对话框 -->
    <el-dialog
      v-model="detailVisible"
      :title="t('factorMining.jobDetail.title')"
      width="80%"
      destroy-on-close
    >
      <JobDetail v-if="selectedJob" :job-id="selectedJob.jobId" />
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, onMounted, onUnmounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { factorMiningApi } from '@/api/factorMining'
import { useI18n } from '@/hooks/useI18n'
import JobDetail from './JobDetail.vue'
import dayjs from 'dayjs'

const { t } = useI18n()

const jobs = ref([])
const loading = ref(false)
const detailVisible = ref(false)
const selectedJob = ref(null)
let pollTimer = null

const loadJobs = async () => {
  loading.value = true
  try {
    // The endpoint answers { items: [...] }, not a bare array.
    const data = await factorMiningApi.getJobs(50)
    jobs.value = data?.items || []
  } catch (error) {
    ElMessage.error(t(`factorMining.errors.${error.message}`) || error.message)
  } finally {
    loading.value = false
  }
}

const getStatusType = (status) => {
  const map = {
    queued: 'info',
    running: 'warning',
    succeeded: 'success',
    failed: 'danger',
    cancelled: 'info'
  }
  return map[status] || 'info'
}

const formatDateRange = (row) => {
  // The list view withholds `result`, so the window it ran comes from `config`.
  const { startDate, endDate } = row.config || {}
  if (!startDate || !endDate) return '-'
  return `${startDate} ~ ${endDate}`
}

const formatDateTime = (datetime) => {
  if (!datetime) return '-'
  return dayjs(datetime).format('YYYY-MM-DD HH:mm:ss')
}

const calculateProgress = (progress) => {
  if (!progress || !progress.total) return 0
  return Math.round((progress.evaluated / progress.total) * 100)
}

const viewDetail = (job) => {
  selectedJob.value = job
  detailVisible.value = true
}

const cancelJob = async (jobId) => {
  try {
    await ElMessageBox.confirm('确定要取消该作业吗？', '提示', {
      confirmButtonText: '确定',
      cancelButtonText: '取消',
      type: 'warning'
    })

    await factorMiningApi.cancelJob(jobId)
    ElMessage.success('作业已取消')
    await loadJobs()
  } catch (error) {
    if (error !== 'cancel') {
      ElMessage.error(t(`factorMining.errors.${error.message}`) || error.message)
    }
  }
}

// 轮询正在运行的作业
const startPolling = () => {
  pollTimer = setInterval(() => {
    const hasRunning = jobs.value.some(job =>
      job.status === 'queued' || job.status === 'running'
    )
    if (hasRunning) {
      loadJobs()
    }
  }, 5000) // 每 5 秒轮询一次
}

const stopPolling = () => {
  if (pollTimer) {
    clearInterval(pollTimer)
    pollTimer = null
  }
}

onMounted(() => {
  loadJobs()
  startPolling()
})

onUnmounted(() => {
  stopPolling()
})
</script>

<style scoped>
.card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}
</style>

<template>
  <div class="job-detail" v-loading="loading">
    <el-descriptions v-if="job" :title="t('factorMining.jobDetail.basicInfo')" :column="2" border>
      <el-descriptions-item :label="t('factorMining.jobs.jobId')">
        {{ job.jobId }}
      </el-descriptions-item>
      <el-descriptions-item :label="t('factorMining.jobs.status')">
        <el-tag :type="getStatusType(job.status)">
          {{ t(`factorMining.jobStatus.${job.status}`) }}
        </el-tag>
      </el-descriptions-item>
      <el-descriptions-item :label="t('factorMining.jobs.createdAt')">
        {{ formatDateTime(job.createdAt) }}
      </el-descriptions-item>
      <el-descriptions-item :label="t('factorMining.jobs.startedAt')">
        {{ formatDateTime(job.startedAt) }}
      </el-descriptions-item>
      <el-descriptions-item :label="t('factorMining.jobs.finishedAt')">
        {{ formatDateTime(job.finishedAt) }}
      </el-descriptions-item>
    </el-descriptions>

    <el-descriptions v-if="job?.request" :title="t('factorMining.jobDetail.config')" :column="2" border style="margin-top: 20px">
      <el-descriptions-item :label="t('factorMining.submit.sourceId')">
        {{ job.request.sourceId }}
      </el-descriptions-item>
      <el-descriptions-item :label="t('factorMining.submit.market')">
        {{ job.request.market }}
      </el-descriptions-item>
      <el-descriptions-item :label="t('factorMining.submit.timeframe')">
        {{ job.request.timeframe }}
      </el-descriptions-item>
      <el-descriptions-item :label="t('factorMining.jobs.dateRange')">
        {{ job.request.startDate }} ~ {{ job.request.endDate }}
      </el-descriptions-item>
      <el-descriptions-item :label="t('factorMining.submit.budget')">
        {{ job.request.budget }}
      </el-descriptions-item>
      <el-descriptions-item :label="t('factorMining.submit.seed')">
        {{ job.request.seed }}
      </el-descriptions-item>
    </el-descriptions>

    <el-descriptions
      v-if="job?.result?.summary"
      :title="t('factorMining.jobDetail.result')"
      :column="2"
      border
      style="margin-top: 20px"
    >
      <el-descriptions-item :label="t('factorMining.jobDetail.trials')">
        <el-tag type="info">{{ job.result.summary.trials || 0 }}</el-tag>
      </el-descriptions-item>
      <el-descriptions-item :label="t('factorMining.jobDetail.candidates')">
        <el-tag type="warning">{{ job.result.summary.candidates || 0 }}</el-tag>
      </el-descriptions-item>
      <el-descriptions-item :label="t('factorMining.jobDetail.frozen')">
        <el-tag type="success">{{ job.result.summary.frozen || 0 }}</el-tag>
      </el-descriptions-item>
      <el-descriptions-item :label="t('factorMining.jobDetail.snapshotId')">
        <el-text truncated style="max-width: 200px">
          {{ job.result.summary.snapshotId }}
        </el-text>
      </el-descriptions-item>
      <el-descriptions-item :label="t('factorMining.jobDetail.searchWindow')" :span="2">
        {{ formatWindow(job.result.split?.searchStart, job.result.split?.searchEnd) }}
      </el-descriptions-item>
      <el-descriptions-item :label="t('factorMining.jobDetail.frozenWindow')" :span="2">
        {{ formatWindow(job.result.split?.frozenStart, job.result.split?.frozenEnd) }}
      </el-descriptions-item>
    </el-descriptions>

    <el-alert
      v-if="job?.error"
      :title="t('factorMining.jobDetail.error')"
      type="error"
      :description="job.error"
      :closable="false"
      style="margin-top: 20px"
    />
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { factorMiningApi } from '@/api/factorMining'
import { useI18n } from '@/hooks/useI18n'
import dayjs from 'dayjs'

const props = defineProps({
  jobId: {
    type: String,
    required: true
  }
})

const { t } = useI18n()

const job = ref(null)
const loading = ref(false)

const loadJobDetail = async () => {
  loading.value = true
  try {
    job.value = await factorMiningApi.getJobDetail(props.jobId)
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

const formatDateTime = (datetime) => {
  if (!datetime) return '-'
  return dayjs(datetime).format('YYYY-MM-DD HH:mm:ss')
}

const formatWindow = (start, end) => {
  if (!start || !end) return '-'
  return `${start} ~ ${end}`
}

onMounted(() => {
  loadJobDetail()
})
</script>

<style scoped>
.job-detail {
  min-height: 400px;
}
</style>

<template>
  <div class="candidate-detail" v-loading="loading">
    <el-descriptions v-if="candidate" :column="2" border>
      <el-descriptions-item label="ID">
        {{ candidate.id }}
      </el-descriptions-item>
      <el-descriptions-item :label="t('factorMining.candidates.status')">
        <el-tag :type="getStatusType(candidate.status)">
          {{ t(`factorMining.candidateStatus.${candidate.status}`) }}
        </el-tag>
      </el-descriptions-item>
      <el-descriptions-item :label="t('factorMining.candidates.market')">
        {{ candidate.market }}
      </el-descriptions-item>
      <el-descriptions-item :label="t('factorMining.submit.timeframe')">
        {{ candidate.timeframe }}
      </el-descriptions-item>
      <el-descriptions-item :label="t('factorMining.candidates.templateId')">
        {{ candidate.templateId }}
      </el-descriptions-item>
      <el-descriptions-item :label="t('factorMining.candidates.origin')">
        {{ candidate.origin }}
      </el-descriptions-item>
      <el-descriptions-item :label="t('factorMining.candidates.expressionHash')" :span="2">
        {{ candidate.expressionHash }}
      </el-descriptions-item>
    </el-descriptions>

    <!-- 表达式 -->
    <el-card style="margin-top: 20px">
      <template #header>
        {{ t('factorMining.candidateDetail.expression') }}
      </template>
      <pre style="background: #f5f7fa; padding: 10px; border-radius: 4px; overflow-x: auto">{{ JSON.stringify(candidate?.expression, null, 2) }}</pre>
    </el-card>

    <!-- 指标 -->
    <el-card v-if="candidate?.metrics" style="margin-top: 20px">
      <template #header>
        {{ t('factorMining.candidateDetail.metrics') }}
      </template>
      <el-descriptions :column="2" border>
        <el-descriptions-item :label="t('factorMining.candidateDetail.coverage')">
          {{ formatPercent(candidate.metrics.coverage) }}
        </el-descriptions-item>
        <el-descriptions-item :label="t('factorMining.candidateDetail.rankIc')">
          {{ formatNumber(candidate.metrics.rankIc) }}
        </el-descriptions-item>
        <el-descriptions-item :label="t('factorMining.candidateDetail.icir')">
          {{ formatNumber(candidate.metrics.icir) }}
        </el-descriptions-item>
        <el-descriptions-item :label="t('factorMining.candidateDetail.averageTurnover')">
          {{ formatPercent(candidate.metrics.averageTurnover) }}
        </el-descriptions-item>
        <el-descriptions-item :label="t('factorMining.candidateDetail.icObservations')">
          {{ (candidate.metrics.icSeries || []).length }}
        </el-descriptions-item>
        <el-descriptions-item :label="t('factorMining.candidateDetail.stability')">
          {{ formatNumber(candidate.metrics.stability?.firstHalfIc) }} /
          {{ formatNumber(candidate.metrics.stability?.secondHalfIc) }}
        </el-descriptions-item>
      </el-descriptions>
    </el-card>

    <!-- 样本外表现 -->
    <el-card v-if="candidate?.outOfSample" style="margin-top: 20px">
      <template #header>
        {{ t('factorMining.candidateDetail.outOfSample') }}
      </template>
      <el-descriptions :column="2" border>
        <el-descriptions-item :label="t('factorMining.candidateDetail.oosRankIc')">
          {{ formatNumber(candidate.outOfSample.rankIc) }}
        </el-descriptions-item>
        <el-descriptions-item :label="t('factorMining.candidateDetail.icir')">
          {{ formatNumber(candidate.outOfSample.icir) }}
        </el-descriptions-item>
      </el-descriptions>
    </el-card>

    <!-- 淘汰原因 -->
    <el-alert
      v-if="candidate?.reason"
      :title="t('factorMining.candidates.rejectReason')"
      type="warning"
      :description="t(`factorMining.errors.${candidate.reason}`) || candidate.reason"
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

const props = defineProps({
  candidateId: {
    type: Number,
    required: true
  }
})

const { t } = useI18n()

const candidate = ref(null)
const loading = ref(false)

const loadCandidateDetail = async () => {
  loading.value = true
  try {
    candidate.value = await factorMiningApi.getCandidateDetail(props.candidateId)
  } catch (error) {
    ElMessage.error(t(`factorMining.errors.${error.message}`) || error.message)
  } finally {
    loading.value = false
  }
}

const getStatusType = (status) => {
  const map = {
    search: 'warning',
    frozen: 'success',
    rejected: 'info'
  }
  return map[status] || 'info'
}

const formatNumber = (value) => {
  if (value === null || value === undefined) return '-'
  return Number(value).toFixed(4)
}

const formatPercent = (value) => {
  if (value === null || value === undefined) return '-'
  return `${(Number(value) * 100).toFixed(2)}%`
}

onMounted(() => {
  loadCandidateDetail()
})
</script>

<style scoped>
.candidate-detail {
  min-height: 400px;
}

pre {
  font-size: 12px;
  line-height: 1.4;
}
</style>

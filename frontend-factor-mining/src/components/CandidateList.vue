<template>
  <div class="candidate-list">
    <el-card>
      <template #header>
        <div class="card-header">
          <span>{{ t('factorMining.candidates.title') }}</span>
        </div>
      </template>

      <!-- 筛选条件 -->
      <el-form :inline="true" :model="filters" class="filter-form">
        <el-form-item :label="t('factorMining.candidates.status')">
          <el-select v-model="filters.status" placeholder="全部状态" clearable>
            <el-option label="搜索通过" value="search" />
            <el-option label="冻结通过" value="frozen" />
            <el-option label="已淘汰" value="rejected" />
          </el-select>
        </el-form-item>

        <el-form-item :label="t('factorMining.candidates.market')">
          <el-select v-model="filters.market" placeholder="全部市场" clearable>
            <el-option label="美股" value="USStock" />
            <el-option label="A股" value="CNStock" />
            <el-option label="加密货币" value="Crypto" />
          </el-select>
        </el-form-item>

        <el-form-item :label="t('factorMining.candidates.runId')">
          <el-input-number v-model="filters.runId" placeholder="运行 ID" clearable />
        </el-form-item>

        <el-form-item>
          <el-button type="primary" @click="handleSearch">
            {{ t('factorMining.candidates.search') }}
          </el-button>
          <el-button @click="handleReset">
            {{ t('factorMining.candidates.reset') }}
          </el-button>
        </el-form-item>
      </el-form>

      <!-- 候选列表 -->
      <el-table :data="candidates" v-loading="loading" stripe>
        <el-table-column type="expand">
          <template #default="{ row }">
            <div style="padding: 20px">
              <el-descriptions :column="2" border>
                <el-descriptions-item :label="t('factorMining.candidates.expressionHash')">
                  <el-text truncated style="max-width: 300px">
                    {{ row.expressionHash }}
                  </el-text>
                </el-descriptions-item>
                <el-descriptions-item :label="t('factorMining.candidates.templateId')">
                  {{ row.templateId }}
                </el-descriptions-item>
                <el-descriptions-item :label="t('factorMining.candidates.origin')">
                  {{ row.origin }}
                </el-descriptions-item>
                <el-descriptions-item :label="t('factorMining.candidates.rejectReason')" v-if="row.reason">
                  {{ t(`factorMining.errors.${row.reason}`) || row.reason }}
                </el-descriptions-item>
                <el-descriptions-item :label="t('factorMining.candidates.createdAt')">
                  {{ formatDateTime(row.createdAt) }}
                </el-descriptions-item>
                <el-descriptions-item :label="t('factorMining.candidates.updatedAt')">
                  {{ formatDateTime(row.updatedAt) }}
                </el-descriptions-item>
              </el-descriptions>

              <el-divider />

              <div style="margin-top: 10px">
                <strong>表达式:</strong>
                <pre style="background: #f5f7fa; padding: 10px; border-radius: 4px; overflow-x: auto">{{ JSON.stringify(row.expression, null, 2) }}</pre>
              </div>
            </div>
          </template>
        </el-table-column>

        <el-table-column prop="id" label="ID" width="80" />

        <el-table-column :label="t('factorMining.candidates.status')" width="120">
          <template #default="{ row }">
            <el-tag :type="getStatusType(row.status)">
              {{ t(`factorMining.candidateStatus.${row.status}`) }}
            </el-tag>
          </template>
        </el-table-column>

        <el-table-column prop="market" :label="t('factorMining.candidates.market')" width="100" />

        <el-table-column prop="timeframe" :label="t('factorMining.submit.timeframe')" width="100" />

        <el-table-column :label="t('factorMining.candidates.templateId')" width="200">
          <template #default="{ row }">
            <el-tooltip :content="t(`factorMining.templates.factorMining.template.${row.templateId}`)" placement="top">
              <span>{{ row.templateId }}</span>
            </el-tooltip>
          </template>
        </el-table-column>

        <el-table-column :label="t('factorMining.candidates.expressionHash')" width="180">
          <template #default="{ row }">
            <el-text truncated style="max-width: 150px">
              {{ row.expressionHash }}
            </el-text>
          </template>
        </el-table-column>

        <el-table-column :label="t('factorMining.candidates.createdAt')" width="180">
          <template #default="{ row }">
            {{ formatDateTime(row.createdAt) }}
          </template>
        </el-table-column>

        <el-table-column :label="t('factorMining.candidates.actions')" fixed="right" width="120">
          <template #default="{ row }">
            <el-button
              type="primary"
              size="small"
              @click="viewDetail(row)"
            >
              {{ t('factorMining.candidates.viewDetail') }}
            </el-button>
          </template>
        </el-table-column>
      </el-table>

      <el-empty v-if="!loading && candidates.length === 0" :description="t('factorMining.candidates.noCandidates')" />

      <!-- 分页 -->
      <div class="pagination" v-if="total > 0">
        <el-button
          :disabled="filters.offset === 0"
          @click="handlePrevPage"
        >
          上一页
        </el-button>
        <span style="margin: 0 20px">
          {{ Math.floor(filters.offset / filters.limit) + 1 }} / {{ Math.ceil(total / filters.limit) }}
        </span>
        <el-button
          :disabled="filters.offset + filters.limit >= total"
          @click="handleNextPage"
        >
          下一页
        </el-button>
      </div>
    </el-card>

    <!-- 候选详情对话框 -->
    <el-dialog
      v-model="detailVisible"
      :title="t('factorMining.candidateDetail.title')"
      width="80%"
      destroy-on-close
    >
      <CandidateDetail v-if="selectedCandidate" :candidate-id="selectedCandidate.id" />
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, reactive, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { factorMiningApi } from '@/api/factorMining'
import { useI18n } from '@/hooks/useI18n'
import CandidateDetail from './CandidateDetail.vue'
import dayjs from 'dayjs'

const { t } = useI18n()

const candidates = ref([])
const loading = ref(false)
const total = ref(0)
const detailVisible = ref(false)
const selectedCandidate = ref(null)

const filters = reactive({
  status: '',
  market: '',
  runId: null,
  limit: 50,
  offset: 0
})

const loadCandidates = async () => {
  loading.value = true
  try {
    const params = { ...filters }
    // 清理空值
    Object.keys(params).forEach(key => {
      if (params[key] === '' || params[key] === null) {
        delete params[key]
      }
    })

    const data = await factorMiningApi.getCandidates(params)
    candidates.value = data || []
    // 注意：后端返回的是数组，没有 total，这里简化处理
    total.value = data?.length || 0
  } catch (error) {
    ElMessage.error(t(`factorMining.errors.${error.message}`) || error.message)
  } finally {
    loading.value = false
  }
}

const handleSearch = () => {
  filters.offset = 0
  loadCandidates()
}

const handleReset = () => {
  filters.status = ''
  filters.market = ''
  filters.runId = null
  filters.offset = 0
  loadCandidates()
}

const handlePrevPage = () => {
  if (filters.offset > 0) {
    filters.offset = Math.max(0, filters.offset - filters.limit)
    loadCandidates()
  }
}

const handleNextPage = () => {
  filters.offset += filters.limit
  loadCandidates()
}

const getStatusType = (status) => {
  const map = {
    search: 'warning',
    frozen: 'success',
    rejected: 'info'
  }
  return map[status] || 'info'
}

const formatDateTime = (datetime) => {
  if (!datetime) return '-'
  return dayjs(datetime).format('YYYY-MM-DD HH:mm:ss')
}

const viewDetail = (candidate) => {
  selectedCandidate.value = candidate
  detailVisible.value = true
}

onMounted(() => {
  loadCandidates()
})
</script>

<style scoped>
.card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.filter-form {
  margin-bottom: 20px;
}

.pagination {
  display: flex;
  justify-content: center;
  align-items: center;
  margin-top: 20px;
}

pre {
  font-size: 12px;
  line-height: 1.4;
}
</style>

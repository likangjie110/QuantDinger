<template>
  <div class="submit-job">
    <el-card>
      <template #header>
        <div class="card-header">
          <span>{{ t('factorMining.submit.title') }}</span>
        </div>
      </template>

      <el-form :model="form" :rules="rules" ref="formRef" label-width="120px">
        <el-form-item :label="t('factorMining.submit.sourceId')" prop="sourceId">
          <el-input-number v-model="form.sourceId" :min="1" placeholder="请输入策略源 ID" />
        </el-form-item>

        <el-form-item :label="t('factorMining.submit.market')" prop="market">
          <el-select v-model="form.market" placeholder="请选择市场">
            <el-option label="美股" value="USStock" />
            <el-option label="A股" value="CNStock" />
            <el-option label="加密货币" value="Crypto" />
          </el-select>
        </el-form-item>

        <el-form-item :label="t('factorMining.submit.timeframe')" prop="timeframe">
          <el-select v-model="form.timeframe" placeholder="请选择时间周期">
            <el-option label="1 分钟" value="1m" />
            <el-option label="5 分钟" value="5m" />
            <el-option label="15 分钟" value="15m" />
            <el-option label="1 小时" value="1H" />
            <el-option label="4 小时" value="4H" />
            <el-option label="1 天" value="1D" />
          </el-select>
        </el-form-item>

        <el-form-item :label="t('factorMining.submit.startDate')" prop="startDate">
          <el-date-picker
            v-model="form.startDate"
            type="date"
            value-format="YYYY-MM-DD"
            placeholder="选择开始日期"
          />
        </el-form-item>

        <el-form-item :label="t('factorMining.submit.endDate')" prop="endDate">
          <el-date-picker
            v-model="form.endDate"
            type="date"
            value-format="YYYY-MM-DD"
            placeholder="选择结束日期"
          />
        </el-form-item>

        <el-form-item :label="t('factorMining.submit.budget')" prop="budget">
          <el-input-number v-model="form.budget" :min="1" :max="2000" />
          <span class="hint">{{ t('factorMining.submit.budgetHint') }}</span>
        </el-form-item>

        <el-form-item :label="t('factorMining.submit.seed')" prop="seed">
          <el-input-number v-model="form.seed" :min="0" />
          <span class="hint">{{ t('factorMining.submit.seedHint') }}</span>
        </el-form-item>

        <el-collapse>
          <el-collapse-item title="高级选项" name="advanced">
            <el-form-item :label="t('factorMining.submit.freezeRatio')">
              <el-input-number v-model="form.freezeRatio" :min="0.1" :max="0.49" :step="0.05" />
              <span class="hint">{{ t('factorMining.submit.freezeRatioHint') }}</span>
            </el-form-item>

            <el-form-item :label="t('factorMining.submit.groups')">
              <el-input-number v-model="form.groups" :min="3" :max="20" />
            </el-form-item>

            <el-form-item :label="t('factorMining.submit.holdingPeriod')">
              <el-input-number v-model="form.holdingPeriod" :min="1" :max="60" />
            </el-form-item>

            <el-form-item :label="t('factorMining.submit.commission')">
              <el-input-number v-model="form.commission" :min="0" :max="0.01" :step="0.0001" :precision="4" />
            </el-form-item>

            <el-form-item :label="t('factorMining.submit.slippage')">
              <el-input-number v-model="form.slippage" :min="0" :max="0.01" :step="0.0001" :precision="4" />
            </el-form-item>
          </el-collapse-item>
        </el-collapse>

        <el-form-item>
          <el-button type="primary" @click="handleEstimate" :loading="estimating">
            {{ estimating ? t('factorMining.submit.estimating') : t('factorMining.submit.estimate') }}
          </el-button>
          <el-button type="success" @click="handleSubmit" :loading="submitting" :disabled="!estimated">
            {{ submitting ? t('factorMining.submit.submitting') : t('factorMining.submit.submit') }}
          </el-button>
        </el-form-item>

        <el-alert
          v-if="estimateResult"
          :title="t('factorMining.submit.estimateResult')"
          type="info"
          :closable="false"
          style="margin-top: 20px"
        >
          <div>{{ t('factorMining.submit.templateCount') }}: {{ estimateResult.templateCount }}</div>
          <div>{{ t('factorMining.submit.candidateBudget') }}: {{ estimateResult.candidateBudget }}</div>
        </el-alert>
      </el-form>
    </el-card>
  </div>
</template>

<script setup>
import { ref, reactive } from 'vue'
import { ElMessage } from 'element-plus'
import { factorMiningApi } from '@/api/factorMining'
import { useI18n } from '@/hooks/useI18n'

const { t } = useI18n()

const formRef = ref(null)
const estimating = ref(false)
const submitting = ref(false)
const estimated = ref(false)
const estimateResult = ref(null)

const form = reactive({
  sourceId: null,
  market: 'USStock',
  timeframe: '1D',
  startDate: '',
  endDate: '',
  budget: 400,
  seed: 42,
  freezeRatio: 0.3,
  groups: 10,
  holdingPeriod: 5,
  commission: 0.001,
  slippage: 0.0005
})

const rules = {
  sourceId: [{ required: true, message: '请输入策略源 ID', trigger: 'blur' }],
  market: [{ required: true, message: '请选择市场', trigger: 'change' }],
  startDate: [{ required: true, message: '请选择开始日期', trigger: 'change' }],
  endDate: [{ required: true, message: '请选择结束日期', trigger: 'change' }]
}

const handleEstimate = async () => {
  const valid = await formRef.value.validate().catch(() => false)
  if (!valid) return

  estimating.value = true
  try {
    const result = await factorMiningApi.estimate(form)
    estimateResult.value = result
    estimated.value = true
    ElMessage.success('估价成功')
  } catch (error) {
    ElMessage.error(t(`factorMining.errors.${error.message}`) || error.message)
  } finally {
    estimating.value = false
  }
}

const handleSubmit = async () => {
  const valid = await formRef.value.validate().catch(() => false)
  if (!valid) return

  submitting.value = true
  try {
    const result = await factorMiningApi.submitRun(form)
    ElMessage.success(`作业已提交，作业 ID: ${result.jobId}`)
    estimated.value = false
    estimateResult.value = null
  } catch (error) {
    ElMessage.error(t(`factorMining.errors.${error.message}`) || error.message)
  } finally {
    submitting.value = false
  }
}
</script>

<style scoped>
.card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.hint {
  margin-left: 10px;
  font-size: 12px;
  color: #909399;
}
</style>

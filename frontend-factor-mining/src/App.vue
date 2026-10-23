<template>
  <div id="app">
    <el-container>
      <el-header>
        <h1>{{ t('factorMining.title') }}</h1>
        <p class="subtitle">{{ t('factorMining.subtitle') }}</p>
      </el-header>

      <el-main>
        <el-tabs v-model="activeTab" type="card">
          <el-tab-pane :label="t('factorMining.tabs.submit')" name="submit">
            <SubmitJob />
          </el-tab-pane>

          <el-tab-pane :label="t('factorMining.tabs.jobs')" name="jobs">
            <JobList />
          </el-tab-pane>

          <el-tab-pane :label="t('factorMining.tabs.candidates')" name="candidates">
            <CandidateList />
          </el-tab-pane>
        </el-tabs>
      </el-main>
    </el-container>
  </div>
</template>

<script setup>
import { ref } from 'vue'
import { useI18n } from '@/hooks/useI18n'
import SubmitJob from '@/components/SubmitJob.vue'
import JobList from '@/components/JobList.vue'
import CandidateList from '@/components/CandidateList.vue'

const { t } = useI18n()
/** @type {import('vue').Ref<string | number>} */
const activeTab = ref('submit')
</script>

<style>
* {
  margin: 0;
  padding: 0;
  box-sizing: border-box;
}

#app {
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial,
    'Noto Sans', sans-serif, 'Apple Color Emoji', 'Segoe UI Emoji', 'Segoe UI Symbol',
    'Noto Color Emoji';
  -webkit-font-smoothing: antialiased;
  -moz-osx-font-smoothing: grayscale;
  color: #2c3e50;
  min-height: 100vh;
  background-color: #f5f7fa;
}

.el-header {
  background-color: #409eff;
  color: white;
  padding: 20px;
  text-align: center;
}

.el-header h1 {
  margin: 0;
  font-size: 28px;
}

.subtitle {
  margin-top: 8px;
  font-size: 14px;
  opacity: 0.9;
}

.el-main {
  padding: 20px;
  max-width: 1400px;
  margin: 0 auto;
}
</style>

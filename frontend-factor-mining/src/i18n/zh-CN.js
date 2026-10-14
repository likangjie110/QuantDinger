export default {
  factorMining: {
    // 页面标题
    title: '全自动因子挖掘',
    subtitle: '提假设 → 造表达式 → 逐层筛选 → 样本外复核 → 沉淀候选库',

    // 标签页
    tabs: {
      submit: '提交作业',
      jobs: '作业列表',
      candidates: '候选库'
    },

    // 提交作业表单
    submit: {
      title: '配置挖掘作业',
      sourceId: '策略源 ID',
      sourceName: '策略源名称',
      market: '市场',
      timeframe: '时间周期',
      startDate: '开始日期',
      endDate: '结束日期',
      budget: '预算',
      budgetHint: '候选数量上限（默认 400，最大 2000）',
      seed: '随机种子',
      seedHint: '相同种子产生相同候选序列',
      freezeRatio: '冻结比例',
      freezeRatioHint: '用于样本外验证的比例（默认 0.3）',
      templateIds: '模板选择',
      templateIdsHint: '留空使用全部 8 个模板',
      groups: '分组数',
      holdingPeriod: '持仓周期',
      commission: '手续费率',
      slippage: '滑点',
      estimate: '估价',
      estimating: '估价中...',
      submit: '提交作业',
      submitting: '提交中...',
      estimateResult: '估价结果',
      templateCount: '模板数量',
      candidateBudget: '候选数量'
    },

    // 作业状态
    jobStatus: {
      queued: '排队中',
      running: '运行中',
      succeeded: '成功',
      failed: '失败',
      cancelled: '已取消'
    },

    // 作业列表
    jobs: {
      title: '作业历史',
      jobId: '作业 ID',
      status: '状态',
      market: '市场',
      dateRange: '日期范围',
      budget: '预算',
      progress: '进度',
      createdAt: '创建时间',
      startedAt: '开始时间',
      finishedAt: '完成时间',
      actions: '操作',
      viewDetail: '查看详情',
      cancel: '取消',
      refresh: '刷新',
      noJobs: '暂无作业记录'
    },

    // 作业详情
    jobDetail: {
      title: '作业详情',
      basicInfo: '基本信息',
      config: '配置信息',
      result: '运行结果',
      trials: '试验总数',
      candidates: '候选总数',
      frozen: '冻结通过数',
      snapshotId: '数据指纹',
      searchWindow: '搜索窗口',
      frozenWindow: '冻结窗口',
      error: '错误信息'
    },

    // 候选状态
    candidateStatus: {
      search: '搜索通过',
      frozen: '冻结通过',
      rejected: '已淘汰'
    },

    // 候选库
    candidates: {
      title: '候选库',
      filter: '筛选',
      status: '状态',
      market: '市场',
      runId: '运行 ID',
      search: '搜索',
      reset: '重置',
      expressionHash: '表达式指纹',
      templateId: '模板',
      origin: '来源',
      rejectReason: '淘汰原因',
      createdAt: '创建时间',
      updatedAt: '更新时间',
      viewDetail: '查看详情',
      noCandidates: '暂无候选记录',
      loadMore: '加载更多'
    },

    // 候选详情
    candidateDetail: {
      title: '候选详情',
      expression: '表达式',
      metrics: '指标',
      outOfSample: '样本外表现',
      coverage: '覆盖率',
      rankIc: 'Rank IC',
      icir: 'ICIR',
      averageTurnover: '平均换手率',
      icObservations: 'IC 观测数',
      foldIcSigns: '分折符号',
      stability: '稳定性',
      factorCorrelation: '因子相关度',
      oosRankIc: '样本外 Rank IC',
      deflatedSharpe: '通缩夏普',
      deflatedSharpeProbability: 'DSR 概率'
    },

    // DSL 词表
    catalog: {
      title: 'DSL 词表',
      fields: '字段',
      operators: '算子',
      templates: '模板',
      operatorCount: '个算子',
      templateCount: '个模板'
    },

    // 错误信息
    errors: {
      'factorMining.runFailed': '提交作业失败',
      'factorMining.estimateFailed': '估价失败',
      'factorMining.jobNotFound': '作业不存在',
      'factorMining.candidateNotFound': '候选不存在',
      'factorMining.invalidQuery': '查询参数无效',
      'factorMining.invalidParameter': '参数无效',
      'factorMining.thresholdsInvalid': '门槛配置无效',
      'factorMining.sourceRequired': '缺少策略源',
      'factorMining.sourceNotFound': '策略源不存在',
      'factorMining.sourceCodeRequired': '策略源代码为空',
      'factorMining.dateRangeRequired': '缺少日期范围',
      'factorMining.dateRangeTooShort': '日期范围过短',
      'factorMining.invalidMarket': '无效的市场',
      'factorMining.budgetInvalid': '预算无效',
      'factorMining.budgetExceeded': '预算超出上限（最大 2000）',
      'factorMining.concurrentLimit': '已有活跃作业，请稍后再试',
      'factorMining.workerInterrupted': '工作进程中断',
      'factorMining.freezeRatioInvalid': '冻结比例无效',
      'factorMining.searchFreezeOverlap': '搜索段与冻结段重叠',
      'factorMining.universeTooSmall': '资产池成员不足 3 个',
      'factorMining.usableUniverseTooSmall': '可用标的不足 3 个',
      'factorMining.noMarketData': '无市场数据',
      'factorMining.missingIcSeries': '缺少 IC 序列',
      'factorMining.runNotFound': '运行记录不存在',

      // 淘汰原因
      'factorMining.reject.invalid': 'DSL 校验失败',
      'factorMining.reject.lowCoverage': '覆盖率过低',
      'factorMining.reject.insufficientObservations': 'IC 观测数不足',
      'factorMining.reject.weakIc': 'IC 强度过弱',
      'factorMining.reject.weakIcir': 'ICIR 过低',
      'factorMining.reject.unstableFolds': '分折不一致',
      'factorMining.reject.unstableHalves': '前后半段不稳定',
      'factorMining.reject.highTurnover': '换手率过高',
      'factorMining.reject.duplicateBuiltin': '与内置因子重复',
      'factorMining.reject.unverifiableRedundancy': '无法验证冗余性',
      'factorMining.reject.weakOutOfSample': '样本外表现过弱',
      'factorMining.reject.selectionBias': '选择偏差过高'
    },

    // 模板假设
    templates: {
      'factorMining.template.momentum': '近期涨幅高的标的继续走强',
      'factorMining.template.reversal': '近期涨幅高的标的短期回落',
      'factorMining.template.low_volatility': '波动低的标的风险调整后表现更好',
      'factorMining.template.liquidity': '成交量相对基线放大预示关注度变化',
      'factorMining.template.liquidity_level': '成交量的绝对水平（截面排名）本身携带信息',
      'factorMining.template.volume_price_confirm': '量价同向确认时动量更可靠',
      'factorMining.template.risk_adjusted_momentum': '单位波动的动量优于原始动量',
      'factorMining.template.conditional_filter': '动量只在成交量处于高位区间时有效'
    }
  }
}

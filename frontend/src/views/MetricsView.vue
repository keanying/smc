<!--
  景区社媒采集系统 · Copyright (c) 2026 keanying
  SPDX-License-Identifier: LicenseRef-SMC-NCLL-1.0
  按《SMC 非商业学习许可证 1.0》授权：仅限学习与研究，禁止任何商业用途，
  不得用于大规模抓取、绕过平台防护或非法获取个人信息。
  首次运行即表示已阅读并同意根目录 LICENSE，使用风险与法律责任由使用者自行承担。
-->

<template>
  <div class="metrics-view" v-loading="loading">
    <div class="page-header">
      <h2 class="page-title">
        指标计算
        <el-tag v-if="status?.running_run_id" type="warning" size="small" class="title-tag">计算中</el-tag>
        <el-tag v-else-if="cfg?.schedule.enabled" type="success" size="small" class="title-tag">定时已开启</el-tag>
        <InfoTip :width="360">
          用采集到的评论（含 AI 标注结果）算舆情指标，写入六张 ADS 表，可定时计算、算完推送给下游。<br>
          计算在独立进程里跑，和采集互不影响。每次计算会顺带重跑目标日期之前几天（评论会晚到）。
        </InfoTip>
      </h2>
      <div class="header-actions">
        <span v-if="status?.next_fire" class="muted next-fire">下次定时 {{ status.next_fire }}</span>
        <el-button v-if="tab !== 'run'" type="primary" :loading="saving" @click="saveConfig">
          保存配置
        </el-button>
      </div>
    </div>

    <el-empty v-if="status && !status.enabled" description="指标计算还没开启">
      <el-button type="primary" @click="router.push('/settings')">去系统设置开启</el-button>
    </el-empty>

    <el-tabs v-else-if="cfg" v-model="tab" class="metric-tabs">
      <!-- ==================== 计算 ==================== -->
      <el-tab-pane label="计算" name="run">
        <el-card shadow="never" class="block">
          <template #header>立即计算</template>
          <div class="run-form">
            <el-date-picker
              v-model="runDates" type="daterange" value-format="YYYYMMDD" format="YYYY-MM-DD"
              range-separator="至" start-placeholder="开始日期" end-placeholder="结束日期"
              :clearable="false" class="run-dates"
            />
            <el-select
              v-model="runScenics" multiple filterable clearable collapse-tags collapse-tags-tooltip
              :placeholder="scenicPlaceholder" class="run-scenics"
            >
              <el-option v-for="s in scenicOptions" :key="s.scenic_id"
                         :label="`${s.scenic_name}（${s.scenic_id}）`" :value="s.scenic_id" />
            </el-select>
            <el-checkbox v-if="cfg.push.enabled" v-model="runNoPush">本次不推送</el-checkbox>
            <el-button type="primary" :loading="starting" :disabled="!!status?.running_run_id"
                       @click="startRun">
              <AppIcon v-if="!starting" name="play" :size="14" class="btn-icon" />开始计算
            </el-button>
          </div>
          <div class="muted hint">
            目标日期之外还会重跑前 {{ cfg.params.rerun_days }} 天；不选景区 =
            {{ cfg.scenics.length ? `配置里的 ${cfg.scenics.length} 个景区` : '取数区间里有评论的全部景区' }}。
            {{ cfg.push.enabled ? (cfg.push.dry_run ? '推送是「只组报文不发送」模式。' : '算完推送到 ' + cfg.push.url) : '推送未开启。' }}
          </div>
        </el-card>

        <el-card shadow="never">
          <template #header>
            <div class="card-head">
              <span>运行记录</span>
              <el-button link type="primary" @click="loadRuns">刷新</el-button>
            </div>
          </template>
          <el-table :data="runs" size="small" class="data-table" max-height="calc(100vh - 430px)">
            <el-table-column label="开始时间" width="160" prop="started_at" />
            <el-table-column label="触发" width="70">
              <template #default="{ row }">
                <el-tag v-if="row.trigger_type === 'mock'" size="small" type="warning">演示</el-tag>
                <template v-else>{{ row.trigger_type === 'schedule' ? '定时' : '手动' }}</template>
              </template>
            </el-table-column>
            <el-table-column label="目标日期" width="190">
              <template #default="{ row }">
                {{ fmtDate(row.start_date) }}<template v-if="row.end_date !== row.start_date"> ~ {{ fmtDate(row.end_date) }}</template>
              </template>
            </el-table-column>
            <el-table-column label="景区" width="90">
              <template #default="{ row }">{{ row.trigger_type === 'mock' ? 1 : (row.scenics.length || '全部') }}</template>
            </el-table-column>
            <el-table-column label="状态" width="100">
              <template #default="{ row }">
                <el-tag :type="STATUS[row.status]?.type" size="small">{{ STATUS[row.status]?.label || row.status }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="用时" width="90">
              <template #default="{ row }">{{ duration(row) }}</template>
            </el-table-column>
            <el-table-column label="结果" min-width="260" show-overflow-tooltip>
              <template #default="{ row }">{{ resultText(row) }}</template>
            </el-table-column>
            <el-table-column label="操作" width="96" fixed="right">
              <template #default="{ row }">
                <IconAction icon="view" tip="详情与日志" @click="openRun(row)" />
                <el-popconfirm v-if="row.status === 'running'" title="停止这次计算？已写完的景区不会回滚。"
                               width="240" @confirm="cancelRun(row)">
                  <template #reference><span><IconAction icon="pause" tip="停止" /></span></template>
                </el-popconfirm>
              </template>
            </el-table-column>
            <template #empty><el-empty description="还没有算过" :image-size="72" /></template>
          </el-table>
          <el-pagination
            v-if="runsTotal > runsPageSize" small background layout="prev, pager, next"
            :total="runsTotal" :page-size="runsPageSize" v-model:current-page="runsPage"
            class="pager" @current-change="loadRuns"
          />
        </el-card>
      </el-tab-pane>

      <!-- ==================== 景区与参数 ==================== -->
      <el-tab-pane label="景区与参数" name="params">
        <el-row :gutter="16">
          <el-col :xs="24" :lg="15">
            <!-- 景区和参数是同一次计算的设置：放进一张卡片，从上往下就是「算谁 → 怎么取数 → 什么口径 → 出哪些表 → 存哪」 -->
            <el-card shadow="never" class="block">
              <template #header>
                <div class="card-head">
                  <span>计算配置</span>
                  <span class="muted head-hint">鼠标移到某一项上，右侧会标出它影响的表</span>
                </div>
              </template>
              <el-form label-width="110px" class="param-form">
                <div class="form-group-title">范围</div>
                <el-form-item label="景区" class="param-item" @mouseenter="hl = ALL_TABLES" @mouseleave="hl = []">
                  <el-select v-model="cfg.scenics" multiple filterable clearable class="w-full"
                             placeholder="全部有评论的景区">
                    <el-option v-for="s in scenicOptions" :key="s.scenic_id"
                               :label="`${s.scenic_name}（${s.scenic_id}）`" :value="s.scenic_id" />
                  </el-select>
                  <ParamNote text="不选 = 每次计算取数区间里有评论的全部景区" tables="all" />
                </el-form-item>

                <div class="form-group-title">取数</div>
                <el-form-item label="取数回溯" class="param-item" @mouseenter="hl = ALL_TABLES" @mouseleave="hl = []">
                  <el-input-number v-model="cfg.params.lookback_days" :min="7" :max="1100" /><span class="unit">天</span>
                  <ParamNote text="往前读多少天的评论参与计算" tables="all" :tips="[
                    '近 N 日环比要 2N 天历史：近 90 日环比需要 180 天',
                    '历史不够时，长窗口退化成「已有历史累计」，几个周期的得分可能一样（不是错）',
                  ]" />
                </el-form-item>
                <el-form-item label="往前重跑" class="param-item" @mouseenter="hl = ALL_TABLES" @mouseleave="hl = []">
                  <el-input-number v-model="cfg.params.rerun_days" :min="0" :max="60" /><span class="unit">天</span>
                  <ParamNote text="每次计算顺带重算目标日期之前几天" tables="all" :tips="[
                    '评论会晚到约 1 天，不重跑的话前几天的数据停在「当时没采齐」的样子',
                    '0 = 不重跑',
                  ]" />
                </el-form-item>
                <el-form-item label="缺数回补" class="param-item" @mouseenter="hl = ALL_TABLES" @mouseleave="hl = []">
                  <el-radio-group v-model="cfg.params.backfill_mode">
                    <el-radio-button value="fill">复制最近一天</el-radio-button>
                    <el-radio-button value="off">记 0</el-radio-button>
                  </el-radio-group>
                  <template v-if="cfg.params.backfill_mode === 'fill'">
                    <span class="unit">最多往前找</span>
                    <el-input-number v-model="cfg.params.backfill_lookback" :min="0" :max="365" size="small" class="num-sm" />
                    <span class="unit">天</span>
                  </template>
                  <ParamNote text="某个平台某天一条评论都没有时怎么算" tables="all" :tips="[
                    '复制最近一天：复制往前最近一个有评论那天的明细再算，趋势不断档',
                    '记 0：当天按 0 条评论算',
                    '往前找不到（超过天数）的，这一天就是 0',
                  ]" />
                </el-form-item>

                <div class="form-group-title">口径</div>
                <el-form-item label="得分权重" class="param-item" @mouseenter="hl = ['core', 'dimension', 'macro']" @mouseleave="hl = []">
                  <div class="weights">
                    <span>正面</span><el-input-number v-model="cfg.params.score_weights.positive" :min="0" :max="1" :step="0.05" :precision="2" size="small" class="num-sm" />
                    <span>中性</span><el-input-number v-model="cfg.params.score_weights.neutral" :min="0" :max="1" :step="0.05" :precision="2" size="small" class="num-sm" />
                    <span>负面</span><el-input-number v-model="cfg.params.score_weights.negative" :min="0" :max="1" :step="0.05" :precision="2" size="small" class="num-sm" />
                  </div>
                  <ParamNote text="得分 = 5 ×（好评率×正面 + 中评率×中性 + 差评率×负面）"
                             :tables="['core', 'dimension', 'macro']" :tips="[
                    '默认 1.00 / 0.90 / 0.50，得分在 2.5 ~ 5 分之间',
                    '改了之后历史分数不会自动变，要把历史日期重新计算一遍',
                  ]" />
                </el-form-item>
                <el-form-item label="关键词保留" class="param-item" @mouseenter="hl = ['content']" @mouseleave="hl = []">
                  <el-input-number v-model="cfg.params.content_top_words" :min="0" :max="100000" /><span class="unit">个</span>
                  <ParamNote text="关键词云每天每种情感只留热度前 N 个词" :tables="['content']"
                             :tips="['0 = 全部保留：表会比其他几张加起来还大']" />
                </el-form-item>

                <div class="form-group-title">产出</div>
                <el-form-item label="下钻明细" class="param-item" @mouseenter="hl = ['drill']" @mouseleave="hl = []">
                  <el-switch v-model="cfg.params.enable_drill_analysis" />
                  <template v-if="cfg.params.enable_drill_analysis">
                    <el-checkbox v-model="cfg.params.drill_keyword_filter" class="inline-check">只收评论内容含关键字的</el-checkbox>
                    <el-checkbox v-model="cfg.params.drill_mask_content" class="inline-check">评论摘要只露关键词</el-checkbox>
                  </template>
                  <ParamNote text="词 → 评论 → 作品的下钻明细" :tables="['drill']" :tips="[
                    '只收评论内容含关键字的：只看评论自己的内容，不看帖子 / 笔记 / 作品的标题',
                    '关键字 = 本景区启用的主关键字 + 附关键字；过滤关键字、停用的关键字不算',
                    '大盘 KPI 词云（wordcloud_map）里的词也会写进下钻表，看板上点词云能下钻到评论',
                    '评论摘要只露关键词：命中的关键词保留，其余内容换成 **',
                  ]" />
                </el-form-item>
                <el-form-item label="大盘 KPI" class="param-item" @mouseenter="hl = ['macro']" @mouseleave="hl = []">
                  <el-switch v-model="cfg.params.enable_macro_metric" />
                  <ParamNote text="各周期（今日、近 7 日、本月…）× 各渠道的 KPI" :tables="['macro']" />
                </el-form-item>

                <div class="form-group-title">存储</div>
                <el-form-item label="写入 ADS 表" class="param-item" @mouseenter="hl = ALL_TABLES" @mouseleave="hl = []">
                  <el-switch v-model="cfg.params.write_db" />
                  <ParamNote text="关掉 = 只计算 + 推送，不写本地库" tables="all" />
                </el-form-item>
                <el-form-item label="ADS 库" class="param-item" @mouseenter="hl = ALL_TABLES" @mouseleave="hl = []">
                  <el-input v-model="cfg.params.ads_database" placeholder="同采集库" class="short-input" />
                  <ParamNote text="指标表放在哪个库，留空 = 和采集数据同一个库" tables="all" />
                </el-form-item>
              </el-form>
            </el-card>
          </el-col>

          <el-col :xs="24" :lg="9" class="ads-col">
            <el-card shadow="never" class="block ads-panel">
              <template #header>
                <div class="card-head">
                  <span class="lbl">产出的 ADS 表<InfoTip content="按引擎自带的建表语句建表，已有的不动。表结构以引擎为准" /></span>
                  <el-button size="small" :loading="initing" @click="initTables">建表</el-button>
                </div>
              </template>
              <div v-for="t in tableStatus" :key="t.key" class="ads-row"
                   :class="{ 'is-hl': hl.includes(t.key), 'is-dim': hl.length && !hl.includes(t.key),
                             'is-off': !producing(t.key) }">
                <span class="ads-name">{{ t.label }}</span>
                <span class="mono muted ads-table">{{ t.table }}</span>
                <el-tag v-if="!producing(t.key)" size="small" type="info">不产出</el-tag>
                <el-tag v-else-if="t.exists" size="small" type="success">约 {{ t.approx_rows }} 行</el-tag>
                <el-tag v-else size="small" type="warning">未建</el-tag>
              </div>
              <div class="muted ads-foot">
                {{ hl.length ? `这一项影响 ${hl.length === 6 ? '全部 6 张表' : hl.length + ' 张表'}` : '下钻明细、大盘 KPI 可以在左侧「产出」里关掉' }}
              </div>
            </el-card>
          </el-col>
        </el-row>
      </el-tab-pane>

      <!-- ==================== 定时 ==================== -->
      <el-tab-pane label="定时" name="schedule">
        <el-card shadow="never" class="block narrow">
          <template #header>定时计算</template>
          <el-form label-width="120px">
            <el-form-item label="开启定时">
              <el-switch v-model="cfg.schedule.enabled" />
            </el-form-item>
            <el-form-item>
              <template #label><span class="lbl">执行时间<InfoTip content="5 段 cron：分 时 日 月 周。每次计算「昨天」，再顺带重跑之前几天" /></span></template>
              <div class="cron-row">
                <el-input v-model="cfg.schedule.cron" class="cron-input mono" />
                <el-radio-group v-model="cfg.schedule.cron" size="small">
                  <el-radio-button v-for="p in CRON_PRESETS" :key="p.cron" :value="p.cron">{{ p.label }}</el-radio-button>
                </el-radio-group>
              </div>
            </el-form-item>
            <el-form-item label="下次执行">
              <span>{{ status?.next_fire || (cfg.schedule.enabled ? '保存后计算' : '未开启') }}</span>
            </el-form-item>
          </el-form>
        </el-card>
      </el-tab-pane>

      <!-- ==================== 推送 ==================== -->
      <el-tab-pane label="推送" name="push">
        <el-card shadow="never" class="block narrow">
          <template #header>推送到下游</template>
          <el-form label-width="130px">
            <el-form-item>
              <template #label><span class="lbl">开启推送<InfoTip content="计算、写库之后，把结果按表分批 POST 给下游。推送失败不影响计算结果" /></span></template>
              <el-switch v-model="cfg.push.enabled" />
              <el-checkbox v-model="cfg.push.dry_run" class="inline-check">只组报文不发送（验证配置用）</el-checkbox>
            </el-form-item>
            <el-form-item label="推送地址">
              <el-input v-model="cfg.push.url" placeholder="https://…/data/sync" />
            </el-form-item>
            <el-form-item label="鉴权">
              <div class="auth-row">
                <div class="auth-cell auth-header">
                  <span class="cell-label">请求头名</span>
                  <el-input v-model="cfg.push.token_header" placeholder="xpftkey" />
                </div>
                <div class="auth-cell auth-prefix">
                  <span class="cell-label">前缀（可空）</span>
                  <el-input v-model="cfg.push.token_prefix" placeholder="不加" />
                </div>
                <div class="auth-cell">
                  <span class="cell-label">令牌</span>
                  <el-input v-model="cfg.push.token" type="password" show-password
                            :placeholder="cfg.push.has_token ? '已填写，留空 = 不修改' : 'pfk_…'" />
                </div>
              </div>
              <div class="auth-result">
                实际发送：<code>{{ authPreview }}</code>
                <span class="muted">（Bearer 方式：请求头填 Authorization、前缀填「Bearer 」）</span>
              </div>
            </el-form-item>
            <el-form-item>
              <template #label><span class="lbl">报文字段名<InfoTip content="请求体里表名、主键、数据三个字段叫什么。默认 tableName / pkId / data" /></span></template>
              <div class="body-row">
                <span>表名</span><el-input v-model="cfg.push.body_fields.table" />
                <span>主键</span><el-input v-model="cfg.push.body_fields.pk" />
                <span>数据</span><el-input v-model="cfg.push.body_fields.data" />
              </div>
            </el-form-item>
            <el-form-item>
              <template #label><span class="lbl">附加字段<InfoTip content="原样带进每个请求体的顶层字段，例如 source = smc" /></span></template>
              <div class="extra-list">
                <div v-for="(row, i) in extraRows" :key="i" class="extra-row">
                  <el-input v-model="row.key" placeholder="字段名" />
                  <el-input v-model="row.value" placeholder="值" />
                  <IconAction icon="delete" tip="删除" @click="extraRows.splice(i, 1)" />
                </div>
                <el-button link type="primary" @click="extraRows.push({ key: '', value: '' })">+ 添加</el-button>
              </div>
            </el-form-item>
            <el-form-item label="每批 / 超时 / 重试">
              <el-input-number v-model="cfg.push.batch_size" :min="1" :max="10000" class="num-sm" /><span class="unit">行</span>
              <el-input-number v-model="cfg.push.timeout" :min="1" :max="600" class="num-sm" /><span class="unit">秒</span>
              <el-input-number v-model="cfg.push.retries" :min="0" :max="10" class="num-sm" /><span class="unit">次</span>
            </el-form-item>
            <el-form-item>
              <template #label><span class="lbl">成功标志字段<InfoTip content="响应体是 JSON 且有这个字段时，它为 false 就算这批失败（网关把业务失败写在 200 里）。留空 = 只看 HTTP 状态码" /></span></template>
              <el-input v-model="cfg.push.success_field" placeholder="status" class="short-input" />
            </el-form-item>
            <el-form-item label="核对报文">
              <el-select v-model="previewTable" class="preview-select">
                <el-option v-for="t in TABLE_OPTIONS" :key="t.key" :label="t.label" :value="t.key" />
              </el-select>
              <el-button :loading="previewing" @click="openPreview">预览报文</el-button>
              <span class="muted preview-hint">按已保存的配置组一个真实请求（不发送），对着下游的接口样例核对</span>
            </el-form-item>
          </el-form>
        </el-card>
      </el-tab-pane>

      <!-- ==================== 推送表 ==================== -->
      <el-tab-pane label="推送表" name="tables">
        <el-alert v-if="catalogError" type="error" :closable="false" show-icon :title="catalogError" class="block" />

        <!-- 按景区选推哪几张表：第一行是默认（就是下面每张表的「推送」开关），之后每行一个单独配置的景区 -->
        <el-card shadow="never" class="block">
          <template #header>
            <div class="card-head">
              <span class="lbl">按景区推送<InfoTip :width="320">
                <ul class="tip-list">
                  <li>没有单独添加的景区，按「默认」这一行推送（和下面每张表的「推送」开关是同一个设置）</li>
                  <li>添加过的景区，只推它这一行勾选的表；一个都不勾 = 这个景区不推送</li>
                  <li>只管推不推，表名、主键、字段仍按下面每张表的设置</li>
                </ul>
              </InfoTip></span>
              <div class="add-scenic">
                <el-select v-model="addScenicId" filterable placeholder="选景区单独配置" size="small" class="add-scenic-select">
                  <el-option v-for="s in addableScenics" :key="s.scenic_id"
                             :label="`${s.scenic_name}（${s.scenic_id}）`" :value="s.scenic_id" />
                </el-select>
                <el-button size="small" :disabled="!addScenicId" @click="addScenicPush">添加</el-button>
              </div>
            </div>
          </template>
          <el-table :data="scenicPushRows" size="small" class="data-table push-matrix">
            <el-table-column label="景区" min-width="200">
              <template #default="{ row }">
                <template v-if="row.isDefault">
                  <b>默认</b><span class="muted matrix-sub">未单独配置的景区</span>
                </template>
                <template v-else>
                  {{ scenicName(row.scenic) }}<span class="muted mono matrix-sub">{{ row.scenic }}</span>
                </template>
              </template>
            </el-table-column>
            <el-table-column v-for="t in TABLE_OPTIONS" :key="t.key" :label="t.label" width="92" align="center">
              <template #default="{ row }">
                <el-checkbox :model-value="isPushed(row, t.key)" @change="(v: any) => setPushed(row, t.key, !!v)" />
              </template>
            </el-table-column>
            <el-table-column label="" width="96" align="right">
              <template #default="{ row }">
                <el-button v-if="!row.isDefault" link type="primary" size="small"
                           @click="removeScenicPush(row.scenic)">恢复默认</el-button>
              </template>
            </el-table-column>
          </el-table>
        </el-card>

        <el-collapse v-model="openTables" class="table-collapse">
          <el-collapse-item v-for="t in catalog" :key="t.key" :name="t.key">
            <template #title>
              <div class="tbl-title">
                <span class="tbl-label">{{ t.label }}</span>
                <span class="mono muted">{{ t.table }}</span>
                <el-tag size="small" :type="cfg.tables[t.key].push ? 'success' : 'info'">
                  {{ cfg.tables[t.key].push ? '默认推送' : '默认不推' }}
                </el-tag>
                <span class="muted tbl-meta">
                  {{ cfg.tables[t.key].push_name ? `→ ${cfg.tables[t.key].push_name}` : '' }}
                  {{ custom[t.key] ? `· ${pickedCount(t.key)}/${t.columns.length} 个字段` : `· 全部 ${t.columns.length} 个字段` }}
                </span>
              </div>
            </template>
            <el-form label-width="110px" class="tbl-form">
              <el-form-item>
                <template #label><span class="lbl">默认推送<InfoTip content="没有在「按景区推送」里单独添加的景区，按这个开关" /></span></template>
                <el-switch v-model="cfg.tables[t.key].push" />
              </el-form-item>
              <el-form-item>
                <template #label><span class="lbl">下游表名<InfoTip content="报文里的表名，留空 = 用本地表名" /></span></template>
                <el-input v-model="cfg.tables[t.key].push_name" :placeholder="t.table" class="mid-input" />
              </el-form-item>
              <el-form-item>
                <template #label><span class="lbl">主键<InfoTip content="下游按它做幂等更新。留空 = 默认主键（对齐建表的唯一索引）。字段改名后会自动换成下游的名字" /></span></template>
                <el-select v-model="cfg.tables[t.key].pk" multiple filterable clearable
                           :placeholder="`默认：${t.pk.join('、')}`" class="mid-input">
                  <el-option v-for="c in t.columns" :key="c.name" :label="c.name" :value="c.name" />
                </el-select>
              </el-form-item>
              <el-form-item label="推送字段">
                <el-radio-group v-model="custom[t.key]" @change="(v: any) => onCustomChange(t, v)">
                  <el-radio-button :value="false">全部字段，原名</el-radio-button>
                  <el-radio-button :value="true">自选字段 / 改名</el-radio-button>
                </el-radio-group>
              </el-form-item>
            </el-form>
            <el-table v-if="custom[t.key]" :data="fieldRows[t.key]" size="small" class="data-table field-table"
                      max-height="420">
              <el-table-column width="56">
                <template #header>
                  <el-checkbox :model-value="allPicked(t.key)" :indeterminate="somePicked(t.key)"
                               @change="(v: any) => pickAll(t, v)" />
                </template>
                <template #default="{ row }">
                  <el-tooltip :disabled="!isPk(t, row.column)" content="主键字段必须推送" placement="top">
                    <el-checkbox v-model="row.include" :disabled="isPk(t, row.column)" />
                  </el-tooltip>
                </template>
              </el-table-column>
              <el-table-column label="字段" width="250">
                <template #default="{ row }">
                  <span class="mono">{{ row.column }}</span>
                  <el-tag v-if="isPk(t, row.column)" size="small" class="pk-tag">主键</el-tag>
                </template>
              </el-table-column>
              <el-table-column label="说明" min-width="200" prop="comment" show-overflow-tooltip />
              <el-table-column label="下游字段名" width="240">
                <template #default="{ row }">
                  <el-input v-model="row.name" size="small" :placeholder="row.column" :disabled="!row.include" />
                </template>
              </el-table-column>
            </el-table>
          </el-collapse-item>
        </el-collapse>
      </el-tab-pane>

      <!-- ==================== 演示数据 ==================== -->
      <el-tab-pane label="演示数据" name="mock">
        <el-card shadow="never" class="block">
          <template #header>
            <div class="card-head">
              <span class="lbl">随计算自动推送<InfoTip :width="360">
                <ul class="tip-list">
                  <li>每次计算（定时或手动）里，源景区算完、并且它自己推送成功后，把它这次的结果换成目标景区的编号和名称，再推一遍</li>
                  <li>日期和主景区这次推的一样（含往前重跑的那几天）；只推送，不写本地库</li>
                  <li>主景区推送失败、或这次选了「本次不推送」，演示数据也不推，运行日志里写明原因</li>
                  <li>源景区要在这次计算的范围里：「计算的景区」不为空时，源景区要在里面</li>
                </ul>
              </InfoTip></span>
              <el-button size="small" @click="addMockTarget">添加</el-button>
            </div>
          </template>
          <el-table :data="cfg.mock_targets" size="small" class="data-table">
            <el-table-column label="源景区" min-width="190">
              <template #default="{ row }">
                <!-- 提醒放在控件右边的小图标里（悬停看说明），不另起一行，免得把这一行撑高、和别的列错位 -->
                <div class="cell-line">
                  <el-select v-model="row.from_scenic" filterable size="small" placeholder="选景区" class="cell-grow">
                    <el-option v-for="s in scenicOptions" :key="s.scenic_id"
                               :label="`${s.scenic_name}（${s.scenic_id}）`" :value="s.scenic_id" />
                  </el-select>
                  <span class="cell-warn-slot">
                    <el-tooltip v-if="row.from_scenic && cfg.scenics.length && !cfg.scenics.includes(row.from_scenic)"
                                content="不在「景区与参数 → 计算的景区」里，计算时不会算它，这一行不会触发" placement="top">
                      <el-icon class="cell-warn"><WarningFilled /></el-icon>
                    </el-tooltip>
                  </span>
                </div>
              </template>
            </el-table-column>
            <el-table-column label="目标景区编号" min-width="160">
              <template #default="{ row }">
                <div class="cell-line">
                  <el-input v-model="row.to_scenic" size="small" placeholder="如 PFTSCA01018088"
                            class="cell-grow" @blur="checkMockTarget(row)" />
                  <span class="cell-warn-slot">
                    <el-tooltip v-if="mockRisk[row.to_scenic]" :content="mockRisk[row.to_scenic]" placement="top">
                      <el-icon class="cell-warn"><WarningFilled /></el-icon>
                    </el-tooltip>
                  </span>
                </div>
              </template>
            </el-table-column>
            <el-table-column label="目标景区名称" min-width="150">
              <template #default="{ row }">
                <el-input v-model="row.to_name" size="small" placeholder="如 泰宁九龙潭生态旅游区" />
              </template>
            </el-table-column>
            <el-table-column label="推哪几张表" min-width="200">
              <template #default="{ row }">
                <el-select v-model="row.tables" multiple collapse-tags collapse-tags-tooltip size="small" class="w-full">
                  <el-option v-for="t in TABLE_OPTIONS" :key="t.key" :label="t.label" :value="t.key" />
                </el-select>
              </template>
            </el-table-column>
            <el-table-column label="启用" width="60" align="center">
              <template #default="{ row }"><el-switch v-model="row.enabled" size="small" /></template>
            </el-table-column>
            <el-table-column label="" width="60" align="center">
              <template #default="{ $index }">
                <IconAction icon="delete" tip="删除" @click="cfg.mock_targets.splice($index, 1)" />
              </template>
            </el-table-column>
            <template #empty><el-empty description="还没有演示景区，点右上角「添加」" :image-size="64" /></template>
          </el-table>
          <div class="muted hint">改完记得点右上角「保存配置」。</div>
        </el-card>

        <!-- 演示数据：把一个景区已经算好的结果复制给另一个景区编号，推给下游（不写本地库） -->
        <el-card shadow="never" class="block">
          <template #header>
            <span class="lbl">手动推一次<InfoTip :width="340">
              <ul class="tip-list">
                <li>把一个景区<b>已经算好</b>的指标结果，换成另一个景区的编号和名称，推给下游；不写本地库</li>
                <li>用于下游要看一个还没开始采集的景区的效果</li>
                <li>下钻明细的 detail_uk 按新景区重算，不会和原景区的数据撞</li>
                <li>推送按「推送」「推送表」里的地址、表名、主键、字段；推哪几张表以这里勾的为准</li>
              </ul>
            </InfoTip></span>
          </template>
          <div class="mock-grid">
            <div class="mock-field">
              <span class="cell-label">从哪个景区复制</span>
              <el-select v-model="mock.from_scenic" filterable placeholder="选景区（要已经算过）">
                <el-option v-for="s in scenicOptions" :key="s.scenic_id"
                           :label="`${s.scenic_name}（${s.scenic_id}）`" :value="s.scenic_id" />
              </el-select>
            </div>
            <div class="mock-field">
              <span class="cell-label">复制给（景区编号）</span>
              <el-input v-model="mock.to_scenic" placeholder="如 PFTSCA01018088" @blur="fillMockName" />
            </div>
            <div class="mock-field">
              <span class="cell-label">景区名称</span>
              <el-input v-model="mock.to_name" placeholder="如 泰宁九龙潭生态旅游区" />
            </div>
            <div class="mock-field">
              <span class="cell-label">日期区间</span>
              <el-date-picker v-model="mock.dates" type="daterange" value-format="YYYYMMDD" format="YYYY-MM-DD"
                              range-separator="至" start-placeholder="开始" end-placeholder="结束" :clearable="false" />
            </div>
          </div>
          <div class="mock-row">
            <span class="cell-label">推哪几张表</span>
            <el-checkbox-group v-model="mock.tables">
              <el-checkbox v-for="t in TABLE_OPTIONS" :key="t.key" :value="t.key">{{ t.label }}</el-checkbox>
            </el-checkbox-group>
          </div>
          <div class="mock-row">
            <el-checkbox v-model="mock.dry_run">只组报文不发送（先验证）</el-checkbox>
            <div class="spacer" />
            <el-button type="primary" :loading="mocking" :disabled="!!status?.running_run_id" @click="startMock">
              <AppIcon v-if="!mocking" name="publish" :size="14" class="btn-icon" />复制并推送
            </el-button>
          </div>
        </el-card>

      </el-tab-pane>
    </el-tabs>

    <!-- 报文预览 -->
    <el-dialog v-model="previewOpen" title="推送报文预览" width="760px">
      <template v-if="preview">
        <el-alert v-if="!preview.enabled" type="warning" :closable="false" show-icon
                  title="推送还没开启，这只是按当前配置组出来的样子" class="block" />
        <el-alert v-else-if="!preview.push" type="warning" :closable="false" show-icon
                  title="这张表设置了「不推送」" class="block" />
        <el-alert v-if="preview.note" type="info" :closable="false" show-icon :title="preview.note" class="block" />
        <el-alert v-if="!preview.has_token" type="warning" :closable="false" show-icon
                  title="还没填令牌，请求里不会带鉴权头" class="block" />
        <pre class="log-terminal preview-code">{{ preview.curl }}</pre>
        <div class="muted preview-hint">令牌只显示头尾几位；数据取这张表最新的一行。</div>
      </template>
      <template #footer>
        <el-button @click="copyPreview">复制</el-button>
        <el-button type="primary" @click="previewOpen = false">关闭</el-button>
      </template>
    </el-dialog>

    <!-- 运行详情 -->
    <el-drawer v-model="drawer" size="62%" :title="detail ? `计算 ${detail.run_id}` : ''" destroy-on-close>
      <template v-if="detail">
        <div class="detail-head">
          <el-tag :type="STATUS[detail.status]?.type">{{ STATUS[detail.status]?.label }}</el-tag>
          <span>{{ fmtDate(detail.start_date) }} ~ {{ fmtDate(detail.end_date) }}</span>
          <span class="muted">{{ detail.started_at }} 开始 · {{ duration(detail) }}</span>
        </div>
        <el-alert v-if="detail.error" type="error" :closable="false" :title="detail.error" class="block" />
        <el-table :data="detail.summary" size="small" class="data-table block" max-height="300">
          <el-table-column label="景区" prop="scenic" width="170" />
          <el-table-column label="结果" width="70">
            <template #default="{ row }">
              <el-tag size="small" :type="row.ok ? 'success' : 'danger'">{{ row.ok ? '成功' : '失败' }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="产出" min-width="220">
            <template #default="{ row }">
              <span v-for="(n, tbl) in row.tables" :key="tbl" class="out-chip">{{ shortTable(String(tbl)) }} {{ n }}</span>
              <span v-if="row.errors?.length" class="err">{{ row.errors.join('；') }}</span>
            </template>
          </el-table-column>
          <el-table-column label="推送" min-width="180">
            <template #default="{ row }">
              <template v-if="row.push">
                <span v-for="p in row.push" :key="p.table" class="out-chip" :class="{ bad: !p.ok && !p.skipped }">
                  {{ shortTable(p.table) }} {{ p.ok ? p.rows : '✗' }}
                </span>
              </template>
              <span v-else-if="!row.mock" class="muted">—</span>
              <div v-for="m in row.mock || []" :key="m.to_scenic" class="mock-line">
                <span class="out-chip" :class="{ bad: !m.ok && !m.skipped, warn: m.skipped && !m.push }">
                  演示 → {{ m.to_name }}
                  {{ m.skipped && !m.push ? `没推：${m.skipped}` : (m.ok ? `${m.rows} 行` : '✗') }}
                </span>
              </div>
            </template>
          </el-table-column>
          <el-table-column label="用时" width="70">
            <template #default="{ row }">{{ row.elapsed }}s</template>
          </el-table-column>
        </el-table>
        <div class="section-title">日志</div>
        <pre class="log-terminal run-log">{{ detail.log || '（暂无）' }}</pre>
      </template>
    </el-drawer>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  metricsApi, type MetricCatalogTable, type MetricRun, type MetricsConfig,
} from '../api'
import ParamNote from '../components/ParamNote.vue'

const router = useRouter()
const loading = ref(false)
const saving = ref(false)
const starting = ref(false)
const initing = ref(false)
const tab = ref('run')
const cfg = ref<MetricsConfig | null>(null)
const status = ref<{ enabled: boolean; running_run_id: string | null; next_fire: string | null } | null>(null)
const scenicOptions = ref<{ scenic_id: string; scenic_name: string }[]>([])
const tableStatus = ref<{ key: string; table: string; label: string; exists: boolean; approx_rows: number }[]>([])
const catalog = ref<MetricCatalogTable[]>([])
const catalogError = ref('')
const openTables = ref<string[]>([])

const STATUS: Record<string, { label: string; type: 'success' | 'warning' | 'danger' | 'info' }> = {
  running: { label: '运行中', type: 'warning' },
  success: { label: '成功', type: 'success' },
  partial: { label: '部分失败', type: 'warning' },
  failed: { label: '失败', type: 'danger' },
  canceled: { label: '已停止', type: 'info' },
}
const CRON_PRESETS = [
  { label: '每天 02:30', cron: '30 2 * * *' },
  { label: '每天 06:00', cron: '0 6 * * *' },
  { label: '每 6 小时', cron: '0 */6 * * *' },
]

// ---------------- 运行 ----------------
const yesterday = (() => {
  const d = new Date(Date.now() - 86400000)
  return `${d.getFullYear()}${String(d.getMonth() + 1).padStart(2, '0')}${String(d.getDate()).padStart(2, '0')}`
})()
const runDates = ref<[string, string]>([yesterday, yesterday])
const runScenics = ref<string[]>([])
const runNoPush = ref(false)
const runs = ref<MetricRun[]>([])
const runsTotal = ref(0)
const runsPage = ref(1)
const runsPageSize = 20
const drawer = ref(false)
const detail = ref<MetricRun | null>(null)
let timer: number | undefined

const scenicPlaceholder = computed(() =>
  cfg.value?.scenics.length ? `按配置的 ${cfg.value.scenics.length} 个景区` : '全部有评论的景区')

function fmtDate(v: string): string {
  return v && v.length === 8 ? `${v.slice(0, 4)}-${v.slice(4, 6)}-${v.slice(6)}` : v
}

function duration(row: MetricRun): string {
  if (!row.started_at) return ''
  const end = row.finished_at ? new Date(row.finished_at.replace(' ', 'T')) : new Date()
  const s = Math.max(0, Math.round((end.getTime() - new Date(row.started_at.replace(' ', 'T')).getTime()) / 1000))
  return s < 60 ? `${s} 秒` : `${Math.floor(s / 60)} 分 ${s % 60} 秒`
}

function shortTable(t: string): string {
  return catalog.value.find((c) => c.table === t)?.label
    || t.replace('ads_trf_social_opinion_', '').replace(/_di$/, '')
}

function resultText(row: MetricRun): string {
  if (row.error && row.status !== 'running') return row.error
  const s = row.summary || []
  if (row.trigger_type === 'mock') {
    const [from, to] = row.scenics
    const head = `${scenicName(from)} → ${s[0]?.to_name || to}（${to}）`
    if (!s.length) return row.status === 'running' ? `${head}，推送中…` : head
    const rows = Object.values(s[0].tables || {}).reduce((a: number, b: any) => a + Number(b), 0)
    const pushed = s[0].dry_run ? '，只组报文未发送' : '，已推送'
    return `${head}：${rows} 行` + (s[0].push_ok === false ? '，推送失败' : (s[0].ok ? pushed : ''))
  }
  if (!s.length) return row.status === 'running' ? '计算中…' : ''
  const ok = s.filter((x) => x.ok).length
  const rows = s.reduce((n, x) => n + Object.values(x.tables || {}).reduce((a: number, b: any) => a + Number(b), 0), 0)
  const pushBad = s.filter((x) => x.push_ok === false).length
  const mocks = s.flatMap((x) => x.mock || [])
  const mockText = mocks.length
    ? `；演示数据 ${mocks.filter((m: any) => m.ok && !m.skipped).length}/${mocks.length} 个已推`
    : ''
  return `${ok}/${s.length} 个景区成功，产出 ${rows} 行`
    + (row.push ? (pushBad ? `，${pushBad} 个推送失败` : '，已推送') : '') + mockText
}

async function loadRuns() {
  const res = await metricsApi.runs(runsPage.value, runsPageSize)
  runs.value = res.items
  runsTotal.value = res.total
}

async function loadStatus() {
  status.value = await metricsApi.status()
}

async function refreshLive() {
  await Promise.all([loadStatus(), loadRuns()])
  if (drawer.value && detail.value?.status === 'running') {
    detail.value = await metricsApi.runDetail(detail.value.run_id)
  }
}

async function startRun() {
  starting.value = true
  try {
    const run = await metricsApi.run({
      start_date: runDates.value[0], end_date: runDates.value[1],
      scenics: runScenics.value.length ? runScenics.value : undefined,
      no_push: runNoPush.value,
    })
    ElMessage.success('已开始计算')
    await refreshLive()
    openRun(run)
  } finally {
    starting.value = false
  }
}

async function openRun(row: MetricRun) {
  detail.value = await metricsApi.runDetail(row.run_id)
  drawer.value = true
}

async function cancelRun(row: MetricRun) {
  await metricsApi.cancel(row.run_id)
  ElMessage.info('已停止')
  await refreshLive()
}

// ---------------- 参数 / ADS 表 ----------------
/** 鼠标停在哪个参数上，右侧高亮它影响的表 */
const ALL_TABLES = ['core', 'platform', 'dimension', 'content', 'drill', 'macro']
const hl = ref<string[]>([])
/** 这张表这次会不会算：下钻、大盘能在「产出」里关掉 */
function producing(key: string): boolean {
  if (key === 'drill') return !!cfg.value?.params.enable_drill_analysis
  if (key === 'macro') return !!cfg.value?.params.enable_macro_metric
  return true
}
async function loadTables() {
  tableStatus.value = await metricsApi.tables()
}

async function initTables() {
  initing.value = true
  try {
    const res = await metricsApi.initTables()
    const created = res.filter((r) => r.result === '已创建').length
    ElMessage.success(created ? `已创建 ${created} 张表` : '表都已经在了')
    await loadTables()
  } finally {
    initing.value = false
  }
}

// ---------------- 鉴权 / 报文预览 ----------------
const TABLE_OPTIONS = [
  { key: 'core', label: '核心指标' }, { key: 'platform', label: '平台分布' },
  { key: 'dimension', label: '维度得分' }, { key: 'content', label: '关键词云' },
  { key: 'drill', label: '下钻明细' }, { key: 'macro', label: '大盘 KPI' },
]
const authPreview = computed(() => {
  const p = cfg.value?.push
  if (!p) return ''
  const token = p.token ? `${p.token.slice(0, 6)}****` : (p.has_token ? '（已填写的令牌）' : '（令牌）')
  return `${p.token_header || 'xpftkey'}: ${p.token_prefix || ''}${token}`
})
const previewTable = ref('drill')
const previewing = ref(false)
const previewOpen = ref(false)
const preview = ref<Record<string, any> | null>(null)

async function openPreview() {
  previewing.value = true
  try {
    preview.value = await metricsApi.pushPreview(previewTable.value)
    previewOpen.value = true
  } finally {
    previewing.value = false
  }
}

async function copyPreview() {
  try {
    await navigator.clipboard.writeText(preview.value?.curl || '')
    ElMessage.success('已复制')
  } catch {
    ElMessage.warning('浏览器不让复制，手动选中复制吧')
  }
}

// ---------------- 按景区推送 ----------------
type PushRow = { isDefault: boolean; scenic: string }
const addScenicId = ref('')
const scenicPushRows = computed<PushRow[]>(() => [
  { isDefault: true, scenic: '' },
  ...Object.keys(cfg.value?.scenic_push || {}).map((s) => ({ isDefault: false, scenic: s })),
])
const addableScenics = computed(() =>
  scenicOptions.value.filter((s) => !(s.scenic_id in (cfg.value?.scenic_push || {}))))
function scenicName(id: string): string {
  return scenicOptions.value.find((s) => s.scenic_id === id)?.scenic_name || id
}
function isPushed(row: PushRow, key: string): boolean {
  if (!cfg.value) return false
  return row.isDefault ? cfg.value.tables[key].push : (cfg.value.scenic_push[row.scenic] || []).includes(key)
}
function setPushed(row: PushRow, key: string, on: boolean) {
  if (!cfg.value) return
  if (row.isDefault) {
    cfg.value.tables[key].push = on
    return
  }
  const cur = new Set(cfg.value.scenic_push[row.scenic] || [])
  if (on) cur.add(key)
  else cur.delete(key)
  cfg.value.scenic_push[row.scenic] = TABLE_OPTIONS.map((t) => t.key).filter((k) => cur.has(k))
}
function addScenicPush() {
  if (!cfg.value || !addScenicId.value) return
  // 先照默认这一行勾好，再按需要去掉几张
  cfg.value.scenic_push = {
    ...cfg.value.scenic_push,
    [addScenicId.value]: TABLE_OPTIONS.map((t) => t.key).filter((k) => cfg.value!.tables[k].push),
  }
  addScenicId.value = ''
}
function removeScenicPush(scenic: string) {
  if (!cfg.value) return
  const next = { ...cfg.value.scenic_push }
  delete next[scenic]
  cfg.value.scenic_push = next
}

// ---------------- 演示数据（随计算自动推送） ----------------
/** 目标编号在本系统里有真实数据时的提醒，按编号缓存 */
const mockRisk = reactive<Record<string, string>>({})
function addMockTarget() {
  cfg.value?.mock_targets.push({
    from_scenic: '', to_scenic: '', to_name: '', enabled: true,
    tables: TABLE_OPTIONS.map((t) => t.key),
  })
}
async function checkMockTarget(row: { to_scenic: string; to_name: string }) {
  const code = row.to_scenic.trim()
  if (!code) return
  const hit = scenicOptions.value.find((s) => s.scenic_id === code)
  if (hit && !row.to_name) row.to_name = hit.scenic_name
  const c = await metricsApi.mockCheck(code)
  mockRisk[code] = (c.comments || c.ads_rows)
    ? `本系统已有真实数据（评论 ${c.comments} 条、指标 ${c.ads_rows} 行），推出去会覆盖下游里它的数据`
    : ''
}

// ---------------- 演示数据（手动推一次） ----------------
const mocking = ref(false)
const mock = reactive({
  from_scenic: '', to_scenic: '', to_name: '',
  dates: [yesterday, yesterday] as [string, string],
  tables: ['core', 'platform', 'dimension', 'content', 'drill', 'macro'],
  dry_run: false,
})

/** 目标编号在景区管理里建过的话，名称自动带出来 */
function fillMockName() {
  const hit = scenicOptions.value.find((s) => s.scenic_id === mock.to_scenic.trim())
  if (hit && !mock.to_name) mock.to_name = hit.scenic_name
}

async function startMock() {
  const to = mock.to_scenic.trim()
  if (!mock.from_scenic || !to || !mock.to_name.trim()) {
    ElMessage.warning('源景区、目标景区编号和名称都要填')
    return
  }
  if (mock.from_scenic === to) {
    ElMessage.error('目标编号和源景区一样：这样会用演示数据覆盖真实景区')
    return
  }
  if (!mock.tables.length) {
    ElMessage.warning('至少勾一张表')
    return
  }
  // 目标景区要是已经有真实数据，推出去会按主键覆盖下游里它的数据——说清楚再让人确认
  const check = await metricsApi.mockCheck(to)
  const risk = check.comments > 0 || check.ads_rows > 0
  const lines = [
    `把「${scenicName(mock.from_scenic)}」${fmtDate(mock.dates[0])} ~ ${fmtDate(mock.dates[1])} 的指标结果，`
      + `复制给「${mock.to_name.trim()}（${to}）」并推给下游；不写本地库。`,
    risk
      ? `⚠️ 这个编号在本系统里已经有真实数据（评论 ${check.comments} 条，指标 ${check.ads_rows} 行）：`
        + '推出去会按主键覆盖下游里它的真实数据。'
      : '这个编号在本系统里没有数据。',
    mock.dry_run ? '本次只组报文、不发送。' : '',
  ].filter(Boolean)
  try {
    await ElMessageBox.confirm(lines.join('\n'), risk ? '目标景区已有真实数据' : '确认推送演示数据', {
      type: risk ? 'error' : 'warning',
      confirmButtonText: risk ? '仍然覆盖' : (mock.dry_run ? '组报文' : '推送'),
      customStyle: { whiteSpace: 'pre-line', maxWidth: '520px' },
    })
  } catch { return }
  mocking.value = true
  try {
    const run = await metricsApi.mock({
      from_scenic: mock.from_scenic, to_scenic: to, to_name: mock.to_name.trim(),
      start_date: mock.dates[0], end_date: mock.dates[1], tables: mock.tables, dry_run: mock.dry_run,
    })
    ElMessage.success(mock.dry_run ? '已开始组报文' : '已开始推送演示数据')
    await refreshLive()
    openRun(run)
  } finally {
    mocking.value = false
  }
}

// ---------------- 推送附加字段 ----------------
const extraRows = ref<{ key: string; value: string }[]>([])

// ---------------- 推送表字段 ----------------
/** 每张表是不是自选字段（false = 全部字段原名） */
const custom = reactive<Record<string, boolean>>({})
/** 自选字段时的编辑行：按引擎的字段顺序，勾了才推 */
const fieldRows = reactive<Record<string, { column: string; comment: string; include: boolean; name: string }[]>>({})

function effectivePk(t: MetricCatalogTable): string[] {
  const own = cfg.value?.tables[t.key]?.pk || []
  return own.length ? own : t.pk
}
function isPk(t: MetricCatalogTable, column: string): boolean {
  return effectivePk(t).includes(column)
}
function buildFieldRows(t: MetricCatalogTable) {
  const chosen = new Map((cfg.value?.tables[t.key]?.fields || []).map((f) => [f.column, f.name]))
  const hasChoice = chosen.size > 0
  fieldRows[t.key] = t.columns.map((c) => ({
    column: c.name, comment: c.comment,
    include: hasChoice ? chosen.has(c.name) : true,
    name: hasChoice && chosen.get(c.name) !== c.name ? (chosen.get(c.name) || '') : '',
  }))
  custom[t.key] = hasChoice
}
function onCustomChange(t: MetricCatalogTable, v: boolean) {
  if (v && !fieldRows[t.key]?.length) buildFieldRows(t)
  custom[t.key] = v
}
function pickedCount(key: string): number {
  return (fieldRows[key] || []).filter((r) => r.include).length
}
function allPicked(key: string): boolean {
  const rows = fieldRows[key] || []
  return rows.length > 0 && rows.every((r) => r.include)
}
function somePicked(key: string): boolean {
  const n = pickedCount(key)
  return n > 0 && n < (fieldRows[key] || []).length
}
function pickAll(t: MetricCatalogTable, v: boolean) {
  for (const r of fieldRows[t.key] || []) r.include = v || isPk(t, r.column)
}

async function loadCatalog() {
  try {
    catalog.value = await metricsApi.catalog()
    catalogError.value = ''
    for (const t of catalog.value) buildFieldRows(t)
  } catch (e: any) {
    catalogError.value = e?.message || '读取指标表字段失败'
  }
}

// ---------------- 读写配置 ----------------
async function loadConfig() {
  cfg.value = await metricsApi.config()
  extraRows.value = Object.entries(cfg.value.push.extra_fields || {}).map(([key, value]) => ({
    key, value: String(value ?? ''),
  }))
}

async function saveConfig() {
  if (!cfg.value) return
  const out: MetricsConfig = JSON.parse(JSON.stringify(cfg.value))
  out.push.extra_fields = Object.fromEntries(
    extraRows.value.filter((r) => r.key.trim()).map((r) => [r.key.trim(), r.value]))
  for (const t of catalog.value) {
    out.tables[t.key].fields = custom[t.key]
      ? (fieldRows[t.key] || []).filter((r) => r.include || isPk(t, r.column))
        .map((r) => ({ column: r.column, name: r.name.trim() || r.column }))
      : []
  }
  saving.value = true
  try {
    cfg.value = await metricsApi.saveConfig(out)
    ElMessage.success('已保存，下次计算按新配置')
    await Promise.all([loadStatus(), loadTables()])
  } finally {
    saving.value = false
  }
}

onMounted(async () => {
  loading.value = true
  try {
    await loadStatus()
    if (!status.value?.enabled) return
    const [scenics] = await Promise.all([metricsApi.scenics(), loadConfig(), loadRuns(), loadTables()])
    scenicOptions.value = scenics
  } finally {
    loading.value = false
  }
  // 字段目录要起一次子进程读引擎（第一次几秒），不挡首屏
  loadCatalog()
  timer = window.setInterval(() => {
    if (status.value?.running_run_id || runs.value.some((r) => r.status === 'running')) refreshLive()
  }, 3000)
})
onUnmounted(() => window.clearInterval(timer))
</script>

<style scoped>
.title-tag { margin-left: 8px; }
.header-actions { display: flex; align-items: center; gap: 12px; }
.next-fire { font-size: 13px; }
.block { margin-bottom: 16px; }
.narrow { max-width: 820px; }
.card-head { display: flex; align-items: center; justify-content: space-between; }
.lbl { display: inline-flex; align-items: center; gap: 4px; }
.btn-icon { margin-right: 6px; }
.run-form { display: flex; flex-wrap: wrap; align-items: center; gap: 12px; }
.run-dates { width: 280px !important; flex: 0 0 auto; }
.run-scenics { width: 320px; }
.hint { margin-top: 10px; font-size: 12px; }
.pager { margin-top: 12px; justify-content: flex-end; }
.unit { margin: 0 8px 0 6px; color: var(--smc-text-secondary); font-size: 13px; }
.num-sm { width: 110px; }
.short-input { width: 220px; }
.mid-input { width: 420px; max-width: 100%; }
.inline-check { margin-left: 16px; }
.weights { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
/* 右侧 ADS 表跟着滚动停在页签下面：粘在列上（列要 align-self: flex-start，不然列和行一样高，粘不住） */
.ads-col { position: sticky; top: 100px; align-self: flex-start; }
/* el-tabs 的内容区默认 overflow: hidden，里面的 sticky 全部失效 */
.metric-tabs > :deep(.el-tabs__content) { overflow: visible; }
.ads-row { display: flex; align-items: center; gap: 10px; padding: 8px 10px; margin: 0 -10px; border-bottom: 1px solid var(--smc-border); border-radius: 6px; transition: background-color .15s, opacity .15s; }
.ads-row.is-hl { background: var(--el-color-primary-light-9); }
.ads-row.is-hl .ads-name { color: var(--el-color-primary); font-weight: 600; }
.ads-row.is-dim { opacity: .4; }
.ads-row.is-off .ads-name { color: var(--smc-text-secondary); }
.ads-foot { margin-top: 10px; font-size: 12px; }
.head-hint { font-size: 12px; font-weight: 400; }
.param-item { border-radius: 6px; transition: background-color .15s; }
.param-item:hover { background: var(--el-fill-color-lighter); }
.ads-row:last-child { border-bottom: none; }
.ads-name { width: 72px; flex-shrink: 0; }
.ads-table { flex: 1; min-width: 0; font-size: 12px; overflow: hidden; text-overflow: ellipsis; }
.cron-row { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
.cron-input { width: 160px; }
.auth-row { display: grid; grid-template-columns: 170px 130px 1fr; gap: 8px; width: 100%; }
.auth-cell { display: flex; flex-direction: column; gap: 2px; }
.cell-label { font-size: 12px; line-height: 18px; color: var(--smc-text-secondary); }
.auth-result { width: 100%; margin-top: 6px; font-size: 12px; line-height: 18px; }
.auth-result code { padding: 1px 6px; border-radius: 4px; background: var(--el-fill-color-light); }
.form-group-title { margin: 6px 0 12px; padding-left: 8px; border-left: 3px solid var(--el-color-primary); font-size: 13px; font-weight: 600; }
.form-group-title:not(:first-child) { margin-top: 22px; }
.param-form :deep(.el-form-item) { margin-bottom: 16px; }
.preview-select { width: 140px; margin-right: 8px; }
.add-scenic { display: flex; align-items: center; gap: 8px; }
.cell-line { display: flex; align-items: center; gap: 6px; }
.cell-grow { flex: 1; min-width: 0; }
/* 图标位固定留出来：有没有提醒，同一列的输入框都一样宽 */
.cell-warn-slot { flex: 0 0 16px; display: inline-flex; align-items: center; }
.cell-warn { font-size: 16px; color: var(--el-color-danger); cursor: help; }
.mock-line { margin-top: 2px; }
.out-chip.warn { color: var(--el-color-warning-dark-2); background: var(--el-color-warning-light-9); }
/* 标题 + 页签固定在顶部（页面在 el-main 里滚动，它上面有 24px 内边距） */
.metrics-view > .page-header {
  position: sticky; top: -24px; z-index: 30;
  margin: -24px 0 0; padding: 24px 0 10px;
  background: var(--smc-bg);
}
.metric-tabs > :deep(.el-tabs__header) {
  position: sticky; top: 40px; z-index: 29;
  margin: 0 0 14px; background: var(--smc-bg);
}
.mock-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; }
.mock-field { display: flex; flex-direction: column; gap: 4px; min-width: 0; }
.mock-field :deep(.el-date-editor) { width: 100% !important; }
.mock-row { display: flex; align-items: center; gap: 12px; margin-top: 12px; flex-wrap: wrap; }
.spacer { flex: 1; }
.add-scenic-select { width: 240px; }
.matrix-sub { margin-left: 8px; font-size: 12px; }
.tip-list { margin: 0; padding-left: 16px; }
.tip-list li + li { margin-top: 4px; }
.preview-hint { margin-left: 8px; font-size: 12px; }
.preview-code { max-height: 460px; overflow: auto; white-space: pre-wrap; word-break: break-all; margin: 0; }
.body-row { display: grid; grid-template-columns: auto 1fr auto 1fr auto 1fr; align-items: center; gap: 8px; width: 100%; }
.extra-list { width: 100%; }
.extra-row { display: grid; grid-template-columns: 1fr 1fr auto; gap: 8px; margin-bottom: 8px; }
.table-collapse { border-radius: var(--smc-radius); background: #fff; padding: 0 16px; border: 1px solid var(--smc-border); }
.tbl-title { display: flex; align-items: center; gap: 10px; min-width: 0; }
.tbl-label { font-weight: 600; width: 72px; }
.tbl-meta { font-size: 12px; }
.tbl-form { padding-top: 4px; }
.field-table { margin-bottom: 8px; }
.pk-tag { margin-left: 6px; }
.detail-head { display: flex; align-items: center; gap: 12px; margin-bottom: 12px; }
.out-chip { display: inline-block; margin: 0 6px 2px 0; padding: 0 6px; border-radius: 4px; font-size: 12px; background: var(--el-fill-color-light); }
.out-chip.bad { color: var(--el-color-danger); background: var(--el-color-danger-light-9); }
.err { color: var(--el-color-danger); font-size: 12px; }
.run-log { max-height: 420px; overflow: auto; white-space: pre-wrap; word-break: break-all; margin: 0; }
</style>

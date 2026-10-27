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
              <template #default="{ row }">{{ row.trigger_type === 'schedule' ? '定时' : '手动' }}</template>
            </el-table-column>
            <el-table-column label="目标日期" width="190">
              <template #default="{ row }">
                {{ fmtDate(row.start_date) }}<template v-if="row.end_date !== row.start_date"> ~ {{ fmtDate(row.end_date) }}</template>
              </template>
            </el-table-column>
            <el-table-column label="景区" width="90">
              <template #default="{ row }">{{ row.scenics.length || '全部' }}</template>
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
          <el-col :xs="24" :lg="14">
            <el-card shadow="never" class="block">
              <template #header>
                <span class="lbl">计算的景区<InfoTip content="不选 = 每次计算取数区间里有评论的全部景区" /></span>
              </template>
              <el-select v-model="cfg.scenics" multiple filterable clearable class="w-full"
                         placeholder="全部有评论的景区">
                <el-option v-for="s in scenicOptions" :key="s.scenic_id"
                           :label="`${s.scenic_name}（${s.scenic_id}）`" :value="s.scenic_id" />
              </el-select>
            </el-card>

            <el-card shadow="never" class="block">
              <template #header>计算参数</template>
              <el-form label-width="150px" class="param-form">
                <el-form-item>
                  <template #label><span class="lbl">取数回溯<InfoTip content="往前读多少天的评论参与计算。近 90 日环比要 180 天历史，历史不够时长窗口会退化成「已有历史累计」" /></span></template>
                  <el-input-number v-model="cfg.params.lookback_days" :min="7" :max="1100" /><span class="unit">天</span>
                </el-form-item>
                <el-form-item>
                  <template #label><span class="lbl">往前重跑<InfoTip content="评论会晚到约 1 天，每次计算顺带把目标日期之前几天也重算一遍。0 = 不重跑" /></span></template>
                  <el-input-number v-model="cfg.params.rerun_days" :min="0" :max="60" /><span class="unit">天</span>
                </el-form-item>
                <el-form-item>
                  <template #label><span class="lbl">缺数回补<InfoTip content="某渠道某天一条评论都没有时：复制往前最近一天的明细再算（保证趋势连续）；或者就记 0" /></span></template>
                  <el-radio-group v-model="cfg.params.backfill_mode">
                    <el-radio-button value="fill">复制最近一天</el-radio-button>
                    <el-radio-button value="off">记 0</el-radio-button>
                  </el-radio-group>
                  <template v-if="cfg.params.backfill_mode === 'fill'">
                    <span class="unit">最多往前找</span>
                    <el-input-number v-model="cfg.params.backfill_lookback" :min="0" :max="365" size="small" class="num-sm" />
                    <span class="unit">天</span>
                  </template>
                </el-form-item>
                <el-form-item>
                  <template #label><span class="lbl">得分权重<InfoTip content="得分 = 5 ×（好评率×正面 + 中评率×中性 + 差评率×负面）。改了之后历史分数不会自动变，要重算历史日期" /></span></template>
                  <div class="weights">
                    <span>正面</span><el-input-number v-model="cfg.params.score_weights.positive" :min="0" :max="1" :step="0.05" :precision="2" size="small" class="num-sm" />
                    <span>中性</span><el-input-number v-model="cfg.params.score_weights.neutral" :min="0" :max="1" :step="0.05" :precision="2" size="small" class="num-sm" />
                    <span>负面</span><el-input-number v-model="cfg.params.score_weights.negative" :min="0" :max="1" :step="0.05" :precision="2" size="small" class="num-sm" />
                  </div>
                </el-form-item>
                <el-form-item>
                  <template #label><span class="lbl">关键词保留<InfoTip content="关键词云表每天每种情感只留热度前 N 个词，0 = 全部保留（表会很大）" /></span></template>
                  <el-input-number v-model="cfg.params.content_top_words" :min="0" :max="100000" /><span class="unit">个</span>
                </el-form-item>
                <el-form-item label="下钻明细表">
                  <el-switch v-model="cfg.params.enable_drill_analysis" />
                  <template v-if="cfg.params.enable_drill_analysis">
                    <el-checkbox v-model="cfg.params.drill_keyword_filter" class="inline-check">只收含采集关键字的评论</el-checkbox>
                    <el-checkbox v-model="cfg.params.drill_mask_content" class="inline-check">评论摘要只露关键词</el-checkbox>
                  </template>
                </el-form-item>
                <el-form-item label="大盘 KPI 表">
                  <el-switch v-model="cfg.params.enable_macro_metric" />
                </el-form-item>
                <el-form-item>
                  <template #label><span class="lbl">写入 ADS 表<InfoTip content="关掉 = 只计算 + 推送，不写本地库" /></span></template>
                  <el-switch v-model="cfg.params.write_db" />
                </el-form-item>
                <el-form-item>
                  <template #label><span class="lbl">ADS 库<InfoTip content="指标表放在哪个库，留空 = 和采集数据同一个库" /></span></template>
                  <el-input v-model="cfg.params.ads_database" placeholder="同采集库" class="short-input" />
                </el-form-item>
              </el-form>
            </el-card>
          </el-col>

          <el-col :xs="24" :lg="10">
            <el-card shadow="never" class="block">
              <template #header>
                <div class="card-head">
                  <span class="lbl">ADS 表<InfoTip content="按引擎自带的建表语句建表，已有的不动。表结构以引擎为准" /></span>
                  <el-button size="small" :loading="initing" @click="initTables">建表</el-button>
                </div>
              </template>
              <div v-for="t in tableStatus" :key="t.key" class="ads-row">
                <span class="ads-name">{{ t.label }}</span>
                <span class="mono muted ads-table">{{ t.table }}</span>
                <el-tag v-if="t.exists" size="small" type="success">约 {{ t.approx_rows }} 行</el-tag>
                <el-tag v-else size="small" type="info">未建</el-tag>
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
            <el-form-item>
              <template #label><span class="lbl">鉴权<InfoTip content="请求头名 + 前缀 + 令牌，例如 Authorization: Bearer sk_xxx。令牌保存后不回显" /></span></template>
              <div class="auth-row">
                <el-input v-model="cfg.push.token_header" placeholder="请求头" class="auth-header" />
                <el-input v-model="cfg.push.token_prefix" placeholder="前缀" class="auth-prefix" />
                <el-input v-model="cfg.push.token" type="password" show-password
                          :placeholder="cfg.push.has_token ? '已填写，留空 = 不修改' : '令牌'" />
              </div>
            </el-form-item>
            <el-form-item>
              <template #label><span class="lbl">报文字段名<InfoTip content='请求体里「表名 / 主键 / 数据」三个字段叫什么，默认 {"tableName", "pkId", "data"}' /></span></template>
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
          </el-form>
        </el-card>
      </el-tab-pane>

      <!-- ==================== 推送表 ==================== -->
      <el-tab-pane label="推送表" name="tables">
        <el-alert v-if="catalogError" type="error" :closable="false" show-icon :title="catalogError" class="block" />
        <el-collapse v-model="openTables" class="table-collapse">
          <el-collapse-item v-for="t in catalog" :key="t.key" :name="t.key">
            <template #title>
              <div class="tbl-title">
                <span class="tbl-label">{{ t.label }}</span>
                <span class="mono muted">{{ t.table }}</span>
                <el-tag size="small" :type="cfg.tables[t.key].push ? 'success' : 'info'">
                  {{ cfg.tables[t.key].push ? '推送' : '不推送' }}
                </el-tag>
                <span class="muted tbl-meta">
                  {{ cfg.tables[t.key].push_name ? `→ ${cfg.tables[t.key].push_name}` : '' }}
                  {{ custom[t.key] ? `· ${pickedCount(t.key)}/${t.columns.length} 个字段` : `· 全部 ${t.columns.length} 个字段` }}
                </span>
              </div>
            </template>
            <el-form label-width="110px" class="tbl-form">
              <el-form-item label="推送">
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
    </el-tabs>

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
              <span v-else class="muted">—</span>
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
import { ElMessage } from 'element-plus'
import {
  metricsApi, type MetricCatalogTable, type MetricRun, type MetricsConfig,
} from '../api'

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
  if (!s.length) return row.status === 'running' ? '计算中…' : ''
  const ok = s.filter((x) => x.ok).length
  const rows = s.reduce((n, x) => n + Object.values(x.tables || {}).reduce((a: number, b: any) => a + Number(b), 0), 0)
  const pushBad = s.filter((x) => x.push_ok === false).length
  return `${ok}/${s.length} 个景区成功，产出 ${rows} 行` + (row.push ? (pushBad ? `，${pushBad} 个推送失败` : '，已推送') : '')
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
.ads-row { display: flex; align-items: center; gap: 10px; padding: 8px 0; border-bottom: 1px solid var(--smc-border); }
.ads-row:last-child { border-bottom: none; }
.ads-name { width: 72px; flex-shrink: 0; }
.ads-table { flex: 1; min-width: 0; font-size: 12px; overflow: hidden; text-overflow: ellipsis; }
.cron-row { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
.cron-input { width: 160px; }
.auth-row { display: grid; grid-template-columns: 160px 110px 1fr; gap: 8px; width: 100%; }
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

<template>
  <div class="labeling-view">
    <!-- 引擎状态横幅：没开 / 开了但不可用 / 正常，三种都要一眼看出来 -->
    <el-alert
      v-if="!status?.engine_present"
      type="error" :closable="false" show-icon
      title="没找到标注引擎"
      description="backend/vendor/opinion_labeling_engine 不完整，标注功能不可用。"
    />
    <el-alert
      v-else-if="status?.degraded"
      type="error" :closable="false" show-icon
      title="标注引擎已降级，本次运行不再尝试标注"
      :description="status.error || '未知原因，点「自检」查看详情'"
    />
    <el-alert
      v-else-if="!status?.enabled"
      type="info" :closable="false" show-icon
      title="边采边标未开启：采集时不会自动标注"
      description="到「系统设置 → 标注」里打开。历史数据可以用下面的「一键补标」补。"
    />

    <!-- 顶部：景区筛选 + 进度 + 动作 -->
    <el-card shadow="never" class="head-card">
      <div class="filters">
        <el-select
          v-model="query.scenic_id" filterable clearable
          placeholder="选择景区（必选，按景区审核）" style="width: 260px"
          @change="onScopeChange"
        >
          <el-option
            v-for="s in scenics" :key="s.scenic_id"
            :label="s.scenic_name" :value="s.scenic_id"
          />
        </el-select>
        <el-select v-model="query.channel" clearable placeholder="平台" style="width: 130px"
                   @change="onScopeChange">
          <el-option v-for="c in channels" :key="c.value" :label="c.label" :value="c.value" />
        </el-select>
        <el-select v-model="query.label_state" clearable placeholder="标注状态" style="width: 130px"
                   @change="reload">
          <el-option label="已标注" value="labeled" />
          <el-option label="未标注" value="unlabeled" />
        </el-select>
        <el-select v-model="query.sentiment" clearable placeholder="情感" style="width: 110px"
                   @change="reload">
          <el-option label="正向" value="正向" />
          <el-option label="中性" value="中性" />
          <el-option label="负向" value="负向" />
        </el-select>
        <el-select v-model="query.review_flag" clearable placeholder="标注/复核状态"
                   style="width: 160px" @change="reload">
          <el-option v-for="f in reviewFlags" :key="f.value"
                     :label="f.label" :value="String(f.value)" />
        </el-select>
        <el-input
          v-model="query.keyword" clearable placeholder="搜评论正文"
          style="width: 200px" @keyup.enter="reload" @clear="reload"
        />
        <el-button type="primary" :icon="Search" @click="reload">查询</el-button>

        <div class="spacer" />
        <el-button :icon="FirstAidKit" @click="runCheck">自检</el-button>
        <el-button type="warning" :icon="MagicStick" :loading="backfilling"
                   @click="startBackfill">
          一键补标历史未标注
        </el-button>
      </div>

      <div v-if="stats" class="stat-row">
        <div class="stat"><span class="n">{{ stats.total }}</span><span class="k">评论总数</span></div>
        <div class="stat is-ok"><span class="n">{{ stats.labeled }}</span><span class="k">已标注</span></div>
        <div class="stat is-warn"><span class="n">{{ stats.unlabeled }}</span><span class="k">未标注</span></div>
        <el-divider direction="vertical" />
        <div class="stat"><span class="n up">{{ stats.positive }}</span><span class="k">正向</span></div>
        <div class="stat"><span class="n">{{ stats.neutral }}</span><span class="k">中性</span></div>
        <div class="stat"><span class="n down">{{ stats.negative }}</span><span class="k">负向</span></div>
        <el-divider direction="vertical" />
        <div class="stat is-warn"><span class="n">{{ stats.pending_review }}</span><span class="k">待人工复核</span></div>
        <div class="stat is-ok"><span class="n">{{ stats.ai_ok }}</span><span class="k">AI标注成功</span></div>
        <div class="stat"><span class="n down">{{ stats.ai_failed }}</span><span class="k">AI标注错误</span></div>
        <div class="stat is-warn"><span class="n">{{ stats.ai_low_conf }}</span><span class="k">低置信</span></div>
        <el-divider direction="vertical" />
        <div class="stat is-ok"><span class="n">{{ stats.human_right }}</span><span class="k">复核正确</span></div>
        <div class="stat"><span class="n down">{{ stats.human_wrong }}</span><span class="k">复核错误</span></div>
        <div class="stat is-ok"><span class="n">{{ stats.human_fixed }}</span><span class="k">复核成功</span></div>
      </div>

      <!-- 补标进度 -->
      <div v-if="job" class="job-panel" :class="`is-${job.status}`">
        <div class="job-head">
          <el-tag :type="jobTagType" size="small" effect="dark">{{ jobLabel }}</el-tag>
          <span class="job-title">{{ jobTitle }}</span>
          <div class="spacer" />
          <span class="job-meta">已用时 {{ fmtDuration(job.elapsed_seconds) }}</span>
          <span v-if="job.status === 'running' && job.eta_seconds !== null"
                class="job-meta">预计还需 {{ fmtDuration(job.eta_seconds) }}</span>
          <el-popconfirm
            v-if="job.status === 'running' && !job.cancel_requested"
            title="停止补标？已经喂进队列的那一批还会跑完。" width="260"
            @confirm="cancelBackfill"
          >
            <template #reference>
              <el-button link type="danger" size="small">停止</el-button>
            </template>
          </el-popconfirm>
          <span v-else-if="job.cancel_requested && job.status === 'running'"
                class="job-meta">正在停止…</span>
          <el-button v-else link size="small" @click="job = null">关闭</el-button>
        </div>

        <el-progress
          :percentage="job.percent"
          :status="job.status === 'failed' ? 'exception'
                   : (job.status === 'finished' ? 'success' : undefined)"
          :stroke-width="14" :text-inside="true"
        />

        <div class="job-nums">
          <span><b>{{ job.done }}</b> / {{ job.total }} 已标完写回</span>
          <span class="sep">·</span>
          <span>已喂入队列 <b>{{ job.submitted }}</b> 条</span>
          <span class="sep">·</span>
          <span>第 <b>{{ job.batches }}</b> 批</span>
          <span class="sep">·</span>
          <span>队列中 <b>{{ job.queued }}</b> 条</span>
        </div>

        <!-- 队列里还压着东西时，进度条可能几十秒不动。不解释一句，
             用户会以为卡死了然后去重启服务，那才是真的把事情搞砸。 -->
        <div v-if="job.status === 'running' && job.done < job.submitted" class="job-hint">
          已喂进去的这批正在跑，写回是攒够一批才落库的，
          所以进度条会一格一格跳，不是卡住了。
        </div>
        <div v-if="job.error" class="job-error">{{ job.error }}</div>
      </div>
    </el-card>

    <!-- 列表 -->
    <el-card shadow="never">
      <el-table :data="rows" v-loading="loading" row-key="id" size="small">
        <el-table-column type="expand">
          <template #default="{ row }">
            <div class="expand">
              <div class="expand-line"><b>评论ID</b>{{ row.comment_id }}</div>
              <div class="expand-line"><b>作品ID</b>{{ row.work_id }}</div>
              <div class="expand-line"><b>维度标签</b>
                <span v-if="!row.dimension_tags.length" class="muted">（无）</span>
                <el-tag v-for="(d, i) in row.dimension_tags" :key="i" size="small"
                        :type="sentimentType(d.sentiment)" class="tag">
                  {{ [d.dim1, d.dim2, d.dim3].filter(Boolean).join(' / ') }}
                </el-tag>
              </div>
              <div class="expand-line"><b>实体标签</b>
                <span v-if="!row.entity_tags.length" class="muted">（无）</span>
                <el-tag v-for="(e, i) in row.entity_tags" :key="i" size="small" class="tag">
                  {{ e.type }}：{{ e.value }}
                </el-tag>
              </div>
              <div class="expand-line"><b>复核</b>
                {{ reviewLabel(row.label_review_flag) }}
                <span v-if="row.label_review_by" class="muted">
                  — {{ row.label_review_by }} {{ row.label_review_time || '' }}
                </span>
              </div>
            </div>
          </template>
        </el-table-column>

        <el-table-column label="评论" min-width="300">
          <template #default="{ row }">
            <div class="content">{{ row.content || '（空）' }}</div>
            <div class="meta">
              {{ CHANNEL_LABELS[row.channel] || row.channel }} ·
              {{ row.commenter_name || '匿名' }} ·
              {{ row.publish_time || '时间未知' }} · 赞 {{ row.likes }}
            </div>
          </template>
        </el-table-column>

        <el-table-column label="情感" width="96" align="center">
          <template #default="{ row }">
            <el-tag v-if="row.sentiment_label" size="small"
                    :type="sentimentType(row.sentiment_score)">
              {{ row.sentiment_label }}
            </el-tag>
            <el-tag v-else size="small" type="info">未标注</el-tag>
          </template>
        </el-table-column>

        <el-table-column label="关键词" min-width="160">
          <template #default="{ row }">
            <el-tag v-for="(k, i) in row.keyword_tags" :key="i" size="small" class="tag">{{ k }}</el-tag>
            <span v-if="!row.keyword_tags.length" class="muted">—</span>
          </template>
        </el-table-column>

        <el-table-column label="标注/复核" width="120" align="center">
          <template #default="{ row }">
            <el-tag size="small" :type="reviewType(row.label_review_flag)">
              {{ row.label_review_label || reviewLabel(row.label_review_flag) }}
            </el-tag>
          </template>
        </el-table-column>

        <el-table-column label="操作" width="310" align="center">
          <template #default="{ row }">
            <el-button
              link type="success" size="small" :disabled="!row.labeled"
              @click="review(row, 1)"
            >标对了</el-button>
            <el-button
              link type="danger" size="small" :disabled="!row.labeled"
              @click="review(row, 2)"
            >标错了</el-button>
            <el-button link type="primary" size="small" @click="openEdit(row)">修改</el-button>
            <el-button
              link type="warning" size="small"
              :loading="relabelingId === row.id" @click="relabelOne(row)"
            >AI 再标</el-button>
            <el-button
              v-if="row.label_review_flag !== 0" link size="small"
              @click="resetOne(row)"
            >撤销</el-button>
          </template>
        </el-table-column>
      </el-table>

      <el-pagination
        class="pager" background layout="total, sizes, prev, pager, next"
        :total="total" :current-page="query.page" :page-size="query.page_size"
        :page-sizes="[20, 50, 100]"
        @current-change="(p: number) => { query.page = p; load() }"
        @size-change="(s: number) => { query.page_size = s; query.page = 1; load() }"
      />
    </el-card>

    <!-- 人工修改 -->
    <el-dialog v-model="editVisible" title="人工修改标注" width="640px">
      <div v-if="editing" class="edit-body">
        <el-alert type="info" :closable="false" class="edit-hint"
                  title="保存后这条会标记为「复核成功」（label_review_flag=3）。补标只取「未标注」的行，所以不会被 AI 覆盖。" />
        <div class="edit-content">{{ editing.content }}</div>
        <el-form label-width="90px">
          <el-form-item label="情感">
            <el-radio-group v-model="form.sentiment_label">
              <el-radio-button value="正向">正向</el-radio-button>
              <el-radio-button value="中性">中性</el-radio-button>
              <el-radio-button value="负向">负向</el-radio-button>
            </el-radio-group>
          </el-form-item>
          <el-form-item label="关键词">
            <el-select
              v-model="form.keyword_tags" multiple filterable allow-create
              default-first-option placeholder="回车添加" style="width: 100%"
            >
              <el-option v-for="k in form.keyword_tags" :key="k" :label="k" :value="k" />
            </el-select>
          </el-form-item>
          <el-form-item label="维度标签">
            <el-input v-model="dimensionText" type="textarea" :rows="4"
                      placeholder='JSON 数组，如 [{"dim1":"游玩体验","dim2":"景色","dim3":"","sentiment":1}]' />
          </el-form-item>
          <el-form-item label="实体标签">
            <el-input v-model="entityText" type="textarea" :rows="3"
                      placeholder='JSON 数组，如 [{"type":"景区地名","value":"八大处"}]' />
          </el-form-item>
        </el-form>
      </div>
      <template #footer>
        <el-button @click="editVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="saveEdit">保存并标记为「复核成功」</el-button>
      </template>
    </el-dialog>

    <!-- 自检 -->
    <el-dialog v-model="checkVisible" title="标注引擎自检" width="720px">
      <el-alert
        v-if="checkResult" :closable="false" show-icon
        :type="checkResult.passed ? 'success' : 'error'" :title="checkResult.summary"
      />
      <el-table v-if="checkResult" :data="checkResult.items" size="small" class="check-table">
        <el-table-column label="" width="52" align="center">
          <template #default="{ row }">
            <el-icon v-if="row.ok" color="#67c23a"><CircleCheckFilled /></el-icon>
            <el-icon v-else color="#f56c6c"><CircleCloseFilled /></el-icon>
          </template>
        </el-table-column>
        <el-table-column prop="name" label="检查项" width="150" />
        <el-table-column prop="detail" label="说明" />
      </el-table>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
// ⚠️ 图标名要在 @element-plus/icons-vue 里真的存在。
// 这套图标**没有** Stethoscope，写了会编译不过（"没有导出的成员"）。
import { FirstAidKit, MagicStick, Search } from '@element-plus/icons-vue'
import {
  labelingApi, scenicApi,
  type BackfillJob, type ChannelOption, type LabelComment,
  type LabelStats, type ScenicOption,
} from '../api'
import { CHANNEL_LABELS } from '../constants'

const scenics = ref<ScenicOption[]>([])
const channels = ref<ChannelOption[]>([])
const rows = ref<LabelComment[]>([])
const total = ref(0)
const stats = ref<LabelStats | null>(null)
const status = ref<Awaited<ReturnType<typeof labelingApi.status>> | null>(null)
const reviewFlags = ref<Array<{ value: number; label: string }>>(
  Object.entries({
    0: '未标注', 1: '人工复核正确', 2: '人工复核错误', 3: '复核成功',
    4: 'AI标注成功', 5: 'AI标注错误', 6: '未人工复核',
  }).map(([value, label]) => ({ value: Number(value), label })),
)
const loading = ref(false)
const relabelingId = ref<number | null>(null)

const query = reactive({
  scenic_id: '', channel: '', label_state: '', sentiment: '',
  review_flag: '', keyword: '', page: 1, page_size: 20,
})

// ---------------- 加载 ----------------
async function load() {
  loading.value = true
  try {
    const result = await labelingApi.comments({ ...query })
    rows.value = result.items
    total.value = result.total
  } finally {
    loading.value = false
  }
}

async function loadStats() {
  stats.value = await labelingApi.stats({
    scenic_id: query.scenic_id, channel: query.channel,
  })
}

function reload() {
  query.page = 1
  load()
}

function onScopeChange() {
  reload()
  loadStats()
}

// ---------------- 自检 ----------------
const checkVisible = ref(false)
const checkResult = ref<Awaited<ReturnType<typeof labelingApi.check>> | null>(null)

async function runCheck() {
  checkVisible.value = true
  checkResult.value = null
  checkResult.value = await labelingApi.check()
  status.value = await labelingApi.status()
}

// ---------------- 一键补标 ----------------
const backfilling = ref(false)
const job = ref<BackfillJob | null>(null)
let timer: number | undefined

const jobTitle = computed(() => {
  if (!job.value) return ''
  const j = job.value
  if (j.status === 'running') return `正在补标「${jobScope(j)}」的历史未标注评论`
  if (j.status === 'finished') return `「${jobScope(j)}」补标完成，共标了 ${j.done} 条`
  if (j.status === 'canceled') return `已停止，这一轮标了 ${j.done} 条`
  if (j.status === 'failed') return '补标失败'
  return j.status
})

const jobLabel = computed(() => ({
  running: '补标进行中', finished: '已完成',
  failed: '失败', canceled: '已停止',
}[job.value?.status || ''] || job.value?.status || ''))

const jobTagType = computed(() => ({
  running: 'warning', finished: 'success',
  failed: 'danger', canceled: 'info',
}[job.value?.status || ''] || 'info') as 'warning' | 'success' | 'danger' | 'info')

function jobScope(j: BackfillJob): string {
  const s = j.scenic_id
    ? (scenics.value.find((x) => x.scenic_id === j.scenic_id)?.scenic_name || j.scenic_id)
    : '全部景区'
  return j.channel ? `${s} / ${CHANNEL_LABELS[j.channel] || j.channel}` : s
}

/** 秒 → 人话。「已用时 943s」要心算，「15 分 43 秒」不用。 */
function fmtDuration(seconds: number | null): string {
  const n = Math.max(0, Math.round(Number(seconds) || 0))
  if (n < 60) return `${n} 秒`
  const m = Math.floor(n / 60)
  if (m < 60) return `${m} 分 ${n % 60} 秒`
  return `${Math.floor(m / 60)} 小时 ${m % 60} 分`
}

async function startBackfill() {
  // 补标要花钱（每条一次模型调用），先说清楚范围再让用户确认
  const scope = query.scenic_id
    ? (scenics.value.find((s) => s.scenic_id === query.scenic_id)?.scenic_name || query.scenic_id)
    : '全部景区'
  try {
    await ElMessageBox.confirm(
      `将对「${scope}」${query.channel ? `（${CHANNEL_LABELS[query.channel]}）` : ''}` +
      `下的**全部**未标注评论调用大模型补标` +
      (stats.value?.unlabeled ? `（当前未标注 ${stats.value.unlabeled} 条）` : '') +
      `。每条一次调用，会产生费用；会一直标到没有为止，中途可以点「停止」。`,
      '确认补标', { type: 'warning', confirmButtonText: '开始补标' },
    )
  } catch { return }

  backfilling.value = true
  try {
    // 不传 limit = 不设上限，一直标到库里没有未标注的为止
    job.value = await labelingApi.backfill({
      scenic_id: query.scenic_id, channel: query.channel,
    })
    pollJob()
  } finally {
    backfilling.value = false
  }
}

/** 停止补标。已入队的那批还会跑完，所以不是"立刻停"。 */
async function cancelBackfill() {
  if (!job.value) return
  await labelingApi.cancelBackfill(job.value.job_id)
  ElMessage.info('已请求停止，正在跑的那一批结束后收工')
  job.value = { ...job.value, cancel_requested: true }
}

function pollJob() {
  window.clearInterval(timer)
  timer = window.setInterval(async () => {
    if (!job.value) return
    try {
      const fresh = await labelingApi.backfillJob(job.value.job_id)
      const before = job.value.done
      job.value = fresh
      if (fresh.status !== 'running') {
        window.clearInterval(timer)
        await Promise.all([load(), loadStats()])
        ElMessage.success(fresh.status === 'canceled'
          ? `已停止，这一轮标了 ${fresh.done} 条` : '补标结束')
      } else if (fresh.done !== before) {
        // 跑的过程中把统计数字也刷新，用户不用等结束才看到变化
        loadStats().catch(() => {})
      }
    } catch {
      // 服务重启后进度就查不到了，停掉轮询别一直报错
      window.clearInterval(timer)
    }
  }, 2000)
}

/**
 * 页面打开时把还在跑的补标任务捞回来。
 *
 * ⚠️ 这就是"刷新页面后补标进行中不见了"的原因：任务一直在服务端好好跑着，
 *    只是前端刷新后把 job 变量清空了、又没人去问一句。
 *    进度本来就存在服务端（/api/labeling/backfill 列表接口），
 *    刷新、换个浏览器、甚至换台电脑打开，都应该能看到同一个进度。
 */
async function restoreRunningJob() {
  try {
    const jobs = await labelingApi.backfillJobs()
    if (!jobs?.length) return
    // 优先接上还在跑的；没有在跑的就显示最近一次的结果
    const running = jobs.find((j) => j.status === 'running')
    if (running) {
      job.value = running
      pollJob()
      return
    }
    // 只回显 10 分钟内结束的，免得每次进页面都弹一条上周的记录
    const recent = jobs[0]
    if (recent && Date.now() / 1000 - (recent.updated_at || 0) < 600) {
      job.value = recent
    }
  } catch {
    // 拿不到就算了，不该拦住整个页面
  }
}

// ---------------- 审核 ----------------
/** 人工复核：1 标对了 / 2 标错了。改过内容的走 openEdit（写 3 复核成功）。 */
async function review(row: LabelComment, flag: 1 | 2) {
  await labelingApi.save(row.id, { flag, reviewer: '人工' })
  ElMessage.success(flag === 1 ? '已记为「人工复核正确」' : '已记为「人工复核错误」')
  await Promise.all([load(), loadStats()])
}

async function resetOne(row: LabelComment) {
  await labelingApi.reset(row.id)
  ElMessage.success('已撤销复核')
  await Promise.all([load(), loadStats()])
}

async function relabelOne(row: LabelComment) {
  relabelingId.value = row.id
  try {
    await labelingApi.relabel(row.id)
    ElMessage.success('已重新标注，请复核')
    await Promise.all([load(), loadStats()])
  } finally {
    relabelingId.value = null
  }
}

// ---------------- 人工修改 ----------------
const editVisible = ref(false)
const saving = ref(false)
const editing = ref<LabelComment | null>(null)
const form = reactive<{ sentiment_label: string; keyword_tags: string[] }>({
  sentiment_label: '中性', keyword_tags: [],
})
const dimensionText = ref('[]')
const entityText = ref('[]')

function openEdit(row: LabelComment) {
  editing.value = row
  form.sentiment_label = row.sentiment_label || '中性'
  form.keyword_tags = [...(row.keyword_tags || [])]
  dimensionText.value = JSON.stringify(row.dimension_tags || [], null, 0)
  entityText.value = JSON.stringify(row.entity_tags || [], null, 0)
  editVisible.value = true
}

async function saveEdit() {
  if (!editing.value) return
  let dims: unknown
  let ents: unknown
  try {
    dims = JSON.parse(dimensionText.value || '[]')
    ents = JSON.parse(entityText.value || '[]')
  } catch {
    ElMessage.error('维度/实体标签不是合法 JSON，检查一下括号和引号')
    return
  }
  saving.value = true
  try {
    await labelingApi.save(editing.value.id, {
      sentiment_label: form.sentiment_label,
      keyword_tags: form.keyword_tags,
      dimension_tags: dims,
      entity_tags: ents,
      reviewer: '人工',
    })
    ElMessage.success('已保存')
    editVisible.value = false
    await Promise.all([load(), loadStats()])
  } finally {
    saving.value = false
  }
}

// ---------------- 展示辅助 ----------------
function sentimentType(score: number | null) {
  if (score === 1) return 'success'
  if (score === -1) return 'danger'
  return 'info'
}

/** 七档标记的中文名。后端也会随 status 一起下发（reviewFlags），
 *  这里留一份是为了后端还没返回时表格不至于空着。 */
const FLAG_TEXT: Record<number, string> = {
  0: '未标注', 1: '人工复核正确', 2: '人工复核错误', 3: '复核成功',
  4: 'AI标注成功', 5: 'AI标注错误', 6: '未人工复核',
}

function reviewLabel(flag: number) {
  return FLAG_TEXT[flag] || `未知(${flag})`
}

function reviewType(flag: number) {
  // 人工确认过的绿、出错的红、等人看的黄、没标的灰
  return ({
    0: 'info', 1: 'success', 2: 'danger', 3: 'success',
    4: '', 5: 'danger', 6: 'warning',
  }[flag] ?? '') as never
}

onMounted(async () => {
  ;[scenics.value, status.value] = await Promise.all([
    scenicApi.options(false), labelingApi.status(),
  ])
  channels.value = status.value?.channels || []
  if (status.value?.review_flags?.length) reviewFlags.value = status.value.review_flags
  await Promise.all([load(), loadStats(), restoreRunningJob()])
})

onUnmounted(() => window.clearInterval(timer))
</script>

<style scoped>
.labeling-view { display: flex; flex-direction: column; gap: 12px; }
.head-card :deep(.el-card__body) { padding: 14px 16px; }
.filters { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; }
.spacer { flex: 1; }
.stat-row {
  display: flex; align-items: center; gap: 22px;
  margin-top: 14px; padding-top: 12px; border-top: 1px solid var(--el-border-color-lighter);
  flex-wrap: wrap;
}
.stat { display: flex; flex-direction: column; align-items: center; min-width: 56px; }
.stat .n { font-size: 20px; font-weight: 600; line-height: 1.2; }
.stat .k { font-size: 12px; color: var(--el-text-color-secondary); margin-top: 2px; }
.stat.is-ok .n { color: var(--el-color-success); }
.stat.is-warn .n { color: var(--el-color-warning); }
.stat .n.up { color: var(--el-color-success); }
.stat .n.down { color: var(--el-color-danger); }
.job-bar { margin-top: 12px; }
.content { line-height: 1.5; word-break: break-all; }
.meta { font-size: 12px; color: var(--el-text-color-secondary); margin-top: 4px; }
.muted { color: var(--el-text-color-placeholder); }
.tag { margin: 2px 4px 2px 0; }
.expand { padding: 4px 12px; font-size: 13px; }
.expand-line { margin: 6px 0; }
.expand-line b { display: inline-block; width: 74px; color: var(--el-text-color-secondary); font-weight: 500; }
.pager { margin-top: 14px; justify-content: flex-end; }
.edit-content {
  background: var(--el-fill-color-light); padding: 10px 12px;
  border-radius: 6px; margin: 12px 0; line-height: 1.6; word-break: break-all;
}
.edit-hint { margin-bottom: 4px; }
.check-table { margin-top: 12px; }
.job-panel {
  margin-top: 12px;
  padding: 12px 14px;
  border: 1px solid #e8ecf3;
  border-radius: 10px;
  background: #fbfcfe;
}
.job-panel.is-running { border-color: #f0c78a; background: #fffbf3; }
.job-panel.is-finished { border-color: #a7e3c4; background: #f4fcf8; }
.job-panel.is-failed { border-color: #f3b5b5; background: #fff6f6; }
.job-head {
  display: flex; align-items: center; gap: 10px;
  margin-bottom: 10px; font-size: 13px;
}
.job-title { font-weight: 500; color: #1f2733; }
.job-meta { color: #7a8699; font-size: 12px; white-space: nowrap; }
.job-nums {
  margin-top: 8px; font-size: 12px; color: #5b6779;
  display: flex; flex-wrap: wrap; gap: 6px; align-items: center;
}
.job-nums b { color: #1f2733; font-weight: 600; }
.job-nums .sep { color: #c9d2e0; }
.job-hint { margin-top: 6px; font-size: 12px; color: #97a1b2; }
.job-error { margin-top: 6px; font-size: 12px; color: #f56c6c; }
</style>
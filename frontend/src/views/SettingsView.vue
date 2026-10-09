<template>
  <div v-loading="loading">
    <div class="page-header">
      <div>
        <h2 class="page-title">系统设置</h2>
        <p class="page-subtitle">
          这里改的设置存在数据库里，优先级高于 config.yaml，保存后立即生效
        </p>
      </div>
      <el-button :icon="Refresh" @click="load">刷新</el-button>
    </div>

    <el-alert type="info" :closable="false" style="margin-bottom: 16px">
      配置文件：<span class="mono">{{ configFile || '（未找到，使用内置默认值）' }}</span>。
      数据库、Redis、服务端口这些只能改文件——避免在页面上把服务改到连不上。
    </el-alert>

    <el-tabs v-model="tab">
      <!-- 代理 -->
      <el-tab-pane label="代理设置" name="proxy">
        <el-row :gutter="16">
          <el-col :span="14">
            <el-card shadow="never" header="快代理私密代理">
              <el-form :model="proxy" label-width="150px">
                <el-form-item label="启用代理">
                  <el-switch v-model="proxy.enabled" />
                  <span class="muted" style="margin-left: 10px; font-size: 12px">
                    关闭后所有平台走本机直连
                  </span>
                </el-form-item>
                <el-form-item label="鉴权方式">
                  <el-radio-group v-model="proxy.auth_mode">
                    <el-radio value="token">密钥令牌（推荐）</el-radio>
                    <el-radio value="plain">SecretKey 明文</el-radio>
                  </el-radio-group>
                </el-form-item>
                <el-form-item label="SecretId">
                  <el-input v-model="proxy.secret_id" placeholder="留空表示不修改" />
                </el-form-item>
                <el-form-item label="SecretKey">
                  <el-input v-model="proxy.secret_key" type="password" show-password placeholder="留空表示不修改" />
                </el-form-item>
                <el-form-item label="代理用户名">
                  <el-input v-model="proxy.username" placeholder="留空则走白名单模式" />
                </el-form-item>
                <el-form-item label="代理密码">
                  <el-input v-model="proxy.password" type="password" show-password />
                </el-form-item>

                <el-divider content-position="left">轮换策略</el-divider>

                <el-form-item label="IP 池隔离">
                  <el-radio-group v-model="proxy.pool_scope">
                    <el-radio value="channel">每平台独立 IP</el-radio>
                    <el-radio value="global">全平台共用</el-radio>
                  </el-radio-group>
                  <div class="muted" style="font-size: 12px">
                    独立：一个平台被封不牵连其他平台；共用：省快代理提取次数
                  </div>
                </el-form-item>
                <el-form-item label="IP 有效期下限">
                  <el-input-number v-model="proxy.min_ttl_seconds" :min="60" :step="60" style="width: 150px" />
                  <span class="muted" style="margin-left: 6px">秒</span>
                </el-form-item>
                <el-form-item label="IP 有效期上限">
                  <el-input-number v-model="proxy.max_ttl_seconds" :min="60" :step="60" style="width: 150px" />
                  <span class="muted" style="margin-left: 6px">秒</span>
                </el-form-item>
                <el-form-item label="每 N 次请求换 IP">
                  <el-input-number v-model="proxy.rotate_every_n_requests" :min="0" style="width: 150px" />
                  <span class="muted" style="margin-left: 6px">0 = 只按有效期和失败信号换</span>
                </el-form-item>
                <el-form-item label="新 IP 先验连通性">
                  <el-switch v-model="proxy.validate_on_fetch" />
                </el-form-item>

                <el-form-item>
                  <el-button type="primary" :loading="saving" @click="saveSection('proxy', proxy)">
                    保存代理设置
                  </el-button>
                  <el-button :loading="testing" @click="testProxy">测试提取一个 IP</el-button>
                </el-form-item>
              </el-form>
            </el-card>
          </el-col>

          <el-col :span="10">
            <el-card shadow="never" header="当前出口 IP">
              <div v-if="!proxyStatus?.enabled" class="muted">代理未启用</div>
              <template v-else>
                <div class="muted" style="margin-bottom: 10px">
                  隔离方式：{{ proxyStatus.pool_scope === 'channel' ? '每平台独立' : '全平台共用' }}
                </div>
                <el-descriptions v-if="Object.keys(proxyStatus.pools || {}).length" :column="1" border size="small">
                  <el-descriptions-item
                    v-for="(info, scope) in proxyStatus.pools" :key="scope"
                    :label="String(scope) === 'global' ? '全平台' : channelLabel(String(scope))"
                  >
                    <div class="mono">{{ info.current_ip || '未持有' }}</div>
                    <div v-if="info.remaining_ttl > 0" class="muted" style="font-size: 12px">
                      剩余 {{ info.remaining_ttl }} 秒
                    </div>
                  </el-descriptions-item>
                </el-descriptions>
                <div v-else class="muted">还没有采集任务取过 IP</div>
              </template>

              <el-divider />
              <el-descriptions :column="1" border size="small">
                <el-descriptions-item label="Redis">
                  <el-tag :type="redisStatus?.connected ? 'success' : 'danger'" size="small">
                    {{ redisStatus?.connected ? '已连接' : '未连接' }}
                  </el-tag>
                  <span v-if="redisStatus && !redisStatus.is_real_redis" class="muted" style="margin-left: 8px">
                    退化为进程内缓存
                  </span>
                </el-descriptions-item>
                <el-descriptions-item label="调度器">
                  {{ schedulerStatus?.enabled ? '运行中' : '已关闭' }}
                  <span class="muted">
                    （{{ schedulerStatus?.running_count || 0 }}/{{ schedulerStatus?.max_running || 0 }}）
                  </span>
                </el-descriptions-item>
              </el-descriptions>
            </el-card>
          </el-col>
        </el-row>
      </el-tab-pane>

      <!-- 采集参数 -->
      <el-tab-pane label="采集参数" name="crawl">
        <el-card shadow="never" style="max-width: 720px">
          <el-form :model="crawl" label-width="200px">
            <el-form-item label="新建任务默认关键字上限">
              <el-input-number v-model="crawl.default_keyword_limit" :min="1" :max="1000" style="width: 150px" />
              <span class="muted" style="margin-left: 8px">「从景区带入」一次最多带这么多</span>
            </el-form-item>
            <el-form-item label="默认每关键字作品数">
              <el-input-number v-model="crawl.default_max_works" :min="0" style="width: 150px" />
            </el-form-item>
            <el-form-item label="默认每作品评论数">
              <el-input-number v-model="crawl.default_max_comments_per_work" :min="0" style="width: 150px" />
            </el-form-item>
            <el-form-item label="默认采集子评论">
              <el-switch v-model="crawl.enable_sub_comments" />
            </el-form-item>
            <el-form-item label="最深评论层级">
              <el-input-number v-model="crawl.max_comment_level" :min="1" :max="5" style="width: 150px" />
            </el-form-item>
            <el-form-item label="请求间隔（秒）">
              <el-input-number v-model="crawl.request_interval_seconds" :min="0" :step="0.5" style="width: 150px" />
              <span class="muted" style="margin-left: 8px">实际会带 ±20% 抖动</span>
            </el-form-item>
            <el-form-item label="单次请求超时（秒）">
              <el-input-number v-model="crawl.request_timeout_seconds" :min="5" style="width: 150px" />
            </el-form-item>
            <el-form-item label="失败重试次数">
              <el-input-number v-model="crawl.max_retries" :min="1" :max="10" style="width: 150px" />
            </el-form-item>
            <el-form-item label="任务看门狗（分钟）">
              <el-input-number v-model="crawl.task_timeout_minutes" :min="0" :step="30" style="width: 150px" />
              <div class="muted" style="font-size: 12px; margin-top: 4px">
                运行超过这个时长的任务会被取消（0 = 不限制）。
                平台风控把连接挂住时任务会永远卡在一个请求上，看门狗负责把它收掉。
              </div>
            </el-form-item>
            <el-divider content-position="left">
              <span style="font-size: 13px; color: #7a8699">单账号配额与冷却（防账号被封）</span>
            </el-divider>
            <el-form-item label="启用配额">
              <el-switch v-model="crawl.account_quota.enabled" />
              <div class="muted" style="font-size: 12px; margin-top: 4px">
                平台判「机器号」看的不只是请求间隔（那个是上面的「请求间隔」和采集节奏），
                还看<b>一个账号的总量和连续在线时长</b>。这两道闸任一超了，账号就进入冷却，
                挑账号时自动跳过，到期自动恢复。
              </div>
            </el-form-item>
            <el-form-item label="每账号每天最多采">
              <el-input-number
                v-model="crawl.account_quota.daily_works" :min="0" :step="50"
                style="width: 150px"
              />
              <span class="muted" style="margin-left: 8px">条作品，0 = 用平台默认</span>
            </el-form-item>
            <el-form-item label="单次连续最多工作">
              <el-input-number
                v-model="crawl.account_quota.session_minutes" :min="0" :step="15"
                style="width: 150px"
              />
              <span class="muted" style="margin-left: 8px">分钟，0 = 用平台默认</span>
            </el-form-item>
            <el-form-item label="触发后冷却">
              <el-input-number
                v-model="crawl.account_quota.cooldown_minutes" :min="1" :step="30"
                style="width: 150px"
              />
              <span class="muted" style="margin-left: 8px">分钟</span>
              <div class="muted" style="font-size: 12px; margin-top: 4px">
                <b>三项都留 0 = 按平台出厂默认</b>：小红书最保守（150 条/天、45 分钟/次、
                冷却 180 分钟），抖音/快手 300 条、90 分钟，微博 500 条、120 分钟。
                填了就全平台统一用你填的值；单个平台要不一样，到
                <b>账号管理 →「配额与轮换」</b>里按平台填（优先于这里）。
              </div>
            </el-form-item>
            <el-form-item label="账号轮换锁">
              <el-switch v-model="crawl.account_quota.rotate_lock_enabled" />
              <el-input-number
                v-model="crawl.account_quota.rotate_lock_hours" :min="0" :max="336" :step="6"
                style="width: 130px; margin-left: 12px"
                :disabled="crawl.account_quota.rotate_lock_enabled === false"
              />
              <span class="muted" style="margin-left: 8px">小时</span>
              <div class="muted" style="font-size: 12px; margin-top: 4px">
                一个号被派去采集之后，这段时间内<b>先让同组的其他号上</b>——
                挑号原来按「最久没校验」排序，而「最久没校验」不等于「最久没采集」，
                结果就是号有三个、压力全在一个上。0 = 用默认的 12 小时；
                单个号想用别的时长，在账号管理里那一行单独填。
                <br>
                <b>只有一个号（或所有号都在锁定期内）时照用不误</b>，不会把采集卡住——
                它跟冷却不一样，冷却是硬闸门，这个只是「有别人就让别人上」。
              </div>
            </el-form-item>
            <el-divider content-position="left">
              <span style="font-size: 13px; color: #7a8699">采集水印（防重复采）</span>
            </el-divider>
            <el-form-item label="启用水印">
              <el-switch v-model="crawl.dedup.enabled" />
              <div class="muted" style="font-size: 12px; margin-top: 4px">
                采过的关键字和作品会在 Redis 里留个记号，任务重开时直接跳过，
                不用把同样的内容再采一遍。Redis 没连上时自动退化为「不跳过」——
                宁可重复采，也不能因为记号读不到就漏采。
              </div>
            </el-form-item>
            <el-form-item label="关键字水印有效期">
              <el-input-number v-model="crawl.dedup.keyword_ttl_seconds" :min="0" :step="3600"
                               :disabled="!crawl.dedup.enabled" style="width: 150px" />
              <span class="muted" style="margin-left: 8px">
                秒（{{ hoursOf(crawl.dedup.keyword_ttl_seconds) }}）
              </span>
              <div class="muted" style="font-size: 12px; margin-top: 4px">
                这段时间内同一个关键字不再重采。设短了会重复采，
                设长了新内容要等过期才采得到。
              </div>
            </el-form-item>
            <el-form-item label="作品水印有效期">
              <el-input-number v-model="crawl.dedup.work_ttl_seconds" :min="0" :step="86400"
                               :disabled="!crawl.dedup.enabled" style="width: 150px" />
              <span class="muted" style="margin-left: 8px">
                秒（{{ hoursOf(crawl.dedup.work_ttl_seconds) }}）
              </span>
              <div class="muted" style="font-size: 12px; margin-top: 4px">
                作品/笔记/微博级别的记号，采过的直接跳过。
                注意它<b>不分景区</b>：同一条作品被两个景区的关键字搜到，也只采一次。
              </div>
            </el-form-item>
            <el-form-item>
              <el-button type="primary" :loading="saving" @click="saveSection('crawl', crawl)">
                保存采集参数
              </el-button>
            </el-form-item>
          </el-form>
        </el-card>
      </el-tab-pane>

      <!-- 调度与浏览器 -->
      <el-tab-pane label="调度与浏览器" name="runtime">
        <el-row :gutter="16">
          <el-col :span="12">
            <el-card shadow="never" header="调度器">
              <el-form :model="scheduler" label-width="150px">
                <el-form-item label="启用调度器">
                  <el-switch v-model="scheduler.enabled" />
                  <div class="muted" style="font-size: 12px">关闭后任务只能手动触发</div>
                </el-form-item>
                <el-form-item label="同时运行任务数">
                  <el-input-number v-model="scheduler.max_running_tasks" :min="1" :max="20" style="width: 150px" />
                </el-form-item>
                <el-form-item label="时区">
                  <el-input v-model="scheduler.timezone" style="width: 200px" />
                </el-form-item>
                <el-form-item>
                  <el-button type="primary" :loading="saving" @click="saveSection('scheduler', scheduler)">
                    保存
                  </el-button>
                </el-form-item>
              </el-form>
            </el-card>
          </el-col>
          <el-col :span="12">
            <el-card shadow="never" header="浏览器">
              <el-form :model="browser" label-width="170px">
                <el-form-item label="采集用无头模式">
                  <el-switch v-model="browser.headless" />
                  <div class="muted" style="font-size: 12px">
                    管「采集」和「采集 Cookie」。排查风控时关掉它，能看到平台弹了什么
                  </div>
                </el-form-item>
                <el-form-item label="登录窗口用无头">
                  <el-switch v-model="browser.headless_login" />
                  <div class="muted" style="font-size: 12px">
                    管「打开浏览器」那个登录窗口。<b>默认关</b>——无头容易被平台风控拦掉，
                    二维码也可能渲染不出来。服务跑在自己电脑上、不想被弹窗打扰就打开它，
                    画面通过推流照样看得到
                  </div>
                </el-form-item>
                <el-form-item label="定时采集 Cookie（分钟）">
                  <el-input-number
                    v-model="browser.auto_refresh_cookie_minutes"
                    :min="0" :step="10" style="width: 150px"
                  />
                  <div class="muted" style="font-size: 12px">
                    后台按这个间隔把各账号的 Cookie 重抓一遍，失效的自动标红。0 = 关闭
                  </div>
                </el-form-item>
                <el-form-item label="登录态复用时长（秒）">
                  <el-input-number
                    v-model="browser.session_check_interval_seconds"
                    :min="60" :step="60" style="width: 150px"
                  />
                  <div class="muted" style="font-size: 12px">
                    Cookie 在这个时间内视为新鲜，直接用，不启动浏览器
                  </div>
                </el-form-item>
                <el-form-item label="登录等待超时（秒）">
                  <el-input-number v-model="browser.login_timeout_seconds" :min="60" :step="30" style="width: 150px" />
                </el-form-item>
                <el-form-item label="浏览器可执行文件">
                  <el-input v-model="browser.executable_path" placeholder="留空用 Playwright 自带的" />
                </el-form-item>
                <el-form-item>
                  <el-button type="primary" :loading="saving" @click="saveSection('browser', browser)">
                    保存
                  </el-button>
                </el-form-item>
              </el-form>
            </el-card>
          </el-col>
        </el-row>
      </el-tab-pane>

      <!-- ==================== AI 标注 ==================== -->
      <el-tab-pane label="AI 标注" name="labeling">
        <el-row :gutter="16">
          <el-col :span="14">
            <el-card shadow="never">
              <template #header>边采边标</template>
              <el-form label-width="150px">
                <el-form-item label="开启边采边标">
                  <el-switch v-model="labeling.enabled" />
                  <span class="hint">
                    开启后采集到的评论会实时进标注队列。
                    <b>开之前会自检</b>（模型配置、数据库联调、表结构），
                    任一项不过就不标注，不会静默跳过。
                  </span>
                </el-form-item>
                <el-form-item label="模型 API Key">
                  <el-input v-model="labeling.ark.api_key" type="password" show-password
                            placeholder="留空 = 不修改；也可用环境变量 SMC_LABELING__ARK__API_KEY" />
                </el-form-item>
                <el-form-item label="模型名称">
                  <el-input v-model="labeling.ark.model" placeholder="填方舟控制台里已开通的模型，如 glm-5-3-flash-260828" />
                </el-form-item>
                <el-form-item label="模型地址">
                  <el-input v-model="labeling.ark.base_url" />
                </el-form-item>
                <el-form-item label="限流 QPS">
                  <el-input-number v-model="labeling.ark.qps" :min="0" :max="100" />
                  <span class="hint">每秒最多几次模型调用，0 = 不限流</span>
                </el-form-item>
                <el-form-item label="few-shot 示例">
                  <el-switch v-model="labeling.ark.with_fewshot" />
                  <span class="hint">准确率明显更好，代价约 1.5k tokens/条</span>
                </el-form-item>
                <el-form-item label="队列">
                  <el-select v-model="labeling.queue_backend" style="width: 160px">
                    <el-option label="memory（单机）" value="memory" />
                    <el-option label="redis（生产）" value="redis" />
                  </el-select>
                  <span class="hint">memory 重启会丢掉没标完的任务</span>
                </el-form-item>
                <el-form-item label="并发线程">
                  <el-input-number v-model="labeling.worker_concurrency" :min="1" :max="32" />
                </el-form-item>
                <el-form-item label="低置信阈值">
                  <el-input-number v-model="labeling.low_confidence_threshold"
                                   :min="0" :max="1" :step="0.05" />
                  <span class="hint">低于它的标成「未人工复核」，在标注审核页面能筛出来</span>
                </el-form-item>
                <el-form-item>
                  <el-button type="primary" :loading="saving"
                             @click="saveSection('labeling', labeling)">保存</el-button>
                  <el-button :loading="checking" @click="runLabelingCheck">自检</el-button>
                </el-form-item>
              </el-form>
            </el-card>
          </el-col>
          <el-col :span="10">
            <el-card shadow="never">
              <template #header>自检结果</template>
              <el-empty v-if="!labelCheck" description="点左边的「自检」看模型配置、数据库联调是否就绪" />
              <template v-else>
                <el-alert :closable="false" show-icon
                          :type="labelCheck.passed ? 'success' : 'error'"
                          :title="labelCheck.summary" />
                <el-table :data="labelCheck.items" size="small" style="margin-top: 12px">
                  <el-table-column width="46" align="center">
                    <template #default="{ row }">
                      <el-icon v-if="row.ok" color="#67c23a"><CircleCheckFilled /></el-icon>
                      <el-icon v-else color="#f56c6c"><CircleCloseFilled /></el-icon>
                    </template>
                  </el-table-column>
                  <el-table-column prop="name" label="检查项" width="130" />
                  <el-table-column prop="detail" label="说明" />
                </el-table>
              </template>
            </el-card>
          </el-col>
        </el-row>
      </el-tab-pane>

      <!-- ==================== 通知 ==================== -->
      <el-tab-pane label="通知" name="notify">
        <el-row :gutter="16">
          <el-col :span="14">
            <el-card shadow="never">
              <template #header>飞书机器人</template>
              <el-form label-width="150px">
                <el-form-item label="开启通知">
                  <el-switch v-model="notify.enabled" />
                  <span class="hint">
                    采集需要人工介入（账号失效 / 二次验证 / 要登录）或采集报错时推送
                  </span>
                </el-form-item>
                <el-form-item label="机器人地址">
                  <el-input v-model="notify.feishu_webhook" type="password" show-password
                            placeholder="https://open.feishu.cn/open-apis/bot/v2/hook/xxx（留空 = 不修改）" />
                </el-form-item>
                <el-form-item label="远程账号">
                  <el-input v-model="notify.remote_account" placeholder="向日葵账号，会写进通知正文" />
                </el-form-item>
                <el-form-item label="远程密码">
                  <el-input v-model="notify.remote_password" type="password" show-password
                            placeholder="留空 = 不修改" />
                </el-form-item>
                <el-form-item label="同类问题冷却">
                  <el-input-number v-model="notify.cooldown_seconds" :min="0" :step="60" />
                  <span class="hint">
                    秒。同一平台的同一类问题在这段时间内只发一次——
                    否则一轮采集能刷出上百条一样的消息，真出事反而被淹掉
                  </span>
                </el-form-item>
                <el-form-item label="需登录时暂停">
                  <el-switch v-model="notify.pause_on_login_required" />
                  <span class="hint">
                    开启后遇到需要登录/验证会<b>暂停等人处理</b>，
                    检测到账号恢复自动继续；关掉则直接跳过该平台
                  </span>
                </el-form-item>
                <el-form-item label="最多等待">
                  <el-input-number v-model="notify.pause_timeout_minutes" :min="0" :max="720" />
                  <span class="hint">分钟。到点还没人处理就放弃这个平台</span>
                </el-form-item>
                <el-form-item>
                  <el-button type="primary" :loading="saving"
                             @click="saveSection('notify', notify)">保存</el-button>
                  <el-button :loading="testingNotify" @click="testNotify">发送测试</el-button>
                </el-form-item>
              </el-form>
            </el-card>
          </el-col>
          <el-col :span="10">
            <el-card shadow="never">
              <template #header>消息预览</template>
              <el-alert v-if="notifyResult && notifyResult.error" type="error" :closable="false"
                        show-icon :title="notifyResult.error" style="margin-bottom: 10px" />
              <el-alert v-else-if="notifyResult" type="success" :closable="false" show-icon
                        title="已发出，去飞书群里看看" style="margin-bottom: 10px" />
              <pre class="notify-preview">{{ notifyResult?.preview || previewPlaceholder }}</pre>
            </el-card>
          </el-col>
        </el-row>
      </el-tab-pane>
    </el-tabs>
  </div>
</template>

<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { Refresh } from '@element-plus/icons-vue'
import { labelingApi, settingsApi } from '../api'
import { channelLabel } from '../constants'

const tab = ref('proxy')
const loading = ref(false)
const saving = ref(false)
const testing = ref(false)
const configFile = ref('')

const proxyStatus = ref<Record<string, any> | null>(null)
const redisStatus = ref<Record<string, any> | null>(null)
const schedulerStatus = ref<Record<string, any> | null>(null)

const proxy = reactive<Record<string, any>>({
  enabled: false, auth_mode: 'token', secret_id: '', secret_key: '',
  username: '', password: '', pool_scope: 'channel',
  min_ttl_seconds: 1200, max_ttl_seconds: 1800,
  rotate_every_n_requests: 0, validate_on_fetch: true,
})
const crawl = reactive<Record<string, any>>({ dedup: {}, account_quota: {} })

/** 把秒数说成人话——「14400」看不出是 4 小时。 */
function hoursOf(seconds: number): string {
  const n = Number(seconds) || 0
  if (n <= 0) return '不过期'
  if (n < 3600) return `${Math.round(n / 60)} 分钟`
  if (n < 86400) return `${+(n / 3600).toFixed(1)} 小时`
  return `${+(n / 86400).toFixed(1)} 天`
}
const labeling = reactive<Record<string, any>>({ ark: {} })
const notify = reactive<Record<string, any>>({})
const checking = ref(false)
const testingNotify = ref(false)
const labelCheck = ref<{ passed: boolean; summary: string; items: any[] } | null>(null)
const notifyResult = ref<{ sent: boolean; preview: string; error: string } | null>(null)

const previewPlaceholder = `舆情采集需人工验证：
平台：抖音
验证项：验证码
请使用向日葵远程登录操作：
远程账号：（保存后点「发送测试」看实际内容）
远程密码：……`
const scheduler = reactive<Record<string, any>>({})
const browser = reactive<Record<string, any>>({})

/** 后端返回的密钥是打码的 ***，不能原样回填后再保存，否则会把真密钥覆盖成 *** */
function stripMasked(value: unknown): string {
  return value === '***' ? '' : String(value ?? '')
}

async function load() {
  loading.value = true
  try {
    const [settings, proxyState, redisState, schedulerState] = await Promise.all([
      settingsApi.get(),
      settingsApi.proxyStatus().catch(() => null),
      settingsApi.redisStatus().catch(() => null),
      settingsApi.schedulerStatus().catch(() => null),
    ])
    configFile.value = settings.config_file
    const config = settings.config

    Object.assign(proxy, config.proxy, {
      secret_id: stripMasked(config.proxy?.secret_id),
      secret_key: stripMasked(config.proxy?.secret_key),
      password: stripMasked(config.proxy?.password),
    })
    delete proxy.per_platform
    Object.assign(crawl, config.crawl)
    // 模板里直接读 crawl.dedup.xxx，后端返回里没有这一段就会报
    // "Cannot read properties of undefined"，整个设置页白屏。
    if (!crawl.dedup) crawl.dedup = { enabled: true, keyword_ttl_seconds: 14400, work_ttl_seconds: 259200 }
    // 同理：模板直接读 crawl.account_quota.xxx，缺这一段整页白屏。
    // 三项默认给 0 = 「按平台出厂默认」，不是「不限制」——
    // 后端 limits_for() 在用户没填时会落到各平台的保守值。
    if (!crawl.account_quota) {
      crawl.account_quota = {
        enabled: true, daily_works: 0, session_minutes: 0, cooldown_minutes: 0,
        rotate_lock_enabled: true, rotate_lock_hours: 12,
      }
    }
    for (const k of ['daily_works', 'session_minutes', 'cooldown_minutes']) {
      if (crawl.account_quota[k] == null) crawl.account_quota[k] = 0
    }
    // 轮换锁：没配过就是「开着、12 小时」，跟后端 account_rotation.py 的
    // 出厂值保持一致。这里给 null 会让 el-input-number 显示成空白，
    // 用户以为没生效、其实后端在用默认值——不如直接把默认值显出来。
    if (crawl.account_quota.rotate_lock_enabled == null) {
      crawl.account_quota.rotate_lock_enabled = true
    }
    if (!crawl.account_quota.rotate_lock_hours) {
      crawl.account_quota.rotate_lock_hours = 12
    }
    // ⚠️ 密钥类字段后端返回的是 ***，原样回填再保存会把真值覆盖成 ***
    Object.assign(labeling, config.labeling, {
      ark: { ...(config.labeling?.ark || {}),
             api_key: stripMasked(config.labeling?.ark?.api_key) },
    })
    Object.assign(notify, config.notify, {
      feishu_webhook: stripMasked(config.notify?.feishu_webhook),
      remote_password: stripMasked(config.notify?.remote_password),
    })
    Object.assign(scheduler, config.scheduler)
    Object.assign(browser, config.browser)

    proxyStatus.value = proxyState
    redisStatus.value = redisState
    schedulerStatus.value = schedulerState
  } finally {
    loading.value = false
  }
}

async function saveSection(section: string, values: Record<string, any>) {
  saving.value = true
  try {
    const payload = { ...values }
    // 密钥留空表示"不修改"，不要把空字符串写进去
    if (section === 'proxy') {
      for (const key of ['secret_id', 'secret_key', 'password']) {
        if (!payload[key]) delete payload[key]
      }
    }
    if (section === 'notify') {
      for (const key of ['feishu_webhook', 'remote_password']) {
        if (!payload[key]) delete payload[key]
      }
    }
    if (section === 'labeling') {
      payload.ark = { ...payload.ark }
      if (!payload.ark.api_key) delete payload.ark.api_key
    }
    await settingsApi.save(section, payload)
    ElMessage.success('已保存并立即生效')
    await load()
  } finally {
    saving.value = false
  }
}

async function runLabelingCheck() {
  checking.value = true
  try {
    labelCheck.value = await labelingApi.check()
  } finally {
    checking.value = false
  }
}

async function testNotify() {
  testingNotify.value = true
  try {
    notifyResult.value = await settingsApi.testNotify()
  } finally {
    testingNotify.value = false
  }
}

async function testProxy() {
  testing.value = true
  try {
    const result = await settingsApi.testProxy({
      secret_id: proxy.secret_id || undefined,
      secret_key: proxy.secret_key || undefined,
      username: proxy.username,
      password: proxy.password || undefined,
      auth_mode: proxy.auth_mode,
    })
    if (result.ok) {
      ElMessage.success(`提取成功：${result.proxy}（有效期约 ${result.ttl_seconds} 秒）`)
    } else {
      ElMessage.error(result.message || '测试失败')
    }
  } finally {
    testing.value = false
  }
}

onMounted(load)
</script>

<style scoped>
.notify-preview {
  background: var(--el-fill-color-light);
  padding: 12px 14px;
  border-radius: 6px;
  font-family: inherit;
  font-size: 13px;
  line-height: 1.8;
  white-space: pre-wrap;
  margin: 0;
}
</style>

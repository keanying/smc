<template>
  <div v-loading="loading" class="settings-page">
    <div class="page-header">
      <h2 class="page-title">
        系统设置
        <InfoTip :width="360">
          设置存数据库，优先级高于 config.yaml，保存后立即生效。<br>
          配置文件：<span class="mono">{{ configFile || '未找到，使用内置默认值' }}</span><br>
          数据库、Redis、服务端口只能改文件，避免在页面上把服务改到连不上。
        </InfoTip>
      </h2>
      <el-button :icon="Refresh" @click="load">刷新</el-button>
    </div>

    <el-tabs v-model="tab">
      <!-- 代理 -->
      <el-tab-pane label="代理设置" name="proxy">
        <el-row :gutter="16">
          <el-col :xs="24" :lg="14" class="col">
            <el-card shadow="never" header="快代理私密代理">
              <el-form :model="proxy" label-width="150px" class="settings-form">
                <el-form-item>
                  <template #label>
                    <span class="lbl">启用代理<InfoTip content="关闭后所有平台走本机直连" /></span>
                  </template>
                  <el-switch v-model="proxy.enabled" />
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

                <div class="section-title form-section">轮换策略</div>

                <el-form-item>
                  <template #label>
                    <span class="lbl">IP 池隔离<InfoTip>
                      每平台独立：一个平台被封不牵连其他平台<br>全平台共用：省快代理提取次数
                    </InfoTip></span>
                  </template>
                  <el-radio-group v-model="proxy.pool_scope">
                    <el-radio value="channel">每平台独立 IP</el-radio>
                    <el-radio value="global">全平台共用</el-radio>
                  </el-radio-group>
                </el-form-item>
                <el-form-item label="IP 有效期下限">
                  <el-input-number v-model="proxy.min_ttl_seconds" :min="60" :step="60" class="num" />
                  <span class="unit">秒</span>
                </el-form-item>
                <el-form-item label="IP 有效期上限">
                  <el-input-number v-model="proxy.max_ttl_seconds" :min="60" :step="60" class="num" />
                  <span class="unit">秒</span>
                </el-form-item>
                <el-form-item>
                  <template #label>
                    <span class="lbl">每 N 次请求换 IP<InfoTip content="0 = 只按有效期和失败信号换" /></span>
                  </template>
                  <el-input-number v-model="proxy.rotate_every_n_requests" :min="0" class="num" />
                  <span class="unit">次</span>
                </el-form-item>
                <el-form-item label="新 IP 先验连通性">
                  <el-switch v-model="proxy.validate_on_fetch" />
                </el-form-item>

                <el-form-item class="form-actions">
                  <el-button type="primary" :loading="saving" @click="saveSection('proxy', proxy)">
                    保存代理设置
                  </el-button>
                  <el-button :loading="testing" @click="testProxy">测试提取一个 IP</el-button>
                </el-form-item>
              </el-form>
            </el-card>
          </el-col>

          <el-col :xs="24" :lg="10" class="col">
            <el-card shadow="never" header="当前出口 IP">
              <div v-if="!proxyStatus?.enabled" class="muted small">代理未启用</div>
              <template v-else>
                <div class="muted small" style="margin-bottom: 10px">
                  隔离方式：{{ proxyStatus.pool_scope === 'channel' ? '每平台独立' : '全平台共用' }}
                </div>
                <el-descriptions v-if="Object.keys(proxyStatus.pools || {}).length" :column="1" border size="small">
                  <el-descriptions-item
                    v-for="(info, scope) in proxyStatus.pools" :key="scope"
                    :label="String(scope) === 'global' ? '全平台' : channelLabel(String(scope))"
                  >
                    <span class="mono">{{ info.current_ip || '未持有' }}</span>
                    <span v-if="info.remaining_ttl > 0" class="muted small" style="margin-left: 8px">
                      剩余 {{ info.remaining_ttl }} 秒
                    </span>
                  </el-descriptions-item>
                </el-descriptions>
                <div v-else class="muted small">还没有采集任务取过 IP</div>
              </template>

              <div class="section-title" style="margin-top: 20px">服务状态</div>
              <el-descriptions :column="1" border size="small">
                <el-descriptions-item label="Redis">
                  <el-tag :type="redisStatus?.connected ? 'success' : 'danger'" size="small">
                    {{ redisStatus?.connected ? '已连接' : '未连接' }}
                  </el-tag>
                  <span v-if="redisStatus && !redisStatus.is_real_redis" class="muted small" style="margin-left: 8px">
                    退化为进程内缓存
                  </span>
                </el-descriptions-item>
                <el-descriptions-item label="调度器">
                  {{ schedulerStatus?.enabled ? '运行中' : '已关闭' }}
                  <span class="muted small">
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
        <el-card shadow="never" class="narrow-card">
          <el-form :model="crawl" label-width="150px" class="settings-form">
            <div class="section-title form-section">任务默认值</div>
            <el-form-item>
              <template #label>
                <span class="lbl">默认关键字上限<InfoTip content="新建任务「从景区带入」时一次最多带这么多关键字" /></span>
              </template>
              <el-input-number v-model="crawl.default_keyword_limit" :min="1" :max="1000" class="num" />
              <span class="unit">个</span>
            </el-form-item>
            <el-form-item label="每关键字作品数">
              <el-input-number v-model="crawl.default_max_works" :min="0" class="num" />
              <span class="unit">条</span>
            </el-form-item>
            <el-form-item label="每作品评论数">
              <el-input-number v-model="crawl.default_max_comments_per_work" :min="0" class="num" />
              <span class="unit">条</span>
            </el-form-item>
            <el-form-item label="采集子评论">
              <el-switch v-model="crawl.enable_sub_comments" />
            </el-form-item>
            <el-form-item label="最深评论层级">
              <el-input-number v-model="crawl.max_comment_level" :min="1" :max="5" class="num" />
              <span class="unit">层</span>
            </el-form-item>

            <div class="section-title form-section">请求与超时</div>
            <el-form-item>
              <template #label>
                <span class="lbl">请求间隔<InfoTip content="实际会带 ±20% 抖动" /></span>
              </template>
              <el-input-number v-model="crawl.request_interval_seconds" :min="0" :step="0.5" class="num" />
              <span class="unit">秒</span>
            </el-form-item>
            <el-form-item label="单次请求超时">
              <el-input-number v-model="crawl.request_timeout_seconds" :min="5" class="num" />
              <span class="unit">秒</span>
            </el-form-item>
            <el-form-item label="失败重试次数">
              <el-input-number v-model="crawl.max_retries" :min="1" :max="10" class="num" />
              <span class="unit">次</span>
            </el-form-item>
            <el-form-item>
              <template #label>
                <span class="lbl">任务看门狗<InfoTip>
                  运行超过这个时长的任务会被取消，0 = 不限制。<br>
                  平台风控把连接挂住时任务会一直卡在一个请求上，看门狗负责把它收掉。
                </InfoTip></span>
              </template>
              <el-input-number v-model="crawl.task_timeout_minutes" :min="0" :step="30" class="num" />
              <span class="unit">分钟</span>
            </el-form-item>

            <!--
              平台判「机器号」不只看请求间隔，还看单个账号的总量和连续在线时长，
              所以单独设这两道闸：任一超限账号就进入冷却，挑号时跳过，到期自动恢复。
            -->
            <div class="section-title form-section">
              单账号配额与冷却
              <InfoTip :width="380">
                防账号被封：单账号<b>每日总量</b>或<b>连续工作时长</b>任一超限即进入冷却，挑号时自动跳过，到期自动恢复。<br>
                <b>三项留 0 = 按平台出厂默认</b>：小红书 150 条/天、45 分钟/次、冷却 180 分钟；
                抖音/快手 300 条、90 分钟；微博 500 条、120 分钟。<br>
                填了则全平台统一使用；单个平台要不同，到<b>账号管理 →「配额与轮换」</b>按平台填（优先于这里）。
              </InfoTip>
            </div>
            <el-form-item label="启用配额">
              <el-switch v-model="crawl.account_quota.enabled" />
            </el-form-item>
            <el-form-item>
              <template #label>
                <span class="lbl">每账号每天最多采<InfoTip content="0 = 用平台默认" /></span>
              </template>
              <el-input-number
                v-model="crawl.account_quota.daily_works" :min="0" :step="50"
                class="num"
              />
              <span class="unit">条作品</span>
            </el-form-item>
            <el-form-item>
              <template #label>
                <span class="lbl">单次连续最多工作<InfoTip content="0 = 用平台默认" /></span>
              </template>
              <el-input-number
                v-model="crawl.account_quota.session_minutes" :min="0" :step="15"
                class="num"
              />
              <span class="unit">分钟</span>
            </el-form-item>
            <el-form-item>
              <template #label>
                <span class="lbl">触发后冷却<InfoTip content="0 = 用平台默认" /></span>
              </template>
              <!-- ⚠️ min 必须是 0。以前是 1：没配过时页面初始化成 0，被输入框夹成 1，
                   一点「保存采集参数」就把全平台冷却改成了 1 分钟——冷却形同虚设。
                   后端 _apply 把 0 当"没填、用平台默认"，和上面两项一致。 -->
              <el-input-number
                v-model="crawl.account_quota.cooldown_minutes" :min="0" :step="30"
                class="num"
              />
              <span class="unit">分钟</span>
            </el-form-item>
            <!--
              轮换锁的由来：挑号原来按「最久没校验」排序，而「最久没校验」不等于「最久没采集」，
              结果号有三个、压力全在一个上。它跟冷却不一样：冷却是硬闸门，这个只是「有别人就让别人上」。
            -->
            <el-form-item>
              <template #label>
                <span class="lbl">账号轮换锁<InfoTip :width="360">
                  一个号被派去采集后，这段时间内<b>先让同组的其他号上</b>，避免压力集中在一个号。<br>
                  0 = 默认 12 小时；单个号想用别的时长，在账号管理里那一行「编辑」中单独填。<br>
                  <b>只有一个号（或都在锁定期内）时照用不误</b>，不会卡住采集。
                </InfoTip></span>
              </template>
              <el-switch v-model="crawl.account_quota.rotate_lock_enabled" />
              <el-input-number
                v-model="crawl.account_quota.rotate_lock_hours" :min="0" :max="336" :step="6"
                class="num" style="margin-left: 12px"
                :disabled="crawl.account_quota.rotate_lock_enabled === false"
              />
              <span class="unit">小时</span>
            </el-form-item>

            <!--
              Redis 没连上时水印自动退化为「不跳过」——宁可重复采，也不能因为记号读不到就漏采。
            -->
            <div class="section-title form-section">
              采集水印
              <InfoTip>
                防重复采：采过的关键字和作品在 Redis 里留记号，任务重开时直接跳过。<br>
                Redis 未连接时自动退化为「不跳过」，宁可重复采也不漏采。
              </InfoTip>
            </div>
            <el-form-item label="启用水印">
              <el-switch v-model="crawl.dedup.enabled" />
            </el-form-item>
            <el-form-item>
              <template #label>
                <span class="lbl">关键字水印有效期<InfoTip>
                  这段时间内同一关键字不再重采。<br>设短了会重复采，设长了新内容要等过期才采得到。
                </InfoTip></span>
              </template>
              <el-input-number v-model="crawl.dedup.keyword_ttl_seconds" :min="0" :step="3600"
                               :disabled="!crawl.dedup.enabled" class="num" />
              <span class="unit">秒 · {{ hoursOf(crawl.dedup.keyword_ttl_seconds) }}</span>
            </el-form-item>
            <el-form-item>
              <template #label>
                <span class="lbl">作品水印有效期<InfoTip>
                  作品 / 笔记 / 微博级别的记号，采过的直接跳过。<br>
                  <b>不分景区</b>：同一条作品被两个景区的关键字搜到，也只采一次。
                </InfoTip></span>
              </template>
              <el-input-number v-model="crawl.dedup.work_ttl_seconds" :min="0" :step="86400"
                               :disabled="!crawl.dedup.enabled" class="num" />
              <span class="unit">秒 · {{ hoursOf(crawl.dedup.work_ttl_seconds) }}</span>
            </el-form-item>

            <el-form-item class="form-actions">
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
          <el-col :xs="24" :lg="10" class="col">
            <el-card shadow="never" header="调度器">
              <el-form :model="scheduler" label-width="150px" class="settings-form">
                <el-form-item>
                  <template #label>
                    <span class="lbl">启用调度器<InfoTip content="关闭后任务只能手动触发" /></span>
                  </template>
                  <el-switch v-model="scheduler.enabled" />
                </el-form-item>
                <el-form-item label="同时运行任务数">
                  <el-input-number v-model="scheduler.max_running_tasks" :min="1" :max="20" class="num" />
                  <span class="unit">个</span>
                </el-form-item>
                <el-form-item label="时区">
                  <el-input v-model="scheduler.timezone" style="width: 200px" />
                </el-form-item>
                <el-form-item class="form-actions">
                  <el-button type="primary" :loading="saving" @click="saveSection('scheduler', scheduler)">
                    保存
                  </el-button>
                </el-form-item>
              </el-form>
            </el-card>
          </el-col>
          <el-col :xs="24" :lg="14" class="col">
            <el-card shadow="never" header="浏览器">
              <el-form :model="browser" label-width="150px" class="settings-form">
                <el-form-item>
                  <template #label>
                    <span class="lbl">采集用无头模式<InfoTip>
                      作用于「采集」和「采集 Cookie」。<br>排查风控时关掉，能看到平台弹了什么。
                    </InfoTip></span>
                  </template>
                  <el-switch v-model="browser.headless" />
                </el-form-item>
                <el-form-item>
                  <template #label>
                    <span class="lbl">登录窗口用无头<InfoTip :width="340">
                      作用于「打开浏览器」的登录窗口。<b>默认关</b>：无头容易被平台风控拦掉，二维码也可能渲染不出来。<br>
                      服务跑在自己电脑上、不想被弹窗打扰时可开启，画面通过推流照样看得到。
                    </InfoTip></span>
                  </template>
                  <el-switch v-model="browser.headless_login" />
                </el-form-item>
                <el-form-item>
                  <template #label>
                    <span class="lbl">定时采集 Cookie<InfoTip content="后台按这个间隔重抓各账号 Cookie，失效的自动标红。0 = 关闭" /></span>
                  </template>
                  <el-input-number
                    v-model="browser.auto_refresh_cookie_minutes"
                    :min="0" :step="10" class="num"
                  />
                  <span class="unit">分钟</span>
                </el-form-item>
                <el-form-item>
                  <template #label>
                    <span class="lbl">登录态复用时长<InfoTip content="Cookie 在这个时间内视为新鲜，直接用，不启动浏览器" /></span>
                  </template>
                  <el-input-number
                    v-model="browser.session_check_interval_seconds"
                    :min="60" :step="60" class="num"
                  />
                  <span class="unit">秒</span>
                </el-form-item>
                <el-form-item label="登录等待超时">
                  <el-input-number v-model="browser.login_timeout_seconds" :min="60" :step="30" class="num" />
                  <span class="unit">秒</span>
                </el-form-item>
                <el-form-item label="浏览器可执行文件">
                  <el-input v-model="browser.executable_path" placeholder="留空用 Playwright 自带的" />
                </el-form-item>
                <el-form-item class="form-actions">
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
          <el-col :xs="24" :lg="14" class="col">
            <el-card shadow="never">
              <template #header>边采边标</template>
              <el-form label-width="150px" class="settings-form">
                <el-form-item>
                  <template #label>
                    <span class="lbl">开启边采边标<InfoTip>
                      采集到的评论实时进入标注队列。<br>
                      <b>开启前会自检</b>（模型配置、数据库联调、表结构），任一项不过就不标注，不会静默跳过。
                    </InfoTip></span>
                  </template>
                  <el-switch v-model="labeling.enabled" />
                </el-form-item>

                <div class="section-title form-section">模型</div>
                <el-form-item>
                  <template #label>
                    <span class="lbl">API Key<InfoTip content="也可用环境变量 SMC_LABELING__ARK__API_KEY" /></span>
                  </template>
                  <el-input v-model="labeling.ark.api_key" type="password" show-password
                            placeholder="留空 = 不修改" />
                </el-form-item>
                <el-form-item>
                  <template #label>
                    <span class="lbl">模型名称<InfoTip content="填方舟控制台里已开通的模型" /></span>
                  </template>
                  <el-input v-model="labeling.ark.model" placeholder="如 glm-5-3-flash-260828" />
                </el-form-item>
                <el-form-item label="模型地址">
                  <el-input v-model="labeling.ark.base_url" />
                </el-form-item>
                <el-form-item>
                  <template #label>
                    <span class="lbl">限流 QPS<InfoTip content="每秒最多几次模型调用，0 = 不限流" /></span>
                  </template>
                  <el-input-number v-model="labeling.ark.qps" :min="0" :max="100" class="num" />
                </el-form-item>
                <el-form-item>
                  <template #label>
                    <span class="lbl">few-shot 示例<InfoTip content="准确率明显更好，代价约 1.5k tokens/条" /></span>
                  </template>
                  <el-switch v-model="labeling.ark.with_fewshot" />
                </el-form-item>

                <div class="section-title form-section">执行</div>
                <el-form-item>
                  <template #label>
                    <span class="lbl">队列<InfoTip content="memory 重启会丢掉没标完的任务" /></span>
                  </template>
                  <el-select v-model="labeling.queue_backend" class="num">
                    <el-option label="memory（单机）" value="memory" />
                    <el-option label="redis（生产）" value="redis" />
                  </el-select>
                </el-form-item>
                <el-form-item label="并发线程">
                  <el-input-number v-model="labeling.worker_concurrency" :min="1" :max="32" class="num" />
                </el-form-item>
                <el-form-item>
                  <template #label>
                    <span class="lbl">低置信阈值<InfoTip content="低于它的标成「未人工复核」，在标注审核页面能筛出来" /></span>
                  </template>
                  <el-input-number v-model="labeling.low_confidence_threshold"
                                   :min="0" :max="1" :step="0.05" class="num" />
                </el-form-item>

                <el-form-item class="form-actions">
                  <el-button type="primary" :loading="saving"
                             @click="saveSection('labeling', labeling)">保存</el-button>
                  <el-button :loading="checking" @click="runLabelingCheck">自检</el-button>
                </el-form-item>
              </el-form>
            </el-card>
          </el-col>
          <el-col :xs="24" :lg="10" class="col">
            <el-card shadow="never">
              <template #header>自检结果</template>
              <el-empty v-if="!labelCheck" :image-size="80" description="点「自检」检查模型配置与数据库是否就绪" />
              <template v-else>
                <el-alert :closable="false" show-icon
                          :type="labelCheck.passed ? 'success' : 'error'"
                          :title="labelCheck.summary" />
                <el-table :data="labelCheck.items" size="small" style="margin-top: 12px">
                  <el-table-column width="40" align="center">
                    <template #default="{ row }">
                      <el-icon v-if="row.ok" color="#67c23a"><CircleCheckFilled /></el-icon>
                      <el-icon v-else color="#f56c6c"><CircleCloseFilled /></el-icon>
                    </template>
                  </el-table-column>
                  <el-table-column prop="name" label="检查项" width="120" />
                  <el-table-column label="说明">
                    <template #default="{ row }">
                      <span class="check-detail" :class="{ 'is-fail': !row.ok }">{{ row.detail }}</span>
                    </template>
                  </el-table-column>
                </el-table>
              </template>
            </el-card>
          </el-col>
        </el-row>
      </el-tab-pane>

      <!-- ==================== 通知 ==================== -->
      <el-tab-pane label="通知" name="notify">
        <el-row :gutter="16">
          <el-col :xs="24" :lg="14" class="col">
            <el-card shadow="never">
              <template #header>飞书机器人</template>
              <el-form label-width="150px" class="settings-form">
                <el-form-item>
                  <template #label>
                    <span class="lbl">开启通知<InfoTip content="采集需要人工介入（账号失效 / 二次验证 / 要登录）或采集报错时推送" /></span>
                  </template>
                  <el-switch v-model="notify.enabled" />
                </el-form-item>
                <el-form-item>
                  <template #label>
                    <span class="lbl">机器人地址<InfoTip content="飞书群机器人 Webhook，形如 https://open.feishu.cn/open-apis/bot/v2/hook/xxx" /></span>
                  </template>
                  <el-input v-model="notify.feishu_webhook" type="password" show-password
                            placeholder="留空 = 不修改" />
                </el-form-item>
                <el-form-item>
                  <template #label>
                    <span class="lbl">远程账号<InfoTip content="向日葵账号，会写进通知正文" /></span>
                  </template>
                  <el-input v-model="notify.remote_account" placeholder="向日葵账号" />
                </el-form-item>
                <el-form-item label="远程密码">
                  <el-input v-model="notify.remote_password" type="password" show-password
                            placeholder="留空 = 不修改" />
                </el-form-item>

                <div class="section-title form-section">节流与暂停</div>
                <!-- 不做冷却的话一轮采集能刷出上百条一样的消息，真出事反而被淹掉 -->
                <el-form-item>
                  <template #label>
                    <span class="lbl">同类问题冷却<InfoTip content="同一平台的同一类问题在这段时间内只发一次，避免刷屏淹没真正的问题" /></span>
                  </template>
                  <el-input-number v-model="notify.cooldown_seconds" :min="0" :step="60" class="num" />
                  <span class="unit">秒</span>
                </el-form-item>
                <el-form-item>
                  <template #label>
                    <span class="lbl">需登录时暂停<InfoTip>
                      开启：遇到需要登录/验证时<b>暂停等人处理</b>，检测到账号恢复自动继续。<br>
                      关闭：直接跳过该平台。
                    </InfoTip></span>
                  </template>
                  <el-switch v-model="notify.pause_on_login_required" />
                </el-form-item>
                <el-form-item>
                  <template #label>
                    <span class="lbl">最多等待<InfoTip content="到点还没人处理就放弃这个平台" /></span>
                  </template>
                  <el-input-number v-model="notify.pause_timeout_minutes" :min="0" :max="720" class="num" />
                  <span class="unit">分钟</span>
                </el-form-item>

                <el-form-item class="form-actions">
                  <el-button type="primary" :loading="saving"
                             @click="saveSection('notify', notify)">保存</el-button>
                  <el-button :loading="testingNotify" @click="testNotify">发送测试</el-button>
                </el-form-item>
              </el-form>
            </el-card>
          </el-col>
          <el-col :xs="24" :lg="10" class="col">
            <el-card shadow="never">
              <template #header>消息预览</template>
              <el-alert v-if="notifyResult && notifyResult.error" type="error" :closable="false"
                        show-icon :title="notifyResult.error" style="margin-bottom: 12px" />
              <el-alert v-else-if="notifyResult" type="success" :closable="false" show-icon
                        title="已发出，去飞书群里看看" style="margin-bottom: 12px" />
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
.col { margin-bottom: 16px; }
.narrow-card { max-width: 760px; }

/* 字段名 + ⓘ：包一层 span，避免 label 的 flex 布局把图标顶到上沿 */
.lbl { display: inline-block; white-space: nowrap; }

.settings-form :deep(.el-form-item) { margin-bottom: 18px; }
.settings-form :deep(.el-form-item__label) { font-size: 13px; }
.settings-form .num { width: 160px; }

/* 卡片内分段标题：首段贴顶，后续段落用细分隔线隔开 */
.form-section {
  margin: 28px 0 16px;
  padding-top: 20px;
  border-top: 1px solid var(--smc-border);
}
.settings-form > .form-section:first-child {
  margin-top: 0;
  padding-top: 0;
  border-top: none;
}

.unit {
  margin-left: 8px;
  font-size: 13px;
  color: var(--smc-text-secondary);
}
.small { font-size: 12px; }

.form-actions {
  margin-top: 8px;
  margin-bottom: 0 !important;
}

.check-detail {
  font-size: 13px;
  line-height: 1.6;
  color: var(--el-text-color-regular);
  white-space: pre-wrap;
  word-break: break-word;
}
.check-detail.is-fail { color: var(--el-color-danger); }

.notify-preview {
  background: var(--el-fill-color-light);
  border: 1px solid var(--smc-border);
  padding: 12px 14px;
  border-radius: var(--smc-radius-sm);
  font-family: inherit;
  font-size: 13px;
  line-height: 1.8;
  color: var(--el-text-color-regular);
  white-space: pre-wrap;
  margin: 0;
}
</style>

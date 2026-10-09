<template>
  <div class="comment-tree">
    <div class="comment-tree-bar">
      <span class="comment-tree-count">
        共 <b>{{ total }}</b> 条一级评论
        <span v-if="comments.length" class="muted">· 已加载 {{ comments.length }}</span>
        <InfoTip content="窗口内滚动到底自动加载更多" />
      </span>
    </div>

    <!-- 固定高度的评论窗口：内部滚动，不把外层表格撑长 -->
    <div
      v-infinite-scroll="loadMore"
      class="comment-tree-window"
      :style="{ height: `${height}px` }"
      :infinite-scroll-disabled="loading || !hasMore"
      :infinite-scroll-distance="60"
      :infinite-scroll-immediate="false"
    >
      <div v-if="loading && !comments.length" style="padding: 16px">
        <el-skeleton :rows="4" animated />
      </div>

      <el-empty
        v-else-if="!comments.length"
        description="这条内容下还没有采集到评论" :image-size="60"
      />

      <template v-else>
        <CommentNode
          v-for="comment in comments" :key="comment.comment_id"
          :comment="comment" :channel="channel" :work-id="workId" :max-level="maxLevel"
        />

        <div v-if="loading" class="comment-tree-foot">
          <el-icon class="is-loading"><Loading /></el-icon> 加载中…
        </div>
        <div v-else-if="hasMore" class="comment-tree-foot">
          <el-button link type="primary" @click="loadMore">
            继续加载（还有 {{ total - comments.length }} 条）
          </el-button>
        </div>
        <div v-else class="comment-tree-foot muted">已加载全部 {{ total }} 条一级评论</div>
      </template>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { Loading } from '@element-plus/icons-vue'
import { dataApi, type Comment } from '../api'
import CommentNode from './CommentNode.vue'

const props = withDefaults(defineProps<{
  channel: string
  workId: string
  scenicId?: string
  maxLevel?: number
  /** 评论窗口固定高度（px），超出部分窗口内滚动 */
  height?: number
}>(), { maxLevel: 5, height: 380 })

const comments = ref<Comment[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = 20
const loading = ref(false)

const hasMore = computed(() => comments.value.length < total.value)

async function load(reset = false) {
  if (reset) {
    page.value = 1
    comments.value = []
  }
  loading.value = true
  try {
    const result = await dataApi.comments({
      channel: props.channel,
      work_id: props.workId,
      scenic_id: props.scenicId || undefined,
      page: page.value,
      page_size: pageSize,
    })
    comments.value = page.value === 1 ? result.items : [...comments.value, ...result.items]
    total.value = result.total
  } finally {
    loading.value = false
  }
}

function loadMore() {
  if (loading.value || !hasMore.value) return
  page.value += 1
  load()
}

onMounted(() => load(true))
defineExpose({ reload: () => load(true) })
</script>

<style scoped>
.comment-tree-bar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  font-size: 12px;
  color: var(--el-text-color-regular);
  padding: 0 2px 8px;
}

.comment-tree-count {
  display: inline-flex;
  align-items: center;
  gap: 4px;
}

.comment-tree-count b {
  font-weight: 600;
  color: var(--smc-text);
}

.comment-tree-window {
  overflow-y: auto;
  overflow-x: hidden;
  border: 1px solid var(--smc-border);
  border-radius: var(--smc-radius-sm);
  background: var(--smc-card-bg);
  padding: 0 14px;
}

.comment-tree-foot {
  text-align: center;
  padding: 10px 0;
  font-size: 12px;
  color: var(--smc-text-secondary);
}
</style>

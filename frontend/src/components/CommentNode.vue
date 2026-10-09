<template>
  <div class="comment-node">
    <div class="comment-meta">
      <b style="color: #303133">{{ comment.commenter_name || comment.commenter_id || '匿名' }}</b>
      <span v-if="comment.location">{{ comment.location }}</span>
      <span>{{ formatTime(comment.publish_time) }}</span>
      <span v-if="comment.likes">👍 {{ formatCount(comment.likes) }}</span>
      <el-tag size="small" type="info" effect="plain">{{ levelLabel }}</el-tag>
      <el-tag v-if="replyType" size="small" type="warning" effect="plain">{{ replyType }}</el-tag>
    </div>

    <div class="comment-content">{{ comment.content || '（无内容）' }}</div>

    <MediaGallery
      :image-list="comment.image_list" :video-list="comment.video_list" :size="72"
    />

    <!-- 展开下一级 -->
    <div v-if="canExpand">
      <el-button link type="primary" size="small" :loading="loading" @click="toggle">
        {{ expanded ? '收起回复' : `展开 ${comment.sub_comment_count} 条回复` }}
        <el-icon style="margin-left: 2px">
          <ArrowUp v-if="expanded" /><ArrowDown v-else />
        </el-icon>
      </el-button>
    </div>

    <div
      v-if="expanded && children.length"
      class="comment-children" :class="{ 'is-scrollable': children.length > 8 }"
    >
      <CommentNode
        v-for="child in children" :key="child.comment_id"
        :comment="child" :channel="channel" :work-id="workId" :max-level="maxLevel"
      />
      <div v-if="childrenHasMore" style="padding: 6px 0">
        <el-button link type="primary" size="small" :loading="loading" @click="loadChildren">
          还有 {{ childrenTotal - children.length }} 条，继续加载
        </el-button>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { ArrowDown, ArrowUp } from '@element-plus/icons-vue'
import { dataApi, type Comment } from '../api'
import { formatCount, formatTime, parseJsonObject } from '../constants'
import MediaGallery from './MediaGallery.vue'

const props = withDefaults(defineProps<{
  comment: Comment
  channel: string
  workId: string
  maxLevel?: number
}>(), { maxLevel: 5 })

const expanded = ref(false)
const loading = ref(false)
const children = ref<Comment[]>([])
const childrenTotal = ref(0)
const childrenPage = ref(1)
const childPageSize = 20

const currentLevel = computed(() => {
  const match = /level_(\d+)/.exec(props.comment.comment_level || 'level_1')
  return match ? Number(match[1]) : 1
})

const levelLabel = computed(() => `${currentLevel.value} 级`)

const replyType = computed(() => {
  const extra = parseJsonObject(props.comment.extra_content)
  if (extra.reply_type === 'cs') return '商家回复'
  if (extra.reply_type === 'sub') return '追评'
  return ''
})

// 有子评论、且还没到最深层级时才允许展开
const canExpand = computed(
  () => (props.comment.sub_comment_count || 0) > 0 && currentLevel.value < props.maxLevel,
)

const childrenHasMore = computed(() => children.value.length < childrenTotal.value)

async function loadChildren() {
  loading.value = true
  try {
    const result = await dataApi.comments({
      channel: props.channel,
      work_id: props.workId,
      parent_id: props.comment.comment_id,
      page: childrenPage.value,
      page_size: childPageSize,
    })
    children.value = childrenPage.value === 1
      ? result.items
      : [...children.value, ...result.items]
    childrenTotal.value = result.total
    childrenPage.value += 1
  } finally {
    loading.value = false
  }
}

async function toggle() {
  if (expanded.value) {
    expanded.value = false
    return
  }
  expanded.value = true
  if (!children.value.length) {
    childrenPage.value = 1
    await loadChildren()
  }
}
</script>

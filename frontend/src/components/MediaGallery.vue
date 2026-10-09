<template>
  <div v-if="images.length || videos.length" class="media-gallery">
    <!-- 视频 -->
    <div
      v-for="(item, i) in videoTiles" :key="`v-${i}`"
      class="media-item media-video" :style="sizeStyle"
      role="button" :title="'播放视频'" @click="playVideo(item.url)"
    >
      <!--
        这里**不直接渲染 <video> 当缩略图**。各平台的视频 CDN 基本都校验
        Referer，网页里直接引用会加载失败，结果是一个黑色/空白的框——
        用户看到的就是"视频没点开的时候显示不正常"。
        有封面就用封面，没有就画一个规规矩矩的占位块，两种都不会翻车。
      -->
      <img
        v-if="item.poster && !failedPosters.has(item.poster)"
        :src="proxiedImage(item.poster)" alt="" referrerpolicy="no-referrer"
        @error="failedPosters.add(item.poster)"
      />
      <div v-else class="media-placeholder">
        <el-icon :size="22"><VideoCamera /></el-icon>
      </div>

      <div class="media-play"><el-icon :size="26"><VideoPlay /></el-icon></div>
      <span class="media-badge">视频</span>
    </div>

    <!-- 图片：点开走 el-image 自带的大图预览，可左右翻页 -->
    <el-image
      v-for="(url, i) in visibleImages" :key="`i-${i}`"
      class="media-item" :style="sizeStyle"
      :src="proxiedImage(url)" :preview-src-list="previewList" :initial-index="i"
      fit="cover" preview-teleported hide-on-click-modal loading="lazy"
      referrerpolicy="no-referrer"
    >
      <template #placeholder>
        <div class="media-placeholder"><el-icon :size="20"><Picture /></el-icon></div>
      </template>
      <template #error>
        <div class="media-placeholder media-broken">
          <el-icon :size="20"><PictureFilled /></el-icon>
          <span>加载失败</span>
        </div>
      </template>
    </el-image>

    <!--
      折叠掉多出来的图。小红书一条笔记常有 9~18 张图，一页 20 条作品
      就是 200 多张——每张都要走后端代理回源，首屏能卡好几秒。
      默认只画前几张，点一下再展开剩下的。
    -->
    <div
      v-if="hiddenCount > 0" class="media-item media-more" :style="sizeStyle"
      role="button" :title="`还有 ${hiddenCount} 张`" @click="expanded = true"
    >
      +{{ hiddenCount }}
    </div>

    <el-dialog
      v-model="playerVisible" :width="760" align-center destroy-on-close
      append-to-body @close="onPlayerClosed"
    >
      <template #header><span>视频播放</span></template>

      <video
        v-if="playingUrl && !playFailed" :src="playingUrl" controls autoplay
        style="width: 100%; max-height: 68vh; background: #000; border-radius: 4px"
        @error="playFailed = true"
      />
      <el-alert
        v-else-if="playFailed" type="warning" :closable="false" show-icon
        title="这个视频在网页里播不了"
        description="平台的视频地址通常校验来源，直接在网页里引用会被拒。用下面的链接在新标签页打开即可。"
      />

      <template #footer>
        <el-link v-if="playingUrl" type="primary" :href="playingUrl" target="_blank">
          在新窗口打开原始地址
        </el-link>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { computed, reactive, ref } from 'vue'
import { Picture, PictureFilled, VideoCamera, VideoPlay } from '@element-plus/icons-vue'
import { parseJsonList, proxiedImage } from '../constants'

const props = withDefaults(defineProps<{
  /** 后端存的 JSON 数组字符串 */
  imageList?: string | null
  videoList?: string | null
  /** 缩略图边长（px） */
  size?: number
}>(), { size: 84 })

const VIDEO_EXT = /\.(mp4|m3u8|mov|webm|flv|avi|mkv)(\?|$)/i

const allImages = computed(() => parseJsonList(props.imageList))
const allVideos = computed(() => parseJsonList(props.videoList))

/**
 * 有的平台把封面图也塞进 video_list（同程的 dpVideoInfo 就是），
 * 按扩展名分开：不是视频扩展名的当封面用。
 */
const videos = computed(() => allVideos.value.filter((url) => VIDEO_EXT.test(url)))
const coversInVideoList = computed(() => allVideos.value.filter((url) => !VIDEO_EXT.test(url)))

/**
 * 给每个视频配一张封面。优先用 video_list 里混进来的封面图，
 * 其次借用作品自己的第一张图——作品是视频时，image_list 里通常就是封面。
 */
const videoTiles = computed(() =>
  videos.value.map((url, index) => ({
    url,
    poster: coversInVideoList.value[index] || coversInVideoList.value[0] || allImages.value[0] || '',
  })),
)

/** 借去当封面的那张图不再单独作为图片显示，免得重复 */
const images = computed(() => {
  const borrowed = videoTiles.value.some((t) => t.poster && t.poster === allImages.value[0])
  return borrowed ? allImages.value.slice(1) : allImages.value
})

/**
 * 首屏最多画这么多张缩略图，其余折叠成一个"+N"。
 * 这是个纯性能取舍：每张缩略图都是一次 /api/media/image 代理请求。
 */
const PREVIEW_LIMIT = 6
const expanded = ref(false)
const visibleImages = computed(() =>
  expanded.value ? images.value : images.value.slice(0, PREVIEW_LIMIT),
)
const hiddenCount = computed(() => images.value.length - visibleImages.value.length)

/** 大图预览也要走代理，否则点开还是一片空白。
 *  预览列表给的是**全部**图片：折叠的只是缩略图，点开大图仍然能翻完。 */
const previewList = computed(() => images.value.map((url) => proxiedImage(url)))

const sizeStyle = computed(() => ({
  width: `${props.size}px`,
  height: `${props.size}px`,
}))

const failedPosters = reactive(new Set<string>())
const playerVisible = ref(false)
const playingUrl = ref('')
const playFailed = ref(false)

function playVideo(url: string) {
  playingUrl.value = url
  playFailed.value = false
  playerVisible.value = true
}

function onPlayerClosed() {
  playingUrl.value = ''
  playFailed.value = false
}
</script>

<style scoped>
.media-gallery {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
  margin: 8px 0;
}

.media-item {
  border-radius: 6px;
  overflow: hidden;
  background: #f5f7fa;
  border: 1px solid #ebeef5;
  flex: 0 0 auto;
}

.media-video {
  position: relative;
  cursor: pointer;
}

/* 折叠起来的"+N"块 */
.media-more {
  display: flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  color: #909399;
  font-size: 13px;
  user-select: none;
}

.media-more:hover {
  color: #409eff;
  border-color: #c6e2ff;
}

.media-video img {
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
}

.media-placeholder {
  width: 100%;
  height: 100%;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 3px;
  color: #b1b3b8;
  background: #f0f2f5;
  font-size: 11px;
}

.media-broken {
  color: #c0c4cc;
}

.media-play {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  color: #fff;
  background: rgba(0, 0, 0, 0.3);
  transition: background 0.2s;
}

.media-video:hover .media-play {
  background: rgba(0, 0, 0, 0.5);
}

.media-badge {
  position: absolute;
  left: 4px;
  bottom: 4px;
  padding: 0 5px;
  font-size: 11px;
  line-height: 17px;
  color: #fff;
  background: rgba(0, 0, 0, 0.6);
  border-radius: 3px;
}
</style>

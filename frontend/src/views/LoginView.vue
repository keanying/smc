<!--
  CloudSense（云听）· Copyright (c) 2026 keanying
  SPDX-License-Identifier: LicenseRef-SMC-NCLL-1.0
  按《SMC 非商业学习许可证 1.0》授权：仅限学习与研究，禁止任何商业用途，
  不得用于大规模抓取、绕过平台防护或非法获取个人信息。
  首次运行即表示已阅读并同意根目录 LICENSE，使用风险与法律责任由使用者自行承担。
-->

<template>
  <div class="login-page">
    <div class="login-card">
      <div class="brand">
        <img class="brand-img" src="/logo.png" alt="CloudSense 云听" />
        <div>
          <div class="brand-title">CloudSense</div>
          <div class="brand-sub">云听社媒舆情分析</div>
        </div>
      </div>

      <template v-if="needsSetup">
        <h2 class="card-title">创建管理员</h2>
        <p class="card-desc">第一次使用，先创建一个管理员账号。之后其他人的账号由管理员在「系统设置 → 用户管理」里添加。</p>
        <el-form ref="setupRef" :model="setupForm" :rules="setupRules" label-position="top"
                 @submit.prevent="submitSetup">
          <el-form-item label="用户名" prop="username">
            <el-input v-model="setupForm.username" placeholder="登录用，如 admin" autocomplete="username" />
          </el-form-item>
          <el-form-item label="显示名" prop="display_name">
            <el-input v-model="setupForm.display_name" placeholder="复核人等处显示，可不填" />
          </el-form-item>
          <el-form-item label="密码" prop="password">
            <el-input v-model="setupForm.password" type="password" show-password
                      placeholder="至少 8 位，不能是纯数字" autocomplete="new-password" />
          </el-form-item>
          <el-form-item label="再输一次" prop="confirm">
            <el-input v-model="setupForm.confirm" type="password" show-password autocomplete="new-password"
                      @keyup.enter="submitSetup" />
          </el-form-item>
          <el-button type="primary" class="submit" :loading="submitting" @click="submitSetup">
            创建并登录
          </el-button>
        </el-form>
      </template>

      <template v-else>
        <h2 class="card-title">登录</h2>
        <el-form ref="loginRef" :model="loginForm" :rules="loginRules" label-position="top"
                 @submit.prevent="submitLogin">
          <el-form-item label="用户名" prop="username">
            <el-input v-model="loginForm.username" autocomplete="username" autofocus />
          </el-form-item>
          <el-form-item label="密码" prop="password">
            <el-input v-model="loginForm.password" type="password" show-password autocomplete="current-password"
                      @keyup.enter="submitLogin" />
          </el-form-item>
          <el-button type="primary" class="submit" :loading="submitting" @click="submitLogin">登录</el-button>
        </el-form>
        <p class="foot muted">忘了密码请找管理员重置。</p>
      </template>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, type FormInstance, type FormRules } from 'element-plus'
import { authApi } from '../api'
import { authStatus, currentUser, loadAuth, setLoggedIn } from '../composables/auth'

const route = useRoute()
const router = useRouter()
const submitting = ref(false)
const loginRef = ref<FormInstance>()
const setupRef = ref<FormInstance>()
const loginForm = reactive({ username: '', password: '' })
const setupForm = reactive({ username: 'admin', display_name: '', password: '', confirm: '' })

const needsSetup = computed(() => !!authStatus.value?.needs_setup)

const loginRules: FormRules = {
  username: [{ required: true, message: '填用户名', trigger: 'blur' }],
  password: [{ required: true, message: '填密码', trigger: 'blur' }],
}
const setupRules: FormRules = {
  username: [{ required: true, message: '填用户名', trigger: 'blur' }],
  password: [
    { required: true, message: '填密码', trigger: 'blur' },
    { min: 8, message: '密码至少 8 位', trigger: 'blur' },
  ],
  confirm: [{
    trigger: 'blur',
    validator: (_r, v, cb) => (v === setupForm.password ? cb() : cb(new Error('两次输入的密码不一样'))),
  }],
}

function goBack() {
  const target = typeof route.query.redirect === 'string' ? route.query.redirect : '/'
  router.replace(target.startsWith('/login') ? '/' : target)
}

async function submitLogin() {
  if (!(await loginRef.value?.validate().catch(() => false))) return
  submitting.value = true
  try {
    setLoggedIn(await authApi.login(loginForm.username.trim(), loginForm.password))
    goBack()
  } finally {
    submitting.value = false
  }
}

async function submitSetup() {
  if (!(await setupRef.value?.validate().catch(() => false))) return
  submitting.value = true
  try {
    setLoggedIn(await authApi.setup({
      username: setupForm.username.trim(), password: setupForm.password,
      display_name: setupForm.display_name.trim(),
    }))
    ElMessage.success('管理员已创建')
    goBack()
  } finally {
    submitting.value = false
  }
}

onMounted(async () => {
  const status = await loadAuth(true)
  // 已经登录了 / 登录被关掉了：不用待在这页
  if (!status || !status.enabled || currentUser.value) goBack()
})
</script>

<style scoped>
.login-page {
  min-height: 100vh; display: flex; align-items: center; justify-content: center; padding: 24px 16px;
  background:
    radial-gradient(1200px 600px at 10% -10%, rgba(91, 108, 255, 0.35), transparent 60%),
    radial-gradient(900px 500px at 110% 110%, rgba(34, 197, 94, 0.18), transparent 60%),
    var(--smc-sidebar-bg, #141a2e);
}
.login-card {
  width: 100%; max-width: 400px; background: var(--el-bg-color); border-radius: 14px;
  padding: 32px 32px 24px; box-shadow: 0 20px 60px rgba(0, 0, 0, 0.35);
}
.brand { display: flex; align-items: center; gap: 12px; margin-bottom: 24px; }
/* LOGO 原样使用，只按高度等比缩放 */
.brand-img { height: 56px; width: auto; display: block; }
.brand-title { font-size: 18px; font-weight: 700; letter-spacing: 0.3px; }
.brand-sub { font-size: 12px; color: var(--smc-text-secondary); letter-spacing: 0.5px; }
.card-title { margin: 0 0 6px; font-size: 20px; }
.card-desc { margin: 0 0 16px; font-size: 13px; line-height: 1.7; color: var(--smc-text-secondary); }
.submit { width: 100%; margin-top: 4px; height: 38px; }
.foot { margin: 16px 0 0; font-size: 12px; text-align: center; }
</style>

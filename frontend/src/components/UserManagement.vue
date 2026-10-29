<!--
  CloudSense（云听）· Copyright (c) 2026 keanying
  SPDX-License-Identifier: LicenseRef-SMC-NCLL-1.0
  按《SMC 非商业学习许可证 1.0》授权：仅限学习与研究，禁止任何商业用途，
  不得用于大规模抓取、绕过平台防护或非法获取个人信息。
  首次运行即表示已阅读并同意根目录 LICENSE，使用风险与法律责任由使用者自行承担。
-->

<template>
  <!-- 系统设置 → 用户管理 -->
  <div>
    <div class="users-toolbar">
      <span class="muted intro">
        登录本系统的人（不是社媒平台账号）
        <InfoTip :width="360">
          <b>管理员</b>：全部功能，含用户管理、系统设置、集群管理；<br>
          <b>操作员</b>：日常操作（景区、任务、账号、复核、指标计算），不能管用户、改系统设置、管集群；<br>
          <b>只读</b>：只能看。<br>
          复核人、任务创建人记的是登录用户的显示名。
        </InfoTip>
      </span>
      <div class="header-actions">
        <el-button :icon="Refresh" @click="load">刷新</el-button>
        <el-button type="primary" :icon="Plus" @click="openAdd">添加用户</el-button>
      </div>
    </div>

    <el-table :data="users" v-loading="loading" class="data-table">
      <el-table-column label="用户" min-width="200">
        <template #default="{ row }">
          <div class="user-cell">
            <span class="avatar">{{ (row.display_name || row.username).slice(0, 1) }}</span>
            <div>
              <div class="name">
                {{ row.display_name || row.username }}
                <el-tag v-if="row.id === currentUser?.id" size="small" type="info">我</el-tag>
              </div>
              <div class="sub">{{ row.username }}</div>
            </div>
          </div>
        </template>
      </el-table-column>
      <el-table-column label="角色" width="150">
        <template #default="{ row }">
          <el-select :model-value="row.role" size="small" style="width: 110px"
                     @change="(v: any) => changeRole(row, v)">
            <el-option v-for="(label, value) in ROLES" :key="value" :label="label" :value="value" />
          </el-select>
        </template>
      </el-table-column>
      <el-table-column label="状态" width="150">
        <template #default="{ row }">
          <el-tag v-if="!row.enabled" type="info" size="small">已停用</el-tag>
          <el-tooltip v-else-if="isLocked(row)" :content="`密码输错太多次，锁到 ${formatTime(row.locked_until)}`">
            <el-tag type="danger" size="small">已锁定</el-tag>
          </el-tooltip>
          <el-tag v-else type="success" size="small">正常</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="最近登录" width="220">
        <template #default="{ row }">
          <template v-if="row.last_login_at">
            {{ formatTime(row.last_login_at) }}
            <div class="sub">{{ row.last_login_ip }}</div>
          </template>
          <span v-else class="muted">从没登录过</span>
        </template>
      </el-table-column>
      <el-table-column label="创建时间" width="170">
        <template #default="{ row }">{{ formatTime(row.create_time) }}</template>
      </el-table-column>
      <el-table-column label="启用" width="72" align="center">
        <template #default="{ row }">
          <el-switch :model-value="!!row.enabled" size="small" :disabled="row.id === currentUser?.id"
                     @change="(v: any) => toggle(row, !!v)" />
        </template>
      </el-table-column>
      <el-table-column label="操作" width="150" align="center" fixed="right">
        <template #default="{ row }">
          <div class="row-actions">
            <IconAction icon="edit" tip="改显示名" @click="openEdit(row)" />
            <IconAction icon="publish" tip="重置密码" @click="openReset(row)" />
            <IconAction v-if="isLocked(row)" icon="online" tip="解锁" @click="unlock(row)" />
            <el-popconfirm v-if="row.id !== currentUser?.id" :title="`删除用户 ${row.username}？`"
                           width="220" @confirm="remove(row)">
              <template #reference>
                <span class="pop-ref"><IconAction icon="delete" tip="删除" /></span>
              </template>
            </el-popconfirm>
          </div>
        </template>
      </el-table-column>
    </el-table>

    <!-- 添加 / 改显示名 -->
    <el-dialog v-model="formVisible" :title="editing ? '改显示名' : '添加用户'" width="440px">
      <el-form ref="formRef" :model="form" :rules="rules" label-width="80px">
        <el-form-item label="用户名" prop="username">
          <el-input v-model="form.username" :disabled="!!editing" placeholder="登录用，如 zhangsan" />
        </el-form-item>
        <el-form-item label="显示名">
          <el-input v-model="form.display_name" placeholder="复核人等处显示，如 张三" />
        </el-form-item>
        <template v-if="!editing">
          <el-form-item label="角色">
            <el-radio-group v-model="form.role">
              <el-radio-button v-for="(label, value) in ROLES" :key="value" :value="value">{{ label }}</el-radio-button>
            </el-radio-group>
          </el-form-item>
          <el-form-item label="初始密码" prop="password">
            <el-input v-model="form.password" type="password" show-password autocomplete="new-password"
                      placeholder="至少 8 位，不能是纯数字" />
          </el-form-item>
        </template>
      </el-form>
      <template #footer>
        <el-button @click="formVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="save">保存</el-button>
      </template>
    </el-dialog>

    <!-- 重置密码 -->
    <el-dialog v-model="resetVisible" :title="`重置 ${resetTarget?.username || ''} 的密码`" width="420px">
      <el-input v-model="resetPassword" type="password" show-password autocomplete="new-password"
                placeholder="新密码：至少 8 位，不能是纯数字" />
      <div class="muted hint">重置后他在所有地方的登录都会退出，用新密码重新登录。</div>
      <template #footer>
        <el-button @click="resetVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="doReset">重置</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { ElMessage, type FormInstance, type FormRules } from 'element-plus'
import { Plus, Refresh } from '@element-plus/icons-vue'
import { usersApi, type SysUser, type UserRole } from '../api'
import { formatTime } from '../constants'
import { currentUser, loadAuth } from '../composables/auth'

const ROLES: Record<UserRole, string> = { admin: '管理员', operator: '操作员', viewer: '只读' }

const users = ref<SysUser[]>([])
const loading = ref(false)
const saving = ref(false)
const formVisible = ref(false)
const editing = ref<SysUser | null>(null)
const formRef = ref<FormInstance>()
const form = reactive({ username: '', display_name: '', role: 'operator' as UserRole, password: '' })
const rules: FormRules = {
  username: [{ required: true, message: '填用户名', trigger: 'blur' }],
  password: [
    { required: true, message: '填初始密码', trigger: 'blur' },
    { min: 8, message: '至少 8 位', trigger: 'blur' },
  ],
}
const resetVisible = ref(false)
const resetTarget = ref<SysUser | null>(null)
const resetPassword = ref('')

function isLocked(row: SysUser): boolean {
  return !!row.locked_until && new Date(String(row.locked_until).replace(' ', 'T')) > new Date()
}

async function load() {
  loading.value = true
  try {
    users.value = await usersApi.list()
  } finally {
    loading.value = false
  }
}

function openAdd() {
  editing.value = null
  Object.assign(form, { username: '', display_name: '', role: 'operator', password: '' })
  formVisible.value = true
  formRef.value?.clearValidate()
}

function openEdit(row: SysUser) {
  editing.value = row
  Object.assign(form, { username: row.username, display_name: row.display_name, role: row.role, password: '' })
  formVisible.value = true
}

async function save() {
  if (editing.value) {
    saving.value = true
    try {
      await usersApi.update(editing.value.id, { display_name: form.display_name })
      ElMessage.success('已保存')
      formVisible.value = false
      await load()
      if (editing.value.id === currentUser.value?.id) await loadAuth(true)
    } finally {
      saving.value = false
    }
    return
  }
  if (!(await formRef.value?.validate().catch(() => false))) return
  saving.value = true
  try {
    await usersApi.create({
      username: form.username.trim(), display_name: form.display_name.trim(),
      role: form.role, password: form.password,
    })
    ElMessage.success(`已添加 ${form.username}，把初始密码告诉他`)
    formVisible.value = false
    await load()
  } finally {
    saving.value = false
  }
}

async function changeRole(row: SysUser, role: UserRole) {
  try {
    await usersApi.update(row.id, { role })
    ElMessage.success(`${row.username} 改成了${ROLES[role]}`)
  } finally {
    await load()
  }
}

async function toggle(row: SysUser, enabled: boolean) {
  try {
    await usersApi.update(row.id, { enabled })
    ElMessage.success(enabled ? '已启用' : '已停用，他的登录都已退出')
  } finally {
    await load()
  }
}

function openReset(row: SysUser) {
  resetTarget.value = row
  resetPassword.value = ''
  resetVisible.value = true
}

async function doReset() {
  if (!resetTarget.value) return
  saving.value = true
  try {
    await usersApi.resetPassword(resetTarget.value.id, resetPassword.value)
    ElMessage.success('密码已重置')
    resetVisible.value = false
    await load()
  } finally {
    saving.value = false
  }
}

async function unlock(row: SysUser) {
  await usersApi.unlock(row.id)
  ElMessage.success('已解锁')
  await load()
}

async function remove(row: SysUser) {
  await usersApi.remove(row.id)
  ElMessage.success('已删除')
  await load()
}

onMounted(load)
</script>

<style scoped>
.users-toolbar { display: flex; align-items: center; justify-content: space-between; margin-bottom: 12px; }
.intro { display: inline-flex; align-items: center; gap: 4px; font-size: 13px; }
.header-actions { display: flex; align-items: center; gap: 12px; }
.user-cell { display: flex; align-items: center; gap: 10px; }
.avatar {
  width: 32px; height: 32px; border-radius: 50%; display: inline-flex; align-items: center; justify-content: center;
  background: var(--el-color-primary-light-8); color: var(--el-color-primary); font-weight: 600; flex: 0 0 auto;
}
.name { display: flex; align-items: center; gap: 6px; font-weight: 500; }
.sub { font-size: 12px; color: var(--smc-text-secondary); }
.row-actions { display: inline-flex; align-items: center; gap: 8px; }
.pop-ref { display: inline-flex; }
.hint { font-size: 12px; margin-top: 8px; }
</style>

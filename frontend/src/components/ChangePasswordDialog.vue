<!--
  CloudSense（云听）· Copyright (c) 2026 keanying
  SPDX-License-Identifier: LicenseRef-SMC-NCLL-1.0
  按《SMC 非商业学习许可证 1.0》授权：仅限学习与研究，禁止任何商业用途，
  不得用于大规模抓取、绕过平台防护或非法获取个人信息。
  首次运行即表示已阅读并同意根目录 LICENSE，使用风险与法律责任由使用者自行承担。
-->

<template>
  <el-dialog v-model="visible" title="修改密码" width="420px" @closed="reset">
    <el-form ref="formRef" :model="form" :rules="rules" label-width="90px">
      <el-form-item label="原密码" prop="old">
        <el-input v-model="form.old" type="password" show-password autocomplete="current-password" />
      </el-form-item>
      <el-form-item label="新密码" prop="password">
        <el-input v-model="form.password" type="password" show-password autocomplete="new-password"
                  placeholder="至少 8 位，不能是纯数字" />
      </el-form-item>
      <el-form-item label="再输一次" prop="confirm">
        <el-input v-model="form.confirm" type="password" show-password autocomplete="new-password"
                  @keyup.enter="submit" />
      </el-form-item>
    </el-form>
    <div class="muted hint">改完之后，你在其他电脑、浏览器上的登录都会退出。</div>
    <template #footer>
      <el-button @click="visible = false">取消</el-button>
      <el-button type="primary" :loading="saving" @click="submit">保存</el-button>
    </template>
  </el-dialog>
</template>

<script setup lang="ts">
import { reactive, ref } from 'vue'
import { ElMessage, type FormInstance, type FormRules } from 'element-plus'
import { authApi } from '../api'

const visible = defineModel<boolean>({ default: false })
const formRef = ref<FormInstance>()
const saving = ref(false)
const form = reactive({ old: '', password: '', confirm: '' })
const rules: FormRules = {
  old: [{ required: true, message: '填原密码', trigger: 'blur' }],
  password: [
    { required: true, message: '填新密码', trigger: 'blur' },
    { min: 8, message: '至少 8 位', trigger: 'blur' },
  ],
  confirm: [{
    trigger: 'blur',
    validator: (_r, v, cb) => (v === form.password ? cb() : cb(new Error('两次输入的密码不一样'))),
  }],
}

function reset() {
  Object.assign(form, { old: '', password: '', confirm: '' })
  formRef.value?.clearValidate()
}

async function submit() {
  if (!(await formRef.value?.validate().catch(() => false))) return
  saving.value = true
  try {
    await authApi.changePassword(form.old, form.password)
    ElMessage.success('密码已修改')
    visible.value = false
  } finally {
    saving.value = false
  }
}
</script>

<style scoped>
.hint { font-size: 12px; padding-left: 90px; }
</style>

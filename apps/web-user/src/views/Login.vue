<script setup lang="ts">
import { ElMessage } from 'element-plus'
import { reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useAuth } from '../auth'

const router = useRouter()
const auth = useAuth()
const form = reactive({ email: '', password: '' })
const loading = ref(false)

// 管理端地址:生产环境用 VITE_ADMIN_URL 配置;开发环境默认同主机的 5174 端口
const adminUrl: string =
  import.meta.env.VITE_ADMIN_URL ??
  (import.meta.env.DEV ? `${location.protocol}//${location.hostname}:5174` : '')

async function submit() {
  loading.value = true
  try {
    await auth.login(form.email.trim(), form.password)
    await router.replace({ name: 'home' })
  } catch (e) {
    ElMessage.error((e as Error).message)
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="wrap">
    <el-card class="card" shadow="never">
      <h2>车研数合</h2>
      <p class="sub">汽车数据跨境合规平台 · 企业登录</p>
      <el-form label-position="top" @submit.prevent="submit">
        <el-form-item label="邮箱">
          <el-input v-model="form.email" autocomplete="username" />
        </el-form-item>
        <el-form-item label="密码">
          <el-input
            v-model="form.password"
            type="password"
            show-password
            autocomplete="current-password"
          />
        </el-form-item>
        <el-button type="primary" native-type="submit" :loading="loading" style="width: 100%">
          登录
        </el-button>
      </el-form>
      <p class="hint">
        平台管理员、复核员请使用管理端登录<template v-if="adminUrl">:<a :href="adminUrl">前往管理端</a></template>
      </p>
    </el-card>
  </div>
</template>

<style scoped>
.wrap {
  height: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
  background: #f5f7fa;
}
.card {
  width: 380px;
}
h2 {
  margin: 0 0 4px;
}
.hint {
  margin: 16px 0 0;
  font-size: 12px;
  color: #909399;
  text-align: center;
}
.sub {
  margin: 0 0 20px;
  color: #909399;
  font-size: 13px;
}
</style>

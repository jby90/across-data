<script setup lang="ts">
import { ElMessage } from 'element-plus'
import { reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useAuth } from '../auth'
import AppIcon from '../components/AppIcon.vue'

const router = useRouter()
const auth = useAuth()
const form = reactive({ email: '', password: '' })
const loading = ref(false)

// 用户端地址:生产环境用 VITE_USER_URL 配置;开发环境默认同主机的 5173 端口
const userUrl: string =
  import.meta.env.VITE_USER_URL ??
  (import.meta.env.DEV ? `${location.protocol}//${location.hostname}:5173` : '')

async function submit() {
  if (loading.value) return
  if (!form.email.trim() || !form.password) {
    ElMessage.warning('请输入邮箱和密码')
    return
  }
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
  <div class="login-page">
    <section class="login-story">
      <div class="app-brand">
        <span class="brand-mark"
          ><svg viewBox="0 0 28 28" fill="none" aria-hidden="true">
            <path
              d="M5 9l9-5 9 5v10l-9 5-9-5V9z M5 9l9 5 9-5 M14 14v10"
              stroke="currentColor"
              stroke-width="1.8"
              stroke-linejoin="round"
            /></svg
        ></span>
        <div>
          <div class="brand-name">车研数合</div>
          <div class="brand-caption">DATA COMPLIANCE</div>
        </div>
      </div>
      <div class="login-story-copy">
        <div class="eyebrow">TRUST ACROSS BORDERS</div>
        <h1>让数据流动，<br />让决策有据。</h1>
        <p>连接全球法规、业务场景与合规行动，<br />让每一次跨境决策，都有清晰的依据。</p>
      </div>
      <svg class="login-art" viewBox="0 0 600 600" fill="none" aria-hidden="true">
        <circle cx="300" cy="300" r="270" stroke="currentColor" />
        <circle cx="300" cy="300" r="220" stroke="currentColor" />
        <ellipse cx="300" cy="300" rx="120" ry="270" stroke="currentColor" />
        <ellipse cx="300" cy="300" rx="270" ry="100" stroke="currentColor" />
        <path d="M30 300h540 M300 30v540 M105 105l390 390 M105 495l390-390" stroke="currentColor" />
        <circle cx="300" cy="30" r="8" fill="currentColor" />
        <circle cx="570" cy="300" r="8" fill="currentColor" />
      </svg>
      <div class="login-story-footer">法规知识 · 合规评估 · 报告交付</div>
    </section>
    <main class="login-main">
      <div class="login-form">
        <p class="demo-notice">演示环境，数据仅供参考</p>
        <div class="eyebrow">ADMIN WORKSPACE</div>
        <h2>管理工作，从这里开始</h2>
        <p class="login-description">登录以管理法规资料、内容复核与平台运行。</p>
        <el-form label-position="top" @submit.prevent="submit">
          <el-form-item label="邮箱"
            ><el-input
              v-model="form.email"
              type="email"
              autocomplete="username"
              placeholder="请输入登录邮箱"
              aria-label="邮箱"
              ><template #prefix><AppIcon name="mail" /></template></el-input
          ></el-form-item>
          <el-form-item label="密码"
            ><el-input
              v-model="form.password"
              type="password"
              show-password
              autocomplete="current-password"
              placeholder="请输入密码"
              aria-label="密码"
              ><template #prefix><AppIcon name="lock" /></template></el-input
          ></el-form-item>
          <el-button class="login-submit" type="primary" native-type="submit" :loading="loading"
            >登录工作台<AppIcon name="arrow" style="margin: 0 0 0 10px"
          /></el-button>
        </el-form>
        <p><RouterLink to="/licenses">开源许可声明</RouterLink></p>
        <div class="login-switch">
          企业用户？<template v-if="userUrl"
            ><a :href="userUrl">前往企业端<AppIcon name="arrow" /></a
          ></template>
        </div>
      </div>
    </main>
  </div>
</template>

<script setup lang="ts">
import {
  DOC_TYPE_LABEL,
  RUN_STATUS_LABEL,
  STRATEGY_LABEL,
  TERMS_LABEL,
  type RunOut,
  type SiteOut,
} from '@cyshuhe/shared'
import { ElMessage, ElMessageBox } from 'element-plus'
import { onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { api } from '../api'

const router = useRouter()
const sites = ref<SiteOut[]>([])
const country = ref('')
const loading = ref(false)

const fmt = (s: string | null) => (s ? new Date(s).toLocaleString('zh-CN') : '—')

async function load() {
  loading.value = true
  try {
    const q = country.value.trim()
      ? `?country_code=${encodeURIComponent(country.value.trim())}`
      : ''
    sites.value = await api.get<SiteOut[]>(`/admin/sites${q}`)
  } finally {
    loading.value = false
  }
}

function goDocuments(site: SiteOut) {
  router.push({
    name: 'documents',
    query: {
      site_id: site.id,
      publisher: site.publisher,
      country_code: site.country_code,
    },
  })
}

// ---- 新增站点 ----
const createDialog = ref(false)
const form = reactive({
  country_code: 'SG',
  publisher: '',
  language: 'en',
  document_type: 'GUIDANCE',
  entry_url: '',
  strategy: 'INDEX_PAGE',
  keywords: '',
  extensions: '.pdf',
  interval_hours: 168,
  terms_url: '',
})

const split = (s: string) =>
  s
    .split(/[,，\n]+/)
    .map((x) => x.trim())
    .filter(Boolean)

async function create() {
  try {
    await api.post('/admin/sites', {
      country_code: form.country_code.trim().toUpperCase(),
      publisher: form.publisher.trim(),
      language: form.language.trim(),
      document_type: form.document_type,
      entry_url: form.entry_url.trim(),
      strategy: form.strategy,
      interval_hours: form.interval_hours,
      terms_url: form.terms_url.trim() || null,
      config: {
        keywords: split(form.keywords),
        extensions: split(form.extensions),
      },
    })
    ElMessage.success('已新增。需先确认来源使用条款才能启用抓取')
    createDialog.value = false
    await load()
  } catch (e) {
    ElMessage.error((e as Error).message)
  }
}

// ---- 条款确认 ----
async function decideTerms(site: SiteOut, status: 'approved' | 'rejected') {
  const action = status === 'approved' ? '确认可以抓取' : '否决'
  try {
    const { value } = await ElMessageBox.prompt(
      `请写明依据(如:法务确认的邮件、条款链接、日期)。该记录会留痕。`,
      `${action}:${site.publisher}`,
      {
        inputPlaceholder: '依据说明',
        inputPattern: /\S+/,
        inputErrorMessage: '必须填写依据',
      },
    )
    await api.post(`/admin/sites/${site.id}/terms`, { status, note: value })
    ElMessage.success('已记录')
    await load()
  } catch (e) {
    if (e !== 'cancel') ElMessage.error((e as Error).message ?? String(e))
  }
}

async function toggle(site: SiteOut, enabled: boolean) {
  try {
    await api.patch(`/admin/sites/${site.id}`, { enabled })
  } catch (e) {
    ElMessage.error((e as Error).message)
  }
  await load()
}

async function runNow(site: SiteOut) {
  try {
    await api.post(`/admin/sites/${site.id}/run`)
    ElMessage.success('已提交,后台任务会尽快执行(需要 worker 在运行)')
    await load()
  } catch (e) {
    ElMessage.error((e as Error).message)
  }
}

// ---- 运行记录 ----
const runsDrawer = ref(false)
const runs = ref<RunOut[]>([])
const runsSite = ref<SiteOut | null>(null)
async function showRuns(site: SiteOut) {
  runsSite.value = site
  runs.value = await api.get<RunOut[]>(`/admin/sites/${site.id}/runs`)
  runsDrawer.value = true
}

onMounted(load)
</script>

<template>
  <div class="management-page">
    <div class="page-heading">
      <div>
        <h1>采集站点</h1>
        <p>连接官方来源，管理采集计划与运行记录。</p>
      </div>
      <div class="page-actions">
        <el-input
          v-model="country"
          placeholder="国家代码，例如 SG"
          aria-label="筛选国家"
          style="width: 180px"
          clearable
          @keyup.enter="load"
        />
        <el-button @click="load">查询</el-button>
        <el-button type="primary" @click="createDialog = true">新增站点</el-button>
      </div>
    </div>
    <el-alert
      type="info"
      :closable="false"
      style="margin-bottom: 12px"
      title="新增站点默认停用。每个站点代表一个官方发布渠道或目录入口,必须先确认使用条款才能启用定期抓取。"
    />

    <el-table :data="sites" v-loading="loading" border>
      <el-table-column prop="country_code" label="国家" width="65" />
      <el-table-column label="发布机构与说明" min-width="200">
        <template #default="{ row }">
          <div style="font-weight: 600">{{ row.publisher }}</div>
          <div v-if="row.terms_note" style="font-size: 12px; color: #909399; margin-top: 2px">
            {{ row.terms_note }}
          </div>
        </template>
      </el-table-column>
      <el-table-column label="入口地址与收录摘要" min-width="280">
        <template #default="{ row }">
          <div style="word-break: break-all; font-size: 12px">
            <a
              :href="row.entry_url"
              target="_blank"
              rel="noopener noreferrer"
              style="color: #409eff; text-decoration: none"
            >
              {{ row.entry_url }}
            </a>
          </div>
          <div
            v-if="row.sample_titles?.length"
            style="font-size: 12px; color: #606266; margin-top: 4px"
          >
            <div
              v-for="(t, i) in row.sample_titles.slice(0, 2)"
              :key="i"
              style="overflow: hidden; text-overflow: ellipsis; white-space: nowrap"
            >
              · {{ t }}
            </div>
            <span v-if="row.document_count > row.sample_titles.length" style="color: #909399">
              等共 {{ row.document_count }} 份法规原文
            </span>
          </div>
        </template>
      </el-table-column>
      <el-table-column label="收录文档" width="95" align="center">
        <template #default="{ row }">
          <el-button v-if="row.document_count > 0" link type="primary" @click="goDocuments(row)">
            {{ row.document_count }} 份
          </el-button>
          <span v-else style="color: #909399">0</span>
        </template>
      </el-table-column>
      <el-table-column label="类型" width="90">
        <template #default="{ row }">{{ DOC_TYPE_LABEL[row.document_type] }}</template>
      </el-table-column>
      <el-table-column label="发现方式" width="125">
        <template #default="{ row }">{{ STRATEGY_LABEL[row.strategy] ?? row.strategy }}</template>
      </el-table-column>
      <el-table-column label="使用条款" width="120">
        <template #default="{ row }">
          <el-tag
            :type="
              row.terms_status === 'approved'
                ? 'success'
                : row.terms_status === 'rejected'
                  ? 'danger'
                  : 'warning'
            "
          >
            {{ TERMS_LABEL[row.terms_status] }}
          </el-tag>
          <div class="muted" style="font-size: 12px; margin-top: 4px">
            {{
              row.terms_approval_scope === 'production'
                ? '生产范围确认'
                : row.terms_approval_scope === 'development'
                  ? '仅开发环境'
                  : '历史确认 · 生产须复核'
            }}
          </div>
        </template>
      </el-table-column>
      <el-table-column label="定期抓取" width="90" align="center">
        <template #default="{ row }">
          <el-switch
            :model-value="row.enabled"
            :disabled="row.terms_status !== 'approved'"
            @change="(v: string | number | boolean) => toggle(row, Boolean(v))"
          />
        </template>
      </el-table-column>
      <el-table-column label="上次结果" width="120">
        <template #default="{ row }">
          {{ row.last_status ? RUN_STATUS_LABEL[row.last_status] : '—' }}
          <span v-if="row.consecutive_failures" style="color: #f56c6c">
            (失败 {{ row.consecutive_failures }})
          </span>
        </template>
      </el-table-column>
      <el-table-column label="下次运行" width="155">
        <template #default="{ row }">{{ fmt(row.next_run_at) }}</template>
      </el-table-column>
      <el-table-column label="操作" width="180" fixed="right">
        <template #default="{ row }">
          <el-button link type="primary" @click="showRuns(row)">运行记录</el-button>
          <el-dropdown trigger="click"
            ><el-button text>更多</el-button
            ><template #dropdown
              ><el-dropdown-menu>
                <el-dropdown-item :disabled="!row.enabled" @click="runNow(row)"
                  >立即抓取</el-dropdown-item
                >
                <el-dropdown-item divided @click="decideTerms(row, 'approved')"
                  >确认来源使用条款</el-dropdown-item
                >
                <el-dropdown-item @click="decideTerms(row, 'rejected')"
                  >否决来源使用条款</el-dropdown-item
                >
              </el-dropdown-menu></template
            ></el-dropdown
          >
        </template>
      </el-table-column>
    </el-table>

    <el-dialog v-model="createDialog" title="新增采集站点" width="580px">
      <el-form label-width="110px">
        <el-form-item label="国家代码"
          ><el-input v-model="form.country_code" maxlength="2"
        /></el-form-item>
        <el-form-item label="发布机构"><el-input v-model="form.publisher" /></el-form-item>
        <el-form-item label="原文语言"
          ><el-input v-model="form.language" placeholder="en / ar / fr / zh"
        /></el-form-item>
        <el-form-item label="文件类型">
          <el-select v-model="form.document_type" style="width: 100%">
            <el-option
              v-for="(label, key) in DOC_TYPE_LABEL"
              :key="key"
              :label="label"
              :value="key"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="入口地址"
          ><el-input v-model="form.entry_url" placeholder="https://"
        /></el-form-item>
        <el-form-item label="发现方式">
          <el-radio-group v-model="form.strategy">
            <el-radio v-for="(label, key) in STRATEGY_LABEL" :key="key" :value="key">{{
              label
            }}</el-radio>
          </el-radio-group>
        </el-form-item>
        <el-form-item label="关键词"
          ><el-input v-model="form.keywords" placeholder="多个用逗号分隔,留空不过滤"
        /></el-form-item>
        <el-form-item label="文件扩展名"
          ><el-input v-model="form.extensions" placeholder=".pdf"
        /></el-form-item>
        <el-form-item label="抓取间隔(小时)"
          ><el-input-number v-model="form.interval_hours" :min="1" :max="2160"
        /></el-form-item>
        <el-form-item label="条款页面链接"
          ><el-input v-model="form.terms_url" placeholder="该站点的使用条款/版权声明页(选填)"
        /></el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="createDialog = false">取消</el-button>
        <el-button type="primary" @click="create">新增</el-button>
      </template>
    </el-dialog>

    <el-drawer v-model="runsDrawer" :title="`运行记录:${runsSite?.publisher ?? ''}`" size="720px">
      <el-table :data="runs" border>
        <el-table-column label="开始时间" width="170"
          ><template #default="{ row }">{{ fmt(row.started_at) }}</template></el-table-column
        >
        <el-table-column label="方式" width="70"
          ><template #default="{ row }">{{
            row.trigger === 'manual' ? '手动' : '定时'
          }}</template></el-table-column
        >
        <el-table-column label="结果" width="100"
          ><template #default="{ row }">{{
            RUN_STATUS_LABEL[row.status]
          }}</template></el-table-column
        >
        <el-table-column prop="links_found" label="发现" width="60" />
        <el-table-column prop="versions_new" label="新版本" width="70" />
        <el-table-column prop="errors" label="失败" width="60" />
        <el-table-column prop="message" label="说明" min-width="160" show-overflow-tooltip />
      </el-table>
    </el-drawer>
  </div>
</template>

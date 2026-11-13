<template>
  <section class="page" data-module="dashboard">
    <header class="page-head">
      <div>
        <h2>看板配置管理</h2>
        <p class="page-desc">配置数据看板页面与指标卡片。发布前逐卡确认引用口径版本已发布且计算粒度一致；指标下线后卡片保留原配置、显示失效来源并停止刷新。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记看板页面</button>
        <button class="btn" type="button" @click="exportRows">导出看板配置清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label class="filter-item">
        <span>看板编号</span>
        <input v-model="keyword" placeholder="按看板编号检索" />
      </label>
      <label class="filter-item">
        <span>状态</span>
        <select v-model="statusFilter">
          <option value="">全部</option>
          <option v-for="s in statuses" :key="s" :value="s">{{ s }}</option>
        </select>
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>卡片健康度</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td>
            <button class="link" type="button" @click="openDetail(Number(row.id))">
              {{ row['卡片总数'] }} 张 / 失效 {{ row['失效卡片数'] }} 张
            </button>
          </td>
          <td class="row-actions">
            <button
              v-for="action in allowedActions(row)"
              :key="action"
              class="link"
              type="button"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
            <span v-if="!allowedActions(row).length" class="muted-text">无可用动作</span>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 2" class="empty-state">暂无看板配置数据，可先登记看板页面</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条看板配置记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
      <span v-else-if="successMessage" class="success-text">{{ successMessage }}</span>
    </footer>

    <!-- 登记弹窗 -->
    <div v-if="creating" class="modal-mask" @click.self="creating = false">
      <div class="modal">
        <h3>登记看板页面</h3>
        <form class="modal-form" @submit.prevent="submitCreate">
          <label v-for="field in createFields" :key="field.prop" class="filter-item">
            <span>{{ field.label }}</span>
            <input v-model="createForm[field.prop]" :placeholder="field.hint" />
          </label>
          <p class="rule-hint">编号须为 DASH-四位数字；刷新间隔允许 30-86400 秒；可见范围限 {{ visibleScopes.join('/') }}。</p>
          <div class="modal-actions">
            <button class="btn" type="button" @click="creating = false">取消</button>
            <button class="btn primary" type="submit">保存</button>
          </div>
        </form>
      </div>
    </div>

    <!-- 详情抽屉：指标卡（含口径版本与失效原因）、发布校验、发布记录（审计） -->
    <div v-if="detail" class="drawer-mask" @click.self="detail = null">
      <aside class="drawer drawer-wide">
        <header class="drawer-head">
          <div>
            <h3>{{ detail['看板编号'] }} · {{ detail['看板名称'] }}</h3>
            <p class="page-desc">
              状态：{{ detail.status }} ｜ 版本号 revision：{{ detail.revision }} ｜
              发布时间：{{ detail.published_at ?? '未发布' }}
            </p>
          </div>
          <button class="btn" type="button" @click="detail = null">关闭</button>
        </header>

        <div v-if="detail.status === '看板评审中'" class="banner" :class="detail.can_publish ? 'banner-ok' : 'banner-block'">
          <template v-if="detail.can_publish">全部指标卡引用口径均已发布且粒度一致，可执行发布。</template>
          <template v-else>
            <strong>发布前校验未通过，看板将停在发布前：</strong>
            <ul class="blocker-list">
              <li v-for="blocker in detail.publish_blockers" :key="blocker">{{ blocker }}</li>
            </ul>
          </template>
        </div>

        <h4>指标卡（引用固定口径版本，历史引用不被最新草稿覆盖）</h4>
        <table class="data-table">
          <thead>
            <tr>
              <th>卡片</th><th>指标</th><th>引用版本</th><th>口径粒度</th><th>卡片粒度</th>
              <th>刷新</th><th>运行状态/失效来源</th><th>版本切换</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="card in detail.cards" :key="card.card_id">
              <td>{{ card.card_id }}</td>
              <td>{{ card.metric_code }}（{{ card.metric_status ?? '—' }}）</td>
              <td>{{ card.metric_version }}<span class="muted-text"> / {{ card.version_status }}</span></td>
              <td>{{ card['口径粒度'] ?? '—' }}</td>
              <td>{{ card.card_grain }}</td>
              <td>
                <span :class="card.refresh ? 'success-text' : 'error-text'">{{ card.refresh ? '刷新中' : '已停止' }}</span>
              </td>
              <td>
                <span v-if="card.valid" class="success-text">有效</span>
                <span v-else class="error-text">{{ card.reason }}</span>
              </td>
              <td>
                <div class="switch-row">
                  <input v-model="switchDraft[card.card_id].version" class="switch-input" placeholder="目标版本如 v2" />
                  <button class="btn" type="button" @click="switchVersion(card)">切换</button>
                </div>
              </td>
            </tr>
            <tr v-if="!detail.cards.length"><td colspan="8" class="empty-state">尚未挂载指标卡</td></tr>
          </tbody>
        </table>

        <h4>看板发布记录（审计）</h4>
        <table class="data-table">
          <thead>
            <tr><th>时间</th><th>动作</th><th>结果</th><th>说明</th></tr>
          </thead>
          <tbody>
            <tr v-for="audit in detail.audits" :key="audit.id">
              <td>{{ audit.created_at }}</td>
              <td>{{ audit.action }}</td>
              <td :class="audit.result === '成功' ? 'success-text' : 'error-text'">{{ audit.result }}</td>
              <td>{{ audit.detail }}</td>
            </tr>
            <tr v-if="!detail.audits.length"><td colspan="4" class="empty-state">暂无发布记录</td></tr>
          </tbody>
        </table>
      </aside>
    </div>
  </section>
</template>

<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'

import { postAction, request } from '@/api/client'

type Row = Record<string, string | number | null>
type CardView = {
  card_id: string
  metric_id: number
  metric_code: string
  metric_status: string | null
  metric_version: string
  version_status: string | null
  card_grain: string
  口径粒度?: string
  valid: boolean
  refresh: boolean
  reason: string | null
}
type AuditRow = { id: number; created_at: string; action: string; result: string; detail: string }
type Detail = {
  id: number
  status: string
  看板编号: string
  看板名称: string
  revision: number
  published_at: string | null
  cards: CardView[]
  publish_blockers: string[]
  can_publish: boolean
  audits: AuditRow[]
}

const ENDPOINT = '/api/dashboard'
const columns = ['看板编号', '看板名称', '包含指标', '布局模板', '刷新间隔', '可见范围', '所属业务线', '看板负责人', 'status']
const statuses = ['草稿', '看板评审中', '已发布', '已下线']
const visibleScopes = ['全员可见', '业务线内可见', '仅负责人可见']
const stats = ref([
  { label: '看板页面总数', value: 0 },
  { label: '已发布看板', value: 0 },
  { label: '失效卡片数', value: 0 },
])

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const successMessage = ref('')
const keyword = ref('')
const statusFilter = ref('')
const detail = ref<Detail | null>(null)
const creating = ref(false)
const switchDraft = reactive<Record<string, { version: string }>>({})
const createFields = [
  { prop: '看板编号', label: '看板编号', hint: 'DASH-0004' },
  { prop: '看板名称', label: '看板名称', hint: '不超过 50 字' },
  { prop: '布局模板', label: '布局模板', hint: '两行四列' },
  { prop: '刷新间隔', label: '刷新间隔(秒)', hint: '30-86400' },
  { prop: '可见范围', label: '可见范围', hint: visibleScopes.join('/') },
  { prop: '所属业务线', label: '所属业务线', hint: '交易线' },
  { prop: '看板负责人', label: '看板负责人', hint: '负责人' },
]
const createForm = reactive<Record<string, string>>({})

function allowedActions(row: Row): string[] {
  switch (row.status) {
    case '草稿':
      return ['提交看板评审']
    case '看板评审中':
      return ['发布看板']
    case '已发布':
      return ['下线看板']
    default:
      return []
  }
}

function resetFilters() {
  keyword.value = ''
  statusFilter.value = ''
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  Object.keys(createForm).forEach((key) => delete createForm[key])
  creating.value = true
}

async function submitCreate() {
  errorMessage.value = ''
  try {
    const result = await postAction(ENDPOINT, { values: { ...createForm } })
    if (!result.ok) {
      errorMessage.value = result.message
      return
    }
    creating.value = false
    successMessage.value = result.message
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '看板保存失败'
  }
}

async function openDetail(id: number) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${id}`)
    if (!response.ok) throw new Error('看板详情读取失败')
    detail.value = (await response.json()) as Detail
    detail.value.cards.forEach((card) => {
      if (!switchDraft[card.card_id]) switchDraft[card.card_id] = { version: '' }
    })
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '看板详情读取失败'
  }
}

async function switchVersion(card: CardView) {
  if (!detail.value) return
  errorMessage.value = ''
  successMessage.value = ''
  const target = switchDraft[card.card_id]?.version?.trim()
  if (!target) {
    errorMessage.value = `请输入卡片 ${card.card_id} 的目标口径版本`
    return
  }
  try {
    // 带当前 revision 做乐观锁：并发切换同一卡片时只有一次生效。
    const response = await request(`${ENDPOINT}/${detail.value.id}/cards/${card.card_id}/switch`, {
      method: 'POST',
      body: JSON.stringify({ metric_version: target, expected_revision: detail.value.revision }),
    })
    const result = await response.json()
    if (!response.ok || !result.ok) {
      errorMessage.value = result.message ?? result.detail ?? '版本切换失败'
      return
    }
    successMessage.value = result.message
    await openDetail(detail.value.id)
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '版本切换失败'
  }
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  successMessage.value = ''
  try {
    const result = await postAction(`${ENDPOINT}/${row.id}/actions`, { values: { action } })
    if (!result.ok) {
      // 发布被拦截时，后端返回逐卡的指标版本原因，直接展示并刷新详情。
      errorMessage.value = result.message
      if (detail.value && detail.value.id === row.id) await openDetail(row.id as number)
      await reload()
      return
    }
    successMessage.value = result.message
    await reload()
    if (detail.value && detail.value.id === row.id) await openDetail(row.id as number)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '看板配置操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const params = new URLSearchParams()
  if (keyword.value) params.set('keyword', keyword.value)
  if (statusFilter.value) params.set('status', statusFilter.value)
  try {
    const response = await request(`${ENDPOINT}?${params.toString()}`)
    if (!response.ok) throw new Error('看板页面列表读取失败')
    const payload = await response.json()
    rows.value = (payload.items ?? []) as Row[]
    total.value = (payload.total ?? rows.value.length) as number
    stats.value[0].value = total.value
    stats.value[1].value = rows.value.filter((row) => row.status === '已发布').length
    stats.value[2].value = rows.value.reduce((sum, row) => sum + Number(row['失效卡片数'] ?? 0), 0)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '看板配置列表读取失败'
  }
}

onMounted(reload)
</script>

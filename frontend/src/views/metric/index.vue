<template>
  <section class="page" data-module="metric">
    <header class="page-head">
      <div>
        <h2>指标口径管理</h2>
        <p class="page-desc">定义和维护核心业务指标的口径，包括计算公式、统计粒度、时间窗口和版本管理。状态只能按 草稿 → 口径评审中 → 已发布 → 已下线 单向推进。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记指标定义</button>
        <button class="btn" type="button" @click="exportRows">导出指标口径清单</button>
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
        <span>指标编号</span>
        <input v-model="keyword" placeholder="按指标编号检索" />
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
          <th>引用看板</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td>
            <button class="link" type="button" @click="openDetail(Number(row.id))">{{ row['引用看板数'] }} 张卡片</button>
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
          <td :colspan="columns.length + 2" class="empty-state">暂无指标口径数据，可先登记指标定义</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条指标口径记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
      <span v-else-if="successMessage" class="success-text">{{ successMessage }}</span>
    </footer>

    <!-- 登记弹窗：保存前按明确阈值校验，后端拒绝时原样展示原因 -->
    <div v-if="creating" class="modal-mask" @click.self="creating = false">
      <div class="modal">
        <h3>登记指标定义</h3>
        <form class="modal-form" @submit.prevent="submitCreate">
          <label v-for="field in createFields" :key="field.prop" class="filter-item">
            <span>{{ field.label }}</span>
            <input v-model="createForm[field.prop]" :placeholder="field.hint" />
          </label>
          <p class="rule-hint">编号须为 METR-四位数字；统计粒度仅限 {{ allowedGrains.join('/') }}；精度位数 0-10。</p>
          <div class="modal-actions">
            <button class="btn" type="button" @click="creating = false">取消</button>
            <button class="btn primary" type="submit">保存</button>
          </div>
        </form>
      </div>
    </div>

    <!-- 详情抽屉：指标定义 → 口径版本 → 看板卡片 → 审计记录 全链路追溯 -->
    <div v-if="detail" class="drawer-mask" @click.self="detail = null">
      <aside class="drawer">
        <header class="drawer-head">
          <div>
            <h3>{{ detail['指标编号'] }} · {{ detail['指标名称'] }}</h3>
            <p class="page-desc">当前状态：{{ detail.status }} ｜ 当前版本：{{ detail.current_version }} ｜ 已发布版本：{{ detail.published_version ?? '无' }}</p>
          </div>
          <button class="btn" type="button" @click="detail = null">关闭</button>
        </header>

        <h4>口径版本（发布后为不可变快照，历史引用不被草稿覆盖）</h4>
        <table class="data-table">
          <thead>
            <tr><th>版本</th><th>版本状态</th><th>计算公式</th><th>统计粒度</th><th>发布时间</th></tr>
          </thead>
          <tbody>
            <tr v-for="ver in detail.versions" :key="ver.version">
              <td>{{ ver.version }}</td>
              <td><span :class="['status-tag', versionTagClass(ver.status)]">{{ ver.status }}</span></td>
              <td>{{ ver.snapshot['计算公式'] }}</td>
              <td>{{ ver.snapshot['统计粒度'] }}</td>
              <td>{{ ver.published_at ?? '—' }}</td>
            </tr>
          </tbody>
        </table>

        <h4>被看板卡片引用（点击可查看卡片实时有效性）</h4>
        <table v-if="detail.references.length" class="data-table">
          <thead>
            <tr><th>看板</th><th>卡片</th><th>引用版本</th><th>卡片粒度</th><th>运行状态</th></tr>
          </thead>
          <tbody>
            <tr v-for="ref in detail.references" :key="ref.card_id">
              <td>{{ ref['看板编号'] }}（{{ ref.dashboard_status }}）</td>
              <td>{{ ref.card_id }}</td>
              <td>{{ ref.metric_version }}</td>
              <td>{{ ref.card_grain }}</td>
              <td>
                <span v-if="ref.card_view?.valid" class="success-text">有效，刷新中</span>
                <span v-else class="error-text">失效：{{ ref.card_view?.reason }}</span>
              </td>
            </tr>
          </tbody>
        </table>
        <p v-else class="muted-text">暂无看板卡片引用该指标。</p>

        <h4>审计记录</h4>
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
            <tr v-if="!detail.audits.length"><td colspan="4" class="empty-state">暂无审计记录</td></tr>
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
type VersionRow = {
  version: string
  status: string
  published_at: string | null
  snapshot: Record<string, string | number | null>
}
type ReferenceRow = {
  dashboard_id: number
  看板编号: string
  dashboard_status: string
  card_id: string
  metric_version: string
  card_grain: string
  card_view?: { valid: boolean; reason: string | null }
}
type AuditRow = { id: number; created_at: string; action: string; result: string; detail: string }
type Detail = {
  id: number
  status: string
  指标编号: string
  指标名称: string
  current_version: string
  published_version: string | null
  versions: VersionRow[]
  references: ReferenceRow[]
  audits: AuditRow[]
  allowed_actions: string[]
}

const ENDPOINT = '/api/metric'
const columns = ['指标编号', '指标名称', '计算公式', '统计粒度', '时间窗口', '筛选条件', '精度位数', 'status', 'current_version', 'published_version']
const statuses = ['草稿', '口径评审中', '已发布', '已下线']
const allowedGrains = ['分钟', '小时', '天', '周', '月']
const stats = ref([
  { label: '指标定义总数', value: 0 },
  { label: '已发布指标', value: 0 },
  { label: '口径评审中', value: 0 },
])

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const successMessage = ref('')
const keyword = ref('')
const statusFilter = ref('')
const detail = ref<Detail | null>(null)
const creating = ref(false)
const createFields = [
  { prop: '指标编号', label: '指标编号', hint: 'METR-0004' },
  { prop: '指标名称', label: '指标名称', hint: '不超过 50 字' },
  { prop: '计算公式', label: '计算公式', hint: '不超过 300 字' },
  { prop: '统计粒度', label: '统计粒度', hint: allowedGrains.join('/') },
  { prop: '时间窗口', label: '时间窗口', hint: 'YYYY-MM-DD' },
  { prop: '筛选条件', label: '筛选条件', hint: '不超过 200 字' },
  { prop: '精度位数', label: '精度位数', hint: '0-10' },
]
const createForm = reactive<Record<string, string>>({})

function allowedActions(row: Row): string[] {
  // 与后端 _allowed_actions 对齐：定义级四态单向推进；
  // 已发布定义上若有在途新版本，按当前版本状态给出提交评审/发布动作。
  switch (row.status) {
    case '草稿':
      return ['提交口径评审']
    case '口径评审中':
      return ['发布指标']
    case '已发布': {
      const actions: string[] = []
      if (row.current_version_status === '草稿') actions.push('提交口径评审')
      else if (row.current_version_status === '口径评审中') actions.push('发布指标')
      else actions.push('新建口径版本')
      actions.push('下线指标')
      return actions
    }
    default:
      return []
  }
}

function versionTagClass(status: string): string {
  return {
    草稿: 'tag-draft',
    口径评审中: 'tag-review',
    已发布: 'tag-published',
    已下线: 'tag-offline',
  }[status] ?? 'tag-draft'
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
    errorMessage.value = error instanceof Error ? error.message : '指标定义保存失败'
  }
}

async function openDetail(id: number) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${id}`)
    if (!response.ok) throw new Error('指标详情读取失败')
    detail.value = (await response.json()) as Detail
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '指标详情读取失败'
  }
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  successMessage.value = ''
  try {
    const result = await postAction(`${ENDPOINT}/${row.id}/actions`, { values: { action } })
    if (!result.ok) {
      // 后端的明确拒绝原因（状态机/版本/粒度）直接展示，不再静默。
      errorMessage.value = result.message
      return
    }
    successMessage.value = result.message
    await reload()
    if (detail.value && detail.value.id === row.id) await openDetail(row.id as number)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '指标口径操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const params = new URLSearchParams()
  if (keyword.value) params.set('keyword', keyword.value)
  if (statusFilter.value) params.set('status', statusFilter.value)
  try {
    const response = await request(`${ENDPOINT}?${params.toString()}`)
    if (!response.ok) throw new Error('指标定义列表读取失败')
    const payload = await response.json()
    rows.value = (payload.items ?? []) as Row[]
    total.value = (payload.total ?? rows.value.length) as number
    stats.value[0].value = total.value
    stats.value[1].value = rows.value.filter((row) => row.status === '已发布').length
    stats.value[2].value = rows.value.filter((row) => row.status === '口径评审中').length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '指标口径列表读取失败'
  }
}

onMounted(reload)
</script>

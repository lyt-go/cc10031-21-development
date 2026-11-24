<template>
  <section class="domain-panel" data-testid="reverify-console">
    <header class="domain-head">
      <div>
        <h3>复验单工作区</h3>
        <p class="page-desc">
          选择一张复验单查看采用范围、已核与待续查时段和每次提交的实际读数；
          续查只处理剩余时段，切换标定后按提交记录重开即可回看旧版。
        </p>
      </div>
      <div class="page-actions">
        <select v-model.number="selectedId" data-testid="reverify-select" @change="loadDetail">
          <option :value="0" disabled>选择复验单</option>
          <option v-for="row in sessions" :key="row.id" :value="Number(row.id)">
            {{ row.复验编号 }} · {{ row.测点编号 }} · {{ row.status }}
          </option>
        </select>
        <button class="btn" data-testid="reverify-refresh" type="button" @click="loadDetail">刷新</button>
      </div>
    </header>

    <p v-if="message" class="hint-text" data-testid="reverify-message">{{ message }}</p>

    <template v-if="detail">
      <dl class="detail-grid">
        <div><dt>标定引用</dt><dd data-testid="reverify-reference">{{ detail.标定引用 }}</dd></div>
        <div><dt>计量单位</dt><dd data-testid="reverify-unit">{{ detail.计量单位 }}</dd></div>
        <div><dt>采用范围</dt><dd>{{ detail.采用范围 }}</dd></div>
        <div><dt>当前状态</dt><dd data-testid="reverify-status">{{ detail.status }}</dd></div>
        <div><dt>已核时段</dt><dd data-testid="reverify-reviewed">{{ detail.已核时段 }}</dd></div>
        <div><dt>待续查时段</dt><dd data-testid="reverify-pending">{{ detail.待续查时段 }}</dd></div>
        <div><dt>边界交叠</dt><dd>{{ detail.边界交叠 }}</dd></div>
        <div><dt>重开自</dt><dd>{{ detail.重开自 || '—' }}</dd></div>
      </dl>

      <div class="action-grid">
        <form class="mini-form" @submit.prevent="submit('提交复验', submitValues)">
          <h4>提交复验（不填区间即取待续查首段）</h4>
          <input v-model="submitValues.起始时刻" data-testid="submit-start" placeholder="起始时刻（可选）" />
          <input v-model="submitValues.截止时刻" data-testid="submit-end" placeholder="截止时刻（可选）" />
          <button class="btn primary" data-testid="reverify-submit" type="submit">提交复验</button>
        </form>

        <form class="mini-form" @submit.prevent="submit('续查', continueValues)">
          <h4>续查（同一张单处理剩余时段）</h4>
          <input v-model="continueValues.样本时刻" data-testid="continue-time" placeholder="样本时刻（可选）" />
          <input v-model="continueValues.起始时刻" placeholder="起始时刻（可选）" />
          <input v-model="continueValues.截止时刻" placeholder="截止时刻（可选）" />
          <button class="btn primary" data-testid="reverify-continue" type="submit">续查</button>
        </form>

        <form class="mini-form" @submit.prevent="submit('按提交记录重开', reopenValues)">
          <h4>按提交记录重开前两点</h4>
          <input v-model="reopenValues.提交记录" data-testid="reopen-run" placeholder="提交记录序号，如 1" />
          <button class="btn" data-testid="reverify-reopen" type="submit">重开</button>
        </form>

        <form class="mini-form" @submit.prevent="submit('单点复验', pointValues)">
          <h4>另发起单点复验</h4>
          <input v-model="pointValues.样本时刻" data-testid="point-time" placeholder="样本时刻，如 2026-09-01T10:03:00" />
          <input v-model="pointValues.标定引用" data-testid="point-reference" placeholder="标定引用（可选）" />
          <button class="btn" data-testid="reverify-point" type="submit">单点复验</button>
        </form>

        <form class="mini-form" @submit.prevent="submit('标记完成', {})">
          <h4>收尾</h4>
          <p class="page-desc">待续查清空后才能标记完成。</p>
          <button class="btn" data-testid="reverify-finish" type="submit">标记完成</button>
        </form>
      </div>

      <h4 class="section-title">提交记录（{{ detail.提交记录?.length ?? 0 }} 次）</h4>
      <table class="data-table">
        <thead>
          <tr>
            <th>序号</th><th>动作</th><th>实际范围</th><th>请求引用</th>
            <th>实际引用</th><th>单位</th><th>样本数</th><th>结论</th><th>快照摘要</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="run in detail.提交记录" :key="run.run" :data-testid="`run-${run.run}`">
            <td>RUN-{{ run.run }}</td>
            <td>{{ run.action }}</td>
            <td class="mono">{{ run.scope.start }} ~ {{ run.scope.end }}</td>
            <td>{{ run.requested_reference }}</td>
            <td>{{ run.actual_references?.join('、') }}</td>
            <td>{{ run.unit }}<span v-if="run.units_seen?.length">（读到 {{ run.units_seen.join('、') }}）</span></td>
            <td>{{ run.sample_count }}</td>
            <td>{{ run.conclusion }}</td>
            <td class="mono">{{ run.digest }}</td>
          </tr>
          <tr v-if="!detail.提交记录?.length">
            <td colspan="9" class="empty-state">还没有提交记录</td>
          </tr>
        </tbody>
      </table>

      <div v-for="run in detail.提交记录" :key="`detail-${run.run}`" class="run-detail">
        <h4>RUN-{{ run.run }} 实际读数</h4>
        <ul>
          <li v-for="sample in run.samples" :key="`${run.run}-${sample.timestamp}`">
            <span class="mono">{{ sample.timestamp }}</span>
            → <strong>{{ sample.value }}</strong> {{ sample.unit }}
            <span class="page-desc">
              （原始 {{ sample.raw_value }} {{ sample.source_unit }} × {{ sample.scale }}，
              规则 {{ sample.reference }}）
            </span>
          </li>
        </ul>
        <p v-for="overlap in run.overlaps" :key="`${run.run}-o-${overlap.timestamp}`" class="overlap-note">
          <span class="mono">{{ overlap.timestamp }}</span> 边界交叠，取
          {{ overlap.chosen }} = {{ overlap.chosen_value }} {{ overlap.unit }}；
          候选：{{ overlap.candidates.map((item) => `${item.reference}=${item.value}${item.unit}`).join('、') }}
        </p>
      </div>
    </template>
    <p v-else class="empty-state">先选择一张复验单查看详情</p>
  </section>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'

import { loadReverifyDetail, runAction } from '@/api/backend-service'
import type { ReverifyDetail } from '@/api/backend-service'
import type { EntryRow } from '@/data/types'

const props = defineProps<{ rows: EntryRow[]; refresh: () => Promise<void> }>()
const sessions = computed(() => props.rows)
const selectedId = ref(0)
const detail = ref<ReverifyDetail | null>(null)
const message = ref('')
const submitValues = ref<Record<string, string>>({})
const continueValues = ref<Record<string, string>>({})
const reopenValues = ref<Record<string, string>>({ 提交记录: '1' })
const pointValues = ref<Record<string, string>>({})

async function loadDetail() {
  if (!selectedId.value) return
  message.value = ''
  try {
    detail.value = await loadReverifyDetail(selectedId.value)
  } catch (error) {
    message.value = error instanceof Error ? error.message : '复验单详情读取失败'
  }
}

async function submit(action: string, values: Record<string, string>) {
  if (!selectedId.value) return
  message.value = ''
  try {
    const compact = Object.fromEntries(
      Object.entries(values).filter(([, value]) => String(value ?? '').trim() !== ''),
    )
    const response = await runAction('reverify_session', selectedId.value, action, compact)
    message.value = response.message
    if (!response.ok) return
    await props.refresh()
    // 重开/单点会生成新单：按新的编号重新定位，否则刷新当前单即可。
    const created = response.entry as EntryRow | undefined
    if ((action === '按提交记录重开' || action === '单点复验') && created?.复验编号) {
      await props.refresh()
      const latest = props.rows.find((row) => row.复验编号 === created.复验编号)
      if (latest) selectedId.value = Number(latest.id)
    }
    await loadDetail()
  } catch (error) {
    message.value = error instanceof Error ? error.message : '操作失败'
  }
}
</script>

<style scoped>
.domain-panel { background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 12px 14px; margin-bottom: 14px; }
.domain-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; }
.domain-head h3 { margin: 0; font-size: 15px; }
.hint-text { color: var(--brand); font-size: 13px; }
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; }
.detail-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 8px 12px; margin: 10px 0; }
.detail-grid dt { font-size: 12px; color: var(--muted); }
.detail-grid dd { margin: 2px 0 0; font-size: 13px; }
.action-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 10px; margin: 12px 0; }
.mini-form { display: flex; flex-direction: column; gap: 6px; border: 1px solid var(--border); border-radius: 8px; padding: 10px; }
.mini-form h4 { margin: 0 0 2px; font-size: 13px; }
.mini-form input { padding: 6px 8px; border: 1px solid var(--border); border-radius: 6px; font-size: 12px; }
.section-title { margin: 14px 0 6px; font-size: 14px; }
.run-detail { border-top: 1px dashed var(--border); margin-top: 10px; padding-top: 8px; font-size: 12px; }
.run-detail ul { margin: 4px 0 0; padding-left: 16px; }
.overlap-note { color: #92400e; background: #fffbeb; border: 1px solid #fde68a; border-radius: 6px; padding: 4px 8px; }
</style>

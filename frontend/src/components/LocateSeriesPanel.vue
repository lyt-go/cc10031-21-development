<template>
  <section class="domain-panel" data-testid="locate-panel">
    <header class="domain-head">
      <div>
        <h3>按换算出处定位时序产物</h3>
        <p class="page-desc">按测点、实际计量单位、采集区间和换算规则定位血脉里的样本，并可直接对错值区间发起复验。</p>
      </div>
    </header>
    <form class="analysis-form" @submit.prevent="runLocate">
      <label>
        <span>测点编号</span>
        <input v-model="query.point" data-testid="locate-point" placeholder="如 SENS-0001" />
      </label>
      <label>
        <span>实际单位</span>
        <input v-model="query.unit" data-testid="locate-unit" placeholder="如 V" />
      </label>
      <label>
        <span>采集起始</span>
        <input v-model="query.start" data-testid="locate-start" placeholder="2026-09-01T10:00:00" />
      </label>
      <label>
        <span>采集截止</span>
        <input v-model="query.end" data-testid="locate-end" placeholder="2026-09-01T10:02:00" />
      </label>
      <label>
        <span>换算规则/引用</span>
        <input v-model="query.rule" data-testid="locate-rule" placeholder="如 UNIT-0001 或 v1" />
      </label>
      <button class="btn primary" data-testid="locate-submit" type="submit">定位</button>
    </form>

    <p v-if="message" class="hint-text" data-testid="locate-message">{{ message }}</p>

    <table v-if="result && result.items.length" class="data-table">
      <thead>
        <tr>
          <th>测点</th>
          <th>原引用</th>
          <th>实际单位</th>
          <th>来源</th>
          <th>命中样本</th>
          <th>产物</th>
          <th>操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="item in result.items" :key="item.index_id" :data-testid="`locate-row-${item.index_id}`">
          <td>{{ item.point }}</td>
          <td>{{ item.reference }}</td>
          <td>{{ item.unit }}</td>
          <td>{{ item.source }}</td>
          <td>{{ item.matched_count }} / {{ item.sample_count }}</td>
          <td class="mono">{{ item.artifact }}</td>
          <td class="row-actions">
            <button
              class="link"
              type="button"
              :data-testid="`locate-reverify-${item.index_id}`"
              @click="launchReverify(item)"
            >
              发起复验
            </button>
          </td>
        </tr>
      </tbody>
    </table>

    <div v-if="result" class="sample-list">
      <article v-for="item in result.items" :key="`samples-${item.index_id}`" class="sample-card">
        <h4>{{ item.reference }} · {{ item.unit }}</h4>
        <p class="page-desc">换算出处：{{ item.source }}；生效 {{ item.samples[0]?.timestamp || '—' }} 起</p>
        <ul>
          <li v-for="sample in item.samples" :key="`${item.index_id}-${sample.timestamp}`">
            <span class="mono">{{ sample.timestamp }}</span>
            → <strong>{{ sample.value }}</strong> {{ sample.unit }}
          </li>
        </ul>
      </article>
    </div>
  </section>
</template>

<script setup lang="ts">
import { ref } from 'vue'

import { locateSeries, runAction } from '@/api/backend-service'
import type { LocateResult } from '@/api/backend-service'

const props = defineProps<{ lineageId: number }>()
const emit = defineEmits<{ (event: 'created', message: string): void }>()

const query = ref({ point: '', unit: '', start: '', end: '', rule: '' })
const result = ref<LocateResult | null>(null)
const message = ref('')

async function runLocate() {
  message.value = ''
  try {
    result.value = await locateSeries('series_lineage', query.value)
    message.value = result.value.total
      ? `定位到 ${result.value.total} 个时序产物`
      : '没有定位到匹配的时序产物'
  } catch (error) {
    message.value = error instanceof Error ? error.message : '定位失败'
  }
}

async function launchReverify(item: LocateResult['items'][number]) {
  message.value = ''
  try {
    const response = await runAction('series_lineage', props.lineageId, '发起复验', {
      测点编号: item.point,
      标定引用: item.reference,
      复验规则: item.reference,
      计量单位: item.unit,
      起始时刻: item.samples[0]?.timestamp ?? query.value.start,
      截止时刻: item.samples[item.samples.length - 1]?.timestamp ?? query.value.end,
    })
    if (!response.ok) throw new Error(response.message)
    message.value = response.message
    emit('created', response.message)
  } catch (error) {
    message.value = error instanceof Error ? error.message : '发起复验失败'
  }
}
</script>

<style scoped>
.domain-panel { background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 12px 14px; margin-bottom: 14px; }
.domain-head h3 { margin: 0; font-size: 15px; }
.hint-text { color: var(--brand); font-size: 13px; }
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; }
.sample-list { display: flex; flex-wrap: wrap; gap: 12px; margin-top: 10px; }
.sample-card { border: 1px solid var(--border); border-radius: 8px; padding: 10px 12px; min-width: 260px; }
.sample-card h4 { margin: 0 0 4px; font-size: 13px; }
.sample-card ul { margin: 6px 0 0; padding-left: 16px; font-size: 12px; }
</style>

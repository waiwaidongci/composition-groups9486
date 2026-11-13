<script setup lang="ts">
import {computed, ref} from 'vue';
import {Component, Sample, Correction, CorrectionDraft} from '../types/domain';
import {previewCorrection, confirmCorrection, getCorrections} from '../api/samples';
import {useAction} from '../hooks/useAction';
import {number, time} from '../utils/format';
import DataTable from './common/DataTable.vue';

const props = defineProps<{studyId:number; components:Component[]; samples:Sample[]; canWrite:boolean; history:Correction[]}>();
const emit = defineEmits<{confirmed:[]; refreshHistory:[]}>();

const a = useAction();
const reason = ref('');
const drafts = ref<CorrectionDraft[]>([]);
const preview = ref<Correction|null>(null);
const expanded = ref<number|null>(null);

const sampleById = computed(() => new Map(props.samples.map(s => [s.id, s])));
const draftIds = computed(() => new Set(drafts.value.map(d => d.sample_id)));

function addSample(s: Sample) {
  if (draftIds.value.has(s.id)) return;
  drafts.value.push({sample_id: s.id, group: s.group, values: {...s.values}});
}
function addCurrent(s: Sample) {
  addSample(s);
}
defineExpose({addCurrent});
function removeDraft(sampleId: number) {
  drafts.value = drafts.value.filter(d => d.sample_id !== sampleId);
}
function clearAll() {
  drafts.value = [];
  preview.value = null;
}
function localIssue(d: CorrectionDraft): string {
  const s = sampleById.value.get(d.sample_id);
  if (!s) return '未知样本';
  const codes = props.components.map(c => c.code);
  const keys = Object.keys(d.values);
  if (keys.length !== codes.length || codes.some(c => !(c in d.values))) return '缺相或含未定义相';
  for (const v of Object.values(d.values)) {
    if (typeof v !== 'number' || !Number.isFinite(v) || v < 0) return '非法数值';
  }
  if (Object.values(d.values).every(v => v === 0)) return '全零不可闭合';
  if (!d.group.trim()) return '组别为空';
  return '';
}
const draftDupes = computed(() => {
  const seen = new Set<number>();
  const dupes = new Set<number>();
  for (const d of drafts.value) {
    if (seen.has(d.sample_id)) dupes.add(d.sample_id);
    seen.add(d.sample_id);
  }
  return dupes;
});
const validDraftCount = computed(() =>
  drafts.value.filter((d, i) => !localIssue(d) && drafts.value.findIndex(x => x.sample_id === d.sample_id) === i).length);

async function doPreview() {
  await a.execute(async () => {
    preview.value = await previewCorrection(props.studyId, reason.value, drafts.value);
  });
}
async function doConfirm() {
  if (!preview.value) return;
  const cid = preview.value.id;
  await a.execute(async () => {
    try {
      preview.value = await confirmCorrection(cid);
      drafts.value = [];
      reason.value = '';
      emit('confirmed');
      emit('refreshHistory');
    } catch (e) {
      // 409 冲突时后端已把该批次标记为 conflicted，拉回最新状态展示冲突说明
      const all = await getCorrections(props.studyId);
      const latest = all.find(c => c.id === cid);
      if (latest) preview.value = latest;
      emit('refreshHistory');
      throw e;
    }
  });
}
function statusText(c: Correction) {
  return c.status === 'confirmed' ? '已确认写入' : c.status === 'conflicted' ? '冲突整批拒绝' : '待确认预览';
}
function fmt(v: number|string|null) {
  return typeof v === 'number' ? number(v, 4) : String(v ?? '—');
}
function toggleHistory(id: number) {
  expanded.value = expanded.value === id ? null : id;
}
</script>

<template>
  <section class="correction-panel">
    <div class="section-heading">
      <h2>批量复测更正</h2>
      <span>先预览逐样本差异，再整批确认 · 已有运行与获批导出保留原始依据</span>
    </div>

    <template v-if="canWrite">
      <div v-if="a.error.value" role="alert" class="notice">{{a.error.value}}</div>
      <p v-if="!drafts.length" class="empty">在上方原始样本矩阵点击样本行，再点“加入更正批”；可一次加入多个化验复测样本。</p>

      <div v-if="drafts.length" class="correction-editor">
        <label class="reason-field">复测更正依据（留痕，8–1500字）
          <textarea aria-label="复测更正依据" v-model="reason" maxlength="1500" placeholder="例如：化验复测后发现部分样本组别串号、两个相通道相对量登记错误，按复测报告整批更正。"/>
        </label>
        <div class="table-wrap">
          <DataTable :head="['样本','更正后组别',...components.map(c=>c.label),'本地校验','操作']">
            <tr v-for="(d,i) in drafts" :key="d.sample_id+'-'+i">
              <td><b>{{sampleById.get(d.sample_id)?.code || '未知#'+d.sample_id}}</b>
                <small>{{sampleById.get(d.sample_id)?.group || '当前库中不存在'}}</small></td>
              <td><input aria-label="更正后组别" v-model="d.group" style="min-width:110px"/></td>
              <td v-for="c in components" :key="c.code">
                <input aria-label="更正后相对量" type="number" step="any" v-model.number="d.values[c.code]" style="min-width:96px"/>
              </td>
              <td>
                <span v-if="draftDupes.has(d.sample_id)" class="tag tag-reject">同批重复</span>
                <span v-else-if="localIssue(d)" class="tag tag-reject">{{localIssue(d)}}</span>
                <span v-else class="tag tag-ok">可写入</span>
              </td>
              <td><button class="text-link" @click="removeDraft(d.sample_id)">移出</button></td>
            </tr>
          </DataTable>
        </div>
        <div class="correction-actions">
          <small>共 {{drafts.length}} 行，本地校验可写 {{validDraftCount}} 行；未知样本、缺相、非法数值与同批重复由后端再次拦截且不写入。</small>
          <div class="btn-row">
            <button class="outline" @click="clearAll">清空批次</button>
            <button class="primary" :disabled="a.busy.value||reason.length<8||!validDraftCount" @click="doPreview">预览逐样本差异</button>
          </div>
        </div>
      </div>

      <div v-if="preview" class="preview-sheet" :class="{'is-conflict':preview.status==='conflicted'}">
        <div class="section-top">
          <h3>批次 #{{preview.id}} · {{statusText(preview)}}</h3>
          <span>写入 {{preview.included_count}} / 排除 {{preview.excluded_count}} · 预览阶段未改动任何样本</span>
        </div>
        <div v-if="preview.status==='conflicted'" role="alert" class="notice">{{preview.conflict.message}}</div>
        <div class="table-wrap">
          <DataTable :head="['样本','字段','更正前','更正后','判定']">
            <template v-for="(item,ii) in preview.items" :key="ii">
              <tr v-if="!item.included">
                <td><b>{{item.code||('未知#'+item.sample_id)}}</b></td>
                <td colspan="3" class="muted">{{item.reject_reason}}</td>
                <td><span class="tag tag-reject">不写入</span></td>
              </tr>
              <template v-else>
                <tr v-for="(ch,ci) in item.changes" :key="ii+'-'+ch.field">
                  <td v-if="ci===0" :rowspan="item.changes.length"><b>{{item.code}}</b><small>{{item.code}} 共{{item.changes.length}}处变化</small></td>
                  <td>{{ch.label}}</td>
                  <td class="before">{{fmt(ch.before)}}</td>
                  <td class="after">{{fmt(ch.after)}}</td>
                  <td v-if="ci===0" :rowspan="item.changes.length"><span class="tag tag-ok">将写入</span></td>
                </tr>
              </template>
            </template>
          </DataTable>
        </div>
        <div v-if="preview.status==='preview'" class="btn-row confirm-row">
          <button class="outline" @click="preview=null">放弃此预览</button>
          <button class="primary" :disabled="a.busy.value||preview.included_count===0" @click="doConfirm">整批确认更正（{{preview.included_count}}个样本）</button>
        </div>
        <p v-else-if="preview.status==='confirmed'" class="caption">已于 {{time(preview.confirmed_at)}} 由 {{preview.confirmed_by}} 确认；重复确认不会产生重复更正。</p>
      </div>
    </template>

    <p v-else class="role-note">只读角色不可发起更正；可在下方查看全部更正批次与前后值留痕。</p>

    <div class="correction-history">
      <div class="section-heading"><h2>更正留痕</h2><span>前后值、操作人与冲突原因均可追溯</span></div>
      <p v-if="!history.length" class="empty">本研究尚无更正批次。</p>
      <article v-for="c in history" :key="c.id" class="history-card" :class="'st-'+c.status">
        <div class="history-head" @click="toggleHistory(c.id)">
          <b>#{{c.id}} {{statusText(c)}}</b>
          <span class="tag" :class="c.status==='confirmed'?'tag-ok':c.status==='conflicted'?'tag-reject':'tag-pending'">
            写入{{c.included_count}} / 排除{{c.excluded_count}}
          </span>
          <small>{{time(c.created_at)}} · {{c.created_by}}<template v-if="c.confirmed_by"> · 确认 {{c.confirmed_by}} {{time(c.confirmed_at)}}</template></small>
        </div>
        <p v-if="expanded===c.id" class="reason-text">{{c.reason}}</p>
        <div v-if="expanded===c.id" class="table-wrap">
          <DataTable :head="['样本','判定','组别 前→后','相对量变化（前 → 后）','排除原因']">
            <tr v-for="(item,ii) in c.items" :key="ii">
              <td><b>{{item.code||('未知#'+item.sample_id)}}</b></td>
              <td><span class="tag" :class="item.included?'tag-ok':'tag-reject'">{{item.included?'已写入':'未写入'}}</span></td>
              <td><span class="before">{{item.before_group||'—'}}</span> → <span class="after">{{item.group}}</span></td>
              <td>
                <template v-if="item.changes.length">
                  <span v-for="ch in item.changes.filter(x=>x.field.startsWith('values.'))" :key="ch.field" class="change-chip">
                    {{ch.label}}: <span class="before">{{fmt(ch.before)}}</span>→<span class="after">{{fmt(ch.after)}}</span>
                  </span>
                </template>
                <span v-else class="muted">无</span>
              </td>
              <td class="muted">{{item.reject_reason||'—'}}</td>
            </tr>
          </DataTable>
        </div>
        <p v-if="expanded===c.id&&c.status==='conflicted'" class="conflict-text">{{c.conflict.message}}</p>
      </article>
    </div>
  </section>
</template>

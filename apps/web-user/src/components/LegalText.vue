<script setup lang="ts">
import { computed } from 'vue'
import { splitLegalText, type LegalTerm } from '@cyshuhe/shared/src/legalText'
const props = defineProps<{
  text: string
  terms?: LegalTerm[]
  clauseId?: string
  sectionPath?: string | null
}>()
const parts = computed(() =>
  splitLegalText(
    props.text,
    (props.terms || []).filter(
      (t) =>
        !t.source_scope ||
        t.source_scope === 'document' ||
        (t.source_scope === 'clause' && t.clause_id === props.clauseId) ||
        (t.source_scope === 'chapter' &&
          !!t.scope_path &&
          props.sectionPath?.includes(t.scope_path)),
    ),
  ),
)
</script>
<template>
  <span class="legal-text"
    ><template v-for="(part, index) in parts" :key="index">
      <el-popover
        v-if="part.term"
        :trigger="['hover', 'focus']"
        :width="390"
        placement="top"
        :show-after="180"
      >
        <template #reference
          ><button class="term" type="button" :aria-label="`${part.text}，查看术语释义`">
            {{ part.text }}
          </button></template
        >
        <div class="term-definition">
          <strong
            >{{ part.term.source_term
            }}<template v-if="part.term.source_term !== part.term.target_zh"
              >（{{ part.term.target_zh }}）</template
            ></strong
          >
          <template v-if="part.term.alternatives"
            ><p>该词在以下来源中有不同说明：</p>
            <section v-for="definition in part.term.alternatives" :key="definition.id">
              <p v-if="definition.definition_zh">{{ definition.definition_zh }}</p>
              <p dir="auto">{{ definition.definition_original }}</p>
              <small>{{ definition.source_reference }}</small>
            </section></template
          >
          <template v-else
            ><p v-if="part.term.definition_zh">{{ part.term.definition_zh }}</p>
            <p v-if="part.term.definition_original" dir="auto">
              {{ part.term.definition_original }}
            </p></template
          >
          <p v-if="!part.term.definition_original">{{ part.term.target_zh }} · 通用译名</p>
          <small>{{ part.term.source_label }} · {{ part.term.source_reference }}</small>
          <p v-if="part.term.source_url">
            <a :href="part.term.source_url" target="_blank" rel="noopener noreferrer"
              >查看来源 ↗</a
            >
          </p>
        </div> </el-popover
      ><template v-else>{{ part.text }}</template>
    </template></span
  >
</template>
<style scoped>
.legal-text {
  white-space: pre-wrap;
  line-height: 1.9;
  overflow-wrap: anywhere;
}
.term {
  font: inherit;
  color: #215c9b;
  background: transparent;
  border: 0;
  border-bottom: 1px dotted #88a9c8;
  padding: 0;
  cursor: help;
  text-align: inherit;
}
.term:focus-visible {
  outline: 2px solid #2563eb;
  outline-offset: 2px;
}
.term-definition {
  font-size: 14px;
  line-height: 1.7;
  max-height: 380px;
  overflow: auto;
}
.term-definition p {
  margin: 10px 0;
}
.term-definition small {
  color: #65788c;
}
.term-definition a {
  color: #215c9b;
}
</style>

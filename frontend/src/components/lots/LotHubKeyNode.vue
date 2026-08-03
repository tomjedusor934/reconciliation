<script setup lang="ts">
import { computed } from 'vue';
import { Handle, Position } from '@vue-flow/core';
import { KEY_TYPE_COLORS } from '@/utils/lotGraph';
import type { KeyTypeNodeData } from '@/utils/lotAggregateGraph';

const props = defineProps<{ data: KeyTypeNodeData }>();

const color = computed(() => KEY_TYPE_COLORS[props.data.keyType] ?? '#94a3b8');
const focused = computed(() => !!props.data.keyValue);

const shortValue = computed(() => {
  const v = props.data.keyValue ?? '';
  return v.length > 24 ? `${v.slice(0, 13)}…${v.slice(-8)}` : v;
});
</script>

<template>
  <div
    class="rounded-2xl border-2 bg-white px-4 py-2.5 shadow-md flex flex-col items-center gap-0.5 transition-shadow hover:shadow-lg cursor-pointer"
    :style="{ borderColor: color }"
    :title="focused
      ? `${data.keyType}: ${data.keyValue}`
      : `${data.keyType} — ${data.distinctCount.toLocaleString()} distinct value(s). Click to explore.`"
  >
    <span class="text-sm font-bold" :style="{ color }">{{ data.keyType }}</span>
    <template v-if="focused">
      <span class="font-mono text-xs text-gray-600">{{ shortValue }}</span>
    </template>
    <template v-else>
      <span class="text-lg font-extrabold tabular-nums text-gray-900">
        {{ data.distinctCount.toLocaleString() }}
      </span>
      <span class="text-[10px] uppercase tracking-wide text-gray-400">
        {{ data.distinctCount === 1 ? 'link' : 'links' }} · click
      </span>
    </template>
    <Handle id="tl" type="target" :position="Position.Left" class="!bg-gray-400" />
    <Handle id="tr" type="target" :position="Position.Right" class="!bg-gray-400" />
  </div>
</template>

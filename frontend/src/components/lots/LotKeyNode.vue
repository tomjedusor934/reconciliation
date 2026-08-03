<script setup lang="ts">
import { computed } from 'vue';
import { Handle, Position } from '@vue-flow/core';
import { KEY_TYPE_COLORS, type KeyNodeData } from '@/utils/lotGraph';

const props = defineProps<{ data: KeyNodeData }>();

const color = computed(() => KEY_TYPE_COLORS[props.data.keyType] ?? '#94a3b8');

const shortValue = computed(() => {
  const v = props.data.keyValue;
  return v.length > 22 ? `${v.slice(0, 12)}…${v.slice(-8)}` : v;
});
</script>

<template>
  <div
    class="rounded-full border bg-white px-3 py-1 text-xs shadow-sm flex items-center gap-1.5"
    :style="{ borderColor: color }"
    :title="`${data.keyType}: ${data.keyValue}`"
  >
    <span class="font-semibold" :style="{ color }">{{ data.keyType }}</span>
    <span class="font-mono text-gray-600">{{ shortValue }}</span>
    <Handle id="tl" type="target" :position="Position.Left" class="!bg-gray-400" />
    <Handle id="tr" type="target" :position="Position.Right" class="!bg-gray-400" />
  </div>
</template>

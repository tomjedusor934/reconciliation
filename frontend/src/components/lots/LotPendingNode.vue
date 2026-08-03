<script setup lang="ts">
import { computed } from 'vue';
import { Handle, Position } from '@vue-flow/core';
import { formatAmount } from '@/utils/formatAmount';
import { PENDING_PAYMENT_COLOR, type PendingNodeData } from '@/utils/lotAggregateGraph';

const props = defineProps<{ data: PendingNodeData }>();

const isDebit = computed(() => Number(props.data.amount) < 0);
</script>

<template>
  <div
    class="rounded-2xl border-2 bg-orange-50 px-4 py-2.5 shadow-md flex flex-col items-center gap-0.5"
    :style="{ borderColor: PENDING_PAYMENT_COLOR }"
    :title="`Payments still pending (PDNG): ${data.count.toLocaleString()} payment(s)`"
  >
    <span class="text-[11px] font-bold uppercase tracking-wide" :style="{ color: PENDING_PAYMENT_COLOR }">
      Pending payments
    </span>
    <span
      class="text-lg font-extrabold tabular-nums"
      :class="isDebit ? 'text-red-600' : 'text-green-600'"
    >
      {{ formatAmount(data.amount) }}
    </span>
    <span class="text-[10px] text-gray-500 tabular-nums">
      {{ data.count.toLocaleString() }} payment(s) · PDNG
    </span>
    <Handle id="tl" type="target" :position="Position.Left" class="!bg-orange-400" />
    <Handle id="tr" type="target" :position="Position.Right" class="!bg-orange-400" />
  </div>
</template>

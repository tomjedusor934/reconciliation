<script setup lang="ts">
import { computed } from 'vue';
import { Handle, Position } from '@vue-flow/core';
import { formatAmount } from '@/utils/formatAmount';
import { MOVEMENT_TYPE_CLASSES, type GroupNodeData } from '@/utils/lotAggregateGraph';

const props = defineProps<{ data: GroupNodeData }>();

const group = computed(() => props.data.group);
const isDebit = computed(() => group.value.direction === 'debit' || Number(group.value.total_amount) < 0);
const count = computed(() => group.value.member_count.toLocaleString());

const matchedPct = computed(() =>
  group.value.member_count ? (group.value.matched_count / group.value.member_count) * 100 : 0
);
const pendingPct = computed(() =>
  group.value.member_count ? (group.value.pending_count / group.value.member_count) * 100 : 0
);
// Pending-payment amount is only surfaced per-node in the focused (scoped) view.
const showPendingPayment = computed(
  () => props.data.showPending && Number(group.value.pending_payment_amount) !== 0
);
</script>

<template>
  <div
    class="w-[300px] rounded-xl border border-gray-200 bg-gradient-to-br from-white shadow-md px-3.5 py-2.5 transition-shadow hover:shadow-lg hover:border-indigo-300 cursor-pointer"
    :class="data.side === 'SP' ? 'to-indigo-50/60' : 'to-teal-50/60'"
  >
    <div class="flex items-center justify-between gap-2">
      <span
        class="inline-flex items-center rounded-full px-2 py-0.5 text-xs font-semibold"
        :class="MOVEMENT_TYPE_CLASSES[group.movement_type] || 'bg-gray-100 text-gray-700'"
      >
        {{ group.movement_type }}
      </span>
      <span
        class="rounded-full px-2 py-0.5 text-[10px] uppercase tracking-wide font-semibold"
        :class="isDebit ? 'bg-red-50 text-red-600' : 'bg-green-50 text-green-600'"
      >
        {{ group.direction }}
      </span>
    </div>
    <div class="mt-1 flex items-baseline justify-between gap-2">
      <span class="text-xl font-bold text-gray-900 tabular-nums">×{{ count }}</span>
      <span
        class="font-semibold tabular-nums text-sm"
        :class="isDebit ? 'text-red-600' : 'text-green-600'"
      >
        {{ formatAmount(group.total_amount) }}
      </span>
    </div>
    <div class="mt-1.5 h-1.5 w-full overflow-hidden rounded-full bg-gray-200 flex">
      <div class="h-full bg-emerald-500" :style="{ width: `${matchedPct}%` }" />
      <div class="h-full bg-amber-400" :style="{ width: `${pendingPct}%` }" />
    </div>
    <div class="mt-1 flex items-center justify-between text-[11px] text-gray-500 tabular-nums">
      <span v-if="group.matched_count" class="text-emerald-600">
        {{ group.matched_count.toLocaleString() }} matched
      </span>
      <span v-if="group.pending_count" class="text-amber-600">
        {{ group.pending_count.toLocaleString() }} pending
      </span>
      <span v-if="group.excluded_count" class="text-gray-400">
        {{ group.excluded_count.toLocaleString() }} excluded
      </span>
    </div>
    <div
      v-if="showPendingPayment"
      class="mt-1 flex items-center justify-between border-t border-orange-100 pt-1 text-[11px] tabular-nums"
    >
      <span class="text-orange-500 font-medium">pending payments</span>
      <span class="font-semibold text-orange-600">
        {{ formatAmount(group.pending_payment_amount) }}
      </span>
    </div>
    <Handle
      type="source"
      :position="data.side === 'SP' ? Position.Right : Position.Left"
      class="!bg-gray-400"
    />
  </div>
</template>

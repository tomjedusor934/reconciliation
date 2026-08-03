<script setup lang="ts">
import { computed } from 'vue';
import { Handle, Position } from '@vue-flow/core';
import Badge from '@/components/ui/Badge.vue';
import { formatAmount } from '@/utils/formatAmount';
import { formatDateShort } from '@/utils/formatDate';
import type { MovementNodeData } from '@/utils/lotGraph';

const props = defineProps<{ data: MovementNodeData }>();

const member = computed(() => props.data.member);
const isDebit = computed(() => Number(member.value.amount) < 0);

const TYPE_CLASSES: Record<string, string> = {
  SCTXB: 'bg-indigo-100 text-indigo-800',
  SDDXB: 'bg-violet-100 text-violet-800',
  SDXBB: 'bg-purple-100 text-purple-800',
  NDGB: 'bg-teal-100 text-teal-800',
  NDRJ: 'bg-rose-100 text-rose-800',
  SWIFT: 'bg-sky-100 text-sky-800',
  BKRTP: 'bg-amber-100 text-amber-800',
};

const statusVariant = (s?: string | null): 'success' | 'warning' | 'danger' | 'default' =>
  s === 'matched' || s === 'forced'
    ? 'success'
    : s === 'pending'
      ? 'warning'
      : s === 'excluded'
        ? 'danger'
        : 'default';

const reference = computed(
  () => member.value.external_ref || member.value.ref_no || member.value.remarks_1 || '—'
);
</script>

<template>
  <div class="w-[300px] rounded-lg border border-gray-200 bg-white shadow-sm px-3 py-2">
    <div class="flex items-center justify-between gap-2">
      <span
        class="inline-flex items-center rounded-full px-2 py-0.5 text-xs font-semibold"
        :class="TYPE_CLASSES[member.movement_type] || 'bg-gray-100 text-gray-700'"
      >
        {{ member.movement_type }}
      </span>
      <Badge :variant="statusVariant(member.entry_status)">
        {{ member.entry_status || 'not ingested' }}
      </Badge>
    </div>
    <div class="mt-1 font-semibold tabular-nums" :class="isDebit ? 'text-red-600' : 'text-green-600'">
      {{ formatAmount(member.amount, member.currency) }}
    </div>
    <div class="mt-0.5 flex items-center justify-between text-xs text-gray-500">
      <span>{{ formatDateShort(member.value_date) }}</span>
      <span class="font-mono truncate max-w-[160px]" :title="reference">{{ reference }}</span>
    </div>
    <Handle
      type="source"
      :position="data.side === 'SP' ? Position.Right : Position.Left"
      class="!bg-gray-400"
    />
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue';
import { Handle, Position } from '@vue-flow/core';
import { Layers } from 'lucide-vue-next';
import Badge from '@/components/ui/Badge.vue';
import { formatAmount } from '@/utils/formatAmount';
import { formatDateShort } from '@/utils/formatDate';
import type { MovementNodeData } from '@/utils/lotGraph';

const props = defineProps<{ data: MovementNodeData }>();

const emit = defineEmits<{ (e: 'open-split', parentHash: string): void }>();

const member = computed(() => props.data.member);
const isDebit = computed(() => Number(member.value.amount) < 0);
// A ghost is a slice of a real movement, not a booking of its own — drawn
// dashed so it never reads as money that actually moved on its own.
const ghostOf = computed(() => member.value.split_parent_hash || null);

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
  <div
    class="w-[300px] rounded-lg bg-white shadow-sm px-3 py-2"
    :class="ghostOf ? 'border-2 border-dashed border-indigo-300' : 'border border-gray-200'"
  >
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
    <button
      v-if="ghostOf"
      type="button"
      class="mt-1.5 flex w-full items-center gap-1.5 rounded-md bg-indigo-50 px-2 py-1 text-left text-[11px] text-indigo-700 transition-colors hover:bg-indigo-100"
      :title="'Show the real movement this is a slice of'"
      @click.stop="emit('open-split', ghostOf)"
    >
      <Layers class="h-3 w-3 shrink-0" />
      <span class="truncate">
        Slice of {{ member.split_parent_external_ref || 'a batch movement' }}
        <template v-if="member.payment_count">· {{ member.payment_count }} payment(s)</template>
      </span>
    </button>
    <Handle
      type="source"
      :position="data.side === 'SP' ? Position.Right : Position.Left"
      class="!bg-gray-400"
    />
  </div>
</template>

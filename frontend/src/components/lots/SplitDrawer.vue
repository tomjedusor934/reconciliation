<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import { AlertTriangle, Layers } from 'lucide-vue-next';
import Drawer from '@/components/ui/Drawer.vue';
import Badge from '@/components/ui/Badge.vue';
import Loader from '@/components/ui/Loader.vue';
import { formatAmount } from '@/utils/formatAmount';
import { formatDateShort } from '@/utils/formatDate';
import lotService from '@/services/lotService';
import toaster from '@/utils/toaster';
import type { SplitChild, SplitDetail } from '@/types';

/**
 * The real movement behind a ghost, and every slice it was cut into.
 *
 * A batch-booked movement settles payments spread over several
 * (PACS008 × MSGID) buckets, so it belongs to none of them: it is replaced by
 * one ghost per bucket and withdrawn from the reconciliation. This drawer is
 * where that stops being invisible — it shows the booking that actually
 * happened, where each slice went, and whether the slices still add back up to
 * it.
 */
const props = defineProps<{
  isOpen: boolean;
  parentHash: string | null;
  // Highlighted in the list: the bucket the user is currently looking at.
  currentLotId?: string | null;
}>();

const emit = defineEmits<{
  (e: 'close'): void;
  (e: 'open-lot', lotId: string): void;
}>();

const detail = ref<SplitDetail | null>(null);
const loading = ref(false);

const fetchSplit = async () => {
  if (!props.parentHash) return;
  loading.value = true;
  detail.value = null;
  try {
    const { data } = await lotService.getSplit(props.parentHash);
    detail.value = data;
  } catch (e) {
    toaster.error('Failed to load the split');
  } finally {
    loading.value = false;
  }
};

const parent = computed(() => detail.value?.parent ?? null);
const conservation = computed(() => detail.value?.conservation ?? null);

/** The ghosts share out the movement's own amount, so this holds unless one was
 *  removed out of band. */
const isConserved = computed(() => Number(conservation.value?.missing_amount ?? 0) === 0);
/** A different question: does std.Payment agree with what finacle booked? */
const hasPaymentGap = computed(() => Number(conservation.value?.payment_gap ?? 0) !== 0);
const isSharedKey = computed(() => (parent.value?.shared_key_movements ?? 1) > 1);

/** Share of the parent each slice represents, for the conservation bar. */
const shares = computed(() => {
  const total = Math.abs(Number(parent.value?.amount ?? 0));
  if (!detail.value || !total) return [];
  return detail.value.children.map((child) => ({
    child,
    percent: (Math.abs(Number(child.amount)) / total) * 100,
  }));
});

const bucketLabel = (child: SplitChild): string => {
  // RESIDUAL is no longer produced; kept for the ghosts of earlier runs.
  if (child.bucket_kind === 'RESIDUAL') return 'Unexplained remainder (legacy)';
  const parts = [child.bucket_pacs008, child.bucket_msgid, child.bucket_po].filter(Boolean);
  return parts.length ? parts.join(' × ') : (child.bucket_kind ?? 'unknown bucket');
};

const statusVariant = (s?: string | null): 'success' | 'warning' | 'danger' | 'default' =>
  s === 'matched' || s === 'forced'
    ? 'success'
    : s === 'pending'
      ? 'warning'
      : s === 'excluded'
        ? 'danger'
        : 'default';

const sliceColor = (child: SplitChild, i: number) => {
  if (child.bucket_kind === 'RESIDUAL') return '#ef4444';
  const palette = ['#6366f1', '#14b8a6', '#f59e0b', '#8b5cf6', '#0ea5e9', '#ec4899'];
  return palette[i % palette.length];
};

watch(
  () => [props.isOpen, props.parentHash] as const,
  ([open]) => {
    if (open && props.parentHash) fetchSplit();
  },
  { immediate: true }
);
</script>

<template>
  <Drawer :is-open="isOpen" title="Split movement" size="lg" @close="emit('close')">
    <div v-if="loading" class="flex justify-center py-10"><Loader size="lg" /></div>

    <div v-else-if="parent && conservation" class="space-y-5">
      <!-- The booking that actually happened on the account. -->
      <section class="rounded-xl border border-gray-200 bg-gray-50 p-4">
        <div class="flex items-center gap-2 text-xs font-semibold uppercase tracking-wide text-gray-500">
          <Layers class="h-4 w-4" />
          Real movement
        </div>
        <div class="mt-2 flex items-baseline justify-between gap-3">
          <span class="font-mono text-sm text-gray-700">{{ parent.external_ref || '—' }}</span>
          <span
            class="text-xl font-semibold tabular-nums"
            :class="Number(parent.amount) < 0 ? 'text-red-600' : 'text-green-600'"
          >
            {{ formatAmount(parent.amount, parent.currency) }}
          </span>
        </div>
        <dl class="mt-3 grid grid-cols-2 gap-x-4 gap-y-1 text-xs text-gray-600">
          <div class="flex gap-1"><dt class="text-gray-400">Type</dt><dd>{{ parent.movement_type }}</dd></div>
          <div class="flex gap-1"><dt class="text-gray-400">Value date</dt><dd>{{ formatDateShort(parent.value_date) }}</dd></div>
          <div class="flex gap-1"><dt class="text-gray-400">Account</dt><dd class="font-mono">{{ parent.account || '—' }}</dd></div>
          <div class="flex gap-1"><dt class="text-gray-400">Payments</dt><dd class="tabular-nums">{{ parent.payment_count }}</dd></div>
          <div class="col-span-2 flex gap-1 min-w-0">
            <dt class="text-gray-400 shrink-0">Particulars</dt>
            <dd class="font-mono truncate" :title="parent.transaction_particulars || ''">
              {{ parent.transaction_particulars || '—' }}
            </dd>
          </div>
        </dl>

        <p
          v-if="parent.parent_emarged"
          class="mt-3 flex items-start gap-2 rounded-lg bg-amber-50 px-3 py-2 text-xs text-amber-800"
        >
          <AlertTriangle class="h-4 w-4 shrink-0" />
          <span>
            This movement was already reconciled when the split was registered, so it was
            <strong>not withdrawn</strong> — it and its slices both count until an operator
            arbitrates.
          </span>
        </p>

        <p
          v-if="isSharedKey"
          class="mt-3 flex items-start gap-2 rounded-lg bg-sky-50 px-3 py-2 text-xs text-sky-800"
        >
          <AlertTriangle class="h-4 w-4 shrink-0" />
          <span>
            Finacle booked this payment batch as
            <strong>{{ parent.shared_key_movements.toLocaleString() }} separate movements</strong>,
            all carrying the same reference — each of them resolves the whole payment group, so the
            slices below are weighted on a shared basis. Each movement still hands out only its own
            amount.
          </span>
        </p>
      </section>

      <!-- Conservation: the slices must add back up to the movement. -->
      <section>
        <div class="flex items-center justify-between text-xs">
          <span class="font-semibold uppercase tracking-wide text-gray-500">
            Split into {{ conservation.child_count }} slice(s)
          </span>
          <span :class="isConserved ? 'text-green-600' : 'text-red-600'" class="font-semibold tabular-nums">
            {{ formatAmount(conservation.children_amount, parent.currency) }}
            / {{ formatAmount(conservation.parent_amount, parent.currency) }}
          </span>
        </div>

        <div class="mt-2 flex h-2.5 w-full overflow-hidden rounded-full bg-gray-100">
          <div
            v-for="(s, i) in shares"
            :key="s.child.source_hash"
            class="h-full"
            :style="{ width: `${s.percent}%`, backgroundColor: sliceColor(s.child, i) }"
            :title="`${bucketLabel(s.child)} — ${formatAmount(s.child.amount, s.child.currency)}`"
          />
        </div>

        <p v-if="!isConserved" class="mt-2 flex items-start gap-2 rounded-lg bg-red-50 px-3 py-2 text-xs text-red-700">
          <AlertTriangle class="h-4 w-4 shrink-0" />
          <span>
            {{ formatAmount(conservation.missing_amount, parent.currency) }} is not carried by any
            slice — a ghost was removed out of band. The movement's amount is no longer fully
            represented in the reconciliation.
          </span>
        </p>
        <p v-else class="mt-2 text-xs text-green-700">
          Every cent of the movement is accounted for by its slices.
        </p>

        <!-- A separate control: the slices always add up, but the payment data
             behind them may still disagree with what the bank booked. -->
        <div class="mt-3 rounded-lg border border-gray-200 px-3 py-2">
          <div class="flex items-center justify-between text-xs">
            <span class="text-gray-500">Payments known to std.Payment</span>
            <span class="tabular-nums" :class="hasPaymentGap ? 'text-amber-700' : 'text-gray-700'">
              {{ formatAmount(conservation.payment_amount, parent.currency) }}
            </span>
          </div>
          <p v-if="hasPaymentGap" class="mt-1 text-[11px] text-amber-700">
            {{ formatAmount(conservation.payment_gap, parent.currency) }} of the booked amount is not
            explained by the payments (charges, FX, a payment missing from the datamart, or a shared
            reference). The slices are weighted by the payments, so this shifts how the amount is
            spread — never how much of it is placed.
          </p>
          <p v-else class="mt-1 text-[11px] text-gray-500">
            The payment data matches what finacle booked.
          </p>
        </div>
      </section>

      <!-- Where each slice went. -->
      <section class="space-y-2">
        <button
          v-for="(s, i) in shares"
          :key="s.child.source_hash"
          type="button"
          class="w-full rounded-lg border px-3 py-2 text-left transition-colors"
          :class="
            s.child.lot_id && s.child.lot_id === currentLotId
              ? 'border-indigo-400 bg-indigo-50'
              : 'border-gray-200 hover:border-indigo-300 hover:bg-gray-50'
          "
          :disabled="!s.child.lot_id"
          @click="s.child.lot_id && emit('open-lot', s.child.lot_id)"
        >
          <div class="flex items-center justify-between gap-2">
            <span class="flex min-w-0 items-center gap-2">
              <span class="h-2.5 w-2.5 shrink-0 rounded-full" :style="{ backgroundColor: sliceColor(s.child, i) }" />
              <span class="truncate font-mono text-xs" :title="bucketLabel(s.child)">
                {{ bucketLabel(s.child) }}
              </span>
            </span>
            <span
              class="shrink-0 font-semibold tabular-nums"
              :class="Number(s.child.amount) < 0 ? 'text-red-600' : 'text-green-600'"
            >
              {{ formatAmount(s.child.amount, s.child.currency) }}
            </span>
          </div>
          <div class="mt-1 flex items-center gap-2 text-[11px] text-gray-500">
            <Badge :variant="statusVariant(s.child.entry_status)">
              {{ s.child.entry_status || 'not ingested' }}
            </Badge>
            <span class="tabular-nums">{{ s.percent.toFixed(1) }}%</span>
            <span v-if="s.child.payment_count">· {{ s.child.payment_count }} payment(s)</span>
            <span
              v-if="s.child.synthetic_only"
              class="rounded-full bg-gray-100 px-1.5 py-0.5"
              title="Every movement in that bucket is a slice, so it balances by construction"
            >
              all slices
            </span>
          </div>
        </button>
      </section>
    </div>

    <p v-else class="py-10 text-center text-sm text-gray-400">No split found for this movement.</p>
  </Drawer>
</template>

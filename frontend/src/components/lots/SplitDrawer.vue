<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import { AlertTriangle, Layers, Users } from 'lucide-vue-next';
import Drawer from '@/components/ui/Drawer.vue';
import Badge from '@/components/ui/Badge.vue';
import Loader from '@/components/ui/Loader.vue';
import { formatAmount } from '@/utils/formatAmount';
import { formatDateShort } from '@/utils/formatDate';
import lotService from '@/services/lotService';
import toaster from '@/utils/toaster';
import type { SplitChild, SplitDetail } from '@/types';

/**
 * The real movement behind a ghost, its claim group, and where each slice went.
 *
 * A batch-booked movement settles payments spread over several
 * (PACS008 × MSGID) buckets, so it belongs to none of them: it is withdrawn
 * from the reconciliation and the CLAIM GROUP it belongs to (every movement
 * resolving the same aggregate key) emits one ghost per bucket, priced at the
 * bucket's exact payment sum. This drawer shows the bookings that actually
 * happened, the group's ghosts, and whether the two still add up — the second
 * reconciliation that tags lots when they do not.
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
const group = computed(() => detail.value?.group ?? null);

/** The group's control: do the booked movements add up to their ghosts? */
const groupBalanced = computed(() => Number(group.value?.delta ?? 0) === 0);
const isSharedKey = computed(() => (group.value?.parents.length ?? 0) > 1);

/** Share of the group's ghost total each slice represents, for the bar. */
const shares = computed(() => {
  const total = Math.abs(Number(group.value?.children_total ?? 0));
  if (!detail.value || !total) return [];
  return detail.value.children.map((child) => ({
    child,
    percent: (Math.abs(Number(child.amount)) / total) * 100,
  }));
});

const bucketLabel = (child: SplitChild): string => {
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

const sliceColor = (_child: SplitChild, i: number) => {
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

    <div v-else-if="parent && group" class="space-y-5">
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
            <strong>not withdrawn</strong> — it and its group's slices both count until an operator
            arbitrates.
          </span>
        </p>
      </section>

      <!-- The claim group: every movement resolving the same key, together. -->
      <section class="rounded-xl border border-gray-200 p-4">
        <div class="flex items-center justify-between gap-2">
          <span class="flex items-center gap-2 text-xs font-semibold uppercase tracking-wide text-gray-500">
            <Users class="h-4 w-4" />
            Claim group
            <span v-if="group.claim_key_value" class="font-mono normal-case text-gray-600">
              {{ group.claim_key_type }}:{{ group.claim_key_value }}
            </span>
          </span>
          <span class="text-[11px] text-gray-400">{{ group.parents.length }} movement(s)</span>
        </div>

        <p
          v-if="isSharedKey"
          class="mt-2 text-[11px] text-gray-500"
        >
          Finacle booked this payment batch as {{ group.parents.length.toLocaleString() }} separate
          movements carrying the same reference — the slices below stand for the whole group, not
          for any single movement.
        </p>

        <ul class="mt-3 max-h-44 space-y-1 overflow-y-auto pr-1">
          <li
            v-for="sibling in group.parents"
            :key="sibling.source_hash"
            class="flex items-center justify-between gap-2 rounded-md px-2 py-1 text-xs"
            :class="sibling.source_hash === parent.source_hash ? 'bg-indigo-50' : ''"
          >
            <span class="flex min-w-0 items-center gap-2">
              <span class="truncate font-mono text-gray-700">{{ sibling.external_ref || '—' }}</span>
              <span class="shrink-0 text-gray-400">{{ formatDateShort(sibling.value_date) }}</span>
              <span
                v-if="sibling.parent_emarged"
                class="shrink-0 rounded-full bg-amber-100 px-1.5 py-0.5 text-[10px] text-amber-800"
                title="Already reconciled — not withdrawn, double counts against the slices"
              >
                émargé
              </span>
            </span>
            <span
              class="shrink-0 tabular-nums"
              :class="Number(sibling.amount) < 0 ? 'text-red-600' : 'text-green-600'"
            >
              {{ formatAmount(sibling.amount, sibling.currency) }}
            </span>
          </li>
        </ul>

        <!-- The second reconciliation: Σ movements vs Σ slices. -->
        <div class="mt-3 border-t border-gray-100 pt-2 text-xs">
          <div class="flex items-center justify-between">
            <span class="text-gray-500">Booked by {{ group.parents.length }} movement(s)</span>
            <span class="font-semibold tabular-nums text-gray-700">
              {{ formatAmount(group.parent_total, parent.currency) }}
            </span>
          </div>
          <div class="mt-1 flex items-center justify-between">
            <span class="text-gray-500">Carried by {{ detail?.children.length ?? 0 }} slice(s)</span>
            <span class="font-semibold tabular-nums text-gray-700">
              {{ formatAmount(group.children_total, parent.currency) }}
            </span>
          </div>
          <p
            v-if="!groupBalanced"
            class="mt-2 flex items-start gap-2 rounded-lg bg-red-50 px-3 py-2 text-red-700"
          >
            <AlertTriangle class="h-4 w-4 shrink-0" />
            <span>
              The group does not add up: <strong>{{ formatAmount(group.delta, parent.currency) }}</strong>
              of the booked amount is not carried by any slice (charges, FX, payments the datamart
              attributes elsewhere). Every lot holding one of these slices is tagged
              <strong>parent&nbsp;mismatch</strong> — matched or not, it cannot be fully validated.
            </span>
          </p>
          <p v-else class="mt-2 text-green-700">
            The movements and their slices add up to the cent.
          </p>
        </div>
      </section>

      <!-- Where each slice went. -->
      <section class="space-y-2">
        <div class="flex h-2.5 w-full overflow-hidden rounded-full bg-gray-100">
          <div
            v-for="(s, i) in shares"
            :key="s.child.source_hash"
            class="h-full"
            :style="{ width: `${s.percent}%`, backgroundColor: sliceColor(s.child, i) }"
            :title="`${bucketLabel(s.child)} — ${formatAmount(s.child.amount, s.child.currency)}`"
          />
        </div>

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

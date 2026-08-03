<script setup lang="ts">
import { ref, onMounted, computed } from 'vue';
import { useRoute } from 'vue-router';
import { Download } from 'lucide-vue-next';
import Card from '@/components/ui/Card.vue';
import Table, { type Column } from '@/components/ui/Table.vue';
import Select from '@/components/ui/Select.vue';
import Input from '@/components/ui/Input.vue';
import Button from '@/components/ui/Button.vue';
import Badge from '@/components/ui/Badge.vue';
import Loader from '@/components/ui/Loader.vue';
import { formatDateShort } from '@/utils/formatDate';
import { formatAmount } from '@/utils/formatAmount';
import flowService from '@/services/flowService';
import reconciliationService from '@/services/reconciliationService';
import toaster from '@/utils/toaster';
import type { Flow, ReconciliationEntry } from '@/types';
import ForceMatchModal from './ForceMatchModal.vue';
import ExclusionModal from './ExclusionModal.vue';
import UnexcludeModal from './UnexcludeModal.vue';

const route = useRoute();
const loading = ref(true);
const flows = ref<Flow[]>([]);
const entries = ref<ReconciliationEntry[]>([]);
const selected = ref<ReconciliationEntry[]>([]);
const totalCount = ref(0);
const batchSize = 200;
const currentOffset = ref(0);
const loadingMore = ref(false);

const filters = ref({
  flow_id: undefined as number | undefined,
  status: 'pending',
  reco_id: '',
  amount_min: '',
  amount_max: '',
  payment_statuses: [] as string[],
  date_from: '',
  date_to: '',
});

const statusOptions = [
  { value: '', label: 'All' },
  { value: 'pending', label: 'Pending' },
  { value: 'matched', label: 'Matched' },
  { value: 'forced', label: 'Forced' },
  { value: 'excluded', label: 'Excluded' },
];

// Payment-status filter options (raw std.Payment.Status values, no fixed enum).
const paymentStatusChoices = ref<string[]>([]);
const togglePaymentStatus = (st: string) => {
  const set = new Set(filters.value.payment_statuses);
  set.has(st) ? set.delete(st) : set.add(st);
  filters.value.payment_statuses = [...set];
};
const fetchPaymentStatusChoices = async () => {
  try {
    const { data } = await reconciliationService.paymentStatusOptions();
    paymentStatusChoices.value = data;
  } catch (e) {
    /* non-blocking: the filter simply shows no options */
  }
};

const flowOptions = computed(() => [
  { value: '', label: 'All flows' },
  ...flows.value.map((f) => ({ value: String(f.id), label: f.name })),
]);

const columns: Column[] = [
  { key: 'select', label: '' },
  { key: 'flow_id', label: 'Flow' },
  { key: 'reco_id', label: 'Reco ID', sortable: true },
  { key: 'account', label: 'Account' },
  { key: 'amount', label: 'Amount', sortable: true, format: 'amount', align: 'right' },
  { key: 'payment_statuses', label: 'Payments' },
  { key: 'value_date', label: 'Value date', sortable: true },
  { key: 'event_type', label: 'Event' },
  { key: 'transaction_particulars', label: 'Particulars' },
  { key : 'ref_no', label: 'Ref no' },
  { key: 'remarks_1', label: 'Remarks 1' },
  { key: 'status', label: 'Status' },
  { key: 'actions', label: 'Actions', pinned: true },
];

// {status: count} → [['ACC', 3], ['PDNG', 2]] sorted by count desc ('?' = payment
// reference not resolvable in std.Payment yet).
const paymentStatusEntries = (item: Record<string, any>): [string, number][] =>
  Object.entries((item.payment_statuses ?? {}) as Record<string, number>).sort(
    (a, b) => b[1] - a[1] || a[0].localeCompare(b[0])
  );

const PAYMENT_STATUS_CLASSES: Record<string, string> = {
  ACC: 'bg-emerald-50 text-emerald-700 border-emerald-200',
  ACSC: 'bg-emerald-50 text-emerald-700 border-emerald-200',
  RJCT: 'bg-rose-50 text-rose-700 border-rose-200',
  PDNG: 'bg-amber-50 text-amber-700 border-amber-200',
  '?': 'bg-gray-50 text-gray-500 border-gray-200',
};

const showForce = ref(false);
const showExclude = ref(false);
const excludeTarget = ref<ReconciliationEntry | null>(null);
const showUnexclude = ref(false);
const unexcludeTarget = ref<ReconciliationEntry | null>(null);

const flowName = (id: number) => flows.value.find((f) => f.id === id)?.code || `#${id}`;
const statusVariant = (s: string): 'success' | 'warning' | 'secondary' | 'danger' =>
  ({ matched: 'success', forced: 'success', pending: 'warning', excluded: 'secondary' } as const)[s] || 'secondary';

const fetchFlows = async () => {
  const { data } = await flowService.getAll();
  flows.value = data;
};

const fetchEntries = async () => {
  loading.value = true;
  currentOffset.value = 0;
  try {
    const params: any = { limit: batchSize, skip: 0, ...filters.value };
    Object.keys(params).forEach((k) => {
      const v = params[k];
      if (v === '' || v === undefined || (Array.isArray(v) && v.length === 0)) delete params[k];
    });
    const { data } = await reconciliationService.list(params);
    entries.value = data.items;
    totalCount.value = data.total_count;
    currentOffset.value = batchSize;
    selected.value = [];
  } catch (e) {
    toaster.error('Failed to load entries');
  } finally {
    loading.value = false;
  }
};

const fetchMoreEntries = async () => {
  if (loadingMore.value) return;
  if (entries.value.length >= totalCount.value) return;
  loadingMore.value = true;
  try {
    const params: any = { limit: batchSize, skip: currentOffset.value, ...filters.value };
    Object.keys(params).forEach((k) => {
      const v = params[k];
      if (v === '' || v === undefined || (Array.isArray(v) && v.length === 0)) delete params[k];
    });
    const { data } = await reconciliationService.list(params);
    entries.value = [...entries.value, ...data.items];
    totalCount.value = data.total_count;
    currentOffset.value += batchSize;
  } catch (e) {
    toaster.error('Failed to load more entries');
  } finally {
    loadingMore.value = false;
  }
};

// Export all rows matching the current filters (server-side, beyond the loaded
// batches). Cookie auth (withCredentials) → window.open carries the session.
const exportExcel = () => {
  window.open(reconciliationService.getExportUrl(filters.value), '_blank');
};

const toggleSelect = (item: ReconciliationEntry, checked: boolean) => {
  if (checked) selected.value = [...selected.value, item];
  else selected.value = selected.value.filter((e) => e.id !== item.id);
};
const isSelected = (id: number) => selected.value.some((e) => e.id === id);

const selectionTotal = computed(() =>
  selected.value.reduce((acc, e) => acc + Number(e.amount || 0), 0),
);
const selectionBalanced = computed(() => Math.abs(selectionTotal.value) < 0.005);
const selectionConsistent = computed(() => {
  const ccy = new Set(selected.value.map((e) => e.currency));
  return ccy.size <= 1;
});
const canForceMatch = computed(() =>
  selected.value.length >= 2 && selectionBalanced.value && selectionConsistent.value,
);

const openExclude = (item: ReconciliationEntry) => {
  excludeTarget.value = item;
  showExclude.value = true;
};

const openExcludeMultiple = () => {
  excludeTarget.value = null;
  showExclude.value = true;
};

const openUnexclude = (item: ReconciliationEntry) => {
  unexcludeTarget.value = item;
  showUnexclude.value = true;
};

// Items per page for the Table component
const itemsPerPage = 50;
const hasMoreToLoad = computed(() => entries.value.length < totalCount.value);

// Watch for page changes to auto-fetch when reaching end of loaded data
const currentPage = ref(1);
const totalPages = computed(() => Math.ceil(entries.value.length / itemsPerPage));

const onPageChange = (page: number) => {
  currentPage.value = page;
  // If user reaches the last page of currently loaded data and more exists
  const maxPageForLoaded = Math.ceil(entries.value.length / itemsPerPage);
  if (page >= maxPageForLoaded && hasMoreToLoad.value) {
    fetchMoreEntries();
  }
};

onMounted(async () => {
  // Read query params from dashboard / lot-view navigation
  const qFlowId = route.query.flow_id;
  const qStatus = route.query.status;
  const qRecoId = route.query.reco_id;
  if (qFlowId) {
    filters.value.flow_id = Number(qFlowId);
  }
  if (qStatus !== undefined) {
    filters.value.status = String(qStatus);
  }
  if (qRecoId) {
    filters.value.reco_id = String(qRecoId);
  }
  await Promise.all([fetchFlows(), fetchPaymentStatusChoices()]);
  await fetchEntries();
});
</script>

<template>
  <div class="flex justify-between items-center mb-6">
    <h1 class="text-2xl font-bold text-space-indigo">Operational view</h1>
  </div>

  <Card class="mb-4">
    <div class="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-7 gap-3">
      <Select v-model="filters.flow_id" label="Flow" :options="flowOptions" />
      <Select v-model="filters.status" label="Status" :options="statusOptions" />
      <Input v-model="filters.reco_id" label="Reco ID" />
      <Input v-model="filters.amount_min" type="number" step="0.01" min="0" label="Amount ≥" />
      <Input v-model="filters.amount_max" type="number" step="0.01" min="0" label="Amount ≤" />
      <Input v-model="filters.date_from" type="date" label="From" />
      <Input v-model="filters.date_to" type="date" label="To" />
    </div>
    <div v-if="paymentStatusChoices.length" class="mt-3">
      <span class="block text-xs font-medium text-gray-500 mb-1.5">Payment status</span>
      <div class="flex flex-wrap gap-1.5">
        <button
          v-for="st in paymentStatusChoices"
          :key="st"
          type="button"
          class="inline-flex items-center rounded-full border px-2.5 py-1 text-[11px] font-medium transition-colors"
          :class="
            filters.payment_statuses.includes(st)
              ? PAYMENT_STATUS_CLASSES[st] || 'bg-sky-50 text-sky-700 border-sky-200'
              : 'bg-white text-gray-500 border-gray-200 hover:border-gray-300'
          "
          @click="togglePaymentStatus(st)"
        >
          {{ st }}
        </button>
      </div>
    </div>
    <div class="flex justify-end mt-3">
      <Button variant="reveals-primary" @click="fetchEntries">Apply filters</Button>
    </div>
  </Card>

  <div v-if="loading" class="flex justify-center py-10"><Loader size="lg" /></div>

  <Card v-else class="pb-20">
    <div class="flex items-center justify-between mb-2">
      <span class="text-sm text-gray-500">
        Showing {{ entries.length }} of {{ totalCount }} entries
        <span v-if="loadingMore" class="ml-2 text-turquoise-surf">Loading more...</span>
      </span>
      <Button variant="reveals-secondary" :disabled="entries.length === 0" @click="exportExcel">
        <Download class="w-4 h-4 inline-block mr-1" /> Export Excel
      </Button>
    </div>
    <Table :columns="columns" :items="entries" searchable pagination :items-per-page="50" @page-change="onPageChange">
      <template #cell-select="{ item }">
        <input
          type="checkbox"
          :checked="isSelected(item.id)"
          :disabled="item.status !== 'pending'"
          @change="(e: any) => item.status === 'pending' && toggleSelect(item, e.target.checked)"
          class="disabled:opacity-30 disabled:cursor-not-allowed"
        />
      </template>
      <template #cell-flow_id="{ item }">{{ flowName(item.flow_id) }}</template>
      <template #cell-value_date="{ item }">{{ formatDateShort(item.value_date) }}</template>
      <template #cell-payment_statuses="{ item }">
        <span v-if="paymentStatusEntries(item).length" class="inline-flex flex-wrap gap-1">
          <span
            v-for="[st, count] in paymentStatusEntries(item)"
            :key="st"
            class="inline-flex items-center gap-0.5 rounded-full border px-1.5 py-0.5 text-[11px] font-medium tabular-nums whitespace-nowrap"
            :class="PAYMENT_STATUS_CLASSES[st] || 'bg-sky-50 text-sky-700 border-sky-200'"
            :title="`${count} payment(s) ${st}`"
          >
            {{ count }} {{ st }}
          </span>
        </span>
        <span v-else class="text-gray-300">—</span>
      </template>
      <template #cell-status="{ item }">
        <Badge :variant="statusVariant(item.status)">{{ item.status }}</Badge>
      </template>
      <template #cell-actions="{ item }">
        <Button
          v-if="item.status === 'pending'"
          size="sm"
          variant="danger"
          action="delete"
          @click="openExclude(item)"
        >Exclude</Button>
        <Button
          v-if="item.status === 'excluded'"
          size="sm"
          variant="reveals-primary"
          action="edit"
          @click="openUnexclude(item)"
        >Unexclude</Button>
      </template>
    </Table>
  </Card>

  <!-- Sticky action footer -->
  <div
    v-if="selected.length > 0"
    class="fixed bottom-0 left-0 right-0 bg-white border-t border-gray-200 shadow-lg p-4 z-40"
    :style="{ 'margin-left': 'var(--sidebar-width, 0)' }"
  >
    <div class="max-w-full mx-auto px-4 flex justify-between items-center gap-4">
      <div class="text-sm font-medium">
        {{ selected.length }} {{ selected.length === 1 ? 'entry' : 'entries' }} selected
        <span v-if="selected.length >= 2" class="ml-2 text-gray-600">Σ {{ formatAmount(selectionTotal, selected[0]?.currency) }}</span>
      </div>
      <div class="flex gap-2">
        <Button
          variant="reveals-primary"
          action="edit"
          :disabled="!canForceMatch"
          @click="showForce = true"
        >
          Force match
        </Button>
        <Button
          variant="danger"
          action="delete"
          @click="openExcludeMultiple"
        >
          Exclude all
        </Button>
        <Button
          variant="secondary"
          @click="selected = []"
        >
          Clear selection
        </Button>
      </div>
    </div>
  </div>

  <ForceMatchModal v-model="showForce" :entries="selected" @forced="fetchEntries" />
  <ExclusionModal v-model="showExclude" :entry="excludeTarget" :entries="selected.length > 0 && !excludeTarget ? selected : undefined" @excluded="() => { fetchEntries(); selected = []; }" />
  <UnexcludeModal v-model="showUnexclude" :entry="unexcludeTarget" @unexcluded="fetchEntries" />
</template>

<script setup lang="ts">
import { ref, onMounted, computed } from 'vue';
import { useRoute } from 'vue-router';
import { Download, ShoppingBasket } from 'lucide-vue-next';
import Card from '@/components/ui/Card.vue';
import Table, { type Column } from '@/components/ui/Table.vue';
import Select from '@/components/ui/Select.vue';
import Input from '@/components/ui/Input.vue';
import Button from '@/components/ui/Button.vue';
import Badge from '@/components/ui/Badge.vue';
import Loader from '@/components/ui/Loader.vue';
import { formatDateShort } from '@/utils/formatDate';
import { formatAmount } from '@/utils/formatAmount';
import { fromMinor, sumMinor } from '@/utils/decimal';
import flowService from '@/services/flowService';
import reconciliationService from '@/services/reconciliationService';
import modal from '@/utils/modal';
import toaster from '@/utils/toaster';
import { useSidebarStore } from '@/stores/sidebar';
import { useMatchBasketStore } from '@/stores/matchBasket';
import type { Flow, ReconciliationEntry } from '@/types';
import ForceMatchModal from './ForceMatchModal.vue';
import ExclusionModal from './ExclusionModal.vue';
import UnexcludeModal from './UnexcludeModal.vue';
import BasketDrawer from './BasketDrawer.vue';

const route = useRoute();
const sidebarStore = useSidebarStore();
const basket = useMatchBasketStore();
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
const showBasket = ref(false);

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
    // The selection is scoped to the current result set on purpose — what has
    // to survive a new search is the basket, not the ticks.
    selected.value = [];
    lastClickedId.value = null;
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

// Pull every remaining batch, so "select all" can cover the whole filtered set
// and not just the 200 rows already on screen.
const loadingAll = ref(false);
const loadAllEntries = async () => {
  if (loadingAll.value) return;
  loadingAll.value = true;
  try {
    while (entries.value.length < totalCount.value) {
      const before = entries.value.length;
      await fetchMoreEntries();
      if (entries.value.length === before) break; // a failed batch — don't spin
    }
  } finally {
    loadingAll.value = false;
  }
};
const confirmLoadAll = () => {
  const remaining = totalCount.value - entries.value.length;
  if (remaining <= 5000) {
    loadAllEntries();
    return;
  }
  modal.open({
    title: 'Charger toutes les lignes',
    message: `${remaining} lignes restent à charger (${Math.ceil(remaining / batchSize)} requêtes). L'affichage peut devenir lent. Continuer ?`,
    buttons: [
      { label: 'Annuler', variant: 'secondary', action: () => modal.close() },
      { label: 'Charger', variant: 'primary', action: () => { modal.close(); loadAllEntries(); } },
    ],
  });
};

// Export all rows matching the current filters (server-side, beyond the loaded
// batches). Cookie auth (withCredentials) → window.open carries the session.
const exportExcel = () => {
  window.open(reconciliationService.getExportUrl(filters.value), '_blank');
};

// ── Selection ───────────────────────────────────────────────────────
// Rows the Table actually shows, after its own search + column filters.
const visibleItems = ref<ReconciliationEntry[]>([]);
const onVisibleItems = (rows: Record<string, any>[]) => {
  visibleItems.value = rows as ReconciliationEntry[];
};
// Only pending rows can be forced or excluded.
const selectableVisible = computed(() => visibleItems.value.filter((i) => i.status === 'pending'));

// A Set, not a scan: isSelected runs once per rendered row, and "Tout charger"
// can put tens of thousands of rows behind the table's own filters.
const selectedIds = computed(() => new Set(selected.value.map((e) => e.id)));
const isSelected = (id: number) => selectedIds.value.has(id);

const setSelected = (rows: ReconciliationEntry[], checked: boolean) => {
  const byId = new Map(selected.value.map((e) => [e.id, e]));
  for (const r of rows) {
    if (checked) byId.set(r.id, r);
    else byId.delete(r.id);
  }
  selected.value = [...byId.values()];
};

const toggleSelect = (item: ReconciliationEntry, checked: boolean) => setSelected([item], checked);

// Anchor for shift-click range selection.
const lastClickedId = ref<number | null>(null);

const onRowCheckboxClick = (item: ReconciliationEntry, event: MouseEvent) => {
  if (item.status !== 'pending') return;
  const checked = (event.target as HTMLInputElement).checked;
  if (event.shiftKey && lastClickedId.value !== null && lastClickedId.value !== item.id) {
    const rows = selectableVisible.value;
    const from = rows.findIndex((r) => r.id === lastClickedId.value);
    const to = rows.findIndex((r) => r.id === item.id);
    if (from !== -1 && to !== -1) {
      const [a, b] = from <= to ? [from, to] : [to, from];
      setSelected(rows.slice(a, b + 1), checked);
      lastClickedId.value = item.id;
      return;
    }
  }
  toggleSelect(item, checked);
  lastClickedId.value = item.id;
};

const allVisibleSelected = computed(
  () => selectableVisible.value.length > 0 && selectableVisible.value.every((r) => isSelected(r.id)),
);
const someVisibleSelected = computed(() => selectableVisible.value.some((r) => isSelected(r.id)));

const toggleSelectAll = (checked: boolean) => {
  setSelected(selectableVisible.value, checked);
  lastClickedId.value = null;
};

// Exact totals, in minor units — a float tolerance would disagree with the
// backend's `sum == Decimal("0")` and green-light a group it then rejects.
const selectionTotalMinor = computed(() => sumMinor(selected.value.map((e) => e.amount)));
const selectionTotal = computed(() => fromMinor(selectionTotalMinor.value));
const selectionBalanced = computed(() => selectionTotalMinor.value === 0);
const selectionConsistent = computed(() => {
  const ccy = new Set(selected.value.map((e) => e.currency));
  return ccy.size <= 1;
});
const canForceMatch = computed(() =>
  selected.value.length >= 2 && selectionBalanced.value && selectionConsistent.value,
);

// ── Basket ──────────────────────────────────────────────────────────
const basketTotal = computed(() => fromMinor(basket.totalMinor));

const addSelectionToBasket = () => {
  const r = basket.addMany(selected.value);
  const skipped: string[] = [];
  if (r.alreadyIn) skipped.push(`${r.alreadyIn} déjà dans le panier`);
  if (r.otherFlow) skipped.push(`${r.otherFlow} d'un autre flux/devise`);
  if (r.notPending) skipped.push(`${r.notPending} non « pending »`);
  if (r.overflow) skipped.push(`${r.overflow} au-delà de la limite du panier`);

  if (r.added === 0) {
    toaster.warning(skipped.length ? `Aucune ligne ajoutée — ${skipped.join(', ')}` : 'Aucune ligne ajoutée');
  } else if (skipped.length) {
    toaster.warning(`${r.added} ligne(s) ajoutée(s) — ignorées : ${skipped.join(', ')}`);
  } else {
    toaster.success(`${r.added} ligne(s) ajoutée(s) au panier`);
  }
  // Clear so the next search starts from a clean slate — the basket keeps them.
  selected.value = [];
  lastClickedId.value = null;
};

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
    <Button variant="reveals-secondary" @click="showBasket = true">
      <ShoppingBasket class="w-4 h-4 inline-block mr-1" />
      Panier<span v-if="basket.count"> ({{ basket.count }})</span>
    </Button>
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

  <Card v-else class="pb-28">
    <div class="flex items-center justify-between mb-2">
      <span class="text-sm text-gray-500">
        Showing {{ entries.length }} of {{ totalCount }} entries
        <span v-if="loadingMore || loadingAll" class="ml-2 text-turquoise-surf">Loading more...</span>
        <button
          v-else-if="hasMoreToLoad"
          type="button"
          class="ml-2 font-medium text-turquoise-surf underline"
          @click="confirmLoadAll"
        >
          Tout charger
        </button>
      </span>
      <Button variant="reveals-secondary" :disabled="entries.length === 0" @click="exportExcel">
        <Download class="w-4 h-4 inline-block mr-1" /> Export Excel
      </Button>
    </div>
    <Table
      :columns="columns"
      :items="entries"
      searchable
      pagination
      :items-per-page="50"
      @page-change="onPageChange"
      @visible-items="onVisibleItems"
    >
      <template #header-select>
        <input
          type="checkbox"
          :checked="allVisibleSelected"
          :indeterminate.prop="someVisibleSelected && !allVisibleSelected"
          :disabled="selectableVisible.length === 0"
          :title="`Sélectionner les ${selectableVisible.length} ligne(s) pending affichées`"
          class="disabled:opacity-30 disabled:cursor-not-allowed"
          @click.stop
          @change="(e: any) => toggleSelectAll(e.target.checked)"
        />
      </template>
      <template #cell-select="{ item }">
        <span class="inline-flex items-center gap-1">
          <input
            type="checkbox"
            :checked="isSelected(item.id)"
            :disabled="item.status !== 'pending'"
            title="Maj+clic pour sélectionner une plage"
            @click="onRowCheckboxClick(item as ReconciliationEntry, $event)"
            class="disabled:opacity-30 disabled:cursor-not-allowed"
          />
          <ShoppingBasket
            v-if="basket.has(item.id)"
            class="w-3.5 h-3.5 text-turquoise-surf"
            aria-label="Déjà dans le panier"
          />
        </span>
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

  <!-- Sticky action footer: basket state on top, current selection below -->
  <div
    v-if="basket.count > 0 || selected.length > 0"
    class="fixed bottom-0 left-0 right-0 bg-white border-t border-gray-200 shadow-lg z-40 transition-all duration-300"
    :class="sidebarStore.isOpen ? 'ml-64' : 'ml-20'"
  >
    <!-- Basket -->
    <div
      v-if="basket.count > 0"
      class="max-w-full mx-auto px-8 py-2.5 flex flex-wrap justify-between items-center gap-4 bg-lavender-blush/40"
    >
      <div class="text-sm font-medium text-space-indigo">
        Panier «&nbsp;{{ basket.active?.name }}&nbsp;» · {{ basket.count }} ligne(s)
        <span class="ml-2 text-gray-600 tabular-nums">
          Σ {{ formatAmount(basketTotal, basket.active?.currency) }}
        </span>
        <Badge class="ml-2" :variant="basket.isBalanced ? 'success' : 'warning'">
          {{ basket.isBalanced ? 'Équilibré' : 'Écart' }}
        </Badge>
        <span v-if="basket.staleItems.length" class="ml-2 text-xs font-medium text-amber-700">
          {{ basket.staleItems.length }} ligne(s) périmée(s)
        </span>
      </div>
      <div class="flex gap-2">
        <Button variant="secondary" @click="showBasket = true">Voir le panier</Button>
        <Button variant="secondary" @click="basket.clear()">Vider</Button>
      </div>
    </div>

    <!-- Current selection -->
    <div
      v-if="selected.length > 0"
      class="max-w-full mx-auto px-8 py-3 flex flex-wrap justify-between items-center gap-4"
    >
      <div class="text-sm font-medium">
        {{ selected.length }} {{ selected.length === 1 ? 'entry' : 'entries' }} selected
        <span v-if="selected.length >= 2" class="ml-2 text-gray-600 tabular-nums">Σ {{ formatAmount(selectionTotal, selected[0]?.currency) }}</span>
      </div>
      <div class="flex gap-2">
        <Button variant="reveals-primary" @click="addSelectionToBasket">
          <ShoppingBasket class="w-4 h-4 inline-block mr-1" /> Ajouter au panier
        </Button>
        <!-- Hidden while a basket is open: the basket is then the only authority
             on what gets forced, so there is never a choice of two Force buttons. -->
        <Button
          v-if="basket.count === 0"
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
  <BasketDrawer v-model="showBasket" :flows="flows" @forced="fetchEntries" />
</template>

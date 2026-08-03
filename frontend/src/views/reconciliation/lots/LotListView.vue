<script setup lang="ts">
import { computed, onMounted, ref } from 'vue';
import { useRouter } from 'vue-router';
import { AlertTriangle } from 'lucide-vue-next';
import Card from '@/components/ui/Card.vue';
import Input from '@/components/ui/Input.vue';
import Select from '@/components/ui/Select.vue';
import Button from '@/components/ui/Button.vue';
import Table, { type Column } from '@/components/ui/Table.vue';
import Badge from '@/components/ui/Badge.vue';
import Loader from '@/components/ui/Loader.vue';
import { formatDateShort } from '@/utils/formatDate';
import flowService from '@/services/flowService';
import lotService, { type LotFilters } from '@/services/lotService';
import toaster from '@/utils/toaster';
import type { Flow, LotSummary } from '@/types';

const router = useRouter();

const loading = ref(false);
const flows = ref<Flow[]>([]);
const lots = ref<LotSummary[]>([]);
const totalCount = ref(0);
const batchSize = 200;
const currentOffset = ref(0);
const loadingMore = ref(false);
const itemsPerPage = 25;

const filters = ref({
  flow_id: undefined as number | undefined,
  status: '',
  balanced: '',
  date_from: '',
  date_to: '',
  search: '',
});

const statusOptions = [
  { value: '', label: 'All' },
  { value: 'pending', label: 'Pending' },
  { value: 'matched', label: 'Matched' },
  { value: 'merged', label: 'Merged' },
];

const balancedOptions = [
  { value: '', label: 'All' },
  { value: 'true', label: 'Balanced' },
  { value: 'false', label: 'Unbalanced' },
];

const flowOptions = computed(() => [
  { value: '', label: 'All flows' },
  ...flows.value.map((f) => ({ value: String(f.id), label: f.name })),
]);

const columns: Column[] = [
  { key: 'lot_id', label: 'Lot' },
  { key: 'flow_id', label: 'Flow' },
  { key: 'member_count', label: 'Movements', align: 'right' },
  { key: 'pending_count', label: 'Pending', align: 'right' },
  { key: 'net_amount', label: 'Net', sortable: true, format: 'amount', align: 'right' },
  { key: 'currency', label: 'Ccy' },
  { key: 'first_value_date', label: 'First date', sortable: true },
  { key: 'last_value_date', label: 'Last date', sortable: true },
  { key: 'is_balanced', label: 'Balance' },
  { key: 'status', label: 'Status' },
];

const statusVariant = (s: string): 'success' | 'warning' | 'info' =>
  s === 'matched' ? 'success' : s === 'merged' ? 'info' : 'warning';

const flowName = (id: number) => flows.value.find((f) => f.id === id)?.code || `#${id}`;

const buildParams = (skip: number): LotFilters => {
  const params: LotFilters = { limit: batchSize, skip };
  if (filters.value.flow_id) params.flow_id = Number(filters.value.flow_id);
  if (filters.value.status) params.status = filters.value.status;
  if (filters.value.balanced) params.balanced = filters.value.balanced === 'true';
  if (filters.value.date_from) params.date_from = filters.value.date_from;
  if (filters.value.date_to) params.date_to = filters.value.date_to;
  if (filters.value.search.trim()) params.search = filters.value.search.trim();
  return params;
};

const fetchLots = async () => {
  loading.value = true;
  currentOffset.value = 0;
  try {
    const { data } = await lotService.list(buildParams(0));
    lots.value = data.items;
    totalCount.value = data.total_count;
    currentOffset.value = batchSize;
  } catch (e) {
    toaster.error('Failed to load lots');
  } finally {
    loading.value = false;
  }
};

const hasMoreToLoad = computed(() => lots.value.length < totalCount.value);

const fetchMoreLots = async () => {
  if (loadingMore.value || !hasMoreToLoad.value) return;
  loadingMore.value = true;
  try {
    const { data } = await lotService.list(buildParams(currentOffset.value));
    lots.value = [...lots.value, ...data.items];
    currentOffset.value += batchSize;
  } catch (e) {
    toaster.error('Failed to load more lots');
  } finally {
    loadingMore.value = false;
  }
};

const onPageChange = (page: number) => {
  const maxPageForLoaded = Math.ceil(lots.value.length / itemsPerPage);
  if (page >= maxPageForLoaded && hasMoreToLoad.value) {
    fetchMoreLots();
  }
};

const openLot = (item: Record<string, any>) => {
  router.push(`/reconciliation/lots/${item.lot_id}`);
};

const fetchFlows = async () => {
  const { data } = await flowService.getAll();
  flows.value = data;
};

onMounted(async () => {
  await fetchFlows();
  await fetchLots();
});
</script>

<template>
  <div class="flex justify-between items-center mb-6">
    <h1 class="text-2xl font-bold text-space-indigo">Lots</h1>
    <span class="text-sm text-gray-500">{{ totalCount }} lot(s)</span>
  </div>

  <Card class="mb-4">
    <div class="grid grid-cols-2 md:grid-cols-6 gap-3">
      <Select v-model="filters.flow_id" label="Flow" :options="flowOptions" />
      <Select v-model="filters.status" label="Status" :options="statusOptions" />
      <Select v-model="filters.balanced" label="Balance" :options="balancedOptions" />
      <Input v-model="filters.date_from" type="date" label="From" />
      <Input v-model="filters.date_to" type="date" label="To" />
      <Input v-model="filters.search" label="Lot uuid / key value" @keyup.enter="fetchLots" />
    </div>
    <div class="flex justify-end mt-3">
      <Button variant="reveals-primary" @click="fetchLots">Apply filters</Button>
    </div>
  </Card>

  <div v-if="loading" class="flex justify-center py-10"><Loader size="lg" /></div>

  <Card v-else>
    <Table
      :columns="columns"
      :items="lots"
      :pagination="true"
      :items-per-page="itemsPerPage"
      storage-key="lots-list"
      clickable-rows
      @row-click="openLot"
      @page-change="onPageChange"
    >
      <template #cell-lot_id="{ item }">
        <span class="inline-flex items-center gap-1.5">
          <span class="font-mono text-xs" :title="item.lot_id">{{ item.lot_id.slice(0, 8) }}…</span>
          <AlertTriangle
            v-if="item.merge_conflict"
            class="h-3.5 w-3.5 text-amber-500"
            title="Merged while some entries were already reconciled under the absorbed lot"
          />
        </span>
      </template>
      <template #cell-flow_id="{ item }">{{ flowName(item.flow_id) }}</template>
      <template #cell-first_value_date="{ item }">
        {{ item.first_value_date ? formatDateShort(item.first_value_date) : '—' }}
      </template>
      <template #cell-last_value_date="{ item }">
        {{ item.last_value_date ? formatDateShort(item.last_value_date) : '—' }}
      </template>
      <template #cell-is_balanced="{ item }">
        <Badge :variant="item.is_balanced ? 'success' : 'warning'">
          {{ item.is_balanced ? 'Balanced' : 'Unbalanced' }}
        </Badge>
      </template>
      <template #cell-status="{ item }">
        <Badge :variant="statusVariant(item.status)">{{ item.status }}</Badge>
      </template>
    </Table>
  </Card>
</template>

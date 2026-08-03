<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue';
import { X } from 'lucide-vue-next';
import Card from '@/components/ui/Card.vue';
import Input from '@/components/ui/Input.vue';
import Select from '@/components/ui/Select.vue';
import Button from '@/components/ui/Button.vue';
import Table, { type Column } from '@/components/ui/Table.vue';
import Badge from '@/components/ui/Badge.vue';
import Loader from '@/components/ui/Loader.vue';
import { formatAmount } from '@/utils/formatAmount';
import { formatDateShort } from '@/utils/formatDate';
import lotService, { type LotMemberFilters } from '@/services/lotService';
import toaster from '@/utils/toaster';
import type { LotMember } from '@/types';

const props = defineProps<{
  lotId: string;
  typeCounts?: Record<string, number>;
  // Graph-driven filters (click a group/key node) — owned by the parent.
  filterType?: string | null;
  filterDirection?: string | null;
  filterKey?: { key_type: string; key_value: string } | null;
}>();

const emit = defineEmits<{
  (e: 'update:filterType', value: string | null): void;
  (e: 'update:filterDirection', value: string | null): void;
  (e: 'update:filterKey', value: { key_type: string; key_value: string } | null): void;
}>();

const loading = ref(false);
const loadingMore = ref(false);
const members = ref<LotMember[]>([]);
const totalCount = ref(0);
const batchSize = 200;
const currentOffset = ref(0);
const itemsPerPage = 25;

const search = ref('');
const status = ref('');

const statusOptions = [
  { value: '', label: 'All statuses' },
  { value: 'pending', label: 'Pending' },
  { value: 'matched', label: 'Matched' },
  { value: 'forced', label: 'Forced' },
  { value: 'excluded', label: 'Excluded' },
];

const typeOptions = computed(() => [
  { value: '', label: 'All types' },
  ...Object.entries(props.typeCounts ?? {})
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([type, count]) => ({ value: type, label: `${type} (${count.toLocaleString()})` })),
]);

const typeModel = computed({
  get: () => props.filterType ?? '',
  set: (v: string) => emit('update:filterType', v || null),
});

const columns: Column[] = [
  { key: 'movement_type', label: 'Type' },
  { key: 'amount', label: 'Amount', align: 'right' },
  { key: 'value_date', label: 'Value date' },
  { key: 'account', label: 'Account' },
  { key: 'reference', label: 'Reference' },
  { key: 'entry_status', label: 'Status' },
];

const statusVariant = (s?: string | null): 'success' | 'warning' | 'danger' | 'default' =>
  s === 'matched' || s === 'forced'
    ? 'success'
    : s === 'pending'
      ? 'warning'
      : s === 'excluded'
        ? 'danger'
        : 'default';

// Loose shape: Table slots expose rows as Record<string, any>.
const reference = (m: Record<string, any>) => m.ref_no || m.external_ref || m.remarks_1 || '—';

const buildParams = (skip: number): LotMemberFilters => {
  const params: LotMemberFilters = { limit: batchSize, skip };
  if (props.filterType) params.movement_type = props.filterType;
  if (props.filterDirection) params.direction = props.filterDirection;
  if (props.filterKey) {
    params.key_type = props.filterKey.key_type;
    params.key_value = props.filterKey.key_value;
  }
  if (status.value) params.entry_status = status.value;
  if (search.value.trim()) params.search = search.value.trim();
  return params;
};

const fetchMembers = async () => {
  loading.value = true;
  currentOffset.value = 0;
  try {
    const { data } = await lotService.getMembers(props.lotId, buildParams(0));
    members.value = data.items;
    totalCount.value = data.total_count;
    currentOffset.value = batchSize;
  } catch (e) {
    toaster.error('Failed to load lot members');
  } finally {
    loading.value = false;
  }
};

const hasMoreToLoad = computed(() => members.value.length < totalCount.value);

const fetchMore = async () => {
  if (loadingMore.value || !hasMoreToLoad.value) return;
  loadingMore.value = true;
  try {
    const { data } = await lotService.getMembers(props.lotId, buildParams(currentOffset.value));
    members.value = [...members.value, ...data.items];
    currentOffset.value += batchSize;
  } catch (e) {
    toaster.error('Failed to load more members');
  } finally {
    loadingMore.value = false;
  }
};

const onPageChange = (page: number) => {
  const maxPageForLoaded = Math.ceil(members.value.length / itemsPerPage);
  if (page >= maxPageForLoaded && hasMoreToLoad.value) {
    fetchMore();
  }
};

watch(
  () => [props.lotId, props.filterType, props.filterDirection, props.filterKey] as const,
  () => fetchMembers()
);
onMounted(fetchMembers);
</script>

<template>
  <Card>
    <div class="mb-3 flex flex-wrap items-end gap-3">
      <h2 class="text-lg font-semibold text-space-indigo mr-auto">
        Movements
        <span class="text-sm font-normal text-gray-500 tabular-nums">
          ({{ totalCount.toLocaleString() }})
        </span>
      </h2>
      <span
        v-if="filterKey"
        class="inline-flex items-center gap-1.5 rounded-full bg-indigo-50 border border-indigo-200 px-2.5 py-1 text-xs text-indigo-800"
      >
        <span class="font-semibold">{{ filterKey.key_type }}</span>
        <span class="font-mono truncate max-w-[220px]" :title="filterKey.key_value">
          {{ filterKey.key_value }}
        </span>
        <button
          class="text-indigo-400 hover:text-indigo-700"
          title="Clear key filter"
          @click="emit('update:filterKey', null)"
        >
          <X class="h-3.5 w-3.5" />
        </button>
      </span>
      <Select v-model="typeModel" :options="typeOptions" class="w-44" />
      <Select v-model="status" :options="statusOptions" class="w-40" @update:model-value="fetchMembers" />
      <Input
        v-model="search"
        placeholder="Reference / particulars…"
        class="w-56"
        @keyup.enter="fetchMembers"
      />
      <Button variant="secondary" size="sm" @click="fetchMembers">Apply</Button>
    </div>

    <div v-if="loading" class="flex justify-center py-8"><Loader size="lg" /></div>

    <Table
      v-else
      :columns="columns"
      :items="members"
      :pagination="true"
      :items-per-page="itemsPerPage"
      storage-key="lot-members"
      @page-change="onPageChange"
    >
      <template #cell-movement_type="{ item }">
        <Badge variant="default">{{ item.movement_type }}</Badge>
      </template>
      <template #cell-amount="{ item }">
        <span
          class="tabular-nums font-medium"
          :class="Number(item.amount) < 0 ? 'text-red-600' : 'text-green-600'"
        >
          {{ formatAmount(item.amount, item.currency) }}
        </span>
      </template>
      <template #cell-value_date="{ item }">{{ formatDateShort(item.value_date) }}</template>
      <template #cell-reference="{ item }">
        <span class="font-mono text-xs truncate max-w-[220px] inline-block" :title="reference(item)">
          {{ reference(item) }}
        </span>
      </template>
      <template #cell-entry_status="{ item }">
        <Badge :variant="statusVariant(item.entry_status)">
          {{ item.entry_status || 'not ingested' }}
        </Badge>
      </template>
    </Table>
  </Card>
</template>

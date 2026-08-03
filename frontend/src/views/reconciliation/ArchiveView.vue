<script setup lang="ts">
import { ref, computed } from 'vue';
import { Download, Camera, ClipboardList } from 'lucide-vue-next';
import Card from '@/components/ui/Card.vue';
import Table, { type Column } from '@/components/ui/Table.vue';
import Select from '@/components/ui/Select.vue';
import Input from '@/components/ui/Input.vue';
import Button from '@/components/ui/Button.vue';
import Badge from '@/components/ui/Badge.vue';
import Loader from '@/components/ui/Loader.vue';
import { formatDate, formatDateShort } from '@/utils/formatDate';
import flowService from '@/services/flowService';
import archiveService from '@/services/archiveService';
import toaster from '@/utils/toaster';
import type { Flow, SnapshotEntry, SnapshotSummary, DailyActivityEntry } from '@/types';

const loading = ref(false);
const loaded = ref(false);
const flows = ref<Flow[]>([]);
const activeTab = ref<'snapshot' | 'activity'>('snapshot');

const snapshotDate = ref('');
const selectedFlowId = ref<number | undefined>(undefined);

const snapshotItems = ref<SnapshotEntry[]>([]);
const summary = ref<SnapshotSummary | null>(null);
const activityItems = ref<DailyActivityEntry[]>([]);

const flowOptions = computed(() => [
  { value: '', label: 'All flows' },
  ...flows.value.map((f) => ({ value: String(f.id), label: f.name })),
]);

const snapshotColumns: Column[] = [
  { key: 'reco_id', label: 'Reco ID', sortable: true },
  { key: 'account', label: 'Account' },
  { key: 'amount', label: 'Amount', sortable: true, format: 'amount', align: 'right' },
  { key: 'currency', label: 'Ccy' },
  { key: 'value_date', label: 'Value date', sortable: true },
  { key: 'event_type', label: 'Event' },
  { key: 'status_at_snapshot', label: 'Status' },
  { key: 'exclusion_reason', label: 'Excl. reason' },
];

const activityColumns = [
  { key: 'ts', label: 'Time', sortable: true },
  { key: 'user_name', label: 'User' },
  { key: 'action', label: 'Action' },
  { key: 'target_type', label: 'Target type' },
  { key: 'target_id', label: 'Target ID' },
  { key: 'details', label: 'Details' },
];

const statusVariant = (s: string): 'success' | 'warning' | 'secondary' | 'danger' => {
  const map: Record<string, 'success' | 'warning' | 'secondary' | 'danger'> = {
    MATCHED: 'success',
    FORCED: 'success',
    PENDING: 'warning',
    EXCLUDED: 'secondary',
  };
  return map[s?.toUpperCase()] || 'secondary';
};

const actionLabel = (action: string): string => {
  const map: Record<string, string> = {
    'entry.exclude': 'Exclusion',
    'entry.unexclude': 'Unexclude',
    'match_group.force': 'Force match',
  };
  return map[action] || action;
};

const formatDetails = (details: Record<string, any> | null | undefined): string => {
  if (!details) return '—';
  const parts: string[] = [];
  if (details.reason) parts.push(`Reason: ${details.reason}`);
  if (details.reco_id) parts.push(`Reco: ${details.reco_id}`);
  if (details.entry_ids) parts.push(`Entries: ${details.entry_ids.join(', ')}`);
  return parts.length > 0 ? parts.join(' | ') : JSON.stringify(details);
};

const loadData = async () => {
  if (!snapshotDate.value) {
    toaster.error('Please select a date');
    return;
  }
  loading.value = true;
  loaded.value = false;
  try {
    const params: any = { date: snapshotDate.value };
    if (selectedFlowId.value) params.flow_id = selectedFlowId.value;

    const [snapRes, actRes] = await Promise.all([
      archiveService.getSnapshot(params),
      archiveService.getActivity(params),
    ]);
    snapshotItems.value = snapRes.data.items;
    summary.value = snapRes.data.summary;
    activityItems.value = actRes.data.actions;
    loaded.value = true;
  } catch (e: any) {
    toaster.error(e?.response?.data?.detail || 'Failed to load archive data');
  } finally {
    loading.value = false;
  }
};

const exportExcel = () => {
  if (!snapshotDate.value) return;
  const url = archiveService.getExportUrl(snapshotDate.value, selectedFlowId.value);
  window.open(url, '_blank');
};

// Load flows on mount
import { onMounted } from 'vue';
onMounted(async () => {
  try {
    const { data } = await flowService.getAll();
    flows.value = data;
  } catch {
    toaster.error('Failed to load flows');
  }
});
</script>

<template>
  <div class="flex justify-between items-center mb-6">
    <h1 class="text-2xl font-bold text-space-indigo">Archive</h1>
  </div>

  <!-- Filters -->
  <Card class="mb-4">
    <div class="grid grid-cols-1 md:grid-cols-4 gap-3 items-end">
      <Input v-model="snapshotDate" type="date" label="Date" required />
      <Select v-model="selectedFlowId" label="Flow" :options="flowOptions" />
      <Button variant="reveals-primary" @click="loadData" :disabled="loading">
        {{ loading ? 'Loading...' : 'Load' }}
      </Button>
      <Button
        v-if="loaded"
        variant="reveals-secondary"
        @click="exportExcel"
      >
        <Download class="w-4 h-4 inline-block mr-1" /> Export Excel
      </Button>
    </div>
  </Card>

  <!-- Loading -->
  <div v-if="loading" class="flex justify-center py-10"><Loader size="lg" /></div>

  <!-- Results -->
  <div v-else-if="loaded && summary">
    <!-- Summary cards -->
    <div class="grid grid-cols-2 md:grid-cols-5 gap-3 mb-4">
      <Card class="text-center">
        <div class="text-sm text-gray-500">Total</div>
        <div class="text-2xl font-bold text-space-indigo">{{ summary.total_entries }}</div>
      </Card>
      <Card class="text-center">
        <div class="text-sm text-gray-500">Pending</div>
        <div class="text-2xl font-bold text-amber-500">{{ summary.pending_count }}</div>
      </Card>
      <Card class="text-center">
        <div class="text-sm text-gray-500">Matched</div>
        <div class="text-2xl font-bold text-green-600">{{ summary.matched_count }}</div>
      </Card>
      <Card class="text-center">
        <div class="text-sm text-gray-500">Forced</div>
        <div class="text-2xl font-bold text-blue-600">{{ summary.forced_count }}</div>
      </Card>
      <Card class="text-center">
        <div class="text-sm text-gray-500">Excluded</div>
        <div class="text-2xl font-bold text-gray-500">{{ summary.excluded_count }}</div>
      </Card>
    </div>

    <!-- Tabs -->
    <div class="flex gap-2 mb-4">
      <Button
        :variant="activeTab === 'snapshot' ? 'reveals-primary' : 'reveals-ghost'"
        @click="activeTab = 'snapshot'"
      >
        <Camera class="w-4 h-4 inline-block mr-1" /> Snapshot ({{ snapshotItems.length }})
      </Button>
      <Button
        :variant="activeTab === 'activity' ? 'reveals-primary' : 'reveals-ghost'"
        @click="activeTab = 'activity'"
      >
        <ClipboardList class="w-4 h-4 inline-block mr-1" /> Activity ({{ activityItems.length }})
      </Button>
    </div>

    <!-- Snapshot tab -->
    <Card v-if="activeTab === 'snapshot'">
      <Table
        :columns="snapshotColumns"
        :items="snapshotItems"
        searchable
        pagination
        :items-per-page="50"
      >
        <template #cell-value_date="{ item }">{{ formatDateShort(item.value_date) }}</template>
        <template #cell-status_at_snapshot="{ item }">
          <Badge :variant="statusVariant(item.status_at_snapshot)">{{ item.status_at_snapshot }}</Badge>
        </template>
        <template #cell-exclusion_reason="{ item }">
          <span class="text-sm text-gray-500">{{ item.exclusion_reason || '—' }}</span>
        </template>
      </Table>
    </Card>

    <!-- Activity tab -->
    <Card v-if="activeTab === 'activity'">
      <Table
        :columns="activityColumns"
        :items="activityItems"
        searchable
        pagination
        :items-per-page="25"
      >
        <template #cell-ts="{ item }">{{ formatDate(item.ts) }}</template>
        <template #cell-action="{ item }">
          <Badge variant="primary">{{ actionLabel(item.action) }}</Badge>
        </template>
        <template #cell-details="{ item }">
          <span class="text-sm text-gray-500">{{ formatDetails(item.details) }}</span>
        </template>
      </Table>
    </Card>
  </div>

  <!-- Empty state -->
  <Card v-else-if="!loading && !loaded" class="text-center py-10">
    <p class="text-gray-500">Select a date and click <strong>Load</strong> to see the archive snapshot.</p>
  </Card>
</template>

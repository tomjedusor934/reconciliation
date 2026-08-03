<script setup lang="ts">
import { ref, onMounted } from 'vue';
import Card from '@/components/ui/Card.vue';
import Table from '@/components/ui/Table.vue';
import Badge from '@/components/ui/Badge.vue';
import Loader from '@/components/ui/Loader.vue';
import { formatDate } from '@/utils/formatDate';
import runService from '@/services/runService';
import type { IngestionRun, ReconciliationRun } from '@/types';

const loading = ref(true);
const ingestionRuns = ref<IngestionRun[]>([]);
const recoRuns = ref<ReconciliationRun[]>([]);

const ingCols = [
  { key: 'flow_id', label: 'Flow' },
  { key: 'source_file', label: 'Source' },
  { key: 'started_at', label: 'Started' },
  { key: 'finished_at', label: 'Finished' },
  { key: 'status', label: 'Status' },
  { key: 'rows_in', label: 'In' },
  { key: 'rows_ok', label: 'OK' },
  { key: 'rows_ko', label: 'KO' },
  { key: 'rows_duplicate', label: 'Dup' },
];

const recoCols = [
  { key: 'started_at', label: 'Started' },
  { key: 'finished_at', label: 'Finished' },
  { key: 'entries_scanned', label: 'Scanned' },
  { key: 'groups_created', label: 'Groups' },
  { key: 'entries_matched', label: 'Matched' },
  { key: 'duration_ms', label: 'Duration (ms)' },
  { key: 'triggered_by', label: 'Triggered by' },
];

const variant = (s: string): 'success' | 'warning' | 'danger' | 'secondary' =>
  ({ success: 'success', partial: 'warning', failed: 'danger', running: 'secondary', pending: 'secondary' } as const)[s] || 'secondary';

onMounted(async () => {
  loading.value = true;
  try {
    const [a, b] = await Promise.all([
      runService.ingestionRuns({ limit: 100 }),
      runService.reconciliationRuns({ limit: 50 }),
    ]);
    ingestionRuns.value = a.data;
    recoRuns.value = b.data;
  } finally {
    loading.value = false;
  }
});
</script>

<template>
  <div class="flex justify-between items-center mb-6">
    <h1 class="text-2xl font-bold text-space-indigo">Runs</h1>
  </div>

  <div v-if="loading" class="flex justify-center py-10"><Loader size="lg" /></div>

  <div v-else class="space-y-6">
    <Card>
      <h2 class="font-semibold mb-3">Ingestion runs</h2>
      <Table :columns="ingCols" :items="ingestionRuns" searchable pagination :items-per-page="25">
        <template #cell-started_at="{ item }">{{ formatDate(item.started_at) }}</template>
        <template #cell-finished_at="{ item }">{{ formatDate(item.finished_at) }}</template>
        <template #cell-status="{ item }">
          <Badge :variant="variant(item.status)">{{ item.status }}</Badge>
        </template>
      </Table>
    </Card>
    <Card>
      <h2 class="font-semibold mb-3">Reconciliation runs</h2>
      <Table :columns="recoCols" :items="recoRuns" pagination :items-per-page="25">
        <template #cell-started_at="{ item }">{{ formatDate(item.started_at) }}</template>
        <template #cell-finished_at="{ item }">{{ formatDate(item.finished_at) }}</template>
      </Table>
    </Card>
  </div>
</template>

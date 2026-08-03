<script setup lang="ts">
import { ref, onMounted } from 'vue';
import Card from '@/components/ui/Card.vue';
import Table from '@/components/ui/Table.vue';
import Loader from '@/components/ui/Loader.vue';
import { formatDate } from '@/utils/formatDate';
import auditService from '@/services/auditService';
import type { AuditLog, UIActionLog } from '@/types';

const loading = ref(true);
const dataLogs = ref<AuditLog[]>([]);
const uiLogs = ref<UIActionLog[]>([]);

const dataCols = [
  { key: 'ts', label: 'Timestamp', sortable: true },
  { key: 'table_name', label: 'Table' },
  { key: 'row_pk', label: 'Row PK' },
  { key: 'op', label: 'Op' },
  { key: 'user_id', label: 'User' },
];
const uiCols = [
  { key: 'ts', label: 'Timestamp', sortable: true },
  { key: 'user_id', label: 'User' },
  { key: 'action', label: 'Action' },
  { key: 'target_type', label: 'Target type' },
  { key: 'target_id', label: 'Target id' },
];

onMounted(async () => {
  loading.value = true;
  try {
    const [a, b] = await Promise.all([
      auditService.data({ limit: 200 }),
      auditService.uiActions({ limit: 200 }),
    ]);
    dataLogs.value = a.data;
    uiLogs.value = b.data;
  } finally {
    loading.value = false;
  }
});
</script>

<template>
  <div class="flex justify-between items-center mb-6">
    <h1 class="text-2xl font-bold text-space-indigo">Audit</h1>
  </div>

  <div v-if="loading" class="flex justify-center py-10"><Loader size="lg" /></div>

  <div v-else class="space-y-6">
    <Card>
      <h2 class="font-semibold mb-3">UI actions</h2>
      <Table :columns="uiCols" :items="uiLogs" searchable pagination :items-per-page="25">
        <template #cell-ts="{ item }">{{ formatDate(item.ts) }}</template>
      </Table>
    </Card>
    <Card>
      <h2 class="font-semibold mb-3">Data audit (DB triggers)</h2>
      <Table :columns="dataCols" :items="dataLogs" searchable pagination :items-per-page="25">
        <template #cell-ts="{ item }">{{ formatDate(item.ts) }}</template>
      </Table>
    </Card>
  </div>
</template>

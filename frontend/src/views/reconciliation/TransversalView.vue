<script setup lang="ts">
import { ref } from 'vue';
import Card from '@/components/ui/Card.vue';
import Input from '@/components/ui/Input.vue';
import Button from '@/components/ui/Button.vue';
import Table, { type Column } from '@/components/ui/Table.vue';
import Badge from '@/components/ui/Badge.vue';
import Loader from '@/components/ui/Loader.vue';
import { formatDateShort } from '@/utils/formatDate';
import { formatAmount } from '@/utils/formatAmount';
import dashboardService from '@/services/dashboardService';
import toaster from '@/utils/toaster';
import type { TransversalGroup } from '@/types';

const loading = ref(false);
const recoId = ref('');
const currency = ref('');
const groups = ref<TransversalGroup[]>([]);

const columns: Column[] = [
  { key: 'flow_id', label: 'Flow' },
  { key: 'account', label: 'Account' },
  { key: 'amount', label: 'Amount', format: 'amount', align: 'right' },
  { key: 'currency', label: 'Ccy' },
  { key: 'ref_no', label: 'Ref no' },
  { key: 'transaction_particulars', label: 'Particulars' },
  { key: 'value_date', label: 'Value date' },
  { key: 'event_type', label: 'Event' },
  { key: 'status', label: 'Status' },
];

const search = async () => {
  if (!recoId.value.trim()) {
    toaster.error('Reco ID required');
    return;
  }
  loading.value = true;
  try {
    const { data } = await dashboardService.transversal(recoId.value.trim(), currency.value || undefined);
    groups.value = Array.isArray(data) ? data : [data];
  } catch (e) {
    toaster.error('Search failed');
  } finally {
    loading.value = false;
  }
};
</script>

<template>
  <div class="flex justify-between items-center mb-6">
    <h1 class="text-2xl font-bold text-space-indigo">Transversal view</h1>
  </div>

  <Card class="mb-4">
    <div class="grid grid-cols-3 gap-3">
      <Input v-model="recoId" label="Reco ID" />
      <Input v-model="currency" label="Currency (optional)" />
      <div class="flex items-end">
        <Button variant="reveals-primary" @click="search">Search</Button>
      </div>
    </div>
  </Card>

  <div v-if="loading" class="flex justify-center py-10"><Loader size="lg" /></div>

  <div v-else class="space-y-4">
    <Card v-for="g in groups" :key="`${g.reco_id}-${g.currency}`">
      <div class="flex items-center justify-between mb-3">
        <h2 class="font-semibold">
          {{ g.reco_id }} <span class="text-xs text-gray-500">({{ g.currency }})</span>
        </h2>
        <Badge :variant="g.is_balanced ? 'success' : 'warning'">
          Sum: {{ formatAmount(g.sum, g.currency) }} — {{ g.is_balanced ? 'Balanced' : 'Unbalanced' }}
        </Badge>
      </div>
      <Table :columns="columns" :items="g.entries">
        <template #cell-value_date="{ item }">{{ formatDateShort(item.value_date) }}</template>
      </Table>
    </Card>
  </div>
</template>

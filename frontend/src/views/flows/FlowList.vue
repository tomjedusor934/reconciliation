<script setup lang="ts">
import { ref, onMounted } from 'vue';
import { useRouter } from 'vue-router';
import Card from '@/components/ui/Card.vue';
import Table from '@/components/ui/Table.vue';
import Button from '@/components/ui/Button.vue';
import Badge from '@/components/ui/Badge.vue';
import Loader from '@/components/ui/Loader.vue';
import flowService from '@/services/flowService';
import toaster from '@/utils/toaster';
import type { Flow } from '@/types';

const router = useRouter();
const loading = ref(true);
const items = ref<Flow[]>([]);

const columns = [
  { key: 'code', label: 'Code', sortable: true },
  { key: 'name', label: 'Name', sortable: true },
  { key: 'sources', label: 'Sources' },
  { key: 'is_active', label: 'Active' },
  { key: 'accounts', label: 'Accounts' },
  { key: 'actions', label: 'Actions' },
];

const fetchData = async () => {
  loading.value = true;
  try {
    const { data } = await flowService.getAll();
    items.value = data;
  } catch (e) {
    console.error(e);
    toaster.error('Failed to load flows');
  } finally {
    loading.value = false;
  }
};

onMounted(fetchData);
</script>

<template>
  <div class="flex justify-between items-center mb-6">
    <h1 class="text-2xl font-bold text-space-indigo">Flows</h1>
    <Button @click="router.push('/flows/create')" variant="reveals-primary" action="create">Create Flow</Button>
  </div>

  <div v-if="loading" class="flex justify-center py-10"><Loader size="lg" /></div>

  <Card v-else>
    <Table :columns="columns" :items="items" searchable searchPlaceholder="Search flows...">
      <template #cell-sources="{ item }">
        <span class="text-xs text-gray-600">
          {{ (item.sources || []).filter((s: any) => s.is_active).length }}/{{ (item.sources || []).length }} active
        </span>
      </template>
      <template #cell-is_active="{ item }">
        <Badge :variant="item.is_active ? 'success' : 'secondary'">
          {{ item.is_active ? 'Active' : 'Inactive' }}
        </Badge>
      </template>
      <template #cell-accounts="{ item }">
        <span class="text-xs text-gray-600">
          {{ (item.sources || []).reduce((n: number, s: any) => n + (s.accounts?.length || 0), 0) }}
        </span>
      </template>
      <template #cell-actions="{ item }">
        <Button size="sm" variant="secondary" action="edit" @click="router.push(`/flows/${item.id}`)">Edit</Button>
      </template>
    </Table>
  </Card>
</template>

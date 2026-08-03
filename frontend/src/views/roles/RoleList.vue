<script setup lang="ts">
import { ref, onMounted } from 'vue';
import { useRouter } from 'vue-router';
import AppLayout from '@/components/layout/AppLayout.vue';
import Card from '@/components/ui/Card.vue';
import Table from '@/components/ui/Table.vue';
import Button from '@/components/ui/Button.vue';
import Loader from '@/components/ui/Loader.vue';
import Badge from '@/components/ui/Badge.vue';
import roleService, { type Role } from '@/services/roleService';
import toaster from '@/utils/toaster';

const router = useRouter();
const loading = ref(true);
const items = ref<Role[]>([]);

const columns = [
  { key: 'name', label: 'Name', sortable: true },
  { key: 'description', label: 'Description' },
  { key: 'actions', label: 'Actions' }
];

const fetchData = async () => {
  loading.value = true;
  try {
    const response = await roleService.getAll();
    items.value = response.data;
  } catch (error) {
    console.error(error);
    toaster.error('Failed to load roles');
  } finally {
    loading.value = false;
  }
};

const handleDelete = async (id: number) => {
  if (!confirm('Are you sure you want to delete this role?')) return;
  
  try {
    await roleService.delete(id);
    toaster.success('Role deleted');
    await fetchData();
  } catch (error) {
    console.error(error);
    toaster.error('Failed to delete role');
  }
};

onMounted(() => {
  fetchData();
});
</script>

<template>
      <div class="flex justify-between items-center mb-6">
        <h1 class="text-2xl font-bold text-space-indigo">Roles</h1>
        <Button @click="router.push('/roles/create')" variant="reveals-primary" action="create">Create Role</Button>
      </div>

      <div v-if="loading" class="flex justify-center py-10">
        <Loader size="lg" />
      </div>

      <Card v-else>
        <Table :columns="columns" :items="items" searchable searchPlaceholder="Search roles...">

          <template #cell-actions="{ item }">
            <div class="flex space-x-2">
              <Button size="sm" variant="reveals-primary" @click="router.push(`/roles/${item.id}`)">Edit</Button>
              <Button size="sm" variant="danger" @click="handleDelete(item.id)" action="delete">Delete</Button>
            </div>
          </template>
        </Table>
        
         <div v-if="items.length === 0" class="text-center py-6 text-space-indigo/50">
          No roles found.
        </div>
      </Card>
</template>

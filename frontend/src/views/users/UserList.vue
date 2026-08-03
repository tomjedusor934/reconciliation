<script setup lang="ts">
import { ref, onMounted } from 'vue';
import Card from '@/components/ui/Card.vue';
import Table from '@/components/ui/Table.vue';
import Button from '@/components/ui/Button.vue';
import Loader from '@/components/ui/Loader.vue';
import userService from '@/services/userService';
import toaster from '@/utils/toaster';
import { useRouter } from 'vue-router';
import Badge from '@/components/ui/Badge.vue';

const router = useRouter();
const loading = ref(true);
const items = ref([]);

const columns = [
  { key: 'email', label: 'Email', sortable: true },
  { key: 'full_name', label: 'Full Name' },
  { key: 'is_active', label: 'Active' },
  { key: 'blocked', label: 'Blocked' },
  { key: 'roles', label: 'Roles' },
  { key: 'actions', label: 'Actions' }
];

const fetchData = async () => {
  loading.value = true;
  try {
    const response = await userService.getAll();
    items.value = response.data;
  } catch (error) {
    console.error(error);
    toaster.error('Failed to load users');
  } finally {
    loading.value = false;
  }
};

const handleDelete = async (id: number) => {
  if (!confirm('Are you sure you want to delete this user?')) return;
  
  try {
    await userService.delete(id);
    toaster.success('User deleted');
    await fetchData();
  } catch (error) {
    console.error(error);
    toaster.error('Failed to delete user');
  }
};

const handleUnblock = async (id: number) => {
  try {
    await userService.unblock(id);
    toaster.success('User unblocked successfully');
    await fetchData();
  } catch (error) {
    console.error(error);
    toaster.error('Failed to unblock user');
  }
};

onMounted(() => {
  fetchData();
});
</script>

<template>
      <div class="flex justify-between items-center mb-6">
        <h1 class="text-2xl font-bold text-space-indigo">Users</h1>
        <Button @click="router.push('/users/create')" variant="reveals-primary" action="create">Create User</Button>
      </div>

      <div v-if="loading" class="flex justify-center py-10">
        <Loader size="lg" />
      </div>

      <Card v-else>
        <Table :columns="columns" :items="items" searchable searchPlaceholder="Search users...">
          <template #cell-is_active="{ item }">
            <Badge :variant="item.is_active ? 'success' : 'danger'">
              {{ item.is_active ? 'Yes' : 'No' }}
            </Badge>
          </template>
          <template #cell-blocked="{ item }">
            <Badge :variant="item.blocked ? 'danger' : 'success'">
              {{ item.blocked ? 'Blocked' : 'No' }}
            </Badge>
          </template>
          <template #cell-roles="{ item }">
             <div class="flex flex-wrap gap-1">
                <Badge v-for="role in item.roles" :key="role.id" variant="info">
                  {{ role.name }}
                </Badge>
                <span v-if="!item.roles || item.roles.length === 0" class="text-space-indigo/40 text-sm">-</span>
             </div>
          </template>
          <template #cell-actions="{ item }">
            <div class="flex space-x-2">
              <Button size="sm" variant="reveals-primary" @click="router.push(`/users/${item.id}`)">Edit</Button>
              <Button
                v-if="item.blocked"
                size="sm"
                variant="secondary"
                @click="handleUnblock(item.id)"
              >
                Unblock
              </Button>
              <Button size="sm" variant="danger" @click="handleDelete(item.id)" action="delete">Delete</Button>
            </div>
          </template>
        </Table>
        
        <div v-if="items.length === 0" class="text-center py-6 text-space-indigo/50">
          No users found.
        </div>
      </Card>
</template>

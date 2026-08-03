<script setup lang="ts">
import { ref, onMounted, computed } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import Card from '@/components/ui/Card.vue';
import Button from '@/components/ui/Button.vue';
import Input from '@/components/ui/Input.vue';
import Select from '@/components/ui/Select.vue';
import Loader from '@/components/ui/Loader.vue';
import roleService from '@/services/roleService';
import type { AccessiblePage } from '@/services/roleService';
import toaster from '@/utils/toaster';
import { getAppRoutes } from '@/utils/routeUtils';
import router from '@/router/index';

const route = useRoute();

const isEditMode = computed(() => route.params.id !== undefined && route.params.id !== 'create');
const loading = ref(false);
const submitting = ref(false);

const form = ref({
  name: '',
  description: '',
  accessible_pages: [] as AccessiblePage[]
});

const accessLevelOptions = [
    { value: 'ALL', label: 'ALL (Edit & Delete)' },
    { value: 'EDIT', label: 'EDIT (Edit only)' },
    { value: 'NONE', label: 'NONE (Read only)' }
];

// Dynamically fetch routes
const allRoutes = getAppRoutes(router.options.routes);
const APP_ROUTES = ref(allRoutes);

// For the "Add Page" selection
const selectedRouteToAdd = ref('');

const availableRoutes = computed(() => {
    const selectedPaths = form.value.accessible_pages.map(p => p.path);
    return APP_ROUTES.value.filter(r => !selectedPaths.includes(r.value));
});

const fetchData = async () => {
  if (!isEditMode.value) return;
  
  loading.value = true;
  try {
      const id = Number(route.params.id);
      const res = await roleService.get(id);
      form.value = {
          name: res.data.name,
          description: res.data.description || '',
          accessible_pages: res.data.accessible_pages || []
      };
  } catch (error) {
    console.error(error);
    toaster.error('Failed to load role');
    router.push('/roles');
  } finally {
    loading.value = false;
  }
};

const handleAddPage = () => {
    if (!selectedRouteToAdd.value) return;
    
    form.value.accessible_pages.push({
        path: selectedRouteToAdd.value,
        access_level: 'ALL' // Default
    });
    
    selectedRouteToAdd.value = ''; // Reset selection
};

const handleRemovePage = (index: number) => {
    form.value.accessible_pages.splice(index, 1);
};

const handleSubmit = async () => {
    if (!form.value.name) {
        toaster.error('Name is required');
        return;
    }
    
    submitting.value = true;
    try {
        if (isEditMode.value) {
            await roleService.update(Number(route.params.id), form.value);
            toaster.success('Role updated successfully');
        } else {
            await roleService.create(form.value);
            toaster.success('Role created successfully');
        }
        router.push('/roles');
    } catch (e) {
        console.error(e);
        toaster.error('Failed to save role');
    } finally {
        submitting.value = false;
    }
};

onMounted(fetchData);
</script>

<template>
    <div class="mx-auto max-w-3xl px-4 sm:px-6 lg:px-8">
      <div class="mb-6 flex items-center justify-between">
         <h1 class="text-2xl font-bold text-space-indigo">
          {{ isEditMode ? 'Edit Role' : 'Create Role' }}
        </h1>
        <Button variant="ghost" @click="router.push('/roles')">Back to List</Button>
      </div>

      <div v-if="loading" class="flex justify-center py-10">
        <Loader size="lg" />
      </div>

      <Card v-else>
          <form @submit.prevent="handleSubmit" class="space-y-6">
              <Input label="Name" v-model="form.name" theme="reveals" required />
              <Input label="Description" v-model="form.description" theme="reveals" />
              <div>
                  <label class="block text-sm font-medium text-space-indigo mb-2">Accessible Pages</label>
                  
                  <div class="mb-4 flex gap-2 items-end">
                      <div class="flex-1">
                          <Select
                              label="Add Page"
                              v-model="selectedRouteToAdd"
                              :options="availableRoutes"
                              placeholder="Select a page to add..."
                              theme="reveals"
                          />
                      </div>
                      <Button type="button" variant="secondary" @click="handleAddPage" :disabled="!selectedRouteToAdd">
                          Add
                      </Button>
                  </div>

                  <div v-if="form.accessible_pages.length > 0" class="border rounded-md divide-y">
                      <div v-for="(page, index) in form.accessible_pages" :key="index" class="p-3 flex items-center justify-between gap-4">
                          <div class="font-medium text-space-indigo/70 w-1/3">
                              {{ APP_ROUTES.find(r => r.value === page.path)?.label || page.path }}
                          </div>
                          <div class="w-1/3">
                              <Select
                                  v-model="form.accessible_pages[index].access_level"
                                  :options="accessLevelOptions"
                                  hideLabel
                              />
                          </div>
                          <Button type="button" variant="danger" size="sm" @click="handleRemovePage(index)">Remove</Button>
                      </div>
                  </div>
                  <div v-else class="text-space-indigo/50 text-sm italic p-4 bg-gray-50 rounded text-center">
                      No accessible pages configured.
                  </div>
              </div>

              <div class="flex justify-end gap-2 pt-4">
                  <Button variant="secondary" type="button" @click="router.push('/roles')">Cancel</Button>
                  <Button type="submit" :loading="submitting" action="save" variant="reveals-primary">Save</Button>
              </div>
          </form>
      </Card>
    </div>
</template>

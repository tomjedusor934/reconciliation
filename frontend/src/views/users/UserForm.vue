<script setup lang="ts">
import { ref, onMounted, computed } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import Card from '@/components/ui/Card.vue';
import Button from '@/components/ui/Button.vue';
import Input from '@/components/ui/Input.vue';
import Checkbox from '@/components/ui/Checkbox.vue';
import Loader from '@/components/ui/Loader.vue';
import MultiSelect from '@/components/ui/MultiSelect.vue';
import PasswordField from '@/components/block/PasswordField.vue';
import userService from '@/services/userService';
import roleService from '@/services/roleService';
import toaster from '@/utils/toaster';

const route = useRoute();
const router = useRouter();

const isEditMode = computed(() => route.params.id !== undefined && route.params.id !== 'create');
const loading = ref(false);
const submitting = ref(false);
const roleOptions = ref<{ value: number; label: string }[]>([]);
const passwordIsValid = ref(false);

const form = ref({
  email: '',
  full_name: '',
  password: '',
  is_active: true,
  is_superuser: false,
  role_ids: [] as number[]
});

//const showPasswordField = computed(() => !isEditMode.value || form.value.password);

const fetchRoles = async () => {
  try {
    const res = await roleService.getAll();
    roleOptions.value = res.data.map(r => ({ value: r.id, label: r.name }));
  } catch (e) { console.error(e); }
};

const fetchData = async () => {
  loading.value = true;
  await fetchRoles();

  if (isEditMode.value) {
    try {
      const id = Number(route.params.id);
      const response = await userService.get(id);
      const { password, roles, ...rest } = response.data;
      form.value = { 
        ...rest, 
        password: '', 
        role_ids: roles ? roles.map((r: any) => r.id) : [] 
      };
    } catch (error) {
      console.error(error);
      toaster.error('Failed to load user');
      router.push('/users');
    }
  }
  loading.value = false;
};

const handleSubmit = async () => {
  if (!form.value.email) {
    toaster.error('Email is required');
    return;
  }
  if (!isEditMode.value && !form.value.password) {
    toaster.error('Password is required for new users');
    return;
  }
  if (form.value.password && !passwordIsValid.value) {
    toaster.error('Password does not meet the required criteria');
    return;
  }

  submitting.value = true;
  try {
    if (isEditMode.value) {
      const payload = { ...form.value };
      if (!payload.password) delete (payload as any).password;
      await userService.update(Number(route.params.id), payload);
      toaster.success('User updated successfully');
    } else {
      await userService.create(form.value);
      toaster.success('User created successfully');
    }
    router.push('/users');
  } catch (error) {
    console.error(error);
    toaster.error('Failed to save user');
  } finally {
    submitting.value = false;
  }
};

onMounted(() => fetchData());
</script>

<template>
  <div class="mx-auto max-w-3xl px-4 sm:px-6 lg:px-8">
    <div class="mb-6 flex items-center justify-between">
      <h1 class="text-2xl font-bold text-space-indigo">
        {{ isEditMode ? 'Edit User' : 'Create User' }}
      </h1>
      <Button variant="ghost" @click="router.push('/users')">Back to List</Button>
    </div>

    <div v-if="loading" class="flex justify-center py-10">
      <Loader size="lg" />
    </div>

    <Card v-else>
      <form @submit.prevent="handleSubmit" class="space-y-6">
        <Input 
          id="email" 
          type="email"
          label="Email" 
          v-model="form.email" 
          placeholder="Enter email address"
          theme="reveals"
          required
        />

        <Input 
          id="full_name" 
          label="Full Name" 
          v-model="form.full_name" 
          placeholder="Enter full name"
          theme="reveals"
        />
        
        <!-- v-if="showPasswordField" -->

        <PasswordField
          v-model="form.password"
          :is-edit-mode="isEditMode"
          @validation-change="passwordIsValid = $event"
        />
        
        <MultiSelect
          label="Roles"
          v-model="form.role_ids"
          :options="roleOptions"
          placeholder="Select roles"
          theme="reveals"
        />

        <div class="flex items-center space-x-2">
          <Checkbox id="is_active" v-model="form.is_active" theme="reveals"/>
          <label for="is_active" class="text-sm font-medium text-space-indigo">Active</label>
        </div>

        <div class="flex justify-end space-x-3">
          <Button type="button" variant="secondary" @click="router.push('/users')">Cancel</Button>
          <Button type="submit" variant="reveals-primary" :disabled="submitting || (!isEditMode && form.password === '') || (form.password !== '' && !passwordIsValid)" action="save">
            {{ submitting ? 'Saving...' : 'Save' }}
          </Button>
        </div>
      </form>
    </Card>
  </div>
</template>

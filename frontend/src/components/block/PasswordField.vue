<script setup lang="ts">
import { ref, watch, computed } from 'vue';
import Input from '../ui/Input.vue';
import PasswordValidationDisplay from '../ui/PasswordValidationDisplay.vue';
import settingsService from '@/services/settingsService';

interface Props {
  modelValue: string;
  isEditMode: boolean;
  label?: string;
}

interface Emits {
  (e: 'update:modelValue', value: string): void;
  (e: 'validationChange', isValid: boolean): void;
}

const props = defineProps<Props>();
const emit = defineEmits<Emits>();

const passwordSettings = ref<any>(null);
const passwordValidation = ref<{
  is_valid: boolean;
  errors: string[];
  criteria: Record<string, any>;
}>({
  is_valid: false,
  errors: [],
  criteria: {}
});

const isLoading = ref(false);

const fetchPasswordSettings = async () => {
  try {
    const res = await settingsService.getPasswordSettings();
    passwordSettings.value = res.data;
  } catch (e) {
    console.error('Failed to fetch password settings:', e);
  }
};

const validatePassword = async (password: string) => {
  if (!password) {
    passwordValidation.value = { is_valid: false, errors: [], criteria: {} };
    emit('validationChange', false);
    return;
  }

  isLoading.value = true;
  try {
    const res = await settingsService.validatePassword(password);
    passwordValidation.value = res.data;
    emit('validationChange', res.data.is_valid);
  } catch (e) {
    console.error('Failed to validate password:', e);
  } finally {
    isLoading.value = false;
  }
};

watch(() => props.modelValue, (newPassword) => {
  if (newPassword && passwordSettings.value) {
    validatePassword(newPassword);
  }
});

// Initialize on mount
void fetchPasswordSettings();
</script>

<template>
  <div class="space-y-4">
    <Input 
      :model-value="modelValue"
      @update:model-value="$emit('update:modelValue', $event)"
      type="password"
      :label="props.label || (isEditMode ? 'Password (leave blank to keep current)' : 'Password')" 
      placeholder="Enter password"
      :error="modelValue && !passwordValidation.is_valid ? 'Password does not meet requirements' : undefined"
      theme="reveals"
    />
    
    <PasswordValidationDisplay 
      v-if="modelValue && passwordSettings"
      :validation="passwordValidation"
      :settings="passwordSettings"
    />
  </div>
</template>

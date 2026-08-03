<script setup lang="ts">
interface Criterion {
  required: boolean;
  met: boolean;
}

interface ValidationState {
  criteria: Record<string, Criterion>;
}

interface Props {
  validation: ValidationState;
  settings: Record<string, any>;
}

defineProps<Props>();

const getCriterionColor = (met: boolean) => met ? '#00E49A' : '#EF4444';
const getCriterionStatus = (met: boolean) => met ? 'OK' : 'X';
</script>

<template>
  <div class="rounded-lg bg-gray-100 p-4">
    <h3 class="text-sm font-semibold text-space-indigo mb-3">Password Requirements</h3>
    
    <!-- Min length -->
    <div class="flex items-center gap-2 mb-2">
      <span 
        class="text-lg font-bold"
        :style="{ color: getCriterionColor(validation.criteria.min_length?.met) }"
      >
        {{ getCriterionStatus(validation.criteria.min_length?.met) }}
      </span>
      <span class="text-sm" :class="{ 'text-space-indigo/50': validation.criteria.min_length?.met, 'text-red-600': !validation.criteria.min_length?.met }">
        At least {{ settings.password_min_length || 8 }} characters
      </span>
    </div>

    <!-- Uppercase -->
    <div v-if="settings.password_require_uppercase" class="flex items-center gap-2 mb-2">
      <span 
        class="text-lg font-bold"
        :style="{ color: getCriterionColor(validation.criteria.uppercase?.met) }"
      >
        {{ getCriterionStatus(validation.criteria.uppercase?.met) }}
      </span>
      <span class="text-sm" :class="{ 'text-space-indigo/50': validation.criteria.uppercase?.met, 'text-red-600': !validation.criteria.uppercase?.met }">
        At least one uppercase letter (A-Z)
      </span>
    </div>

    <!-- Lowercase -->
    <div v-if="settings.password_require_lowercase" class="flex items-center gap-2 mb-2">
      <span 
        class="text-lg font-bold"
        :style="{ color: getCriterionColor(validation.criteria.lowercase?.met) }"
      >
        {{ getCriterionStatus(validation.criteria.lowercase?.met) }}
      </span>
      <span class="text-sm" :class="{ 'text-space-indigo/50': validation.criteria.lowercase?.met, 'text-red-600': !validation.criteria.lowercase?.met }">
        At least one lowercase letter (a-z)
      </span>
    </div>

    <!-- Numbers -->
    <div v-if="settings.password_require_numbers" class="flex items-center gap-2 mb-2">
      <span 
        class="text-lg font-bold"
        :style="{ color: getCriterionColor(validation.criteria.numbers?.met) }"
      >
        {{ getCriterionStatus(validation.criteria.numbers?.met) }}
      </span>
      <span class="text-sm" :class="{ 'text-space-indigo/50': validation.criteria.numbers?.met, 'text-red-600': !validation.criteria.numbers?.met }">
        At least one number (0-9)
      </span>
    </div>

    <!-- Special characters -->
    <div v-if="settings.password_require_special" class="flex items-center gap-2">
      <span 
        class="text-lg font-bold"
        :style="{ color: getCriterionColor(validation.criteria.special?.met) }"
      >
        {{ getCriterionStatus(validation.criteria.special?.met) }}
      </span>
      <span class="text-sm" :class="{ 'text-space-indigo/50': validation.criteria.special?.met, 'text-red-600': !validation.criteria.special?.met }">
        At least one special character (!@#$%^&*...)
      </span>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed } from 'vue';
import Button from '@/components/ui/Button.vue';
import Loader from '@/components/ui/Loader.vue';
import Badge from '@/components/ui/Badge.vue';
import sourceConnectionService from '@/services/sourceConnectionService';
import type { TestQueryResult } from '@/services/sourceConnectionService';

const props = defineProps<{
  connectionId: number | null | undefined;
  modelValue: string;
}>();

const emit = defineEmits<{
  (e: 'update:modelValue', value: string): void;
}>();

const query = computed({
  get: () => props.modelValue,
  set: (val: string) => emit('update:modelValue', val),
});

const loading = ref(false);
const result = ref<TestQueryResult | null>(null);
const error = ref<string | null>(null);

async function executeTest() {
  if (!props.connectionId) {
    error.value = 'Please select a connection first.';
    return;
  }
  if (!query.value.trim()) {
    error.value = 'Query cannot be empty.';
    return;
  }

  loading.value = true;
  error.value = null;
  result.value = null;

  try {
    const { data } = await sourceConnectionService.testQuery(props.connectionId, query.value);
    result.value = data;
  } catch (e: any) {
    error.value = e?.response?.data?.detail || 'Query execution failed.';
  } finally {
    loading.value = false;
  }
}
</script>

<template>
  <div class="space-y-3">
    <div class="flex items-center justify-between">
      <label class="block text-sm font-medium text-gray-700">SQL Query</label>
      <Badge v-if="result && !error" variant="info">
        {{ result.row_count }} row{{ result.row_count !== 1 ? 's' : '' }}
        <span v-if="result.truncated"> (truncated to 50)</span>
      </Badge>
    </div>

    <textarea
      v-model="query"
      rows="5"
      class="w-full rounded-md border border-gray-300 px-3 py-2 text-xs font-mono focus:border-turquoise-surf focus:ring-1 focus:ring-turquoise-surf"
      placeholder="SELECT column1, column2 FROM table WHERE ..."
    />

    <div class="flex items-center gap-3">
      <Button
        type="button"
        size="sm"
        variant="secondary"
        :disabled="!connectionId || loading"
        @click="executeTest"
      >
        <span v-if="loading">Testing...</span>
        <span v-else>Test query</span>
      </Button>
      <span v-if="!connectionId" class="text-xs text-gray-400 italic">
        Select a connection to enable testing
      </span>
    </div>

    <!-- Error -->
    <div v-if="error" class="rounded-md bg-red-50 border border-red-200 p-3 text-sm text-red-700">
      {{ error }}
    </div>

    <!-- Loading -->
    <div v-if="loading" class="flex justify-center py-4">
      <Loader size="md" />
    </div>

    <!-- Results table -->
    <div v-if="result && !error" class="overflow-x-auto rounded-md border border-gray-200">
      <table class="min-w-full text-xs">
        <thead class="bg-gray-50">
          <tr>
            <th
              v-for="col in result.columns"
              :key="col"
              class="px-3 py-2 text-left font-medium text-gray-600 border-b border-gray-200"
            >
              {{ col }}
            </th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="(row, idx) in result.rows"
            :key="idx"
            class="hover:bg-gray-50"
            :class="idx % 2 === 0 ? 'bg-white' : 'bg-gray-25'"
          >
            <td
              v-for="(cell, ci) in row"
              :key="ci"
              class="px-3 py-1.5 border-b border-gray-100 font-mono whitespace-nowrap"
            >
              {{ cell ?? 'NULL' }}
            </td>
          </tr>
          <tr v-if="result.rows.length === 0">
            <td :colspan="result.columns.length" class="px-3 py-4 text-center text-gray-400 italic">
              No rows returned
            </td>
          </tr>
        </tbody>
      </table>
      <div v-if="result.truncated" class="px-3 py-2 text-xs text-amber-600 bg-amber-50 border-t border-amber-200">
        Results truncated to 50 rows. Refine your query for complete results.
      </div>
    </div>
  </div>
</template>

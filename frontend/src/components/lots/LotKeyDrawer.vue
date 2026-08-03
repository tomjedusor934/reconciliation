<script setup lang="ts">
import { ref, watch } from 'vue';
import { Search } from 'lucide-vue-next';
import Drawer from '@/components/ui/Drawer.vue';
import Input from '@/components/ui/Input.vue';
import Button from '@/components/ui/Button.vue';
import Loader from '@/components/ui/Loader.vue';
import { KEY_TYPE_COLORS } from '@/utils/lotGraph';
import lotService from '@/services/lotService';
import toaster from '@/utils/toaster';
import type { LotKeyValue } from '@/types';

const props = defineProps<{
  isOpen: boolean;
  lotId: string;
  keyType: string | null;
  // The value currently focused in the graph (highlighted in the list).
  selectedValue?: string | null;
}>();

const emit = defineEmits<{
  (e: 'close'): void;
  (e: 'select-value', payload: { key_type: string; key_value: string }): void;
}>();

const values = ref<LotKeyValue[]>([]);
const total = ref(0);
const offset = ref(0);
const batchSize = 100;
const search = ref('');
const loading = ref(false);
const loadingMore = ref(false);

const buildParams = (skip: number) => ({
  key_type: props.keyType as string,
  search: search.value.trim() || undefined,
  skip,
  limit: batchSize,
});

const fetchValues = async () => {
  if (!props.keyType) return;
  loading.value = true;
  offset.value = 0;
  try {
    const { data } = await lotService.getKeys(props.lotId, buildParams(0));
    values.value = data.items;
    total.value = data.total_count;
    offset.value = batchSize;
  } catch (e) {
    toaster.error('Failed to load values');
  } finally {
    loading.value = false;
  }
};

const fetchMore = async () => {
  if (loadingMore.value || values.value.length >= total.value || !props.keyType) return;
  loadingMore.value = true;
  try {
    const { data } = await lotService.getKeys(props.lotId, buildParams(offset.value));
    values.value = [...values.value, ...data.items];
    offset.value += batchSize;
  } catch (e) {
    toaster.error('Failed to load more values');
  } finally {
    loadingMore.value = false;
  }
};

const pick = (v: LotKeyValue) => {
  if (!props.keyType) return;
  emit('select-value', { key_type: props.keyType, key_value: v.key_value });
};

// (Re)load whenever the drawer opens or targets a different key type.
watch(
  () => [props.isOpen, props.keyType] as const,
  ([open]) => {
    if (open && props.keyType) {
      search.value = '';
      fetchValues();
    }
  },
  { immediate: true }
);
</script>

<template>
  <Drawer :is-open="isOpen" :title="keyType ? `${keyType} values` : 'Values'" size="lg" @close="emit('close')">
    <div class="flex items-center gap-2 mb-3">
      <div
        v-if="keyType"
        class="h-3 w-3 rounded-full shrink-0"
        :style="{ backgroundColor: KEY_TYPE_COLORS[keyType] ?? '#94a3b8' }"
      />
      <Input
        v-model="search"
        placeholder="Search value…"
        class="flex-1"
        @keyup.enter="fetchValues"
      />
      <Button variant="secondary" size="sm" @click="fetchValues">
        <Search class="h-4 w-4" />
      </Button>
    </div>

    <p class="text-xs text-gray-500 mb-2 tabular-nums">
      {{ total.toLocaleString() }} value(s) — pick one to filter the table and the graph.
    </p>

    <div v-if="loading" class="flex justify-center py-8"><Loader size="lg" /></div>

    <div v-else class="space-y-1 max-h-[65vh] overflow-y-auto pr-1">
      <button
        v-for="v in values"
        :key="v.key_value"
        class="w-full flex items-center justify-between gap-2 rounded-lg border px-3 py-2 text-left text-sm transition-colors"
        :class="
          selectedValue === v.key_value
            ? 'border-indigo-400 bg-indigo-50'
            : 'border-gray-200 hover:border-indigo-300 hover:bg-gray-50'
        "
        @click="pick(v)"
      >
        <span class="font-mono truncate" :title="v.key_value">{{ v.key_value }}</span>
        <span
          class="shrink-0 rounded-full bg-gray-100 px-2 py-0.5 text-[11px] font-semibold text-gray-600 tabular-nums"
        >
          ×{{ v.member_count.toLocaleString() }}
        </span>
      </button>

      <p v-if="!values.length" class="py-6 text-center text-sm text-gray-400">No values found.</p>

      <div v-if="values.length < total" class="pt-2 text-center">
        <Button variant="secondary" size="sm" :disabled="loadingMore" @click="fetchMore">
          {{ loadingMore ? 'Loading…' : 'Load more' }}
        </Button>
      </div>
    </div>
  </Drawer>
</template>

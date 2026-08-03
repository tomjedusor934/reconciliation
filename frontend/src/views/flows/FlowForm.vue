<script setup lang="ts">
import { ref, onMounted, computed } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import Card from '@/components/ui/Card.vue';
import Input from '@/components/ui/Input.vue';
import Select from '@/components/ui/Select.vue';
import TextArea from '@/components/ui/TextArea.vue';
import Checkbox from '@/components/ui/Checkbox.vue';
import Button from '@/components/ui/Button.vue';
import Loader from '@/components/ui/Loader.vue';
import flowService from '@/services/flowService';
import toaster from '@/utils/toaster';
import type { Flow, FlowSourceAccount, FlowSourceCreate, FlowSourceType } from '@/types';

const route = useRoute();
const router = useRouter();
const isEdit = computed(() => !!route.params.id);
const loading = ref(false);

const sourceTypeOptions = [
  { value: 'file', label: 'File' },
  { value: 'finacle_db', label: 'Finacle DB' },
];

const fileParserOptions = [
  { value: 'cobol_mosel', label: 'Cobol / MOSEL (ATM flat file)' },
  { value: 'excel', label: 'Excel XLSX (WebriPost, etc.)' },
  { value: 'csv', label: 'CSV' },
  { value: 'xml', label: 'XML' },
  { value: 'mt940', label: 'MT940 (BCEE)' },
];
const finacleParserOptions = [
  { value: 'finacle_db', label: 'Finacle DB extractor' },
  { value: 'finacle_batch_booking_true', label: 'Finacle Batch Booking True' },
];

const fileMatchKeyOptions = [
  { value: 'reco_id_amount', label: 'reco_id + amount' },
  { value: 'file_ref_amount', label: 'file + ref + amount' },
  { value: 'ref_amount', label: 'ref + amount' },
];
const finacleMatchKeyOptions = [
  { value: 'reco_id_amount', label: 'reco_id + amount' },
  { value: 'ref_amount', label: 'ref + amount' },
];

const parserOptionsFor = (sourceType: FlowSourceType) =>
  sourceType === 'finacle_db' ? finacleParserOptions : fileParserOptions;

const matchKeyOptionsFor = (sourceType: FlowSourceType) =>
  sourceType === 'finacle_db' ? finacleMatchKeyOptions : fileMatchKeyOptions;

const form = ref<Partial<Flow>>({
  code: '',
  name: '',
  description: '',
  is_active: false,
  default_currency: 'EUR',
  sources: [],
});

// Each source tracks its parser_config as stringified JSON for the textarea
const sourceConfigTexts = ref<string[]>([]);

const newSource = (): FlowSourceCreate => ({
  code: '',
  name: '',
  description: '',
  is_active: true,
  source_type: 'file',
  parser_type: 'csv',
  match_key_strategy: 'reco_id_amount',
  inbox_subfolder: '',
  file_pattern: '',
  parser_config: {},
  accounts: [],
});

const load = async () => {
  if (!isEdit.value) return;
  loading.value = true;
  try {
    const { data } = await flowService.get(Number(route.params.id));
    const sources = (data.sources || []).map((s: any) => ({ ...s, accounts: s.accounts || [] }));
    form.value = { ...data, sources };
    sourceConfigTexts.value = sources.map((s: any) =>
      JSON.stringify(s.parser_config || {}, null, 2)
    );
  } catch (e) {
    console.error(e);
    toaster.error('Failed to load flow');
  } finally {
    loading.value = false;
  }
};

const addSourceAccount = (src: FlowSourceCreate) => {
  src.accounts = [...(src.accounts || []), { account_number: '', label: '' } as FlowSourceAccount];
};
const removeSourceAccount = (src: FlowSourceCreate, idx: number) => {
  src.accounts = (src.accounts || []).filter((_, i) => i !== idx);
};

const addSource = () => {
  form.value.sources = [...(form.value.sources || []), newSource()];
  sourceConfigTexts.value.push('{}');
};
const removeSource = (idx: number) => {
  form.value.sources = (form.value.sources || []).filter((_, i) => i !== idx);
  sourceConfigTexts.value = sourceConfigTexts.value.filter((_, i) => i !== idx);
};

const onSourceTypeChange = (src: FlowSourceCreate, newType: FlowSourceType) => {
  src.source_type = newType;
  if (newType === 'finacle_db') {
    src.parser_type = 'finacle_db';
    src.inbox_subfolder = '';
    src.file_pattern = '';
    src.accounts = src.accounts || [];
    if (src.match_key_strategy === 'file_ref_amount') {
      src.match_key_strategy = 'reco_id_amount';
    }
  } else {
    if (src.parser_type === 'finacle_db') {
      src.parser_type = 'csv';
    }
    src.accounts = [];
  }
};

const submit = async () => {
  // Parse all source parser_config JSON strings
  const sources = form.value.sources || [];
  for (let i = 0; i < sources.length; i++) {
    try {
      sources[i].parser_config = JSON.parse(sourceConfigTexts.value[i] || '{}');
    } catch {
      toaster.error(`Source #${i + 1}: invalid parser config JSON`);
      return;
    }
  }
  try {
    if (isEdit.value) {
      await flowService.update(Number(route.params.id), form.value as any);
      toaster.success('Flow updated');
    } else {
      await flowService.create(form.value as any);
      toaster.success('Flow created');
    }
    router.push('/flows');
  } catch (e: any) {
    console.error(e);
    toaster.error(e?.response?.data?.detail || 'Save failed');
  }
};

onMounted(load);
</script>

<template>
  <div class="flex justify-between items-center mb-6">
    <h1 class="text-2xl font-bold text-space-indigo">{{ isEdit ? 'Edit Flow' : 'Create Flow' }}</h1>
    <Button variant="secondary" @click="router.push('/flows')">Back</Button>
  </div>

  <div v-if="loading" class="flex justify-center py-10"><Loader size="lg" /></div>

  <Card v-else>
    <form class="space-y-6" @submit.prevent="submit">
      <!-- Global Flow Info -->
      <fieldset class="space-y-4">
        <legend class="text-lg font-semibold text-gray-800 mb-2">Flow Information</legend>
        <div class="grid grid-cols-2 gap-4">
          <Input v-model="form.code" label="Code" required :readonly="isEdit" />
          <Input v-model="form.name" label="Name" required />
        </div>
        <TextArea v-model="form.description" label="Description" rows="2" />
        <div class="grid grid-cols-2 gap-4">
          <Input v-model="form.default_currency" label="Default currency" />
          <Checkbox v-model="form.is_active" label="Active" />
        </div>
      </fieldset>

      <!-- Sources -->
      <fieldset class="space-y-4">
        <div class="flex items-center justify-between">
          <legend class="text-lg font-semibold text-gray-800">Sources</legend>
          <Button type="button" size="sm" variant="secondary" action="create" @click="addSource">+ Add source</Button>
        </div>
        <p v-if="!form.sources?.length" class="text-sm text-gray-400 italic">No sources. Add at least one ingestion source.</p>

        <Card v-for="(src, si) in form.sources" :key="si" class="border border-gray-200 bg-gray-50 space-y-3 relative">
          <div class="flex items-center justify-between mb-2">
            <span class="text-sm font-semibold text-gray-700">Source #{{ si + 1 }}</span>
            <div class="flex gap-2 items-center">
              <Checkbox v-model="src.is_active" label="Active" />
              <Button type="button" size="sm" variant="danger" action="delete" @click="removeSource(si)">Remove</Button>
            </div>
          </div>
          <div class="grid grid-cols-2 gap-4">
            <Input v-model="src.code" label="Source code" placeholder="e.g. cobol_file" required />
            <Input v-model="src.name" label="Source name" placeholder="e.g. Cobol/MOSEL File" required />
          </div>
          <TextArea v-model="src.description" label="Description" rows="1" />
          <div class="grid grid-cols-2 gap-4">
            <Select
              :modelValue="src.source_type"
              @update:modelValue="(v: any) => onSourceTypeChange(src, v)"
              label="Source type"
              :options="sourceTypeOptions"
            />
            <Select v-model="src.parser_type" label="Parser" :options="parserOptionsFor(src.source_type)" />
          </div>
          <div class="grid grid-cols-3 gap-4">
            <Select v-model="src.match_key_strategy" label="Match key strategy" :options="matchKeyOptionsFor(src.source_type)" />
            <Input v-model="src.inbox_subfolder" label="Inbox subfolder" :disabled="src.source_type !== 'file'" />
            <Input v-model="src.file_pattern" label="File pattern (glob)" placeholder="e.g. *.csv, ATM_*.dat" :disabled="src.source_type !== 'file'" />
          </div>
          <div v-if="src.source_type === 'finacle_db'" class="space-y-2">
            <div class="flex items-center justify-between">
              <label class="block text-sm font-medium">Reference Accounts</label>
              <Button type="button" size="sm" variant="secondary" action="create" @click="addSourceAccount(src)">+ Add account</Button>
            </div>
            <div class="text-xs text-gray-600 bg-gray-50 border border-gray-200 rounded p-2">
              Accounts used by the Finacle ingestion DAG to filter datamart movements (WHERE account IN …).
            </div>
            <div v-for="(acc, ai) in src.accounts" :key="ai" class="grid grid-cols-[2fr_3fr_auto] gap-2 items-end">
              <Input v-model="acc.account_number" placeholder="Account number (e.g. 0010110040001)" />
              <Input v-model="acc.label" placeholder="Label (e.g. NOSTRO MIRROR - IP)" />
              <Button type="button" size="sm" variant="danger" action="delete" @click="removeSourceAccount(src, ai)">Remove</Button>
            </div>
            <p v-if="!src.accounts?.length" class="text-xs text-gray-400 italic">No accounts declared. At least one is required for Finacle DB sources.</p>
          </div>
          <div>
            <label class="block text-sm font-medium mb-1">Parser config (JSON)</label>
            <TextArea v-model="sourceConfigTexts[si]" rows="6" class="font-mono text-xs" :disabled="src.source_type !== 'file'" />
          </div>
        </Card>
      </fieldset>

      <div class="flex justify-end gap-2 pt-4">
        <Button type="button" variant="secondary" @click="router.push('/flows')">Cancel</Button>
        <Button type="submit" variant="reveals-primary" :action="isEdit ? 'edit' : 'create'">Save</Button>
      </div>
    </form>
  </Card>
</template>

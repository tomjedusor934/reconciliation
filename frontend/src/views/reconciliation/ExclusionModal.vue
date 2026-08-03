<script setup lang="ts">
import { ref, computed } from 'vue';
import Modal from '@/components/ui/Modal.vue';
import TextArea from '@/components/ui/TextArea.vue';
import Button from '@/components/ui/Button.vue';
import { formatDateShort } from '@/utils/formatDate';
import { formatAmount } from '@/utils/formatAmount';
import reconciliationService from '@/services/reconciliationService';
import toaster from '@/utils/toaster';
import type { ReconciliationEntry } from '@/types';

const props = defineProps<{ modelValue: boolean; entry: ReconciliationEntry | null; entries?: ReconciliationEntry[] }>();
const emit = defineEmits<{
  (e: 'update:modelValue', v: boolean): void;
  (e: 'excluded'): void;
}>();

const reason = ref('');
const submitting = ref(false);

const close = () => emit('update:modelValue', false);

const isMultiple = computed(() => props.entries && props.entries.length > 0);
const totalAmount = computed(() => 
  props.entries?.reduce((acc, e) => acc + Number(e.amount || 0), 0) || 0
);

const submit = async () => {
  if (!reason.value.trim()) {
    toaster.error('A reason is mandatory');
    return;
  }
  
  submitting.value = true;
  try {
    if (isMultiple.value && props.entries) {
      // Bulk exclusion
      await Promise.all(
        props.entries.map((entry) =>
          reconciliationService.exclude({
            entry_id: entry.id,
            reason: reason.value.trim(),
          }),
        ),
      );
      toaster.success(`${props.entries.length} entries excluded`);
    } else if (props.entry) {
      // Single exclusion
      await reconciliationService.exclude({
        entry_id: props.entry.id,
        reason: reason.value.trim(),
      });
      toaster.success('Entry excluded');
    }
    reason.value = '';
    emit('excluded');
    close();
  } catch (e: any) {
    toaster.error(e?.response?.data?.detail || 'Exclusion failed');
  } finally {
    submitting.value = false;
  }
};
</script>

<template>
  <Modal :is-open="modelValue" @close="close" :title="isMultiple ? 'Exclude multiple entries' : 'Exclude entry'">
    <div class="space-y-4">
      <!-- Multiple entries info box -->
      <div v-if="isMultiple" class="bg-blue-50 border border-blue-200 rounded p-3 text-sm">
        <div class="font-medium text-blue-900">Bulk exclusion details</div>
        <div class="text-blue-800 mt-2">
          <div><strong>Entries to exclude:</strong> {{ entries?.length }}</div>
          <div><strong>Total amount:</strong> {{ formatAmount(totalAmount, entries?.[0]?.currency) }}</div>
          <div class="mt-2 text-xs">
            The same reason will be applied to all {{ entries?.length }} entries.
          </div>
        </div>
      </div>

      <!-- Single entry info -->
      <div v-else-if="entry" class="text-sm">
        <div><strong>Reco ID:</strong> {{ entry.reco_id }}</div>
        <div><strong>Amount:</strong> {{ formatAmount(entry.amount, entry.currency) }}</div>
        <div><strong>Value date:</strong> {{ formatDateShort(entry.value_date) }}</div>
      </div>

      <TextArea v-model="reason" label="Reason (mandatory)" required rows="4" />
      <div class="flex justify-end gap-2">
        <Button variant="secondary" @click="close">Cancel</Button>
        <Button 
          variant="danger" 
          :disabled="submitting" 
          action="delete" 
          @click="submit"
        >
          {{ isMultiple ? `Exclude ${entries?.length} entries` : 'Exclude' }}
        </Button>
      </div>
    </div>
  </Modal>
</template>

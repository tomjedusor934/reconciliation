<script setup lang="ts">
import { ref } from 'vue';
import Modal from '@/components/ui/Modal.vue';
import TextArea from '@/components/ui/TextArea.vue';
import Button from '@/components/ui/Button.vue';
import { formatDateShort } from '@/utils/formatDate';
import { formatAmount } from '@/utils/formatAmount';
import reconciliationService from '@/services/reconciliationService';
import toaster from '@/utils/toaster';
import type { ReconciliationEntry } from '@/types';

const props = defineProps<{ modelValue: boolean; entry: ReconciliationEntry | null }>();
const emit = defineEmits<{
  (e: 'update:modelValue', v: boolean): void;
  (e: 'unexcluded'): void;
}>();

const reason = ref('');
const submitting = ref(false);

const close = () => emit('update:modelValue', false);

const submit = async () => {
  if (!reason.value.trim()) {
    toaster.error('A reason is mandatory');
    return;
  }

  if (!props.entry) return;

  submitting.value = true;
  try {
    await reconciliationService.unexclude({
      entry_id: props.entry.id,
      reason: reason.value.trim(),
    });
    toaster.success('Entry unexcluded');
    reason.value = '';
    emit('unexcluded');
    close();
  } catch (e: any) {
    toaster.error(e?.response?.data?.detail || 'Unexclude failed');
  } finally {
    submitting.value = false;
  }
};
</script>

<template>
  <Modal :is-open="modelValue" @close="close" title="Unexclude entry">
    <div class="space-y-4">
      <div v-if="entry" class="text-sm">
        <div><strong>Reco ID:</strong> {{ entry.reco_id }}</div>
        <div><strong>Amount:</strong> {{ formatAmount(entry.amount, entry.currency) }}</div>
        <div><strong>Value date:</strong> {{ formatDateShort(entry.value_date) }}</div>
      </div>

      <div class="bg-amber-50 border border-amber-200 rounded p-3 text-sm text-amber-800">
        This will restore the entry to <strong>Pending</strong> status.
      </div>

      <TextArea v-model="reason" label="Reason for unexclude (mandatory)" required rows="3" />
      <div class="flex justify-end gap-2">
        <Button variant="secondary" @click="close">Cancel</Button>
        <Button
          variant="reveals-primary"
          :disabled="submitting"
          @click="submit"
        >
          Unexclude
        </Button>
      </div>
    </div>
  </Modal>
</template>

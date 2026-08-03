<script setup lang="ts">
import { ref, computed } from 'vue';
import Modal from '@/components/ui/Modal.vue';
import TextArea from '@/components/ui/TextArea.vue';
import Button from '@/components/ui/Button.vue';
import Badge from '@/components/ui/Badge.vue';
import { formatAmount } from '@/utils/formatAmount';
import matchGroupService from '@/services/matchGroupService';
import toaster from '@/utils/toaster';
import type { ReconciliationEntry } from '@/types';

const props = defineProps<{ modelValue: boolean; entries: ReconciliationEntry[] }>();
const emit = defineEmits<{
  (e: 'update:modelValue', v: boolean): void;
  (e: 'forced'): void;
}>();

const comment = ref('');
const submitting = ref(false);

const total = computed(() =>
  props.entries.reduce((acc, e) => acc + Number(e.amount || 0), 0),
);
const isBalanced = computed(() => Math.abs(total.value) < 0.005);
const flowsConsistent = computed(() => {
  const flows = new Set(props.entries.map((e) => e.flow_id));
  const ccy = new Set(props.entries.map((e) => e.currency));
  return flows.size <= 1 && ccy.size <= 1;
});

const close = () => emit('update:modelValue', false);

const submit = async () => {
  if (!flowsConsistent.value) {
    toaster.error('Selected entries must share the same flow & currency');
    return;
  }
  if (!isBalanced.value) {
    toaster.error('Cannot force match: total amount must be 0');
    return;
  }
  submitting.value = true;
  try {
    await matchGroupService.forceMatch({
      entry_ids: props.entries.map((e) => e.id),
      comment: comment.value.trim() || undefined,
    });
    toaster.success('Match group forced');
    comment.value = '';
    emit('forced');
    close();
  } catch (e: any) {
    toaster.error(e?.response?.data?.detail || 'Force match failed');
  } finally {
    submitting.value = false;
  }
};
</script>

<template>
  <Modal :is-open="modelValue" @close="close" title="Force match">
    <div class="space-y-4">
      <div class="text-sm space-y-1">
        <div><strong>Selected:</strong> {{ entries.length }} entries</div>
        <div>
          <strong>Sum:</strong> <span class="tabular-nums" :class="total < 0 ? 'text-red-600' : ''">{{ formatAmount(total, entries[0]?.currency) }}</span>
          <Badge :variant="isBalanced ? 'success' : 'warning'">
            {{ isBalanced ? 'Balanced' : 'Unbalanced' }}
          </Badge>
        </div>
        <div v-if="!flowsConsistent" class="text-red-600 text-xs font-medium">
          Entries don't share the same flow / currency.
        </div>
      </div>
      <TextArea
        v-model="comment"
        label="Comment (optional)"
        rows="3"
      />
      <div class="flex justify-end gap-2">
        <Button variant="secondary" @click="close">Cancel</Button>
        <Button
          variant="reveals-primary"
          action="edit"
          :disabled="submitting || !flowsConsistent || !isBalanced || entries.length < 2"
          @click="submit"
        >Force match</Button>
      </div>
    </div>
  </Modal>
</template>

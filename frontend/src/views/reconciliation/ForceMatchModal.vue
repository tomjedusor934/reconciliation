<script setup lang="ts">
import { ref, computed } from 'vue';
import Modal from '@/components/ui/Modal.vue';
import TextArea from '@/components/ui/TextArea.vue';
import Button from '@/components/ui/Button.vue';
import Badge from '@/components/ui/Badge.vue';
import { formatAmount } from '@/utils/formatAmount';
import { fromMinor, sumMinor } from '@/utils/decimal';
import matchGroupService from '@/services/matchGroupService';
import toaster from '@/utils/toaster';
import type { BasketItem, ReconciliationEntry } from '@/types';

/** Works off either a live selection or a basket — both carry the fields the
 *  pre-flight checks and the request need. */
type Forceable = Pick<ReconciliationEntry, 'id' | 'flow_id' | 'currency' | 'amount'> &
  Partial<Pick<BasketItem, 'current_status' | 'missing'>>;

const props = defineProps<{ modelValue: boolean; entries: Forceable[] }>();
const emit = defineEmits<{
  (e: 'update:modelValue', v: boolean): void;
  (e: 'forced'): void;
}>();

const comment = ref('');
const submitting = ref(false);

// Exact, in minor units: the backend requires sum == Decimal("0") on the nose,
// so a float tolerance here would green-light groups it then rejects.
const totalMinor = computed(() => sumMinor(props.entries.map((e) => e.amount)));
const total = computed(() => fromMinor(totalMinor.value));
const isBalanced = computed(() => totalMinor.value === 0);
const flowsConsistent = computed(() => {
  const flows = new Set(props.entries.map((e) => e.flow_id));
  const ccy = new Set(props.entries.map((e) => e.currency));
  return flows.size <= 1 && ccy.size <= 1;
});
// Rows a basket refresh found already reconciled or gone — force_match would
// reject the whole group on them.
const staleCount = computed(
  () =>
    props.entries.filter(
      (e) => e.missing === true || (e.current_status !== undefined && e.current_status !== 'pending'),
    ).length,
);

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
        <div v-if="staleCount > 0" class="text-red-600 text-xs font-medium">
          {{ staleCount }} entr{{ staleCount === 1 ? 'y is' : 'ies are' }} no longer pending — remove
          them from the basket first.
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
          :disabled="submitting || !flowsConsistent || !isBalanced || staleCount > 0 || entries.length < 2"
          @click="submit"
        >Force match</Button>
      </div>
    </div>
  </Modal>
</template>

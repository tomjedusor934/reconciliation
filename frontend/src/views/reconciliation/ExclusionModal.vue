<script setup lang="ts">
import { ref, computed } from 'vue';
import Modal from '@/components/ui/Modal.vue';
import TextArea from '@/components/ui/TextArea.vue';
import Button from '@/components/ui/Button.vue';
import { formatDateShort } from '@/utils/formatDate';
import { formatAmount } from '@/utils/formatAmount';
import reconciliationService from '@/services/reconciliationService';
import { fromMinor, sumMinor } from '@/utils/decimal';
import toaster from '@/utils/toaster';
import type { ReconciliationEntry } from '@/types';

/** There is no bulk exclude endpoint, so this fans out one POST per entry.
 *  Since the operational view gained a "select all", that set can be thousands
 *  of rows — firing them all at once would swamp the API, so they go in bounded
 *  waves instead. */
const EXCLUDE_CONCURRENCY = 10;

const props = defineProps<{ modelValue: boolean; entry: ReconciliationEntry | null; entries?: ReconciliationEntry[] }>();
const emit = defineEmits<{
  (e: 'update:modelValue', v: boolean): void;
  (e: 'excluded'): void;
}>();

const reason = ref('');
const submitting = ref(false);
const done = ref(0);

const close = () => emit('update:modelValue', false);

const isMultiple = computed(() => props.entries && props.entries.length > 0);
const totalAmount = computed(() =>
  fromMinor(sumMinor((props.entries ?? []).map((e) => e.amount))),
);

/** Exclude in bounded waves. allSettled, not all: a rejection must not hide the
 *  fact that the rest went through — the entries are already excluded server
 *  side and the operator has to know which ones. */
const excludeMany = async (entries: ReconciliationEntry[], why: string) => {
  let ok = 0;
  const errors: string[] = [];
  done.value = 0;
  for (let i = 0; i < entries.length; i += EXCLUDE_CONCURRENCY) {
    const wave = entries.slice(i, i + EXCLUDE_CONCURRENCY);
    const results = await Promise.allSettled(
      wave.map((e) => reconciliationService.exclude({ entry_id: e.id, reason: why })),
    );
    for (const r of results) {
      if (r.status === 'fulfilled') ok += 1;
      else errors.push((r.reason as any)?.response?.data?.detail || 'unknown error');
    }
    done.value += wave.length;
  }
  return { ok, failed: errors.length, firstError: errors[0] };
};

const submit = async () => {
  if (!reason.value.trim()) {
    toaster.error('A reason is mandatory');
    return;
  }
  
  submitting.value = true;
  try {
    if (isMultiple.value && props.entries) {
      const { ok, failed, firstError } = await excludeMany(props.entries, reason.value.trim());
      if (ok === 0) {
        toaster.error(`Exclusion failed: ${firstError}`);
        return; // nothing was excluded — leave the modal open to retry
      }
      if (failed > 0) {
        // Partly applied: say so, the caller reloads and the rest stay pending.
        toaster.warning(`${ok} entries excluded, ${failed} failed (${firstError})`);
      } else {
        toaster.success(`${ok} entries excluded`);
      }
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
    done.value = 0;
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
          <template v-if="submitting && isMultiple">
            Excluding… {{ done }} / {{ entries?.length }}
          </template>
          <template v-else>
            {{ isMultiple ? `Exclude ${entries?.length} entries` : 'Exclude' }}
          </template>
        </Button>
      </div>
    </div>
  </Modal>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import { Check, Pencil, Plus, RefreshCw, Trash2, X } from 'lucide-vue-next';

import Drawer from '@/components/ui/Drawer.vue';
import Table, { type Column } from '@/components/ui/Table.vue';
import Button from '@/components/ui/Button.vue';
import Badge from '@/components/ui/Badge.vue';
import Input from '@/components/ui/Input.vue';
import Select from '@/components/ui/Select.vue';
import ForceMatchModal from './ForceMatchModal.vue';
import { formatDateShort } from '@/utils/formatDate';
import { formatAmount } from '@/utils/formatAmount';
import { fromMinor } from '@/utils/decimal';
import { useMatchBasketStore } from '@/stores/matchBasket';
import modal from '@/utils/modal';
import toaster from '@/utils/toaster';
import type { BasketItem, Flow } from '@/types';

const props = defineProps<{ modelValue: boolean; flows: Flow[] }>();
const emit = defineEmits<{
  (e: 'update:modelValue', v: boolean): void;
  (e: 'forced'): void;
}>();

const basket = useMatchBasketStore();

const close = () => emit('update:modelValue', false);

const flowName = (id: number | null) =>
  id === null ? '—' : props.flows.find((f) => f.id === id)?.code || `#${id}`;

const basketOptions = computed(() =>
  basket.baskets.map((b) => ({ value: b.id, label: `${b.name} (${b.items.length})` })),
);
// Select's model is string | number | null; the store validates the id.
const activeSelection = computed<string | number | null>({
  get: () => basket.activeId,
  set: (v) => { if (typeof v === 'string') basket.setActive(v); },
});

// ── Rename, inline (no browser dialog) ──────────────────────────────
const renaming = ref(false);
const draftName = ref('');
const startRename = () => {
  if (!basket.active) return;
  draftName.value = basket.active.name;
  renaming.value = true;
};
const commitRename = () => {
  if (basket.active && draftName.value.trim()) {
    basket.renameBasket(basket.active.id, draftName.value);
  }
  renaming.value = false;
};
// Switching basket or closing the panel must not leave a half-typed rename.
watch(() => basket.activeId, () => { renaming.value = false; });
watch(() => props.modelValue, (open) => { if (!open) renaming.value = false; });

const confirmDelete = () => {
  const target = basket.active;
  if (!target) return;
  modal.open({
    title: 'Supprimer le panier',
    message: `Supprimer « ${target.name} » et ses ${target.items.length} ligne(s) ? Les écritures elles-mêmes ne sont pas touchées.`,
    buttons: [
      { label: 'Annuler', variant: 'secondary', action: () => modal.close() },
      {
        label: 'Supprimer',
        variant: 'danger',
        action: () => {
          basket.deleteBasket(target.id);
          modal.close();
        },
      },
    ],
  });
};

// ── Totals ──────────────────────────────────────────────────────────
const currency = computed(() => basket.active?.currency ?? undefined);
const total = computed(() => fromMinor(basket.totalMinor));
const debitTotal = computed(() => fromMinor(basket.debitMinor));
const creditTotal = computed(() => fromMinor(basket.creditMinor));

// ── Staleness ───────────────────────────────────────────────────────
const staleCount = computed(() => basket.staleItems.length);
const staleLabel = (item: BasketItem) =>
  item.missing ? 'introuvable' : item.current_status ?? '';

const runRefresh = async () => {
  try {
    const stale = await basket.refresh();
    if (stale === 0) toaster.success('Toutes les lignes du panier sont encore rapprochables');
    else toaster.warning(`${stale} ligne(s) ne sont plus « pending »`);
  } catch {
    toaster.error('Échec du rafraîchissement des statuts');
  }
};

// ── Force ───────────────────────────────────────────────────────────
const showForce = ref(false);
const canForce = computed(
  () => basket.count >= 2 && basket.isBalanced && staleCount.value === 0,
);
const onForced = () => {
  basket.clear();
  emit('forced');
  close();
};

const columns: Column[] = [
  { key: 'reco_id', label: 'Reco ID', sortable: true },
  { key: 'amount', label: 'Montant', sortable: true, format: 'amount', align: 'right' },
  { key: 'value_date', label: 'Date valeur', sortable: true },
  { key: 'transaction_particulars', label: 'Particulars' },
  { key: 'ref_no', label: 'Ref no' },
  { key: 'state', label: 'État' },
  { key: 'remove', label: '', pinned: true },
];
</script>

<template>
  <Drawer :is-open="modelValue" size="2xl" title="Panier de rapprochement" @close="close">
    <div class="space-y-4">
      <!-- Basket switcher -->
      <div class="flex flex-wrap items-end gap-2">
        <div v-if="!renaming" class="min-w-[16rem]">
          <Select v-model="activeSelection" label="Panier" :options="basketOptions" />
        </div>
        <div v-else class="min-w-[16rem]">
          <Input v-model="draftName" label="Nom du panier" @keyup.enter="commitRename" />
        </div>

        <Button v-if="!renaming" variant="secondary" size="sm" @click="startRename" :disabled="!basket.active">
          <Pencil class="w-4 h-4 inline-block mr-1" /> Renommer
        </Button>
        <Button v-else variant="reveals-primary" size="sm" @click="commitRename">
          <Check class="w-4 h-4 inline-block mr-1" /> Valider
        </Button>

        <Button variant="secondary" size="sm" @click="basket.createBasket()">
          <Plus class="w-4 h-4 inline-block mr-1" /> Nouveau
        </Button>
        <Button variant="danger" size="sm" :disabled="!basket.active" @click="confirmDelete">
          <Trash2 class="w-4 h-4 inline-block mr-1" /> Supprimer
        </Button>
      </div>

      <!-- Lock + totals -->
      <div class="rounded-lg border border-gray-200 bg-gray-50 px-4 py-3 text-sm">
        <div class="flex flex-wrap items-center gap-x-6 gap-y-1">
          <span class="text-gray-500">
            Flux <strong class="text-space-indigo">{{ flowName(basket.active?.flow_id ?? null) }}</strong>
            · Devise <strong class="text-space-indigo">{{ basket.active?.currency ?? '—' }}</strong>
          </span>
          <span class="text-gray-500">{{ basket.count }} ligne(s)</span>
        </div>
        <div class="mt-2 flex flex-wrap items-center gap-x-6 gap-y-1 tabular-nums">
          <span>Débits <strong class="text-red-600">{{ formatAmount(debitTotal, currency) }}</strong></span>
          <span>Crédits <strong class="text-emerald-700">{{ formatAmount(creditTotal, currency) }}</strong></span>
          <span>
            Σ <strong :class="total < 0 ? 'text-red-600' : ''">{{ formatAmount(total, currency) }}</strong>
            <Badge class="ml-2" :variant="basket.isBalanced ? 'success' : 'warning'">
              {{ basket.isBalanced ? 'Équilibré' : 'Écart' }}
            </Badge>
          </span>
        </div>
        <p v-if="!basket.isEmpty && !basket.isBalanced" class="mt-2 text-xs text-gray-500">
          Le forçage exige une somme strictement nulle : continue d'ajouter les jambes manquantes.
        </p>
      </div>

      <!-- Stale warning -->
      <div
        v-if="staleCount > 0"
        class="rounded-lg border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-800"
      >
        {{ staleCount }} ligne(s) ne sont plus « pending » (rapprochées ou exclues entre-temps) et
        bloquent le forçage.
        <button type="button" class="ml-1 font-medium underline" @click="basket.removeStale()">
          Les retirer du panier
        </button>
      </div>

      <!-- Items -->
      <div v-if="basket.isEmpty" class="py-10 text-center text-sm text-gray-500">
        Panier vide. Sélectionne des lignes dans la vue opérationnelle puis « Ajouter au panier ».
      </div>
      <Table
        v-else
        :columns="columns"
        :items="basket.items"
        searchable
        pagination
        :items-per-page="25"
        search-placeholder="Filtrer le panier…"
      >
        <template #cell-value_date="{ item }">{{ formatDateShort(item.value_date) }}</template>
        <template #cell-state="{ item }">
          <Badge v-if="basket.isStale(item as BasketItem)" variant="warning">
            {{ staleLabel(item as BasketItem) }}
          </Badge>
          <span v-else class="text-gray-300">—</span>
        </template>
        <template #cell-remove="{ item }">
          <button
            type="button"
            class="rounded p-1 text-gray-400 transition-colors hover:bg-red-50 hover:text-red-600"
            title="Retirer du panier"
            @click="basket.remove(item.id)"
          >
            <X class="h-4 w-4" />
          </button>
        </template>
      </Table>

      <!-- Actions -->
      <div class="flex flex-wrap justify-end gap-2 border-t border-gray-200 pt-3">
        <Button
          variant="secondary"
          :disabled="basket.isEmpty || basket.refreshing"
          @click="runRefresh"
        >
          <RefreshCw class="mr-1 inline-block h-4 w-4" :class="basket.refreshing ? 'animate-spin' : ''" />
          Rafraîchir les statuts
        </Button>
        <Button variant="secondary" :disabled="basket.isEmpty" @click="basket.clear()">Vider</Button>
        <Button
          variant="reveals-primary"
          action="update"
          :disabled="!canForce"
          @click="showForce = true"
        >
          Forcer le rapprochement
        </Button>
      </div>
    </div>
  </Drawer>

  <ForceMatchModal v-model="showForce" :entries="basket.items" @forced="onForced" />
</template>

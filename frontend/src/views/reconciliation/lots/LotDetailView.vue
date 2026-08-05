<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { AlertTriangle, ArrowLeft, ExternalLink } from 'lucide-vue-next';
import Card from '@/components/ui/Card.vue';
import Button from '@/components/ui/Button.vue';
import Badge from '@/components/ui/Badge.vue';
import Loader from '@/components/ui/Loader.vue';
import LotGraph from '@/components/lots/LotGraph.vue';
import LotGraphAggregate from '@/components/lots/LotGraphAggregate.vue';
import LotKeyDrawer from '@/components/lots/LotKeyDrawer.vue';
import SplitDrawer from '@/components/lots/SplitDrawer.vue';
import LotMembersTable from '@/components/lots/LotMembersTable.vue';
import type { LotGraphFocus } from '@/utils/lotAggregateGraph';
import { formatAmount } from '@/utils/formatAmount';
import { formatDateShort } from '@/utils/formatDate';
import lotService from '@/services/lotService';
import toaster from '@/utils/toaster';
import type { LotDetail, LotGraphData, LotTypeDirectionGroup } from '@/types';

const route = useRoute();
const router = useRouter();

const loading = ref(false);
const detail = ref<LotDetail | null>(null);
const graph = ref<LotGraphData | null>(null);

// Graph-driven filters for the members table (click a node → filter).
const filterType = ref<string | null>(null);
const filterDirection = ref<string | null>(null);
const filterKey = ref<{ key_type: string; key_value: string } | null>(null);

// Drawer (click a link node → list its values) + graph focus (pick a value →
// show only its related movement nodes).
const drawerOpen = ref(false);
const drawerKeyType = ref<string | null>(null);
const focus = ref<LotGraphFocus | null>(null);

// Split drawer (click a ghost → the real movement it is a slice of).
const splitHash = ref<string | null>(null);

const lot = computed(() => detail.value?.lot ?? null);
// Giant lots ship without inlined members: aggregate graph + paginated table.
const isAggregate = computed(() => detail.value?.members_included === false);

/** What the bucket stands for, e.g. "26070613550600068 × LUXEMBOURG". */
const bucketLabel = computed(() => {
  if (!lot.value || lot.value.bucket_kind === 'LEGACY') return null;
  if (lot.value.bucket_kind === 'RESIDUAL') return 'Unexplained remainder of a split';
  const parts = [lot.value.bucket_pacs008, lot.value.bucket_msgid, lot.value.bucket_po].filter(
    Boolean
  );
  return parts.length ? parts.join(' × ') : null;
});

const typeCounts = computed<Record<string, number>>(() => {
  if (isAggregate.value) return graph.value?.meta.type_counts ?? {};
  const counts: Record<string, number> = {};
  for (const m of detail.value?.members ?? []) {
    counts[m.movement_type] = (counts[m.movement_type] || 0) + 1;
  }
  return counts;
});

const typeCountEntries = computed(() =>
  Object.entries(typeCounts.value).sort(([a], [b]) => a.localeCompare(b))
);

const statusVariant = computed(() =>
  lot.value?.status === 'matched' ? 'success' : lot.value?.status === 'merged' ? 'info' : 'warning'
);

const fetchDetail = async () => {
  const lotId = String(route.params.id || '');
  if (!lotId) return;
  loading.value = true;
  graph.value = null;
  filterType.value = null;
  filterDirection.value = null;
  filterKey.value = null;
  focus.value = null;
  drawerOpen.value = false;
  drawerKeyType.value = null;
  splitHash.value = null;
  try {
    const { data } = await lotService.get(lotId);
    detail.value = data;
    if (data.members_included === false) {
      const { data: graphData } = await lotService.getGraph(lotId);
      graph.value = graphData;
    }
  } catch (e) {
    detail.value = null;
    toaster.error('Lot not found');
  } finally {
    loading.value = false;
  }
};

// Click a movement node → filter the table to that (type, direction).
const onSelectGroup = (group: LotTypeDirectionGroup) => {
  filterKey.value = null;
  filterType.value = group.movement_type;
  filterDirection.value = group.direction;
};

// Click a link node → open the drawer listing that key type's values.
const onOpenKeyType = (keyType: string) => {
  drawerKeyType.value = keyType;
  drawerOpen.value = true;
};

// Pick a value in the drawer → filter the table to it AND focus the graph on
// its related movement nodes (fetched scoped to that value).
const onDrawerSelectValue = async ({
  key_type,
  key_value,
}: {
  key_type: string;
  key_value: string;
}) => {
  filterType.value = null;
  filterDirection.value = null;
  filterKey.value = { key_type, key_value };
  try {
    const { data } = await lotService.getGraph(String(route.params.id || ''), {
      key_type,
      key_value,
    });
    focus.value = { keyType: key_type, keyValue: key_value, groups: data.groups };
  } catch (e) {
    toaster.error('Failed to load related nodes');
  }
};

// Reset: back to the full mega graph + clear the table's value filter.
const onClearFocus = () => {
  focus.value = null;
  filterKey.value = null;
};

// Clearing the value chip in the table also un-focuses the graph.
watch(filterKey, (v) => {
  if (!v) focus.value = null;
});

const openOperational = () => {
  if (!lot.value) return;
  router.push({
    path: '/reconciliation/operational',
    query: { reco_id: lot.value.lot_id, status: '' },
  });
};

// Click a ghost → the real movement it came from, and its sibling slices.
const onOpenSplit = (parentHash: string) => {
  splitHash.value = parentHash;
};

// Jump from a sibling slice to the bucket it settled in.
const onOpenSplitLot = (lotId: string) => {
  splitHash.value = null;
  if (lotId !== lot.value?.lot_id) router.push(`/reconciliation/lots/${lotId}`);
};

const openSurvivor = () => {
  if (lot.value?.merged_into_lot_id) {
    router.push(`/reconciliation/lots/${lot.value.merged_into_lot_id}`);
  }
};

watch(() => route.params.id, fetchDetail);
onMounted(fetchDetail);
</script>

<template>
  <div class="flex justify-between items-center mb-6">
    <div class="flex items-center gap-3 min-w-0">
      <Button variant="secondary" size="sm" @click="router.push('/reconciliation/lots')">
        <ArrowLeft class="h-4 w-4" />
      </Button>
      <h1 class="text-2xl font-bold text-space-indigo truncate" :title="lot?.lot_id">
        Lot
        <span v-if="bucketLabel" class="font-mono text-xl">{{ bucketLabel }}</span>
        <span v-else class="font-mono text-xl">{{ lot?.lot_id || route.params.id }}</span>
      </h1>
      <template v-if="lot">
        <Badge :variant="statusVariant">{{ lot.status }}</Badge>
        <Badge :variant="lot.is_balanced ? 'success' : 'warning'">
          {{ lot.is_balanced ? 'Balanced' : 'Unbalanced' }}
        </Badge>
        <Badge v-if="lot.currencies.length > 1" variant="danger">multi-currency</Badge>
        <Badge v-if="lot.bucket_kind === 'RESIDUAL'" variant="danger">residual</Badge>
        <Badge
          v-if="lot.synthetic_only"
          variant="info"
          title="Every movement here is a slice of a batch, so the bucket nets to zero by construction — the real proof is each parent's conservation"
        >
          proven by construction
        </Badge>
      </template>
    </div>
    <Button v-if="lot" variant="reveals-primary" @click="openOperational">
      <ExternalLink class="h-4 w-4 mr-1" /> Open in Operational view
    </Button>
  </div>

  <div v-if="loading" class="flex justify-center py-10"><Loader size="lg" /></div>

  <template v-else-if="detail && lot">
    <Card v-if="lot.merge_conflict" class="mb-4 border-amber-300 bg-amber-50">
      <div class="flex items-center gap-2 text-amber-800 text-sm">
        <AlertTriangle class="h-4 w-4 shrink-0" />
        This lot absorbed another lot AFTER some of its entries were already reconciled — those
        émargement entries keep the absorbed lot's reco_id.
      </div>
    </Card>
    <Card v-if="lot.status === 'merged'" class="mb-4 border-sky-300 bg-sky-50">
      <div class="flex items-center justify-between gap-2 text-sky-800 text-sm">
        <span>
          This lot was merged into
          <span class="font-mono">{{ lot.merged_into_lot_id }}</span> — its movements now live there.
        </span>
        <Button variant="secondary" size="sm" @click="openSurvivor">View surviving lot</Button>
      </div>
    </Card>

    <Card class="mb-4">
      <div class="grid grid-cols-2 md:grid-cols-5 gap-4">
        <div>
          <div class="text-xs text-gray-500">Total debit</div>
          <div class="font-semibold text-red-600 tabular-nums">
            {{ formatAmount(lot.total_debit, lot.currency) }}
          </div>
        </div>
        <div>
          <div class="text-xs text-gray-500">Total credit</div>
          <div class="font-semibold text-green-600 tabular-nums">
            {{ formatAmount(lot.total_credit, lot.currency) }}
          </div>
        </div>
        <div>
          <div class="text-xs text-gray-500">Net</div>
          <div
            class="font-semibold tabular-nums"
            :class="Number(lot.net_amount) === 0 ? 'text-gray-800' : 'text-amber-600'"
          >
            {{ formatAmount(lot.net_amount, lot.currency) }}
          </div>
          <div
            v-if="lot.pending_payment_count"
            class="text-[11px] text-orange-500 tabular-nums mt-0.5"
            :title="`${lot.pending_payment_count} movement(s) with a pending (PDNG) payment — the net still to settle`"
          >
            en attente (PDNG) : {{ formatAmount(lot.pending_payment_amount, lot.currency) }}
          </div>
        </div>
        <div>
          <div class="text-xs text-gray-500">Movements</div>
          <div class="font-semibold">
            {{ lot.member_count.toLocaleString() }}
            <span class="text-xs text-gray-500 font-normal">
              ({{ lot.pending_count.toLocaleString() }} pending ·
              {{ lot.matched_count.toLocaleString() }} matched<template v-if="lot.excluded_count">
                · {{ lot.excluded_count.toLocaleString() }} excluded</template
              >)
            </span>
          </div>
        </div>
        <div>
          <div class="text-xs text-gray-500">Value dates</div>
          <div class="font-semibold">
            {{ lot.first_value_date ? formatDateShort(lot.first_value_date) : '—' }}
            <span class="text-gray-400">→</span>
            {{ lot.last_value_date ? formatDateShort(lot.last_value_date) : '—' }}
          </div>
        </div>
      </div>
      <div
        v-if="typeCountEntries.length"
        class="mt-3 flex flex-wrap gap-2 border-t border-gray-100 pt-3"
      >
        <span
          v-for="[type, count] in typeCountEntries"
          :key="type"
          class="inline-flex items-center gap-1 rounded-full bg-gray-100 px-2.5 py-0.5 text-xs text-gray-700"
        >
          <span class="font-semibold">{{ type }}</span> × {{ count.toLocaleString() }}
        </span>
      </div>
    </Card>

    <div class="mb-4">
      <LotGraphAggregate
        v-if="isAggregate && graph"
        :graph="graph"
        :focus="focus"
        @select-group="onSelectGroup"
        @open-key-type="onOpenKeyType"
        @clear-focus="onClearFocus"
      />
      <div v-else-if="isAggregate" class="flex justify-center py-10"><Loader size="lg" /></div>
      <LotGraph
        v-else
        :members="detail.members"
        :keys="detail.keys"
        @open-split="onOpenSplit"
      />
    </div>

    <LotMembersTable
      :lot-id="lot.lot_id"
      :type-counts="typeCounts"
      v-model:filter-type="filterType"
      v-model:filter-direction="filterDirection"
      v-model:filter-key="filterKey"
      @open-split="onOpenSplit"
    />

    <LotKeyDrawer
      :is-open="drawerOpen"
      :lot-id="lot.lot_id"
      :key-type="drawerKeyType"
      :selected-value="filterKey?.key_value ?? null"
      @close="drawerOpen = false"
      @select-value="onDrawerSelectValue"
    />

    <SplitDrawer
      :is-open="splitHash !== null"
      :parent-hash="splitHash"
      :current-lot-id="lot.lot_id"
      @close="splitHash = null"
      @open-lot="onOpenSplitLot"
    />
  </template>

  <Card v-else>
    <div class="py-8 text-center text-gray-500">Lot not found.</div>
  </Card>
</template>

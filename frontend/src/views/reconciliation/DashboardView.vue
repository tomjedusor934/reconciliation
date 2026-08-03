<script setup lang="ts">
import { ref, onMounted, computed, watch } from 'vue';
import { useRouter } from 'vue-router';
import Card from '@/components/ui/Card.vue';
import Loader from '@/components/ui/Loader.vue';
import Modal from '@/components/ui/Modal.vue';
import Button from '@/components/ui/Button.vue';
import { formatDate, formatDateShort } from '@/utils/formatDate';
import { formatCompactAmount } from '@/utils/formatAmount';
import dashboardService from '@/services/dashboardService';
import toaster from '@/utils/toaster';
import { useAuthStore } from '@/stores/auth';
import type { DashboardData, FlowKPI, IngestionCalendarDay } from '@/types';

const router = useRouter();
const loading = ref(true);
const data = ref<DashboardData | null>(null);

// Per-user show/hide of flow cards — UI preference only, kept in localStorage.
const authStore = useAuthStore();
const storageKey = computed(() => `reco:dashboard:hiddenFlows:${authStore.user?.id ?? 'anon'}`);
const hiddenIds = ref<Set<number>>(new Set());
const showHidden = ref(false);

const loadHidden = () => {
  try {
    const raw = localStorage.getItem(storageKey.value);
    hiddenIds.value = new Set(raw ? (JSON.parse(raw) as number[]) : []);
  } catch {
    hiddenIds.value = new Set();
  }
};
const persistHidden = () => {
  localStorage.setItem(storageKey.value, JSON.stringify([...hiddenIds.value]));
};
const toggleHidden = (kpi: FlowKPI, event: Event) => {
  event.stopPropagation();
  const next = new Set(hiddenIds.value);
  if (next.has(kpi.flow_id)) next.delete(kpi.flow_id);
  else next.add(kpi.flow_id);
  hiddenIds.value = next;
  if (next.size === 0) showHidden.value = false;
  persistHidden();
};
const visibleFlows = computed<FlowKPI[]>(() => {
  const flows = data.value?.flows || [];
  return showHidden.value ? flows : flows.filter((f) => !hiddenIds.value.has(f.flow_id));
});
// Reload when the user id resolves (auth may populate after mount).
watch(storageKey, loadHidden);

// Calendar modal state
const showCalendarModal = ref(false);
const calendarFlowId = ref<number>(0);
const calendarFlowName = ref('');
const calendarYear = ref(new Date().getFullYear());
const calendarMonth = ref(new Date().getMonth() + 1); // 1-based
const calendarDays = ref<IngestionCalendarDay[]>([]);
const calendarLoading = ref(false);

const WEEKDAY_LABELS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];

const calendarMonthLabel = computed(() => {
  const d = new Date(calendarYear.value, calendarMonth.value - 1);
  return d.toLocaleDateString('fr-FR', { month: 'long', year: 'numeric' });
});

const calendarGrid = computed(() => {
  const year = calendarYear.value;
  const month = calendarMonth.value - 1; // JS 0-based
  const firstDay = new Date(year, month, 1);
  const lastDay = new Date(year, month + 1, 0);
  const daysInMonth = lastDay.getDate();

  // Build lookup: day → count
  const countMap: Record<number, number> = {};
  for (const d of calendarDays.value) {
    countMap[d.day] = d.count;
  }

  // Monday = 0, Sunday = 6 (ISO weekday)
  let startWeekday = firstDay.getDay() - 1;
  if (startWeekday < 0) startWeekday = 6;

  const cells: { day: number | null; count: number }[] = [];
  // Leading blanks
  for (let i = 0; i < startWeekday; i++) cells.push({ day: null, count: 0 });
  // Days
  for (let d = 1; d <= daysInMonth; d++) {
    cells.push({ day: d, count: countMap[d] || 0 });
  }
  // Trailing blanks to fill last row
  while (cells.length % 7 !== 0) cells.push({ day: null, count: 0 });

  return cells;
});

const load = async () => {
  loading.value = true;
  try {
    const resp = await dashboardService.overview();
    console.log('Dashboard data:', resp.data);
    data.value = resp.data;
  } catch (e) {
    console.error(e);
    toaster.error('Failed to load dashboard');
  } finally {
    loading.value = false;
  }
};

const loadCalendar = async () => {
  calendarLoading.value = true;
  try {
    const resp = await dashboardService.ingestionCalendar(
      calendarFlowId.value, calendarYear.value, calendarMonth.value
    );
    calendarDays.value = resp.data.days || [];
  } catch (e) {
    console.error(e);
    toaster.error('Failed to load ingestion calendar');
  } finally {
    calendarLoading.value = false;
  }
};

const goToOperational = (kpi: FlowKPI) => {
  router.push({ path: '/reconciliation/operational', query: { flow_id: String(kpi.flow_id), status: 'pending' } });
};

const openCalendarModal = (kpi: FlowKPI, event: Event) => {
  event.stopPropagation();
  calendarFlowId.value = kpi.flow_id;
  calendarFlowName.value = kpi.flow_name;
  calendarYear.value = new Date().getFullYear();
  calendarMonth.value = new Date().getMonth() + 1;
  calendarDays.value = [];
  showCalendarModal.value = true;
  loadCalendar();
};

const prevMonth = () => {
  if (calendarMonth.value === 1) {
    calendarMonth.value = 12;
    calendarYear.value--;
  } else {
    calendarMonth.value--;
  }
  loadCalendar();
};

const nextMonth = () => {
  if (calendarMonth.value === 12) {
    calendarMonth.value = 1;
    calendarYear.value++;
  } else {
    calendarMonth.value++;
  }
  loadCalendar();
};

onMounted(() => {
  loadHidden();
  load();
});
</script>

<template>
  <div class="flex justify-between items-center mb-6">
    <h1 class="text-2xl font-bold text-space-indigo">Reconciliation Dashboard</h1>
    <div class="flex items-center gap-3">
      <span v-if="data" class="text-sm text-gray-500">
        <span class="font-semibold text-green-600">{{ data.total_matched_count }}</span> entries matched
        <template v-if="data.last_reconciliation_run">
          — Last run: {{ formatDate(data.last_reconciliation_run.started_at) }}
          <template v-if="data.last_reconciliation_run.entries_matched > 0">
            ({{ data.last_reconciliation_run.entries_matched }} new)
          </template>
        </template>
      </span>
      <Button
        v-if="hiddenIds.size > 0"
        size="sm"
        variant="secondary"
        @click="showHidden = !showHidden"
      >
        {{ showHidden ? 'Hide hidden' : `Show hidden (${hiddenIds.size})` }}
      </Button>
    </div>
  </div>

  <div v-if="loading" class="flex justify-center py-10"><Loader size="lg" /></div>

  <div v-else class="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
    <Card
      v-for="kpi in visibleFlows"
      :key="kpi.flow_id"
      class="cursor-pointer hover:ring-2 hover:ring-indigo-300 transition-all"
      :class="{ 'opacity-50': hiddenIds.has(kpi.flow_id) }"
      @click="goToOperational(kpi)"
    >
      <!-- Header -->
      <div class="flex items-start justify-between mb-4">
        <div>
          <h2 class="text-lg font-semibold">{{ kpi.flow_name }}</h2>
          <span class="text-xs text-gray-500">{{ kpi.flow_code }}</span>
          <p v-if="!kpi.is_active" class="text-xs text-red-500 font-medium mt-1">Inactive flow</p>
        </div>
        <button
          @click="toggleHidden(kpi, $event)"
          class="p-1 -mr-1 text-gray-400 hover:text-gray-700 transition-colors shrink-0"
          :title="hiddenIds.has(kpi.flow_id) ? 'Show on dashboard' : 'Hide from dashboard'"
        >
          <!-- eye (hidden → click to show) -->
          <svg v-if="hiddenIds.has(kpi.flow_id)" class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M2.458 12C3.732 7.943 7.523 5 12 5c4.478 0 8.268 2.943 9.542 7-1.274 4.057-5.064 7-9.542 7-4.477 0-8.268-2.943-9.542-7z" />
          </svg>
          <!-- eye-off (visible → click to hide) -->
          <svg v-else class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13.875 18.825A10.05 10.05 0 0112 19c-4.478 0-8.268-2.943-9.543-7a9.97 9.97 0 011.563-3.029m5.858.908a3 3 0 114.243 4.243M9.878 9.878l4.242 4.242M9.88 9.88l-3.29-3.29m7.532 7.532l3.29 3.29M3 3l18 18" />
          </svg>
        </button>
      </div>

      <!-- Central: pending count (prominent) + credit/debit split -->
      <div class="text-center mb-4 py-3 bg-orange-50 rounded-lg">
        <div class="text-4xl font-extrabold text-orange-600">{{ kpi.pending_count }}</div>
        <div class="text-sm text-orange-500 font-medium mt-1">Pending rows</div>
        <div
          v-if="Number(kpi.pending_amount_credit) || Number(kpi.pending_amount_debit)"
          class="mt-2 flex items-center justify-center gap-3 text-xs tabular-nums"
        >
          <span class="text-green-600" title="Sum of pending credits">
            + {{ formatCompactAmount(kpi.pending_amount_credit) }}
          </span>
          <span class="text-red-600" title="Sum of pending debits">
            − {{ formatCompactAmount(Math.abs(Number(kpi.pending_amount_debit))) }}
          </span>
        </div>
      </div>

      <!-- Metrics grid -->
      <div class="grid grid-cols-3 gap-2 text-center mb-4">
        <div>
          <div class="text-xs text-gray-500">Matched</div>
          <div class="text-xl font-bold text-green-600">{{ kpi.matched_count }}</div>
        </div>
        <div>
          <div class="text-xs text-gray-500">Excluded</div>
          <div class="text-xl font-bold text-gray-500">{{ kpi.excluded_count }}</div>
        </div>
        <div>
          <div class="text-xs text-gray-500">New entries</div>
          <div class="text-xl font-bold text-blue-600">
            <template v-if="kpi.new_entries_delta > 0">+{{ kpi.new_entries_delta }}</template>
            <template v-else>{{ kpi.new_entries_delta }}</template>
          </div>
        </div>
      </div>

      <!-- Bottom info -->
      <div class="flex items-center justify-between text-xs text-gray-400 border-t pt-3">
        <div>
          <span v-if="kpi.oldest_unmatched_date">
            Oldest pending: <span class="font-medium text-gray-600">{{ formatDateShort(kpi.oldest_unmatched_date) }}</span>
          </span>
          <span v-else>No pending</span>
        </div>
        <Button
          size="sm"
          variant="secondary"
          @click="openCalendarModal(kpi, $event)"
          class="text-xs"
        >+</Button>
      </div>

      <div v-if="kpi.last_ingestion_at" class="mt-2 text-xs text-gray-400">
        Last ingestion: {{ formatDate(kpi.last_ingestion_at) }}
      </div>
    </Card>
  </div>

  <!-- Ingestion calendar modal -->
  <Modal :isOpen="showCalendarModal" @close="showCalendarModal = false" :title="`Ingestion — ${calendarFlowName}`">
    <!-- Month navigation -->
    <div class="flex items-center justify-between mb-4">
      <button @click="prevMonth" class="p-2 hover:bg-gray-100 rounded-lg transition-colors">
        <svg class="w-5 h-5 text-gray-600" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 19l-7-7 7-7" />
        </svg>
      </button>
      <h3 class="text-lg font-semibold text-gray-700">{{ calendarMonthLabel }}</h3>
      <button @click="nextMonth" class="p-2 hover:bg-gray-100 rounded-lg transition-colors">
        <svg class="w-5 h-5 text-gray-600" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5l7 7-7 7" />
        </svg>
      </button>
    </div>

    <div v-if="calendarLoading" class="flex justify-center py-8"><Loader size="md" /></div>

    <div v-else>
      <!-- Weekday headers -->
      <div class="grid grid-cols-7 gap-1 mb-1">
        <div v-for="label in WEEKDAY_LABELS" :key="label"
          class="text-center text-xs font-medium text-gray-400 py-1">
          {{ label }}
        </div>
      </div>

      <!-- Calendar grid -->
      <div class="grid grid-cols-7 gap-1">
        <div
          v-for="(cell, idx) in calendarGrid"
          :key="idx"
          class="aspect-square flex flex-col items-center justify-center rounded-lg text-sm border"
          :class="cell.day == null
            ? 'border-transparent'
            : cell.count > 0
              ? 'border-blue-200 bg-blue-50'
              : 'border-gray-100 bg-white'"
        >
          <template v-if="cell.day != null">
            <span class="text-xs text-gray-500">{{ cell.day }}</span>
            <span v-if="cell.count > 0" class="text-sm font-bold text-blue-600">{{ cell.count }}</span>
          </template>
        </div>
      </div>
    </div>
  </Modal>
</template>

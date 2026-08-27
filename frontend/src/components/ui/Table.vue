<script setup lang="ts">
import { ref, computed, watch, onMounted, onBeforeUnmount, nextTick } from 'vue';
import { ArrowUpDown, ArrowUp, ArrowDown, Search as SearchIcon, Settings2, GripVertical, Eye, EyeOff, X, Check, Filter } from 'lucide-vue-next';
import Input from './Input.vue';
import Pagination from './Pagination.vue';
import Select from './Select.vue';
import Amount from './Amount.vue';

export interface Column {
  key: string;
  label: string;
  sortable?: boolean;
  class?: string;
  visible?: boolean;
  width?: number;
  resizable?: boolean;
  pinned?: boolean; // Columns that can't be hidden or reordered (e.g. actions)
  align?: 'left' | 'right' | 'center'; // Cell/header text alignment (numbers → 'right')
  format?: 'amount'; // Render the default cell as a formatted monetary amount
}

const props = withDefaults(defineProps<{
  items?: Record<string, any>[];
  columns?: Column[];
  headers?: string[]; // Fallback for backward compatibility usage
  searchable?: boolean;
  searchPlaceholder?: string;
  theme?: 'default' | 'reveals';
  pagination?: boolean;
  itemsPerPage?: number;
  storageKey?: string; // Key for persisting column preferences in localStorage
  clickableRows?: boolean;
}>(), {
  items: () => [],
  searchable: false,
  searchPlaceholder: 'Search...',
  theme: 'default',
  pagination: false,
  itemsPerPage: 10,
  clickableRows: false,
});

const emit = defineEmits<{
  (e: 'row-click', item: Record<string, any>): void
  (e: 'page-change', page: number): void
  // Rows left after this component's own search + column filters. Emitted so a
  // parent can offer a "select all" that matches what is actually displayed —
  // filteredAndSortedItems is internal state otherwise.
  (e: 'visible-items', items: Record<string, any>[]): void
}>();

function handleRowClick(item: Record<string, any>, event: MouseEvent) {
  if (!props.clickableRows) return
  // Don't trigger row click if user clicked a button or link inside the row
  const target = event.target as HTMLElement
  if (target.closest('button, a, [role="button"]')) return
  emit('row-click', item)
}

const searchQuery = ref('');
const sortKey = ref<string | null>(null);
const sortOrder = ref<'asc' | 'desc'>('asc');

// Pagination state
const currentPage = ref(1);
const pageSize = ref(props.itemsPerPage);
const pageSizeOptions = [
  { value: 10, label: '10' },
  { value: 25, label: '25' },
  { value: 50, label: '50' },
  { value: 100, label: '100' }
];

// ── Column management state ──
const managedColumns = ref<Column[]>([]);
const showColumnConfig = ref(false);
const tempColumns = ref<Column[]>([]);
const draggedColumn = ref<number | null>(null);
const draggedConfigColumn = ref<number | null>(null);

// ── Column value filter (double-click) ──
const columnFilterKey = ref<string | null>(null);
const columnFilterValues = ref<string[]>([]);
const columnFilterSelected = ref<Set<string>>(new Set());
const columnFilterSearch = ref('');
const columnFilterPosition = ref({ top: 0, left: 0 });
const columnFilterRef = ref<HTMLElement | null>(null);

// ── Column resizing ──
const resizingColumn = ref<number | null>(null);
const resizeStartX = ref(0);
const resizeStartWidth = ref(0);

// ── Column filters (active per-column filters) ──
const activeColumnFilters = ref<Record<string, Set<string>>>({});

// Initialize managedColumns from props
function initColumns() {
  const base = props.columns
    ? props.columns.map((c: any) => ({
        ...c,
        visible: c.visible !== false,
        width: c.width || 0,
        resizable: c.resizable !== false,
        pinned: c.pinned || false,
      }))
    : props.headers
      ? props.headers.map((h: string) => ({
          key: h.toLowerCase(),
          label: h,
          sortable: false,
          visible: true,
          width: 0,
          resizable: true,
          pinned: false,
        }))
      : [];

  // Try to restore from localStorage
  if (props.storageKey) {
    const saved = localStorage.getItem(`table-columns-${props.storageKey}`);
    if (saved) {
      try {
        const parsed = JSON.parse(saved) as { key: string; visible: boolean; width: number }[];
        // Merge saved prefs with current columns (in case columns definition changed)
        const savedMap = new Map(parsed.map(c => [c.key, c]));
        const ordered: Column[] = [];
        // First add columns in saved order
        for (const s of parsed) {
          const col = base.find((c: any) => c.key === s.key);
          if (col) {
            ordered.push({ ...col, visible: s.visible, width: s.width || col.width || 0 });
          }
        }
        // Then add any new columns not in saved prefs
        for (const col of base) {
          if (!savedMap.has(col.key)) {
            ordered.push(col);
          }
        }
        // Ensure pinned columns are always at the end
        const regularCols = ordered.filter((c: any) => !c.pinned);
        const pinnedCols = ordered.filter((c: any) => c.pinned);
        managedColumns.value = [...regularCols, ...pinnedCols];
        return;
      } catch {
        // Fall through to default
      }
    }
  }

  managedColumns.value = base;
}

function saveColumnPreferences() {
  if (!props.storageKey) return;
  const toSave = managedColumns.value.map((c: any) => ({
    key: c.key,
    visible: c.visible,
    width: c.width,
  }));
  localStorage.setItem(`table-columns-${props.storageKey}`, JSON.stringify(toSave));
}

// Visible columns (for actual table rendering) — pinned columns always at the end
const visibleColumns = computed(() => {
  const regular = managedColumns.value.filter((c: any) => !c.pinned && c.visible !== false);
  const pinned = managedColumns.value.filter((c: any) => c.pinned);
  return [...regular, ...pinned];
});

// ── Drag & Drop: Table header reorder ──
function onHeaderDragStart(index: number) {
  const col = visibleColumns.value[index];
  if (col.pinned) return;
  draggedColumn.value = index;
}

function onHeaderDragOver(event: DragEvent, index: number) {
  event.preventDefault();
  if (draggedColumn.value === null || draggedColumn.value === index) return;
  const col = visibleColumns.value[index];
  if (col.pinned) return;

  // Map visible indices back to managedColumns indices
  const fromKey = visibleColumns.value[draggedColumn.value].key;
  const toKey = visibleColumns.value[index].key;
  const fromIdx = managedColumns.value.findIndex((c: any) => c.key === fromKey);
  const toIdx = managedColumns.value.findIndex((c: any) => c.key === toKey);

  const newCols = [...managedColumns.value];
  const [item] = newCols.splice(fromIdx, 1);
  newCols.splice(toIdx, 0, item);
  managedColumns.value = newCols;
  draggedColumn.value = index;
}

function onHeaderDragEnd() {
  draggedColumn.value = null;
  saveColumnPreferences();
}

// ── Drag & Drop: Config modal reorder ──
function onConfigDragStart(index: number) {
  const col = tempColumns.value[index] as any;
  if (col.pinned) return;
  draggedConfigColumn.value = index;
}

function onConfigDragOver(event: DragEvent, index: number) {
  event.preventDefault();
  if (draggedConfigColumn.value === null || draggedConfigColumn.value === index) return;
  const col = tempColumns.value[index] as any;
  if (col.pinned) return;

  const newCols = [...tempColumns.value];
  const [item] = newCols.splice(draggedConfigColumn.value, 1);
  newCols.splice(index, 0, item);
  tempColumns.value = newCols;
  draggedConfigColumn.value = index;
}

function onConfigDragEnd() {
  draggedConfigColumn.value = null;
}

// ── Column config modal ──
function openColumnConfig() {
  // Exclude pinned columns from the configurator
  tempColumns.value = JSON.parse(JSON.stringify(managedColumns.value.filter((c: any) => !c.pinned)));
  showColumnConfig.value = true;
}

function closeColumnConfig() {
  showColumnConfig.value = false;
}

function toggleTempColumnVisibility(index: number) {
  const col = tempColumns.value[index] as any;
  if (col.pinned) return;
  col.visible = !col.visible;
}

function applyColumnConfig() {
  // Preserve pinned columns at the end
  const pinned = managedColumns.value.filter((c: any) => c.pinned);
  managedColumns.value = [...tempColumns.value, ...pinned];
  saveColumnPreferences();
  showColumnConfig.value = false;
}

// ── Column resize ──
function startResize(event: MouseEvent, colIndex: number) {
  const col = visibleColumns.value[colIndex];
  if (!col.resizable) return;

  event.preventDefault();
  event.stopPropagation();

  resizingColumn.value = colIndex;
  resizeStartX.value = event.clientX;

  // Get current actual width from the th element
  const th = (event.target as HTMLElement).closest('th');
  resizeStartWidth.value = col.width || (th ? th.offsetWidth : 100);

  document.addEventListener('mousemove', onResize);
  document.addEventListener('mouseup', stopResize);
}

function onResize(event: MouseEvent) {
  if (resizingColumn.value === null) return;
  const diff = event.clientX - resizeStartX.value;
  const newWidth = Math.max(60, resizeStartWidth.value + diff);
  const key = (visibleColumns.value[resizingColumn.value] as any).key;
  const idx = managedColumns.value.findIndex((c: any) => c.key === key);
  if (idx >= 0) {
    managedColumns.value[idx] = { ...managedColumns.value[idx], width: newWidth };
  }
}

function stopResize() {
  resizingColumn.value = null;
  document.removeEventListener('mousemove', onResize);
  document.removeEventListener('mouseup', stopResize);
  saveColumnPreferences();
}

// ── Compute items filtered by all columns EXCEPT the given one (+ global search) ──
function getItemsFilteredExcluding(excludeKey: string) {
  let result = [...props.items];

  // Apply global search
  if (props.searchable && searchQuery.value) {
    const terms = searchQuery.value.toLowerCase().split(/\s+/).filter(Boolean);
    result = result.filter(item => {
      const values = Object.values(item).map(val => String(val).toLowerCase());
      return terms.every((term: any) => values.some(val => val.includes(term)));
    });
  }

  // Apply all active column filters EXCEPT the excluded one
  for (const [key, selected] of Object.entries(activeColumnFilters.value)) {
    if (key === excludeKey) continue;
    result = result.filter(item => {
      const val = item[key];
      if (val === null || val === undefined || val === '') return false;
      return (selected as Set<string>).has(String(val));
    });
  }

  return result;
}

// ── Click filter icon: show unique values filter ──
function onFilterClick(event: MouseEvent, col: Column) {
  if (col.pinned) return;

  const colKey = col.key;

  // Compute unique values from items filtered by OTHER columns (dynamic/cascading)
  const baseItems = getItemsFilteredExcluding(colKey);
  const values = new Set<string>();
  for (const item of baseItems) {
    const val = item[colKey];
    if (val !== null && val !== undefined && val !== '') {
      values.add(String(val));
    }
  }
  const sorted = Array.from(values).sort((a, b) => a.localeCompare(b, undefined, { numeric: true }));

  columnFilterKey.value = colKey;
  columnFilterValues.value = sorted;
  columnFilterSearch.value = '';

  // If there's an active filter for this column, restore its selection (only keep still-available values)
  if (activeColumnFilters.value[colKey]) {
    const stillAvailable = new Set<string>();
    for (const v of activeColumnFilters.value[colKey]) {
      if (values.has(v)) stillAvailable.add(v);
    }
    columnFilterSelected.value = stillAvailable;
  } else {
    // By default all are selected
    columnFilterSelected.value = new Set(sorted);
  }

  // Position the popup near the header
  const th = (event.target as HTMLElement).closest('th');
  if (th) {
    const rect = th.getBoundingClientRect();
    columnFilterPosition.value = {
      top: rect.bottom + 4,
      left: Math.max(8, rect.left),
    };
  }

  nextTick(() => {
    document.addEventListener('mousedown', onClickOutsideFilter);
  });
}

function onClickOutsideFilter(event: MouseEvent) {
  if (columnFilterRef.value && !columnFilterRef.value.contains(event.target as Node)) {
    closeColumnFilter();
  }
}

function closeColumnFilter() {
  columnFilterKey.value = null;
  document.removeEventListener('mousedown', onClickOutsideFilter);
}

function toggleFilterValue(val: string) {
  const s = new Set(columnFilterSelected.value);
  if (s.has(val)) {
    s.delete(val);
  } else {
    s.add(val);
  }
  columnFilterSelected.value = s;
}

function selectAllFilterValues() {
  columnFilterSelected.value = new Set(filteredColumnFilterValues.value);
}

function deselectAllFilterValues() {
  columnFilterSelected.value = new Set();
}

function applyColumnFilter() {
  const key = columnFilterKey.value;
  if (!key) return;

  // If all values selected, remove the filter
  if (columnFilterSelected.value.size === columnFilterValues.value.length) {
    const newFilters = { ...activeColumnFilters.value };
    delete newFilters[key];
    activeColumnFilters.value = newFilters;
  } else {
    activeColumnFilters.value = {
      ...activeColumnFilters.value,
      [key]: new Set(columnFilterSelected.value),
    };
  }
  closeColumnFilter();
  currentPage.value = 1;
}

function clearColumnFilter(key: string) {
  const newFilters = { ...activeColumnFilters.value };
  delete newFilters[key];
  activeColumnFilters.value = newFilters;
  currentPage.value = 1;
}

function clearAllColumnFilters() {
  activeColumnFilters.value = {};
  currentPage.value = 1;
}

const filteredColumnFilterValues = computed(() => {
  if (!columnFilterSearch.value) return columnFilterValues.value;
  const q = columnFilterSearch.value.toLowerCase();
  return columnFilterValues.value.filter((v: any) => v.toLowerCase().includes(q));
});

const hasActiveFilters = computed(() => Object.keys(activeColumnFilters.value).length > 0);

const activeFilterLabels = computed(() => {
  const labels: { key: string; label: string; count: number; total: number }[] = [];
  for (const [key, selected] of Object.entries(activeColumnFilters.value)) {
    const col = managedColumns.value.find((c: any) => c.key === key);
    // Count total unique values for this column (considering other active filters)
    const baseItems = getItemsFilteredExcluding(key);
    const allValues = new Set<string>();
    for (const item of baseItems) {
      const val = item[key];
      if (val !== null && val !== undefined && val !== '') allValues.add(String(val));
    }
    labels.push({
      key,
      label: col?.label || key,
      count: (selected as Set<string>).size,
      total: allValues.size,
    });
  }
  return labels;
});

// ── Filtering & sorting ──
const filteredAndSortedItems = computed(() => {
  let result = [...props.items];

  // Global search (multi-criteria: each space-separated term must match at least one column)
  if (props.searchable && searchQuery.value) {
    const terms = searchQuery.value.toLowerCase().split(/\s+/).filter(Boolean);
    result = result.filter(item => {
      const values = Object.values(item).map(val => String(val).toLowerCase());
      return terms.every((term: string) => values.some(val => val.includes(term)));
    });
  }

  // Per-column filters
  for (const [key, selected] of Object.entries(activeColumnFilters.value)) {
    result = result.filter(item => {
      const val = item[key];
      if (val === null || val === undefined || val === '') return false;
      return (selected as Set<string>).has(String(val));
    });
  }

  // Sort
  if (sortKey.value) {
    result.sort((a, b) => {
      const valA = a[sortKey.value!] ?? '';
      const valB = b[sortKey.value!] ?? '';
      if (valA === valB) return 0;
      const comparison = valA > valB ? 1 : -1;
      return sortOrder.value === 'asc' ? comparison : -comparison;
    });
  }

  return result;
});

// Keep the parent in step with what is on screen (see the 'visible-items' emit).
watch(filteredAndSortedItems, (rows) => emit('visible-items', rows), { immediate: true });

const paginatedItems = computed(() => {
  if (!props.pagination) return filteredAndSortedItems.value;
  const start = (currentPage.value - 1) * pageSize.value;
  const end = start + pageSize.value;
  return filteredAndSortedItems.value.slice(start, end);
});

const totalItems = computed(() => filteredAndSortedItems.value.length);

const totalPages = computed(() => {
  if (!props.pagination || pageSize.value <= 0) return 1;
  return Math.ceil(totalItems.value / pageSize.value);
});

const toggleSort = (key: string) => {
  if (sortKey.value === key) {
    sortOrder.value = sortOrder.value === 'asc' ? 'desc' : 'asc';
  } else {
    sortKey.value = key;
    sortOrder.value = 'asc';
  }
};

// Reset page when search or filters change
watch([searchQuery, pageSize], () => {
  currentPage.value = 1;
});

// Emit page changes for parent components
watch(currentPage, (newPage) => {
  emit('page-change', newPage);
});

// Re-init columns when columns prop changes
watch(() => props.columns, () => {
  initColumns();
}, { deep: true });

onMounted(() => {
  initColumns();
});

onBeforeUnmount(() => {
  document.removeEventListener('mousedown', onClickOutsideFilter);
  document.removeEventListener('mousemove', onResize);
  document.removeEventListener('mouseup', stopResize);
});

// Theme-aware classes
const theadClasses = computed(() => {
  return props.theme === 'reveals' ? 'bg-gray-50' : 'bg-gray-100';
});

const thClasses = computed(() => {
  return props.theme === 'reveals'
    ? 'py-3.5 pl-4 pr-3 text-sm font-semibold text-space-indigo sm:pl-6 select-none font-inter'
    : 'py-3.5 pl-4 pr-3 text-sm font-semibold text-space-indigo sm:pl-6 select-none';
});

// Per-column text alignment (defaults to left). Numbers use 'right'.
function alignClass(col: Column) {
  if (col.align === 'right') return 'text-right';
  if (col.align === 'center') return 'text-center';
  return 'text-left';
}

const thHoverClasses = computed(() => {
  return props.theme === 'reveals' ? 'hover:bg-ocean-mist/10' : 'hover:bg-gray-100';
});

const sortIconActiveClass = computed(() => {
  return props.theme === 'reveals' ? 'text-tropical-mint' : 'text-indigo-600';
});

const tbodyClasses = computed(() => {
  return props.theme === 'reveals' ? 'divide-y divide-space-indigo/10 bg-white' : 'divide-y divide-gray-200 bg-white';
});

const tdClasses = computed(() => {
  return props.theme === 'reveals'
    ? 'whitespace-nowrap px-3 py-4 text-sm text-space-indigo/70 first:pl-6 last:pr-6 font-inter'
    : 'whitespace-nowrap px-3 py-4 text-sm text-space-indigo/50 first:pl-6 last:pr-6';
});

const noResultsClasses = computed(() => {
  return props.theme === 'reveals' 
    ? 'text-center py-6 text-space-indigo/50 text-sm font-inter'
    : 'text-center py-6 text-space-indigo/50 text-sm';
});

const ringClasses = computed(() => {
  return props.theme === 'reveals'
    ? 'overflow-visible shadow ring-1 ring-space-indigo/10 sm:rounded-lg'
    : 'overflow-visible shadow ring-1 ring-black ring-opacity-5 sm:rounded-lg';
});

const tableClasses = computed(() => {
  return props.theme === 'reveals'
    ? 'min-w-full divide-y divide-space-indigo/20'
    : 'min-w-full divide-y divide-gray-300';
});

const hasStickyColumn = computed(() => visibleColumns.value.some(col => col.pinned));

function stickyThClasses(col: any) {
  if (!col.pinned) return '';
  const bg = props.theme === 'reveals' ? 'bg-lavender-blush' : 'bg-gray-50';
  return `sticky top-0 right-0 z-20 ${bg} after:absolute after:top-0 after:bottom-0 after:-left-4 after:w-4 after:pointer-events-none after:bg-gradient-to-r after:from-transparent after:to-black/5`;
}

function stickyTdClasses(col: any) {
  if (!col.pinned) return '';
  return 'sticky right-0 z-10 bg-white after:absolute after:top-0 after:bottom-0 after:-left-4 after:w-4 after:pointer-events-none after:bg-gradient-to-r after:from-transparent after:to-black/5';
}
</script>

<template>
  <div class="space-y-4">
    <!-- Toolbar: Search + Column Config -->
    <div class="flex items-center justify-between gap-4">
      <div v-if="searchable" class="relative max-w-sm flex-1">
        <div class="relative">
          <Input 
            v-model="searchQuery" 
            :placeholder="searchPlaceholder" 
            :theme="theme"
            class="pl-10" 
          />
          <div class="absolute inset-y-0 left-0 flex items-center pl-3 pointer-events-none">
            <SearchIcon class="h-4 w-4 text-space-indigo/40" />
          </div>
        </div>
      </div>
      <div v-else class="flex-1" />

      <button
        type="button"
        class="inline-flex items-center gap-1.5 rounded-md border border-gray-300 bg-white px-3 py-2 text-sm font-medium text-gray-700 shadow-sm hover:bg-gray-50 transition-colors"
        @click="openColumnConfig"
        title="Configurer les colonnes"
      >
        <Settings2 class="h-4 w-4" />
        <span class="hidden sm:inline">Colonnes</span>
      </button>
    </div>

    <!-- Active column filters indicator -->
    <div v-if="hasActiveFilters" class="flex flex-wrap items-center gap-2">
      <span class="text-xs font-medium text-gray-500">Filtres actifs :</span>
      <span
        v-for="f in activeFilterLabels"
        :key="f.key"
        class="inline-flex items-center gap-1 rounded-full bg-turquoise-surf/10 px-2.5 py-0.5 text-xs font-medium text-turquoise-surf"
      >
        {{ f.label }} ({{ f.count }}/{{ f.total }})
        <button type="button" class="ml-0.5 hover:text-red-500 transition-colors" @click="clearColumnFilter(f.key)">
          <X class="h-3 w-3" />
        </button>
      </span>
      <button
        type="button"
        class="text-xs text-gray-400 hover:text-red-500 hover:underline transition-colors"
        @click="clearAllColumnFilters"
      >
        Tout effacer
      </button>
    </div>

    <!-- Table -->
    <div class="flow-root">
      <div class="-mx-4 -my-2 overflow-x-auto sm:-mx-6 lg:-mx-8">
        <div class="inline-block min-w-full py-2 align-middle sm:px-6 lg:px-8">
          <div :class="ringClasses">
            <table :class="tableClasses">
              <thead :class="theadClasses">
                <tr>
                  <th 
                    v-for="(col, index) in visibleColumns" 
                    :key="col.key" 
                    scope="col" 
                    :class="[
                      thClasses,
                      alignClass(col),
                      col.class,
                      col.sortable ? `cursor-pointer ${thHoverClasses}` : '',
                      !col.pinned ? 'draggable-header group' : '',
                      activeColumnFilters[col.key] ? 'bg-turquoise-surf/5' : '',
                      stickyThClasses(col),
                    ]"
                    :style="col.width ? { width: col.width + 'px', minWidth: col.width + 'px', position: col.pinned ? 'static' : 'relative' } : { position: col.pinned ? 'static' : 'relative' }"
                    :draggable="!col.pinned"
                    @click="col.sortable && toggleSort(col.key)"
                    @dragstart="onHeaderDragStart(index)"
                    @dragover="onHeaderDragOver($event, index)"
                    @dragend="onHeaderDragEnd"
                  >
                    <div class="flex items-center gap-1.5" :class="col.align === 'right' ? 'justify-end' : ''">
                      <!-- Dynamic Slot for Header: header-key (named slot wins; else the label) -->
                      <slot :name="`header-${col.key}`" :column="col">{{ col.label }}</slot>
                      <!-- Filter icon (funnel) -->
                      <button
                        v-if="!col.pinned"
                        type="button"
                        class="p-0.5 rounded hover:bg-gray-200 transition-all"
                        :class="activeColumnFilters[col.key] ? 'text-turquoise-surf' : 'text-space-indigo/30 opacity-0 group-hover:opacity-100'"
                        :title="`Filtrer par ${col.label}`"
                        @click.stop="onFilterClick($event, col)"
                      >
                        <Filter class="h-3.5 w-3.5" />
                      </button>
                      <span v-if="col.sortable" class="text-space-indigo/40">
                        <ArrowUp v-if="sortKey === col.key && sortOrder === 'asc'" :class="['h-4 w-4', sortIconActiveClass]" />
                        <ArrowDown v-else-if="sortKey === col.key && sortOrder === 'desc'" :class="['h-4 w-4', sortIconActiveClass]" />
                        <ArrowUpDown v-else class="h-4 w-4 opacity-50" />
                      </span>
                    </div>
                    <!-- Resize handle -->
                    <div
                      v-if="col.resizable && col.width"
                      class="absolute top-0 right-0 w-1 h-full cursor-col-resize hover:bg-turquoise-surf/50 transition-colors"
                      @mousedown="startResize($event, index)"
                      @click.stop
                      @dblclick.stop
                    />
                  </th>
                </tr>
              </thead>
              <tbody :class="tbodyClasses">
                <!-- Data Mode (Smart Table) -->
                <template v-if="items.length > 0">
                   <tr v-if="paginatedItems.length === 0">
                      <td :colspan="visibleColumns.length" :class="noResultsClasses">
                        Aucun résultat.
                      </td>
                   </tr>
                   <tr v-for="item in paginatedItems" :key="item.id || JSON.stringify(item)"
                      :class="{ 'cursor-pointer hover:bg-gray-50 transition-colors': clickableRows }"
                      @click="handleRowClick(item, $event)">
                      <td v-for="col in visibleColumns" :key="col.key" :class="[tdClasses, alignClass(col), stickyTdClasses(col)]" :style="col.width ? { width: col.width + 'px', minWidth: col.width + 'px' } : {}">
                          <!-- Dynamic Slot for Cell: cell-key (named slot wins; else amount formatting; else raw) -->
                          <slot :name="`cell-${col.key}`" :item="item" :value="item[col.key]">
                             <Amount v-if="col.format === 'amount'" :value="item[col.key]" :currency="item.currency" />
                             <template v-else>{{ item[col.key] }}</template>
                          </slot>
                      </td>
                   </tr>
                </template>

                <!-- Legacy/Manual Slot Mode (Fallback) -->
                <template v-else>
                   <slot />
                </template>
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </div>

    <!-- Pagination -->
    <div v-if="pagination && totalItems > 0" class="flex items-center justify-between border-t border-space-indigo/10 pt-4">
       <!-- Select Page Size -->
       <div class="flex items-center space-x-2">
          <span class="text-sm text-space-indigo/50">Afficher</span>
          <Select 
             v-model="pageSize" 
             :options="pageSizeOptions" 
             :theme="theme" 
             class="w-20" 
          />
       </div>

       <!-- Pagination Component -->
       <Pagination 
          v-model:page="currentPage" 
          :totalPages="totalPages" 
          :totalItems="totalItems"
          :itemsPerPage="pageSize"
          :theme="theme"
       />
    </div>

    <!-- ───── Column Config Modal ───── -->
    <Teleport to="body">
      <div v-if="showColumnConfig" class="fixed inset-0 z-[60] flex items-center justify-center">
        <!-- Backdrop -->
        <div class="fixed inset-0 bg-black/30 backdrop-blur-sm" @click="closeColumnConfig" />
        <!-- Modal -->
        <div class="relative z-10 w-full max-w-md rounded-xl bg-white shadow-2xl ring-1 ring-black/5">
          <!-- Header -->
          <div class="flex items-center justify-between border-b border-gray-200 px-6 py-4">
            <div class="flex items-center gap-2">
              <Settings2 class="h-5 w-5 text-gray-500" />
              <h3 class="text-base font-semibold text-gray-900">Configurer les colonnes</h3>
            </div>
            <button type="button" class="rounded-md p-1 text-gray-400 hover:text-gray-600 transition-colors" @click="closeColumnConfig">
              <X class="h-5 w-5" />
            </button>
          </div>
          <!-- Body -->
          <div class="px-6 py-4">
            <p class="text-sm text-gray-500 mb-3">
              Glissez pour réordonner, décochez pour masquer.
            </p>
            <div class="max-h-80 overflow-y-auto space-y-1">
              <div
                v-for="(col, index) in tempColumns"
                :key="col.key"
                class="flex items-center gap-2 rounded-lg px-3 py-2 transition-colors"
                :class="[
                  col.pinned ? 'bg-gray-50 opacity-60' : 'hover:bg-gray-50 cursor-grab',
                  draggedConfigColumn === index ? 'bg-turquoise-surf/10 ring-1 ring-turquoise-surf/30' : '',
                ]"
                :draggable="!col.pinned"
                @dragstart="onConfigDragStart(index)"
                @dragover="onConfigDragOver($event, index)"
                @dragend="onConfigDragEnd"
              >
                <GripVertical v-if="!col.pinned" class="h-4 w-4 text-gray-400 shrink-0" />
                <span v-else class="h-4 w-4 shrink-0" />
                <label class="flex flex-1 items-center gap-2 cursor-pointer select-none">
                  <input
                    v-if="!col.pinned"
                    type="checkbox"
                    :checked="col.visible !== false"
                    class="h-4 w-4 rounded border-gray-300 text-turquoise-surf focus:ring-turquoise-surf cursor-pointer"
                    @change="toggleTempColumnVisibility(index)"
                  />
                  <span v-else class="h-4 w-4" />
                  <span class="text-sm text-gray-700">{{ col.label }}</span>
                </label>
                <Eye v-if="col.visible !== false" class="h-3.5 w-3.5 text-gray-400" />
                <EyeOff v-else class="h-3.5 w-3.5 text-gray-300" />
              </div>
            </div>
          </div>
          <!-- Footer -->
          <div class="flex items-center justify-end gap-3 border-t border-gray-200 px-6 py-4">
            <button
              type="button"
              class="rounded-md px-3 py-2 text-sm font-medium text-gray-700 hover:bg-gray-100 transition-colors"
              @click="closeColumnConfig"
            >
              Annuler
            </button>
            <button
              type="button"
              class="rounded-md bg-turquoise-surf px-4 py-2 text-sm font-medium text-white shadow-sm hover:bg-turquoise-surf/90 transition-colors"
              @click="applyColumnConfig"
            >
              Appliquer
            </button>
          </div>
        </div>
      </div>
    </Teleport>

    <!-- ───── Column Filter Popup (double-click) ───── -->
    <Teleport to="body">
      <div
        v-if="columnFilterKey"
        ref="columnFilterRef"
        class="fixed z-[70] w-72 rounded-xl bg-white shadow-2xl ring-1 ring-black/10"
        :style="{ top: columnFilterPosition.top + 'px', left: columnFilterPosition.left + 'px', maxHeight: '400px' }"
      >
        <!-- Header -->
        <div class="flex items-center justify-between border-b border-gray-200 px-4 py-3">
          <span class="text-sm font-semibold text-gray-900">
            Filtrer : {{ (managedColumns.find((c: any) => c.key === columnFilterKey) as any)?.label }}
          </span>
          <button type="button" class="rounded-md p-1 text-gray-400 hover:text-gray-600 transition-colors" @click="closeColumnFilter">
            <X class="h-4 w-4" />
          </button>
        </div>
        <!-- Search within values -->
        <div class="px-4 pt-3 pb-2">
          <div class="relative">
            <input
              v-model="columnFilterSearch"
              type="text"
              placeholder="Rechercher…"
              class="w-full rounded-md border border-gray-300 px-3 py-1.5 text-sm focus:border-turquoise-surf focus:ring-1 focus:ring-turquoise-surf focus:outline-none"
            />
            <SearchIcon class="absolute right-2.5 top-1/2 -translate-y-1/2 h-3.5 w-3.5 text-gray-400" />
          </div>
        </div>
        <!-- Select all / none -->
        <div class="flex items-center justify-between px-4 py-1">
          <button type="button" class="text-xs text-turquoise-surf hover:underline" @click="selectAllFilterValues">Tout sélectionner</button>
          <button type="button" class="text-xs text-gray-400 hover:text-gray-600 hover:underline" @click="deselectAllFilterValues">Tout désélectionner</button>
        </div>
        <!-- Value list -->
        <div class="max-h-48 overflow-y-auto px-2 py-1">
          <label
            v-for="val in filteredColumnFilterValues"
            :key="val"
            class="flex items-center gap-2 rounded px-2 py-1 hover:bg-gray-50 cursor-pointer text-sm text-gray-700"
          >
            <input
              type="checkbox"
              :checked="columnFilterSelected.has(val)"
              class="h-3.5 w-3.5 rounded border-gray-300 text-turquoise-surf focus:ring-turquoise-surf cursor-pointer"
              @change="toggleFilterValue(val)"
            />
            <span class="truncate">{{ val }}</span>
          </label>
          <div v-if="filteredColumnFilterValues.length === 0" class="px-2 py-3 text-center text-xs text-gray-400">
            Aucune valeur trouvée
          </div>
        </div>
        <!-- Footer -->
        <div class="flex items-center justify-end gap-2 border-t border-gray-200 px-4 py-3">
          <button
            type="button"
            class="rounded-md px-3 py-1.5 text-xs font-medium text-gray-600 hover:bg-gray-100 transition-colors"
            @click="closeColumnFilter"
          >
            Annuler
          </button>
          <button
            type="button"
            class="rounded-md bg-turquoise-surf px-3 py-1.5 text-xs font-medium text-white shadow-sm hover:bg-turquoise-surf/90 transition-colors"
            @click="applyColumnFilter"
          >
            Appliquer
          </button>
        </div>
      </div>
    </Teleport>
  </div>
</template>

<style scoped>
.draggable-header {
  cursor: grab;
  transition: background-color 0.15s ease;
}
.draggable-header:active {
  cursor: grabbing;
}
</style>

/**
 * Manual match baskets — a working set of entries assembled across searches.
 *
 * The operational view wipes its selection on every "Apply filters", so two
 * entries that offset each other under different reco_ids could never be picked
 * together. A basket survives filter changes (and reloads): search, tick, add,
 * search again, and force once the total reaches zero.
 *
 * State lives in localStorage — the basket is a scratchpad, what matters for the
 * audit trail is the force itself, which is already persisted server-side
 * (reco.match_group + audit.ui_action_log). Everything goes through this store
 * so swapping the adapter for a server-side table later touches nothing else.
 */
import { defineStore } from 'pinia';
import { computed, ref, watch } from 'vue';

import reconciliationService from '@/services/reconciliationService';
import { useAuthStore } from '@/stores/auth';
import { sumMinor, toMinor } from '@/utils/decimal';
import toaster from '@/utils/toaster';
import type { Basket, BasketItem, ReconciliationEntry } from '@/types';

const STORAGE_VERSION = 1;

/** Keeps a basket reviewable, and the serialized state well inside the ~5 MB
 *  localStorage quota (a trimmed item is ~250 bytes). */
export const MAX_ITEMS_PER_BASKET = 2000;

interface StoredState {
  v: number;
  activeId: string | null;
  baskets: Basket[];
}

const newId = () => `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;

/** Drop payload_raw / payment_statuses: heavy, and useless to a basket. */
const toItem = (e: ReconciliationEntry): BasketItem => ({
  id: e.id,
  flow_id: e.flow_id,
  currency: e.currency,
  amount: e.amount,
  reco_id: e.reco_id ?? null,
  account: e.account ?? null,
  value_date: e.value_date,
  ref_no: e.ref_no ?? null,
  remarks_1: e.remarks_1 ?? null,
  transaction_particulars: e.transaction_particulars ?? null,
});

/** What addMany could not take, so the caller can explain it in one toast. */
export interface AddOutcome {
  added: number;
  alreadyIn: number;
  notPending: number;
  otherFlow: number;
  overflow: number;
}

export const useMatchBasketStore = defineStore('matchBasket', () => {
  const authStore = useAuthStore();

  const baskets = ref<Basket[]>([]);
  const activeId = ref<string | null>(null);
  const refreshing = ref(false);

  const storageKey = computed(() => `reco:matchBaskets:${authStore.user?.id ?? 'anon'}`);

  // ── Persistence ───────────────────────────────────────────────────

  let hydrating = false;

  const load = () => {
    hydrating = true;
    try {
      const raw = localStorage.getItem(storageKey.value);
      const parsed = raw ? (JSON.parse(raw) as StoredState) : null;
      if (parsed && parsed.v === STORAGE_VERSION && Array.isArray(parsed.baskets)) {
        baskets.value = parsed.baskets;
        activeId.value = parsed.activeId ?? parsed.baskets[0]?.id ?? null;
      } else {
        // No state, or a version we don't understand — start clean rather than
        // guessing at a shape we no longer write.
        baskets.value = [];
        activeId.value = null;
      }
    } catch {
      baskets.value = [];
      activeId.value = null;
    } finally {
      hydrating = false;
    }
  };

  let persistTimer: ReturnType<typeof setTimeout> | null = null;
  const persist = () => {
    if (hydrating) return;
    if (persistTimer) clearTimeout(persistTimer);
    persistTimer = setTimeout(() => {
      const payload: StoredState = {
        v: STORAGE_VERSION,
        activeId: activeId.value,
        baskets: baskets.value,
      };
      try {
        localStorage.setItem(storageKey.value, JSON.stringify(payload));
      } catch {
        // Quota exceeded, private mode, storage disabled… The basket stays
        // usable in memory; it just will not survive a reload.
        toaster.error("Panier non sauvegardé : stockage local indisponible ou plein");
      }
    }, 150);
  };

  watch([baskets, activeId], persist, { deep: true });
  // Rehydrate once auth resolves, and again if the user changes.
  watch(storageKey, load, { immediate: true });

  // Another tab wrote our key: adopt its state instead of overwriting it on our
  // next change. The event only fires in the OTHER tabs, so this cannot loop.
  if (typeof window !== 'undefined') {
    window.addEventListener('storage', (e: StorageEvent) => {
      if (e.key === storageKey.value) load();
    });
  }

  // ── Derived state (active basket) ─────────────────────────────────

  const active = computed<Basket | null>(
    () => baskets.value.find((b) => b.id === activeId.value) ?? null,
  );
  const items = computed<BasketItem[]>(() => active.value?.items ?? []);
  const count = computed(() => items.value.length);
  const isEmpty = computed(() => count.value === 0);

  /** Exact, in minor units — must agree with the backend's Decimal check. */
  const totalMinor = computed(() => sumMinor(items.value.map((i) => i.amount)));
  const isBalanced = computed(() => count.value > 0 && totalMinor.value === 0);

  // Debit = negative amount, credit = positive (parsers apply -abs/+abs from
  // direction; see apply_sign_from_direction).
  const debitMinor = computed(() =>
    items.value.reduce((s, i) => (toMinor(i.amount) < 0 ? s + toMinor(i.amount) : s), 0),
  );
  const creditMinor = computed(() =>
    items.value.reduce((s, i) => (toMinor(i.amount) > 0 ? s + toMinor(i.amount) : s), 0),
  );

  /** Items a refresh() found are no longer pending — they cannot be forced. */
  const isStale = (i: BasketItem) =>
    i.missing === true || (i.current_status !== undefined && i.current_status !== 'pending');
  const staleItems = computed(() => items.value.filter(isStale));

  // A Set, not a scan: has() is called once per rendered row, against a basket
  // that can hold thousands of items.
  const itemIds = computed(() => new Set(items.value.map((i) => i.id)));
  const has = (entryId: number) => itemIds.value.has(entryId);

  // ── Basket management ─────────────────────────────────────────────

  const createBasket = (name?: string): Basket => {
    const now = new Date().toISOString();
    const basket: Basket = {
      id: newId(),
      name: name?.trim() || `Panier ${baskets.value.length + 1}`,
      flow_id: null,
      currency: null,
      items: [],
      created_at: now,
      updated_at: now,
    };
    baskets.value = [...baskets.value, basket];
    activeId.value = basket.id;
    // Hand back the REACTIVE instance, not the literal above: writes to the raw
    // object bypass the proxy, so the persist watcher would never see them.
    return baskets.value[baskets.value.length - 1];
  };

  /** The active basket, creating one on first use. */
  const ensureActive = (): Basket => active.value ?? createBasket();

  const setActive = (id: string) => {
    if (baskets.value.some((b) => b.id === id)) activeId.value = id;
  };

  const renameBasket = (id: string, name: string) => {
    const b = baskets.value.find((x) => x.id === id);
    if (!b || !name.trim()) return;
    b.name = name.trim();
    b.updated_at = new Date().toISOString();
  };

  const deleteBasket = (id: string) => {
    baskets.value = baskets.value.filter((b) => b.id !== id);
    if (activeId.value === id) activeId.value = baskets.value[0]?.id ?? null;
  };

  // ── Items ─────────────────────────────────────────────────────────

  /**
   * Add entries to the active basket.
   *
   * The first item locks the basket's flow and currency: force_match refuses a
   * group spanning several flows or currencies, so refusing here — where we can
   * say which rows were dropped — beats a 400 at the end of the workflow.
   */
  const addMany = (entries: ReconciliationEntry[]): AddOutcome => {
    const outcome: AddOutcome = { added: 0, alreadyIn: 0, notPending: 0, otherFlow: 0, overflow: 0 };
    if (entries.length === 0) return outcome;

    const basket = ensureActive();
    const present = new Set(basket.items.map((i) => i.id));
    const accepted: BasketItem[] = [];

    let flowId = basket.flow_id;
    let currency = basket.currency;

    for (const e of entries) {
      if (present.has(e.id)) {
        outcome.alreadyIn += 1;
        continue;
      }
      if (e.status !== 'pending') {
        outcome.notPending += 1;
        continue;
      }
      if (flowId === null || currency === null) {
        flowId = e.flow_id;
        currency = e.currency;
      } else if (e.flow_id !== flowId || e.currency !== currency) {
        outcome.otherFlow += 1;
        continue;
      }
      if (basket.items.length + accepted.length >= MAX_ITEMS_PER_BASKET) {
        outcome.overflow += 1;
        continue;
      }
      present.add(e.id);
      accepted.push(toItem(e));
    }

    if (accepted.length > 0) {
      basket.flow_id = flowId;
      basket.currency = currency;
      basket.items = [...basket.items, ...accepted];
      basket.updated_at = new Date().toISOString();
      outcome.added = accepted.length;
    }
    return outcome;
  };

  const remove = (entryId: number) => {
    const basket = active.value;
    if (!basket) return;
    basket.items = basket.items.filter((i) => i.id !== entryId);
    if (basket.items.length === 0) {
      // Empty again → unlock, so the next add can pick any flow.
      basket.flow_id = null;
      basket.currency = null;
    }
    basket.updated_at = new Date().toISOString();
  };

  const removeStale = () => {
    const basket = active.value;
    if (!basket) return;
    basket.items = basket.items.filter((i) => !isStale(i));
    if (basket.items.length === 0) {
      basket.flow_id = null;
      basket.currency = null;
    }
    basket.updated_at = new Date().toISOString();
  };

  const clear = () => {
    const basket = active.value;
    if (!basket) return;
    basket.items = [];
    basket.flow_id = null;
    basket.currency = null;
    basket.updated_at = new Date().toISOString();
  };

  /**
   * Re-read the server state of every item.
   *
   * A basket built over several days rots silently: an entry can be
   * auto-matched or excluded meanwhile, and force_match would then fail with an
   * opaque "entry <id> is not pending". This marks those rows instead.
   *
   * Returns how many items came back unusable.
   */
  const refresh = async (): Promise<number> => {
    const basket = active.value;
    if (!basket || basket.items.length === 0) return 0;
    refreshing.value = true;
    try {
      const fresh = await reconciliationService.listByIds(basket.items.map((i) => i.id));
      const byId = new Map(fresh.map((e) => [e.id, e]));
      basket.items = basket.items.map((i) => {
        const e = byId.get(i.id);
        // Gone entirely: purged by a re-ingest, or withdrawn as a split parent.
        if (!e) return { ...i, missing: true, current_status: undefined };
        // Amount/date may have been corrected by a re-ingest — take the server's.
        return { ...toItem(e), current_status: e.status, missing: false };
      });
      basket.updated_at = new Date().toISOString();
      return basket.items.filter(isStale).length;
    } finally {
      refreshing.value = false;
    }
  };

  return {
    // state
    baskets,
    activeId,
    refreshing,
    // getters
    active,
    items,
    count,
    isEmpty,
    totalMinor,
    isBalanced,
    debitMinor,
    creditMinor,
    staleItems,
    // actions
    has,
    isStale,
    createBasket,
    setActive,
    renameBasket,
    deleteBasket,
    addMany,
    remove,
    removeStale,
    clear,
    refresh,
  };
});

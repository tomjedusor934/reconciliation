<script setup lang="ts">
import { ref, onMounted, computed } from 'vue';
import Button from '@/components/ui/Button.vue';
import Loader from '@/components/ui/Loader.vue';
import Select from '@/components/ui/Select.vue';
import adminService from '@/services/adminService';
import flowService from '@/services/flowService';
import modal from '@/utils/modal';
import { success, error } from '@/utils/toaster';
import type { Flow } from '@/types';

const isLoading = ref(true);
const isResetting = ref(false);
const environment = ref('');
const resetAllowed = ref(false);

const fetchEnvironment = async () => {
  isLoading.value = true;
  try {
    const { data } = await adminService.getEnvironment();
    environment.value = data.environment;
    resetAllowed.value = data.reset_ingestion_allowed;
  } catch (err) {
    console.error('Error loading environment', err);
    resetAllowed.value = false;
  } finally {
    isLoading.value = false;
  }
};

const doReset = async () => {
  isResetting.value = true;
  try {
    const { data } = await adminService.resetIngestion();
    success(`Données d'ingestion réinitialisées (${data.total_deleted} lignes supprimées)`);
    modal.close();
  } catch (err: any) {
    error(err?.response?.data?.detail || 'Échec de la réinitialisation');
  } finally {
    isResetting.value = false;
  }
};

const confirmReset = () => {
  modal.open({
    title: "Réinitialiser les données d'ingestion",
    messageHtml: `
      <p>Cette action est <strong>irréversible</strong>. Seront définitivement supprimés :</p>
      <ul style="margin-top:0.5rem;list-style:disc;padding-left:1.25rem;">
        <li>les écritures en cours et leur archive d'émargement</li>
        <li>les rapprochements (match groups)</li>
        <li>les exécutions d'ingestion et de rapprochement</li>
        <li>les exclusions</li>
      </ul>
      <p style="margin-top:0.75rem;">La <strong>configuration des flux</strong> et les <strong>utilisateurs</strong> sont conservés.</p>
    `,
    buttons: [
      { label: 'Annuler', variant: 'secondary', action: () => modal.close() },
      { label: 'Réinitialiser', variant: 'danger', action: doReset },
    ],
  });
};

// ── Réinitialisation ciblée par flux ──────────────────────────────────
const flows = ref<Flow[]>([]);
const selectedFlowId = ref('');
const isResettingFlow = ref(false);

const flowOptions = computed(() => [
  { value: '', label: '— Choisir un flux —' },
  ...flows.value.map((f) => ({ value: String(f.id), label: f.name })),
]);

const fetchFlows = async () => {
  try {
    const { data } = await flowService.getAll();
    flows.value = data;
  } catch (err) {
    console.error('Error loading flows', err);
  }
};

const doResetFlow = async () => {
  if (!selectedFlowId.value) return;
  isResettingFlow.value = true;
  try {
    const { data } = await adminService.resetIngestionFlow(Number(selectedFlowId.value));
    success(`Données du flux réinitialisées (${data.total_deleted} lignes supprimées)`);
    selectedFlowId.value = '';
    modal.close();
  } catch (err: any) {
    error(err?.response?.data?.detail || 'Échec de la réinitialisation');
  } finally {
    isResettingFlow.value = false;
  }
};

const confirmResetFlow = () => {
  if (!selectedFlowId.value) return;
  const flow = flows.value.find((f) => String(f.id) === selectedFlowId.value);
  const flowLabel = flow ? flow.name : `#${selectedFlowId.value}`;
  modal.open({
    title: "Réinitialiser les données d'un flux",
    messageHtml: `
      <p>Cette action est <strong>irréversible</strong>. Pour le flux <strong>${flowLabel}</strong>, seront définitivement supprimés :</p>
      <ul style="margin-top:0.5rem;list-style:disc;padding-left:1.25rem;">
        <li>les écritures en cours et leur archive d'émargement</li>
        <li>les rapprochements (match groups)</li>
        <li>les exécutions d'ingestion</li>
        <li>les exclusions</li>
      </ul>
      <p style="margin-top:0.75rem;">Les <strong>autres flux</strong>, la <strong>configuration des flux</strong> et les <strong>utilisateurs</strong> sont conservés.</p>
    `,
    buttons: [
      { label: 'Annuler', variant: 'secondary', action: () => modal.close() },
      { label: 'Réinitialiser', variant: 'danger', action: doResetFlow },
    ],
  });
};

onMounted(async () => {
  await fetchEnvironment();
  if (resetAllowed.value) await fetchFlows();
});
</script>

<template>
  <div class="space-y-8">
    <div class="mb-6">
      <h2 class="text-2xl font-bold text-space-indigo">Maintenance</h2>
      <p class="text-space-indigo/60 mt-2">Opérations de maintenance sur les données de l'application</p>
    </div>

    <Loader v-if="isLoading" />

    <template v-else>
      <!-- Désactivé en production -->
      <div v-if="!resetAllowed" class="rounded-lg border border-amber-300 bg-amber-50 p-6">
        <h3 class="text-lg font-semibold text-amber-800">Indisponible en production</h3>
        <p class="text-sm text-amber-700 mt-2">
          La réinitialisation des données d'ingestion est désactivée dans cet environnement
          (<code>{{ environment }}</code>).
        </p>
      </div>

      <!-- Zones de danger (hors production uniquement) -->
      <template v-else>
        <!-- Reset global -->
        <div class="rounded-lg border-2 border-red-300 bg-red-50 p-6">
          <h3 class="text-lg font-semibold text-red-700">Zone de danger</h3>
          <p class="text-sm text-red-700/90 mt-2">
            Réinitialise toutes les données issues de l'ingestion et du rapprochement
            (écritures, archive d'émargement, rapprochements, exécutions, exclusions).
            La <strong>configuration des flux</strong> et les <strong>utilisateurs</strong> sont conservés.
          </p>
          <p class="text-xs text-red-700/70 mt-1">Environnement : <code>{{ environment }}</code></p>
          <div class="mt-4">
            <Button variant="danger" :disabled="isResetting" @click="confirmReset">
              <span v-if="isResetting">Réinitialisation…</span>
              <span v-else>Réinitialiser les données d'ingestion</span>
            </Button>
          </div>
        </div>

        <!-- Reset ciblé par flux -->
        <div class="rounded-lg border-2 border-red-300 bg-red-50 p-6">
          <h3 class="text-lg font-semibold text-red-700">Zone de danger — par flux</h3>
          <p class="text-sm text-red-700/90 mt-2">
            Réinitialise les données d'ingestion et de rapprochement d'un <strong>seul flux</strong>
            (écritures, archive d'émargement, rapprochements, exécutions d'ingestion, exclusions).
            Les <strong>autres flux</strong>, la configuration et les utilisateurs sont conservés.
          </p>
          <div class="mt-4 flex flex-wrap items-end gap-3">
            <div class="w-64">
              <Select v-model="selectedFlowId" label="Flux à réinitialiser" :options="flowOptions" />
            </div>
            <Button variant="danger" :disabled="isResettingFlow || !selectedFlowId" @click="confirmResetFlow">
              <span v-if="isResettingFlow">Réinitialisation…</span>
              <span v-else>Réinitialiser les données d'un flux</span>
            </Button>
          </div>
        </div>
      </template>
    </template>
  </div>
</template>

<script setup lang="ts">
/**
 * Temporary operator tool — reattribute the return/reject movements
 * (RCP/RCC/RRS/WCC) to what they undo.
 *
 * The datamart carries no link between a return batch (one movement, one
 * `msgid`) and the payments it returns, so that movement ends up alone while the
 * individual return legs it aggregates already sit with the originals. The
 * uploaded extracts carry the missing link. A movement is split either into
 * LOTS (batch-booking flow) or onto RECONCILIATION KEYS (classic bulk flow) —
 * decided by the flow the movement was found in, shown on every row. Nothing is
 * written until the operator ticks movements and confirms.
 */
import { computed, nextTick, onMounted, ref } from 'vue';
import Badge from '@/components/ui/Badge.vue';
import Button from '@/components/ui/Button.vue';
import Card from '@/components/ui/Card.vue';
import Loader from '@/components/ui/Loader.vue';
import Modal from '@/components/ui/Modal.vue';
import Select from '@/components/ui/Select.vue';
import Amount from '@/components/ui/Amount.vue';
import flowService from '@/services/flowService';
import sourceConnectionService, { type SourceConnection } from '@/services/sourceConnectionService';
import rcpReattributionService, {
  RCP_ACTIONABLE,
  RCP_ORPHAN_ACTIONABLE,
  RCP_SERVICES,
  type RcpAnalyzeResponse,
  type RcpCommitResponse,
  type RcpOrphan,
  type RcpOrphanAnalyzeResponse,
  type RcpOrphanCommitResponse,
  type RcpProposal,
  type RcpRun,
} from '@/services/rcpReattributionService';
import { error as toastError, success, warning } from '@/utils/toaster';

const linkFile = ref<File | null>(null);
const dumpFiles = ref<File[]>([]);
const connections = ref<SourceConnection[]>([]);
const flows = ref<{ id: number; code: string; name: string }[]>([]);
const connectionId = ref<number | null>(null);
const flowId = ref<number | null>(0); // 0 = tous les flux (aucun filtre envoyé)

const isAnalyzing = ref(false);
const isCommitting = ref(false);
const report = ref<RcpAnalyzeResponse | null>(null);
const commitReport = ref<RcpCommitResponse | null>(null);
const selected = ref<Set<string>>(new Set());
const expanded = ref<Set<string>>(new Set());
const onlyControlErrors = ref(true);
const confirmOpen = ref(false);
const KEPT_SERVICES = RCP_SERVICES;
/** Phase reported by the running job — the whole point of the job pattern is
 *  that the operator sees this instead of an endless spinner. */
const jobPhase = ref('');
const jobProgress = ref<{ done: number; total: number }>({ done: 0, total: 0 });
const jobLogs = ref<string[]>([]);
const POLL_MS = 1500;
/** The running job id survives a reload: the batch lives server-side, so
 *  leaving the page must not look like a cancellation. */
const JOB_STORAGE_KEY = 'rcp-reattribution-job';
const logBox = ref<HTMLElement | null>(null);
const runs = ref<RcpRun[]>([]);
const isLoadingRun = ref(false);

/** Second section — the movements the ingestion could not key at all. Separate
 *  state throughout: it is a different population, a different write (a
 *  retargeting, not a split) and it must never share a selection with the
 *  upload-driven half above. */
const orphanFlowId = ref<number | null>(null);
const isOrphanAnalyzing = ref(false);
const isOrphanCommitting = ref(false);
const orphanReport = ref<RcpOrphanAnalyzeResponse | null>(null);
const orphanCommitReport = ref<RcpOrphanCommitResponse | null>(null);
const orphanSelected = ref<Set<string>>(new Set());
const orphanConfirmOpen = ref(false);

const connectionOptions = computed(() =>
  connections.value.map((c) => ({ value: c.id, label: `${c.name} (${c.code})` }))
);
// Optional narrowing only: the default sweeps every finacle source and each
// movement is routed by its own flow (lot or reconciliation key). Picking a flow
// hides the movements of the others — they come back ENTRY_NOT_FOUND.
const flowOptions = computed(() => [
  { value: 0, label: 'Tous les flux (recommandé)' },
  ...flows.value.map((f) => ({ value: f.id, label: `${f.name} (${f.code})` })),
]);

const actionable = computed(() =>
  (report.value?.proposals || []).filter((p) => RCP_ACTIONABLE.includes(p.status))
);
const blocked = computed(() =>
  (report.value?.proposals || []).filter((p) => !RCP_ACTIONABLE.includes(p.status))
);
const controlsShown = computed(() => {
  const controls = report.value?.controls || [];
  return onlyControlErrors.value ? controls.filter((c) => c.status !== 'OK') : controls;
});
const selectedProposals = computed(() =>
  actionable.value.filter((p) => selected.value.has(p.msgid))
);
const selectedTotal = computed(() =>
  selectedProposals.value.reduce((sum, p) => sum + Number(p.resolved_amount || 0), 0)
);
/** How the actionable movements split between the two flows — the quickest way
 *  to see that the BB and classic sides were both picked up. */
const byTargetKind = computed(() => {
  const counts: Record<string, number> = {};
  actionable.value.forEach((p) => {
    const key = targetLabel(p.target_kind);
    counts[key] = (counts[key] || 0) + 1;
  });
  return counts;
});

/** A flow must be picked here: the analysis reads ONE flow's stranded
 *  movements, and "all flows" would sweep populations that have nothing to do
 *  with each other. */
const orphanFlowOptions = computed(() =>
  flows.value.map((f) => ({ value: f.id, label: `${f.name} (${f.code})` }))
);
const orphanProposals = computed(() => orphanReport.value?.proposals || []);
const orphanActionable = computed(() =>
  orphanProposals.value.filter((p) => RCP_ORPHAN_ACTIONABLE.includes(p.status))
);
const orphanBlocked = computed(() =>
  orphanProposals.value.filter((p) => !RCP_ORPHAN_ACTIONABLE.includes(p.status))
);
const orphanSelectedItems = computed(() =>
  orphanActionable.value.filter((p) => orphanSelected.value.has(p.source_hash))
);
const orphanSelectedTotal = computed(() =>
  orphanSelectedItems.value.reduce((sum, p) => sum + Number(p.amount || 0), 0)
);
/** What the blocked movements are blocked ON — the number that says whether the
 *  next win is a parser fix or a human arbitration. */
const orphanByStatus = computed(() => {
  const counts: Record<string, number> = {};
  orphanBlocked.value.forEach((p) => {
    counts[p.status] = (counts[p.status] || 0) + 1;
  });
  return counts;
});

/** Polls a job to completion, keeping the phase visible. Every request here is
 *  a dict lookup server-side, so nothing can time out however long the batch
 *  takes — which is exactly what the 504 taught us. */
async function pollJob<T>(jobId: string, kind: 'analyze' | 'commit'): Promise<T> {
  localStorage.setItem(JOB_STORAGE_KEY, JSON.stringify({ jobId, kind }));
  try {
    for (;;) {
      const { data } = await rcpReattributionService.getJob<T>(jobId);
      jobPhase.value = data.phase;
      jobProgress.value = { done: data.done, total: data.total };
      jobLogs.value = data.logs || [];
      nextTick(() => {
        if (logBox.value) logBox.value.scrollTop = logBox.value.scrollHeight;
      });
      if (data.status === 'error') throw new Error(data.error || 'le traitement a échoué');
      if (data.status === 'done') return data.result as T;
      await new Promise((resolve) => setTimeout(resolve, POLL_MS));
    }
  } finally {
    localStorage.removeItem(JOB_STORAGE_KEY);
    fetchRuns();
  }
}

async function fetchRuns() {
  try {
    const { data } = await rcpReattributionService.listRuns(20);
    runs.value = data;
  } catch {
    /* l'historique est un confort : son échec ne doit rien bloquer */
  }
}

/** Re-opens a past analysis. The report was persisted; the logs were not. */
async function openRun(run: RcpRun) {
  if (run.kind !== 'analyze') return;
  isLoadingRun.value = true;
  try {
    const { data } = await rcpReattributionService.getRun<RcpAnalyzeResponse>(run.job_id);
    if (!data.result) {
      toastError("Ce traitement n'a pas de rapport (échec ou encore en cours).");
      return;
    }
    report.value = data.result;
    commitReport.value = null;
    jobLogs.value = [];
    selected.value = new Set(
      data.result.proposals
        .filter((p) => RCP_ACTIONABLE.includes(p.status))
        .map((p) => p.msgid)
    );
    success(`Rapport du ${formatRunDate(run.started_at)} rouvert`);
  } catch (e: any) {
    toastError(e?.response?.data?.detail || "Impossible d'ouvrir ce traitement");
  } finally {
    isLoadingRun.value = false;
  }
}

function formatRunDate(value: string | null) {
  return value ? new Date(value).toLocaleString('fr-FR') : '—';
}

function runVariant(status: string) {
  if (status === 'done') return 'modern-success';
  if (status === 'error') return 'modern-danger';
  return 'modern-warning';
}

function onLinkPicked(event: Event) {
  const files = (event.target as HTMLInputElement).files;
  linkFile.value = files && files.length ? files[0] : null;
}

function onDumpsPicked(event: Event) {
  const files = (event.target as HTMLInputElement).files;
  // Appending rather than replacing: the dumps arrive in batches (8 files at a
  // time), and picking a second folder should not drop the first.
  if (files) dumpFiles.value = [...dumpFiles.value, ...Array.from(files)];
}

function removeDump(index: number) {
  dumpFiles.value = dumpFiles.value.filter((_, i) => i !== index);
}

function toggle(msgid: string) {
  const next = new Set(selected.value);
  next.has(msgid) ? next.delete(msgid) : next.add(msgid);
  selected.value = next;
}

function toggleExpanded(msgid: string) {
  const next = new Set(expanded.value);
  next.has(msgid) ? next.delete(msgid) : next.add(msgid);
  expanded.value = next;
}

function selectAll(value: boolean) {
  selected.value = value ? new Set(actionable.value.map((p) => p.msgid)) : new Set();
}

function controlVariant(status: string) {
  return status === 'OK' ? 'modern-success' : 'modern-warning';
}

function targetLabel(kind: string) {
  return kind === 'RECO' ? 'Clé reco' : 'Lot';
}

function statusVariant(status: string) {
  if (status === 'PROPOSED') return 'modern-success';
  if (status === 'TO_RECOMMIT' || status === 'TO_REPLAY') return 'modern-warning';
  if (status === 'EMARGED' || status === 'ALREADY_COMMITTED') return 'modern-info';
  return 'modern-danger';
}

async function analyze() {
  if (!linkFile.value) {
    toastError('Le fichier de lien (xlsx) est requis.');
    return;
  }
  if (!dumpFiles.value.length) {
    toastError('Au moins un fichier de paiements est requis.');
    return;
  }
  isAnalyzing.value = true;
  report.value = null;
  commitReport.value = null;
  selected.value = new Set();
  jobPhase.value = 'envoi des fichiers';
  jobProgress.value = { done: 0, total: 0 };
  try {
    const { data: job } = await rcpReattributionService.startAnalyze({
      linkFile: linkFile.value,
      dumpFiles: dumpFiles.value,
      connectionId: connectionId.value,
      flowId: flowId.value || null,
    });
    const data = await pollJob<RcpAnalyzeResponse>(job.job_id, 'analyze');
    report.value = data;
    // Pre-tick everything that can be committed; the operator unticks.
    selected.value = new Set(
      data.proposals.filter((p) => RCP_ACTIONABLE.includes(p.status)).map((p) => p.msgid)
    );
    if (data.datamart_error) warning(`Datamart: ${data.datamart_error}`);
    success(`${data.proposals.length} mouvements RCP analysés`);
  } catch (e: any) {
    toastError(e?.response?.data?.detail || e?.message || "L'analyse a échoué");
  } finally {
    isAnalyzing.value = false;
    jobPhase.value = '';
  }
}

async function commit() {
  confirmOpen.value = false;
  isCommitting.value = true;
  jobPhase.value = 'préparation';
  jobProgress.value = { done: 0, total: 0 };
  try {
    const items = selectedProposals.value.map((p: RcpProposal) => ({
      msgid: p.msgid,
      entry_source_hash: p.entry?.source_hash || '',
      targets: p.targets.map((t) => ({
        target_id: t.target_id,
        amount: t.amount,
        payment_count: t.payment_count,
        pos: t.pos,
      })),
    }));
    const { data: job } = await rcpReattributionService.startCommit(items);
    const data = await pollJob<RcpCommitResponse>(job.job_id, 'commit');
    commitReport.value = data;
    if (data.failed) warning(`${data.applied} réattribué(s), ${data.failed} en échec`);
    else success(`${data.applied} mouvement(s) réattribué(s)`);
    // The committed movements are gone from the live table: the report is stale.
    selected.value = new Set();
  } catch (e: any) {
    toastError(e?.response?.data?.detail || e?.message || 'Le commit a échoué');
  } finally {
    isCommitting.value = false;
    jobPhase.value = '';
  }
}

function toggleOrphan(sourceHash: string) {
  const next = new Set(orphanSelected.value);
  if (next.has(sourceHash)) next.delete(sourceHash);
  else next.add(sourceHash);
  orphanSelected.value = next;
}

function selectAllOrphans(value: boolean) {
  orphanSelected.value = value
    ? new Set(orphanActionable.value.map((p) => p.source_hash))
    : new Set();
}

/** How much the operator should trust a proposal. The rule IS the evidence: a
 *  PaymentNumber Finacle wrote into ref_no is not a digit run scraped out of a
 *  label an operator typed, and the two must not look alike. */
function orphanRuleLabel(rule: string) {
  if (rule === 'REF_NO') return 'ref_no';
  if (rule === 'TP_RETURN_SHAPE') return 'libellé retour';
  if (rule === 'TP_NAMES_MOVEMENT') return 'mouvement cité';
  if (rule === 'TP_DIGITS') return 'chiffres en clair';
  return '—';
}

function orphanRuleVariant(rule: string) {
  if (rule === 'REF_NO' || rule === 'TP_RETURN_SHAPE') return 'modern-success';
  if (rule === 'TP_NAMES_MOVEMENT') return 'modern-info';
  return 'modern-warning';
}

function orphanStatusVariant(status: string) {
  if (status === 'PROPOSED') return 'modern-success';
  if (status === 'KEY_AMBIGUOUS') return 'modern-warning';
  return 'modern-danger';
}

function orphanStatusLabel(status: string) {
  if (status === 'NO_KEY') return 'Aucune clé';
  if (status === 'KEY_AMBIGUOUS') return 'Ambigu';
  if (status === 'NO_TARGET') return 'Sans cible';
  return status;
}

async function analyzeOrphans() {
  if (!orphanFlowId.value) {
    toastError('Choisissez un flux.');
    return;
  }
  isOrphanAnalyzing.value = true;
  orphanReport.value = null;
  orphanCommitReport.value = null;
  orphanSelected.value = new Set();
  jobPhase.value = 'démarrage';
  jobProgress.value = { done: 0, total: 0 };
  try {
    const { data: job } = await rcpReattributionService.startOrphanAnalyze({
      flowId: orphanFlowId.value,
      connectionId: connectionId.value,
    });
    const data = await pollJob<RcpOrphanAnalyzeResponse>(job.job_id, 'analyze');
    orphanReport.value = data;
    // Pre-tick what can be committed; the operator unticks. Nothing else is
    // ever tickable — an ambiguity is a decision, not a default.
    orphanSelected.value = new Set(
      data.proposals
        .filter((p) => RCP_ORPHAN_ACTIONABLE.includes(p.status))
        .map((p) => p.source_hash)
    );
    if (data.datamart_error) warning(`Datamart: ${data.datamart_error}`);
    success(`${data.proposals.length} mouvement(s) non rattaché(s) analysé(s)`);
  } catch (e: any) {
    toastError(e?.response?.data?.detail || e?.message || "L'analyse a échoué");
  } finally {
    isOrphanAnalyzing.value = false;
    jobPhase.value = '';
  }
}

async function commitOrphans() {
  orphanConfirmOpen.value = false;
  isOrphanCommitting.value = true;
  jobPhase.value = 'préparation';
  jobProgress.value = { done: 0, total: 0 };
  try {
    const items = orphanSelectedItems.value.map((p: RcpOrphan) => ({
      source_hash: p.source_hash,
      target_id: p.target_id,
      rule: p.rule,
      key: p.keys[0] || '',
    }));
    const { data: job } = await rcpReattributionService.startOrphanCommit(items);
    const data = await pollJob<RcpOrphanCommitResponse>(job.job_id, 'commit');
    orphanCommitReport.value = data;
    if (data.failed) warning(`${data.applied} rattaché(s), ${data.failed} en échec`);
    else success(`${data.applied} mouvement(s) rattaché(s)`);
    // The committed movements are no longer stranded: the report is stale.
    orphanSelected.value = new Set();
  } catch (e: any) {
    toastError(e?.response?.data?.detail || e?.message || 'Le rattachement a échoué');
  } finally {
    isOrphanCommitting.value = false;
    jobPhase.value = '';
  }
}

function exportControlsCsv() {
  const controls = report.value?.controls || [];
  const header = [
    'msgid', 'status', 'serviceid', 'direction', 'tran_id', 'sp_date', 'expected_count',
    'found_count', 'delta_count', 'expected_amount', 'found_amount', 'delta_amount', 'files',
  ];
  const lines = [header.join(';')].concat(
    controls.map((c) =>
      [
        c.msgid, c.status, c.service_id, c.direction, c.tran_id, c.sp_date, c.expected_count,
        c.found_count, c.delta_count, c.expected_amount, c.found_amount,
        c.delta_amount, c.files.join('|'),
      ].join(';')
    )
  );
  const blob = new Blob([lines.join('\n')], { type: 'text/csv;charset=utf-8;' });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = 'rcp_control.csv';
  link.click();
  URL.revokeObjectURL(url);
}

/** Re-attaches to a job left running when the page was reloaded or the tab
 *  left. The server never stopped working — only the browser stopped watching. */
async function resumeJob() {
  const saved = localStorage.getItem(JOB_STORAGE_KEY);
  if (!saved) return;
  let parsed: { jobId: string; kind: 'analyze' | 'commit' };
  try {
    parsed = JSON.parse(saved);
  } catch {
    localStorage.removeItem(JOB_STORAGE_KEY);
    return;
  }
  const isAnalyze = parsed.kind === 'analyze';
  if (isAnalyze) isAnalyzing.value = true;
  else isCommitting.value = true;
  jobPhase.value = 'reprise du traitement en cours…';
  try {
    if (isAnalyze) {
      const data = await pollJob<RcpAnalyzeResponse>(parsed.jobId, 'analyze');
      report.value = data;
      selected.value = new Set(
        data.proposals.filter((p) => RCP_ACTIONABLE.includes(p.status)).map((p) => p.msgid)
      );
      success(`${data.proposals.length} mouvements analysés (traitement repris)`);
    } else {
      commitReport.value = await pollJob<RcpCommitResponse>(parsed.jobId, 'commit');
      success('Commit terminé (traitement repris)');
    }
  } catch (e: any) {
    // A job the server no longer knows (restart, TTL) is not an error worth a
    // red toast — the operator simply relaunches.
    toastError(e?.response?.status === 404
      ? "Le traitement précédent n'est plus disponible — relancez l'analyse."
      : e?.message || 'Le traitement précédent a échoué');
  } finally {
    isAnalyzing.value = false;
    isCommitting.value = false;
    jobPhase.value = '';
  }
}

onMounted(async () => {
  resumeJob();
  fetchRuns();
  try {
    const [{ data: conns }, { data: flowList }] = await Promise.all([
      sourceConnectionService.getAll(),
      flowService.getAll(),
    ]);
    connections.value = conns;
    flows.value = flowList;
    const datamart = conns.find((c) => c.type === 'mssql');
    if (datamart) connectionId.value = datamart.id;
  } catch (e: any) {
    toastError(e?.response?.data?.detail || 'Chargement des connexions impossible');
  }
});
</script>

<template>
  <div class="space-y-6">
    <div>
      <h2 class="text-xl font-semibold text-space-indigo">Réattribution RCP (temporaire)</h2>
      <p class="text-sm text-space-indigo/60 mt-1">
        Rattache les mouvements agrégés de retour/rejet (RCP) au lot d'origine des paiements
        qu'ils annulent. Le datamart ne porte pas ce lien : il est reconstruit à partir du
        rapport de lien et des dumps de paiements. Rien n'est écrit sans validation explicite.
      </p>
    </div>

    <!-- Uploads -->
    <Card title="Fichiers">
      <div class="grid grid-cols-1 md:grid-cols-2 gap-6">
        <div>
          <label class="block text-sm font-medium text-space-indigo">
            1. Rapport de lien (.xlsx)
          </label>
          <p class="text-xs text-space-indigo/50 mt-1">
            SP_LINK_REPORT — un mouvement par ligne, les lignes NCP sont ignorées.
          </p>
          <input
            type="file"
            accept=".xlsx"
            class="mt-2 block w-full text-sm text-space-indigo file:mr-4 file:rounded-md file:border-0 file:bg-lavender-blush file:px-3 file:py-2 file:text-sm file:font-medium file:text-space-indigo"
            @change="onLinkPicked"
          />
          <p v-if="linkFile" class="text-xs text-space-indigo/70 mt-2 font-mono">
            {{ linkFile.name }}
          </p>
        </div>

        <div>
          <label class="block text-sm font-medium text-space-indigo">
            2. Fiches paiements (return / reject)
          </label>
          <p class="text-xs text-space-indigo/50 mt-1">
            Dumps de la table des retours/rejets — autant de fichiers que nécessaire.
          </p>
          <input
            type="file"
            multiple
            accept=".csv,.txt"
            class="mt-2 block w-full text-sm text-space-indigo file:mr-4 file:rounded-md file:border-0 file:bg-lavender-blush file:px-3 file:py-2 file:text-sm file:font-medium file:text-space-indigo"
            @change="onDumpsPicked"
          />
          <ul v-if="dumpFiles.length" class="mt-2 space-y-1">
            <li
              v-for="(file, index) in dumpFiles"
              :key="file.name + index"
              class="flex items-center justify-between text-xs font-mono text-space-indigo/70"
            >
              <span class="truncate">{{ file.name }}</span>
              <button class="text-red-600 hover:underline ml-2" @click="removeDump(index)">
                retirer
              </button>
            </li>
          </ul>
        </div>

        <Select
          v-model="connectionId"
          label="Connexion datamart"
          :options="connectionOptions"
          theme="reveals"
        />
        <div>
          <Select
            v-model="flowId"
            label="Flux (filtre facultatif)"
            :options="flowOptions"
            theme="reveals"
          />
          <p class="text-xs text-space-indigo/50 mt-1">
            Par défaut tous les flux finacle sont balayés et chaque mouvement est
            routé selon le sien. Restreindre ici masque les mouvements des autres flux.
          </p>
        </div>
      </div>

      <div class="mt-6 flex items-center gap-3">
        <Button variant="reveals-primary" :disabled="isAnalyzing || isCommitting" @click="analyze">
          {{ isAnalyzing ? 'Analyse en cours…' : 'Analyser' }}
        </Button>
        <Loader v-if="isAnalyzing || isCommitting" size="sm" />
        <div v-if="jobPhase" class="min-w-0 flex-1">
          <div class="text-xs text-space-indigo/70">
            {{ jobPhase
            }}<span v-if="jobProgress.total"> — {{ jobProgress.done }}/{{ jobProgress.total }}</span>
          </div>
          <div v-if="jobProgress.total" class="mt-1 h-1 w-full rounded bg-space-indigo/10">
            <div
              class="h-1 rounded bg-tropical-mint transition-all"
              :style="{ width: `${Math.min(100, (jobProgress.done / jobProgress.total) * 100)}%` }"
            ></div>
          </div>
        </div>
      </div>

      <!-- What the batch is actually doing, as it does it. -->
      <div v-if="jobLogs.length" class="mt-4">
        <div class="flex items-center justify-between mb-1">
          <span class="text-xs uppercase text-space-indigo/50">Journal du traitement</span>
          <span class="text-xs text-space-indigo/40">{{ jobLogs.length }} ligne(s)</span>
        </div>
        <div
          ref="logBox"
          class="max-h-56 overflow-y-auto rounded bg-space-indigo/[0.04] border border-space-indigo/10 p-3 font-mono text-xs leading-relaxed"
        >
          <div
            v-for="(line, index) in jobLogs"
            :key="index"
            :class="/ERREUR|ATTENTION|ÉCHEC|REFUSÉ/.test(line) ? 'text-red-600' : 'text-space-indigo/70'"
          >{{ line }}</div>
        </div>
      </div>
    </Card>

    <!-- Second section: the movements the ingestion could not key at all.
         Same job machinery, different population and a different write — a
         retargeting, never a split. -->
    <Card title="Mouvements non rattachés">
      <template #header>
        <div class="flex items-center justify-between">
          <h3 class="text-lg leading-6 font-medium text-space-indigo">
            Mouvements non rattachés
          </h3>
          <Badge v-if="orphanReport" variant="modern-info">
            {{ orphanProposals.length }} mouvement(s)
          </Badge>
        </div>
      </template>
      <p class="text-sm text-space-indigo/60 mb-4">
        Reprend les mouvements que l'ingestion n'a pas su classer (<code>Not Supported</code>
        ou sans <code>reco_id</code>), cherche dans chacun ce qu'il nomme encore — un
        PaymentNumber dans <code>ref_no</code>, un libellé de retour, un mouvement cité en
        clair — et le résout sur le datamart. Un mouvement dont aucun paiement ne confirme la
        clé n'est pas proposé, et une écriture manuelle qui ne porte que des montants
        (contrepassations en texte libre) reste un arbitrage humain.
      </p>

      <div class="grid grid-cols-1 md:grid-cols-3 gap-4 items-end">
        <div>
          <label class="block text-sm font-medium text-space-indigo mb-1">Flux</label>
          <Select v-model="orphanFlowId" :options="orphanFlowOptions" placeholder="Choisir un flux" />
        </div>
        <div class="text-xs text-space-indigo/50">
          La connexion datamart choisie ci-dessus est réutilisée.
        </div>
        <div class="flex gap-2">
          <Button
            :disabled="isOrphanAnalyzing || isOrphanCommitting || !orphanFlowId"
            @click="analyzeOrphans"
          >
            <Loader v-if="isOrphanAnalyzing" class="w-4 h-4 mr-2" />
            Analyser les non rattachés
          </Button>
        </div>
      </div>

      <template v-if="orphanReport">
        <div class="flex flex-wrap items-center gap-2 mt-6 mb-3">
          <Badge variant="modern-success">{{ orphanActionable.length }} rattachable(s)</Badge>
          <Badge
            v-for="(count, status) in orphanByStatus"
            :key="status"
            :variant="orphanStatusVariant(String(status))"
          >
            {{ orphanStatusLabel(String(status)) }} : {{ count }}
          </Badge>
          <span v-if="orphanReport.datamart_error" class="text-xs text-red-600">
            {{ orphanReport.datamart_error }}
          </span>
        </div>

        <p v-if="!orphanActionable.length" class="text-sm text-space-indigo/50 italic">
          Aucun mouvement rattachable automatiquement — voir le détail ci-dessous.
        </p>
        <template v-else>
          <div class="flex items-center justify-between mb-2">
            <div class="flex items-center gap-3 text-sm">
              <button class="text-space-indigo/70 hover:underline" @click="selectAllOrphans(true)">
                Tout cocher
              </button>
              <button class="text-space-indigo/70 hover:underline" @click="selectAllOrphans(false)">
                Tout décocher
              </button>
              <span class="text-space-indigo/50">
                {{ orphanSelectedItems.length }} sélectionné(s) —
                <Amount :value="orphanSelectedTotal" />
              </span>
            </div>
            <Button
              :disabled="!orphanSelectedItems.length || isOrphanCommitting"
              @click="orphanConfirmOpen = true"
            >
              <Loader v-if="isOrphanCommitting" class="w-4 h-4 mr-2" />
              Rattacher la sélection
            </Button>
          </div>
          <div class="overflow-x-auto">
            <table class="min-w-full text-sm">
              <thead>
                <tr class="text-left text-xs uppercase text-space-indigo/50">
                  <th class="py-2 pr-2"></th>
                  <th class="py-2 pr-4">Mouvement</th>
                  <th class="py-2 pr-4">Libellé</th>
                  <th class="py-2 pr-4 text-right">Montant</th>
                  <th class="py-2 pr-4">Preuve</th>
                  <th class="py-2 pr-4">Clé</th>
                  <th class="py-2 pr-4">Cible</th>
                </tr>
              </thead>
              <tbody>
                <tr
                  v-for="proposal in orphanActionable"
                  :key="proposal.source_hash"
                  class="border-t border-space-indigo/10"
                >
                  <td class="py-2 pr-2">
                    <input
                      type="checkbox"
                      :checked="orphanSelected.has(proposal.source_hash)"
                      @change="toggleOrphan(proposal.source_hash)"
                    />
                  </td>
                  <td class="py-2 pr-4 font-mono text-xs">
                    {{ proposal.external_ref || proposal.source_hash.slice(0, 12) }}
                    <div class="text-space-indigo/40">{{ proposal.value_date?.slice(0, 10) }}</div>
                  </td>
                  <td class="py-2 pr-4 text-xs max-w-xs truncate" :title="proposal.transaction_particulars || ''">
                    {{ proposal.transaction_particulars || '—' }}
                  </td>
                  <td class="py-2 pr-4 text-right">
                    <Amount :value="Number(proposal.amount)" />
                  </td>
                  <td class="py-2 pr-4">
                    <Badge :variant="orphanRuleVariant(proposal.rule)">
                      {{ orphanRuleLabel(proposal.rule) }}
                    </Badge>
                  </td>
                  <td class="py-2 pr-4 font-mono text-xs">{{ proposal.keys[0] || '—' }}</td>
                  <td class="py-2 pr-4 font-mono text-xs">
                    <Badge variant="modern-info" class="mr-1">
                      {{ targetLabel(proposal.target_kind) }}
                    </Badge>
                    {{ proposal.target_label || proposal.target_id }}
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        </template>

        <div v-if="orphanBlocked.length" class="mt-6">
          <h4 class="text-sm font-medium text-space-indigo mb-2">
            Non rattachables ({{ orphanBlocked.length }})
          </h4>
          <div class="overflow-x-auto max-h-96 overflow-y-auto">
            <table class="min-w-full text-sm">
              <thead>
                <tr class="text-left text-xs uppercase text-space-indigo/50">
                  <th class="py-2 pr-4">Mouvement</th>
                  <th class="py-2 pr-4">Libellé</th>
                  <th class="py-2 pr-4 text-right">Montant</th>
                  <th class="py-2 pr-4">Statut</th>
                  <th class="py-2 pr-4">Pourquoi</th>
                </tr>
              </thead>
              <tbody>
                <tr
                  v-for="proposal in orphanBlocked"
                  :key="proposal.source_hash"
                  class="border-t border-space-indigo/10"
                >
                  <td class="py-2 pr-4 font-mono text-xs">
                    {{ proposal.external_ref || proposal.source_hash.slice(0, 12) }}
                  </td>
                  <td class="py-2 pr-4 text-xs max-w-xs truncate" :title="proposal.transaction_particulars || ''">
                    {{ proposal.transaction_particulars || '—' }}
                  </td>
                  <td class="py-2 pr-4 text-right">
                    <Amount :value="Number(proposal.amount)" />
                  </td>
                  <td class="py-2 pr-4">
                    <Badge :variant="orphanStatusVariant(proposal.status)">
                      {{ orphanStatusLabel(proposal.status) }}
                    </Badge>
                  </td>
                  <td class="py-2 pr-4 text-xs text-space-indigo/60">{{ proposal.message }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>

        <div v-if="orphanCommitReport" class="mt-6">
          <h4 class="text-sm font-medium text-space-indigo mb-2">
            Résultat : {{ orphanCommitReport.applied }} rattaché(s),
            {{ orphanCommitReport.failed }} en échec
          </h4>
          <ul class="text-xs font-mono space-y-1">
            <li
              v-for="result in orphanCommitReport.results.filter((r) => !r.applied)"
              :key="result.source_hash"
              class="text-red-600"
            >
              {{ result.source_hash.slice(0, 12) }} — {{ result.error }}
            </li>
          </ul>
        </div>
      </template>
    </Card>

    <!-- Past runs: a batch nobody has to watch must be findable afterwards. -->
    <Card title="Traitements récents">
      <template #header>
        <div class="flex items-center justify-between">
          <h3 class="text-lg leading-6 font-medium text-space-indigo">Traitements récents</h3>
          <Button variant="ghost" size="sm" :disabled="isLoadingRun" @click="fetchRuns">
            Rafraîchir
          </Button>
        </div>
      </template>
      <p v-if="!runs.length" class="text-sm text-space-indigo/50 italic">
        Aucun traitement enregistré pour l'instant.
      </p>
      <table v-else class="min-w-full text-sm">
        <thead class="text-xs uppercase text-space-indigo/50">
          <tr>
            <th class="text-left py-2">Lancé le</th>
            <th class="text-left py-2">Type</th>
            <th class="text-left py-2">Statut</th>
            <th class="text-right py-2">Mouvements</th>
            <th class="text-right py-2">Résultat</th>
            <th class="text-left py-2 pl-4">Fichiers</th>
            <th class="py-2"></th>
          </tr>
        </thead>
        <tbody class="divide-y divide-space-indigo/10">
          <tr v-for="run in runs" :key="run.job_id" class="hover:bg-gray-50">
            <td class="py-2 whitespace-nowrap">{{ formatRunDate(run.started_at) }}</td>
            <td class="py-2">{{ run.kind === 'analyze' ? 'Analyse' : 'Commit' }}</td>
            <td class="py-2">
              <Badge :variant="runVariant(run.status)">{{ run.status }}</Badge>
            </td>
            <td class="py-2 text-right">{{ run.movements }}</td>
            <td class="py-2 text-right">
              <span v-if="run.kind === 'analyze'">{{ run.actionable }} réattribuables</span>
              <span v-else>{{ run.applied }} appliqué(s), {{ run.failed }} en échec</span>
            </td>
            <td class="py-2 pl-4 text-xs text-space-indigo/50 truncate max-w-md">
              {{ run.error || run.label }}
            </td>
            <td class="py-2 text-right">
              <Button
                v-if="run.kind === 'analyze' && run.status === 'done'"
                variant="ghost"
                size="sm"
                :disabled="isLoadingRun"
                @click="openRun(run)"
              >
                Ouvrir
              </Button>
            </td>
          </tr>
        </tbody>
      </table>
      <p class="mt-3 text-xs text-space-indigo/50">
        Les rapports sont conservés en base : tu peux quitter la page pendant qu'un
        traitement tourne et rouvrir le résultat ici. Le journal, lui, n'est
        disponible que pendant l'exécution.
      </p>
    </Card>

    <template v-if="report">
      <!-- Per-file quality -->
      <Card title="Fichiers lus">
        <table class="min-w-full text-sm">
          <thead class="text-xs uppercase text-space-indigo/50">
            <tr>
              <th class="text-left py-2">Fichier</th>
              <th class="text-right py-2">Lignes</th>
              <th class="text-right py-2">Avec msgid</th>
              <th class="text-right py-2">msgid distincts</th>
              <th class="text-left py-2 pl-4">Services / colonne montant</th>
              <th class="text-left py-2">État</th>
            </tr>
          </thead>
          <tbody class="divide-y divide-space-indigo/10">
            <tr>
              <td class="py-2 font-mono">{{ report.link_file.file_name }}</td>
              <td class="py-2 text-right">{{ report.link_file.rows }}</td>
              <td class="py-2 text-right">{{ report.link_file.rows_with_msgid }}</td>
              <td class="py-2 text-right">{{ report.link_file.distinct_msgids }}</td>
              <td class="py-2 pl-4 text-xs">
                <span
                  v-for="(count, service) in report.link_file.services"
                  :key="service"
                  class="mr-2"
                  :class="KEPT_SERVICES.includes(String(service)) ? 'text-space-indigo' : 'text-space-indigo/40'"
                >{{ service }}={{ count }}</span>
              </td>
              <td class="py-2">
                <Badge :variant="report.link_file.error ? 'modern-danger' : 'modern-success'">
                  {{ report.link_file.error || 'ok' }}
                </Badge>
              </td>
            </tr>
            <tr v-for="file in report.dump_files" :key="file.file_name">
              <td class="py-2 font-mono">{{ file.file_name }}</td>
              <td class="py-2 text-right">{{ file.rows }}</td>
              <td class="py-2 text-right">{{ file.rows_with_msgid }}</td>
              <td class="py-2 text-right">{{ file.distinct_msgids }}</td>
              <td class="py-2 pl-4 font-mono text-xs">{{ file.amount_column || '—' }}</td>
              <td class="py-2">
                <Badge
                  :variant="
                    file.error
                      ? 'modern-danger'
                      : file.rows_with_msgid === 0
                        ? 'modern-warning'
                        : 'modern-success'
                  "
                >
                  {{ file.error || (file.rows_with_msgid === 0 ? 'aucun msgid exploitable' : 'ok') }}
                </Badge>
              </td>
            </tr>
          </tbody>
        </table>
        <p v-if="report.datamart_error" class="mt-3 text-sm text-red-600">
          Datamart : {{ report.datamart_error }} — la résolution s'est repliée sur les données
          de l'application.
        </p>
      </Card>

      <!-- Part 1 -->
      <Card title="Contrôle des montants (partie 1)">
        <template #header>
          <div class="flex items-center justify-between flex-wrap gap-2">
            <h3 class="text-lg leading-6 font-medium text-space-indigo">
              Contrôle des montants (partie 1)
            </h3>
            <div class="flex items-center gap-3">
              <label class="text-xs text-space-indigo/60 flex items-center gap-1">
                <input v-model="onlyControlErrors" type="checkbox" /> erreurs seulement
              </label>
              <Button variant="ghost" size="sm" @click="exportControlsCsv">Export CSV</Button>
            </div>
          </div>
        </template>

        <div class="flex flex-wrap gap-2 mb-4">
          <Badge
            v-for="(count, status) in report.control_summary"
            :key="status"
            :variant="status === 'total' ? 'modern-default' : controlVariant(String(status))"
          >
            {{ status }}: {{ count }}
          </Badge>
        </div>
        <p v-if="report.duplicate_msgids.length" class="text-sm text-amber-700 mb-3">
          msgid présents plusieurs fois dans le xlsx :
          {{ report.duplicate_msgids.join(', ') }}
        </p>

        <p v-if="!controlsShown.length" class="text-sm text-space-indigo/50 italic">
          Aucun écart : chaque mouvement retrouve exactement ses lignes et sa somme.
        </p>
        <div v-else class="overflow-x-auto">
          <table class="min-w-full text-sm">
            <thead class="text-xs uppercase text-space-indigo/50">
              <tr>
                <th class="text-left py-2">msgid</th>
                <th class="text-left py-2">Service</th>
                <th class="text-left py-2">Statut</th>
                <th class="text-right py-2">Lignes</th>
                <th class="text-right py-2">Attendu</th>
                <th class="text-right py-2">Trouvé</th>
                <th class="text-right py-2">Écart</th>
                <th class="text-left py-2 pl-4">Fichiers</th>
              </tr>
            </thead>
            <tbody class="divide-y divide-space-indigo/10">
              <tr v-for="control in controlsShown" :key="control.msgid">
                <td class="py-2 font-mono">{{ control.msgid }}</td>
                <td class="py-2 text-xs">{{ control.service_id }}</td>
                <td class="py-2">
                  <Badge :variant="controlVariant(control.status)">{{ control.status }}</Badge>
                </td>
                <td class="py-2 text-right">
                  {{ control.found_count }} / {{ control.expected_count }}
                </td>
                <td class="py-2 text-right"><Amount :value="control.expected_amount" /></td>
                <td class="py-2 text-right"><Amount :value="control.found_amount" /></td>
                <td class="py-2 text-right"><Amount :value="control.delta_amount" /></td>
                <td class="py-2 pl-4 text-xs font-mono">{{ control.files.join(', ') || '—' }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </Card>

      <!-- Proposals -->
      <Card title="Réattributions proposées">
        <template #header>
          <div class="flex items-center justify-between flex-wrap gap-2">
            <h3 class="text-lg leading-6 font-medium text-space-indigo">
              Réattributions proposées ({{ actionable.length }})
              <span
                v-for="(count, kind) in byTargetKind"
                :key="kind"
                class="ml-2 text-xs font-normal text-space-indigo/60"
              >{{ count }} → {{ kind }}</span>
            </h3>
            <div class="flex items-center gap-2">
              <Button variant="ghost" size="sm" @click="selectAll(true)">Tout cocher</Button>
              <Button variant="ghost" size="sm" @click="selectAll(false)">Tout décocher</Button>
            </div>
          </div>
        </template>

        <p v-if="!actionable.length" class="text-sm text-space-indigo/50 italic">
          Aucune réattribution possible avec ces fichiers.
        </p>
        <div v-else class="overflow-x-auto">
          <table class="min-w-full text-sm">
            <thead class="text-xs uppercase text-space-indigo/50">
              <tr>
                <th class="py-2 w-8"></th>
                <th class="text-left py-2">msgid</th>
                <th class="text-left py-2">Service</th>
                <th class="text-left py-2">Mouvement</th>
                <th class="text-right py-2">Booké</th>
                <th class="text-right py-2">Réattribué</th>
                <th class="text-left py-2 pl-4">Destination</th>
                <th class="text-left py-2 pl-4">Signalements</th>
              </tr>
            </thead>
            <tbody class="divide-y divide-space-indigo/10">
              <template v-for="proposal in actionable" :key="proposal.msgid">
                <tr class="hover:bg-gray-50">
                  <td class="py-2">
                    <input
                      type="checkbox"
                      :checked="selected.has(proposal.msgid)"
                      @change="toggle(proposal.msgid)"
                    />
                  </td>
                  <td class="py-2 font-mono cursor-pointer" @click="toggleExpanded(proposal.msgid)">
                    {{ proposal.msgid }}
                  </td>
                  <td class="py-2 text-xs">{{ proposal.service_id }}</td>
                  <td class="py-2">
                    <div class="font-mono text-xs truncate max-w-xs">
                      {{ proposal.entry?.external_ref || '—' }}
                    </div>
                    <div class="text-xs text-space-indigo/50">
                      {{ (proposal.entry?.value_date || '').slice(0, 10) }}
                      · {{ proposal.flow_code || proposal.entry?.account }}
                    </div>
                  </td>
                  <td class="py-2 text-right">
                    <Amount :value="proposal.entry?.amount" :currency="proposal.entry?.currency" />
                  </td>
                  <td class="py-2 text-right"><Amount :value="proposal.resolved_amount" /></td>
                  <td class="py-2 pl-4 whitespace-nowrap">
                    <Badge
                      :variant="proposal.target_kind === 'RECO' ? 'modern-info' : 'modern-reveals-default'"
                    >
                      {{ proposal.targets.length }} {{ targetLabel(proposal.target_kind) }}
                    </Badge>
                  </td>
                  <td class="py-2 pl-4">
                    <div class="flex flex-wrap gap-1">
                      <Badge v-if="proposal.status === 'TO_RECOMMIT'" variant="modern-warning">
                        à re-committer
                      </Badge>
                      <!-- Already reattributed: the movement stays withdrawn and
                           the replay updates the ghosts already in place. -->
                      <Badge v-if="proposal.status === 'TO_REPLAY'" variant="modern-warning">
                        à rejouer
                      </Badge>
                      <Badge
                        v-if="proposal.control_status !== 'OK'"
                        variant="modern-warning"
                      >
                        contrôle {{ proposal.control_status }}
                      </Badge>
                      <Badge
                        v-if="proposal.unresolved_payments.length"
                        variant="modern-warning"
                      >
                        {{ proposal.unresolved_payments.length }} paiement(s) non rattaché(s)
                      </Badge>
                    </div>
                  </td>
                </tr>
                <tr v-if="expanded.has(proposal.msgid)" class="bg-gray-50">
                  <td colspan="8" class="px-4 py-3">
                    <div class="text-xs uppercase text-space-indigo/50 mb-1">
                      Cibles ({{ targetLabel(proposal.target_kind) }})
                    </div>
                    <table class="min-w-full text-xs mb-3">
                      <tbody>
                        <tr v-for="target in proposal.targets" :key="target.target_id">
                          <td class="py-1 font-mono">{{ target.target_id }}</td>
                          <td class="py-1 pl-3">{{ target.label }}</td>
                          <td class="py-1 pl-3 text-space-indigo/50">
                            via {{ target.resolved_via }}
                          </td>
                          <td class="py-1 pl-3 text-right">
                            {{ target.payment_count }} paiement(s)
                          </td>
                          <td class="py-1 pl-3 text-right"><Amount :value="target.amount" /></td>
                        </tr>
                      </tbody>
                    </table>
                    <div v-if="proposal.unresolved_payments.length">
                      <div class="text-xs uppercase text-space-indigo/50 mb-1">
                        Paiements non rattachés
                      </div>
                      <div class="flex flex-wrap gap-2">
                        <span
                          v-for="item in proposal.unresolved_payments"
                          :key="item.po"
                          class="text-xs font-mono bg-white border border-space-indigo/10 rounded px-2 py-1"
                        >
                          {{ item.po }} · {{ item.amount }} · {{ item.reason }}
                        </span>
                      </div>
                    </div>
                  </td>
                </tr>
              </template>
            </tbody>
          </table>
        </div>

        <div
          v-if="actionable.length"
          class="mt-5 flex items-center justify-between border-t border-space-indigo/10 pt-4"
        >
          <div class="text-sm text-space-indigo/70">
            {{ selectedProposals.length }} mouvement(s) sélectionné(s) ·
            <Amount :value="selectedTotal" /> à réattribuer
          </div>
          <Button
            variant="reveals-primary"
            :disabled="!selectedProposals.length || isCommitting"
            @click="confirmOpen = true"
          >
            {{ isCommitting ? 'Commit en cours…' : 'Committer la sélection' }}
          </Button>
        </div>
      </Card>

      <!-- Everything that cannot be committed -->
      <Card :title="`Non réattribuables (${blocked.length})`">
        <p v-if="!blocked.length" class="text-sm text-space-indigo/50 italic">
          Tous les mouvements RCP ont trouvé leur lot.
        </p>
        <table v-else class="min-w-full text-sm">
          <thead class="text-xs uppercase text-space-indigo/50">
            <tr>
              <th class="text-left py-2">msgid</th>
              <th class="text-left py-2">Service</th>
              <th class="text-left py-2">Statut</th>
              <th class="text-right py-2">Montant</th>
              <th class="text-left py-2 pl-4">Détail</th>
            </tr>
          </thead>
          <tbody class="divide-y divide-space-indigo/10">
            <tr v-for="proposal in blocked" :key="proposal.msgid">
              <td class="py-2 font-mono">{{ proposal.msgid }}</td>
              <td class="py-2 text-xs">{{ proposal.service_id }}</td>
              <td class="py-2">
                <Badge :variant="statusVariant(proposal.status)">{{ proposal.status }}</Badge>
              </td>
              <td class="py-2 text-right"><Amount :value="proposal.settlement_amount" /></td>
              <td class="py-2 pl-4 text-xs text-space-indigo/70">{{ proposal.message }}</td>
            </tr>
          </tbody>
        </table>
      </Card>

      <!-- Commit outcome -->
      <Card v-if="commitReport" title="Résultat du commit">
        <div class="flex gap-2 mb-3">
          <Badge variant="modern-success">{{ commitReport.applied }} appliqué(s)</Badge>
          <Badge v-if="commitReport.failed" variant="modern-danger">
            {{ commitReport.failed }} en échec
          </Badge>
        </div>
        <table class="min-w-full text-sm">
          <tbody class="divide-y divide-space-indigo/10">
            <tr v-for="result in commitReport.results" :key="result.msgid">
              <td class="py-2 font-mono">{{ result.msgid }}</td>
              <td class="py-2">
                <Badge :variant="result.applied ? 'modern-success' : 'modern-danger'">
                  {{ result.applied ? 'ok' : 'échec' }}
                </Badge>
              </td>
              <td class="py-2 text-right">
                <Amount v-if="result.applied" :value="result.ghost_total" />
              </td>
              <td class="py-2 pl-4 text-xs text-space-indigo/70">
                {{ result.error || `${result.targets.length} cible(s)` }}
                <span v-if="result.parents_emarged" class="text-amber-700">
                  · mouvement déjà émargé, non retiré
                </span>
              </td>
            </tr>
          </tbody>
        </table>
        <p class="mt-3 text-xs text-space-indigo/50">
          Les mouvements commités ne sont plus dans la table vivante : relancez une analyse
          pour repartir d'un état à jour.
        </p>
      </Card>
    </template>

    <Modal :is-open="confirmOpen" title="Confirmer la réattribution" @close="confirmOpen = false">
      <p class="text-sm text-space-indigo/70">
        {{ selectedProposals.length }} mouvement(s) vont être retirés de la réconciliation et
        remplacés par un mouvement fantôme par cible (lot ou clé reco), pour un total de
        <Amount :value="selectedTotal" />. L'opération est journalisée mais n'a pas d'annulation
        automatique.
      </p>
      <template #footer>
        <Button variant="reveals-primary" class="sm:ml-3" @click="commit">Committer</Button>
        <Button variant="secondary" @click="confirmOpen = false">Annuler</Button>
      </template>
    </Modal>

    <Modal
      :is-open="orphanConfirmOpen"
      title="Confirmer le rattachement"
      @close="orphanConfirmOpen = false"
    >
      <p class="text-sm text-space-indigo/70">
        {{ orphanSelectedItems.length }} mouvement(s) vont être rattachés à leur cible, pour un
        total de <Amount :value="orphanSelectedTotal" />. Le mouvement n'est pas découpé : il
        prend simplement le lot (ou la clé) que sa propre référence désigne. L'opération est
        journalisée mais n'a pas d'annulation automatique.
      </p>
      <template #footer>
        <Button variant="reveals-primary" class="sm:ml-3" @click="commitOrphans">Rattacher</Button>
        <Button variant="secondary" @click="orphanConfirmOpen = false">Annuler</Button>
      </template>
    </Modal>
  </div>
</template>

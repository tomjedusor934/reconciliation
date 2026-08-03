<script setup lang="ts">
import { ref, computed, onMounted } from 'vue';
import Card from '@/components/ui/Card.vue';
import Input from '@/components/ui/Input.vue';
import Select from '@/components/ui/Select.vue';
import Checkbox from '@/components/ui/Checkbox.vue';
import Button from '@/components/ui/Button.vue';
import Badge from '@/components/ui/Badge.vue';
import Loader from '@/components/ui/Loader.vue';
import SqlExplorer from '@/components/ui/SqlExplorer.vue';
import sourceConnectionService, {
  type SourceConnection,
  type SourceConnectionInput,
} from '@/services/sourceConnectionService';
import { success, error as toastError } from '@/utils/toaster';

const isLoading = ref(false);
const isSaving = ref(false);
const connections = ref<SourceConnection[]>([]);
const editingId = ref<number | null>(null); // null = create mode
const testQuery = ref('SELECT TOP 1 * FROM std.Payment');

const typeOptions = [
  { value: 'mssql', label: 'SQL Server (MSSQL)' },
  { value: 'postgres', label: 'PostgreSQL' },
  { value: 'oracle', label: 'Oracle' },
  { value: 'folder', label: 'Folder / path' },
];

// Local form uses non-null values (Input/Checkbox v-models don't take null);
// mapped to the nullable API payload only at save time.
interface ConnForm {
  code: string;
  name: string;
  type: string;
  host: string;
  port: number | undefined;
  database: string;
  username: string;
  password: string;
  odbc_driver: string;
  encrypt: boolean;
  trust_server_certificate: boolean;
  dsn: string;
}

function emptyForm(): ConnForm {
  return {
    code: '',
    name: '',
    type: 'mssql',
    host: '',
    port: 1433,
    database: '',
    username: '',
    password: '',
    odbc_driver: 'ODBC Driver 18 for SQL Server',
    encrypt: true,
    trust_server_certificate: true,
    dsn: '',
  };
}

const form = ref<ConnForm>(emptyForm());

// Structured (host/user/…) vs. raw DSN (folder path / full URL).
const isStructured = computed(
  () => form.value.type === 'mssql' || form.value.type === 'postgres' || form.value.type === 'oracle'
);
const selectedConnection = computed(
  () => connections.value.find((c) => c.id === editingId.value) || null
);

async function fetchConnections() {
  isLoading.value = true;
  try {
    const { data } = await sourceConnectionService.getAll();
    connections.value = data;
  } catch (e: any) {
    toastError(e?.response?.data?.detail || 'Failed to load connections');
  } finally {
    isLoading.value = false;
  }
}

function startCreate() {
  editingId.value = null;
  form.value = emptyForm();
}

function startEdit(conn: SourceConnection) {
  editingId.value = conn.id;
  const extra = (conn.extra || {}) as Record<string, any>;
  form.value = {
    code: conn.code,
    name: conn.name,
    type: conn.type,
    host: extra.host ?? '',
    port: extra.port ?? undefined,
    database: extra.database ?? '',
    username: extra.username ?? '',
    password: '', // never prefilled — leave blank to keep the stored one
    odbc_driver: extra.odbc_driver ?? 'ODBC Driver 18 for SQL Server',
    encrypt: extra.encrypt ?? true,
    trust_server_certificate: extra.trust_server_certificate ?? true,
    dsn: conn.dsn ?? '',
  };
}

async function save() {
  if (!form.value.code || !form.value.name) {
    toastError('Code and name are required');
    return;
  }
  isSaving.value = true;
  try {
    const payload: SourceConnectionInput = { ...form.value };
    if (!payload.password) delete payload.password; // keep existing on update
    if (editingId.value === null) {
      const { data } = await sourceConnectionService.create(payload);
      success('Connection created');
      editingId.value = data.id;
    } else {
      const { code, ...rest } = payload; // code is immutable
      await sourceConnectionService.update(editingId.value, rest);
      success('Connection updated');
    }
    await fetchConnections();
  } catch (e: any) {
    toastError(e?.response?.data?.detail || 'Save failed');
  } finally {
    isSaving.value = false;
  }
}

onMounted(fetchConnections);
</script>

<template>
  <div class="space-y-6">
    <div>
      <h2 class="text-xl font-semibold text-space-indigo">Data Connections</h2>
      <p class="text-sm text-space-indigo/60 mt-1">
        Configure the databases the app queries directly (e.g. the Finacle datamart).
        Secrets are encrypted at rest and never displayed.
      </p>
    </div>

    <!-- Existing connections -->
    <Card title="Connections">
      <template #header>
        <div class="flex items-center justify-between">
          <h3 class="text-lg leading-6 font-medium text-space-indigo">Connections</h3>
          <Button variant="reveals-primary" size="sm" @click="startCreate">New connection</Button>
        </div>
      </template>

      <div v-if="isLoading" class="flex justify-center py-6"><Loader size="md" /></div>
      <p v-else-if="connections.length === 0" class="text-sm text-space-indigo/50 italic py-4">
        No connection configured yet.
      </p>
      <ul v-else class="divide-y divide-space-indigo/10">
        <li
          v-for="conn in connections"
          :key="conn.id"
          class="flex items-center justify-between py-3 cursor-pointer hover:bg-gray-50 px-2 -mx-2 rounded"
          :class="editingId === conn.id ? 'bg-lavender-blush' : ''"
          @click="startEdit(conn)"
        >
          <div class="min-w-0">
            <div class="flex items-center gap-2">
              <span class="font-medium text-space-indigo truncate">{{ conn.name }}</span>
              <Badge variant="info">{{ conn.type }}</Badge>
              <Badge v-if="conn.has_password" variant="success">secret set</Badge>
            </div>
            <div class="text-xs text-space-indigo/50 truncate font-mono">
              {{ conn.code }} · {{ conn.dsn || '—' }}
            </div>
          </div>
          <Button variant="ghost" size="sm" @click.stop="startEdit(conn)">Edit</Button>
        </li>
      </ul>
    </Card>

    <!-- Create / edit form -->
    <Card :title="editingId === null ? 'New connection' : 'Edit connection'">
      <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
        <Input
          v-model="form.code"
          label="Code (unique)"
          placeholder="datamart"
          :disabled="editingId !== null"
          required
        />
        <Input v-model="form.name" label="Name" placeholder="Finacle datamart" required />
        <Select v-model="form.type" label="Type" :options="typeOptions" />
        <div></div>

        <template v-if="isStructured">
          <Input v-model="form.host" label="Host" placeholder="regdmartdbp01.ept.lu" />
          <Input v-model="form.port" type="number" label="Port" placeholder="443" />
          <Input v-model="form.database" label="Database / schema" placeholder="regdmp" />
          <Input v-model="form.username" label="Username" placeholder="regdmpusr" />
          <Input
            v-model="form.password"
            type="password"
            label="Password"
            :placeholder="editingId !== null ? '•••••• (unchanged)' : ''"
          />
          <Input v-model="form.odbc_driver" label="ODBC driver" placeholder="ODBC Driver 18 for SQL Server" />
          <div class="flex items-end gap-6">
            <Checkbox v-model="form.encrypt" label="Encrypt" />
            <Checkbox v-model="form.trust_server_certificate" label="Trust server cert" />
          </div>
        </template>

        <template v-else>
          <div class="md:col-span-2">
            <Input v-model="form.dsn" label="DSN / path" placeholder="/data/inbox or a full SQLAlchemy URL" />
          </div>
        </template>
      </div>

      <template #footer>
        <div class="flex items-center gap-3">
          <Button variant="reveals-primary" :disabled="isSaving" @click="save">
            <span v-if="isSaving">Saving…</span>
            <span v-else>{{ editingId === null ? 'Create' : 'Save changes' }}</span>
          </Button>
          <Button v-if="editingId !== null" variant="ghost" size="sm" @click="startCreate">Cancel</Button>
        </div>
      </template>
    </Card>

    <!-- Test query -->
    <Card title="Test query">
      <p v-if="!selectedConnection" class="text-sm text-space-indigo/50 italic">
        Select or save a connection above, then run a read-only query to verify it.
      </p>
      <template v-else>
        <p class="text-xs text-space-indigo/50 mb-3">
          Testing <span class="font-mono">{{ selectedConnection.code }}</span> — SELECT-only, always rolled back.
        </p>
        <SqlExplorer :connection-id="selectedConnection.id" v-model="testQuery" />
      </template>
    </Card>
  </div>
</template>

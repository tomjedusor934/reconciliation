<script setup lang="ts">
import { ref, computed, onMounted } from 'vue';
import Card from '@/components/ui/Card.vue';
import Input from '@/components/ui/Input.vue';
import Button from '@/components/ui/Button.vue';
import Checkbox from '@/components/ui/Checkbox.vue';
import Select from '@/components/ui/Select.vue';
import Loader from '@/components/ui/Loader.vue';
import Modal from '@/components/ui/Modal.vue';
import ssoService from '@/services/ssoService';
import roleService, { type Role } from '@/services/roleService';
import { success, error as toastError } from '@/utils/toaster';
import type {
  SSOProvider,
  SSOProviderInput,
  SSOProviderType,
  SSOPreset,
  SSOSettings,
} from '@/types';

const isLoading = ref(false);
const isSavingSettings = ref(false);
const isSavingProvider = ref(false);

const settings = ref<SSOSettings>({
  sso_enabled: false,
  sso_force: false,
  sso_create_account_on_login: false,
  sso_default_role_id: null,
});

const providers = ref<SSOProvider[]>([]);
const presets = ref<SSOPreset[]>([]);
const roles = ref<Role[]>([]);

const showProviderModal = ref(false);
const editingProviderId = ref<number | null>(null);
const providerForm = ref<SSOProviderInput>(emptyForm());

function emptyForm(): SSOProviderInput {
  return {
    name: '',
    display_name: '',
    provider_type: 'generic_oidc',
    client_id: '',
    client_secret: '',
    authorization_url: '',
    token_url: '',
    userinfo_url: '',
    jwks_url: '',
    issuer: '',
    scopes: 'openid profile email',
    tenant_id: '',
    icon: '',
    button_color: '',
    enabled: true,
    order: 0,
  };
}

const currentPreset = computed<SSOPreset | undefined>(() =>
  presets.value.find((p) => p.type === providerForm.value.provider_type),
);

const presetFieldNames = computed<string[]>(() =>
  (currentPreset.value?.fields || []).map((f) => f.name),
);

function showField(field: 'authorization_url' | 'token_url' | 'userinfo_url' | 'jwks_url' | 'issuer' | 'tenant_id'): boolean {
  if (providerForm.value.provider_type === 'generic_oidc') {
    return ['authorization_url', 'token_url', 'userinfo_url', 'jwks_url', 'issuer'].includes(field);
  }
  return presetFieldNames.value.includes(field);
}

const roleOptions = computed(() =>
  roles.value.map((r) => ({ value: r.id, label: r.name })),
);

const presetOptions = computed(() =>
  presets.value.map((p) => ({ value: p.type, label: p.label })),
);

async function fetchAll() {
  isLoading.value = true;
  try {
    const [settingsRes, providersRes, presetsRes, rolesRes] = await Promise.all([
      ssoService.getSettings(),
      ssoService.getProviders(),
      ssoService.getPresets(),
      roleService.getAll(),
    ]);
    settings.value = settingsRes.data;
    providers.value = providersRes.data;
    presets.value = presetsRes.data;
    roles.value = rolesRes.data;
  } catch (err) {
    console.error(err);
    toastError('Failed to load SSO configuration');
  } finally {
    isLoading.value = false;
  }
}

async function saveSettings() {
  isSavingSettings.value = true;
  try {
    if (settings.value.sso_force && !settings.value.sso_enabled) {
      // Force only makes sense when SSO is enabled
      settings.value.sso_force = false;
    }
    const { data } = await ssoService.updateSettings(settings.value);
    settings.value = data;
    success('SSO settings saved');
  } catch (err) {
    console.error(err);
    toastError('Failed to save SSO settings');
  } finally {
    isSavingSettings.value = false;
  }
}

function openCreateModal() {
  editingProviderId.value = null;
  providerForm.value = emptyForm();
  showProviderModal.value = true;
}

function openEditModal(provider: SSOProvider) {
  editingProviderId.value = provider.id;
  providerForm.value = {
    name: provider.name,
    display_name: provider.display_name,
    provider_type: provider.provider_type,
    client_id: provider.client_id,
    client_secret: '',
    authorization_url: provider.authorization_url || '',
    token_url: provider.token_url || '',
    userinfo_url: provider.userinfo_url || '',
    jwks_url: provider.jwks_url || '',
    issuer: provider.issuer || '',
    scopes: provider.scopes,
    tenant_id: provider.tenant_id || '',
    icon: provider.icon || '',
    button_color: provider.button_color || '',
    enabled: provider.enabled,
    order: provider.order,
  };
  showProviderModal.value = true;
}

function closeProviderModal() {
  showProviderModal.value = false;
  editingProviderId.value = null;
}

function applyPresetDefaults() {
  if (!currentPreset.value) return;
  if (!providerForm.value.scopes) {
    providerForm.value.scopes = currentPreset.value.default_scopes;
  }
}

async function saveProvider() {
  if (!providerForm.value.name || !providerForm.value.display_name || !providerForm.value.client_id) {
    toastError('Name, display name and client ID are required');
    return;
  }
  isSavingProvider.value = true;
  try {
    const payload: Partial<SSOProviderInput> = { ...providerForm.value };
    // Don't send empty client_secret on update (keeps the existing one)
    if (editingProviderId.value !== null && !payload.client_secret) {
      delete payload.client_secret;
    }
    if (editingProviderId.value === null) {
      await ssoService.createProvider(payload as SSOProviderInput);
      success('Provider created');
    } else {
      await ssoService.updateProvider(editingProviderId.value, payload);
      success('Provider updated');
    }
    closeProviderModal();
    const { data } = await ssoService.getProviders();
    providers.value = data;
  } catch (err: any) {
    console.error(err);
    const detail = err?.response?.data?.detail || 'Failed to save provider';
    toastError(detail);
  } finally {
    isSavingProvider.value = false;
  }
}

async function deleteProvider(provider: SSOProvider) {
  if (!confirm(`Delete provider "${provider.display_name}"? Linked SSO identities will also be removed.`)) return;
  try {
    await ssoService.deleteProvider(provider.id);
    success('Provider deleted');
    providers.value = providers.value.filter((p) => p.id !== provider.id);
  } catch (err) {
    console.error(err);
    toastError('Failed to delete provider');
  }
}

onMounted(fetchAll);
</script>

<template>
  <div class="space-y-8">
    <div class="mb-2">
      <h2 class="text-2xl font-bold text-space-indigo">Single Sign-On</h2>
      <p class="text-space-indigo/60 mt-2">Allow users to authenticate through external OIDC / OAuth2 providers.</p>
    </div>

    <Loader v-if="isLoading" />

    <template v-else>
      <!-- Global SSO settings -->
      <Card title="SSO Configuration">
        <div class="space-y-4">
          <Checkbox
            v-model="settings.sso_enabled"
            label="Enable Single Sign-On"
            theme="reveals"
          />
          <p class="text-xs text-space-indigo/50 ml-7 -mt-2">When disabled, SSO buttons are hidden from the login page.</p>

          <Checkbox
            v-model="settings.sso_force"
            label="Force SSO (disable email/password login)"
            theme="reveals"
            :disabled="!settings.sso_enabled"
          />
          <p class="text-xs text-space-indigo/50 ml-7 -mt-2">
            Once enabled, the email/password form is hidden and only SSO buttons remain. Make sure at least one provider is configured first.
          </p>

          <Checkbox
            v-model="settings.sso_create_account_on_login"
            label="Create account on first SSO login"
            theme="reveals"
          />
          <p class="text-xs text-space-indigo/50 ml-7 -mt-2">
            Auto-provisions a new local user when an unknown identity successfully authenticates via SSO.
          </p>

          <div v-if="settings.sso_create_account_on_login" class="ml-7">
            <Select
              v-model="settings.sso_default_role_id"
              label="Default role for auto-created accounts"
              :options="roleOptions"
              placeholder="No role"
              theme="reveals"
              clearable
            />
          </div>

          <div class="flex justify-end pt-4 border-t border-space-indigo/10">
            <Button :disabled="isSavingSettings" variant="reveals-primary" @click="saveSettings">
              <span v-if="isSavingSettings">Saving...</span>
              <span v-else>Save settings</span>
            </Button>
          </div>
        </div>
      </Card>

      <!-- Providers list -->
      <Card>
        <template #header>
          <div class="flex items-center justify-between">
            <h3 class="text-lg font-semibold text-space-indigo">Providers</h3>
            <Button variant="reveals-primary" size="sm" @click="openCreateModal">+ Add provider</Button>
          </div>
        </template>

        <div v-if="providers.length === 0" class="text-sm text-space-indigo/60 py-6 text-center">
          No SSO provider configured yet. Add one to let users sign in through Google, Azure AD, GitHub, Okta or any OIDC provider.
        </div>

        <table v-else class="w-full text-sm">
          <thead class="text-left text-xs text-space-indigo/60 uppercase border-b border-space-indigo/10">
            <tr>
              <th class="py-2 pr-4">Order</th>
              <th class="py-2 pr-4">Name</th>
              <th class="py-2 pr-4">Display Name</th>
              <th class="py-2 pr-4">Type</th>
              <th class="py-2 pr-4">Status</th>
              <th class="py-2 pr-4 text-right">Actions</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="provider in providers"
              :key="provider.id"
              class="border-b border-space-indigo/5 last:border-0"
            >
              <td class="py-2 pr-4 text-space-indigo/70">{{ provider.order }}</td>
              <td class="py-2 pr-4 font-mono text-space-indigo">{{ provider.name }}</td>
              <td class="py-2 pr-4 text-space-indigo">{{ provider.display_name }}</td>
              <td class="py-2 pr-4 text-space-indigo/70">{{ provider.provider_type }}</td>
              <td class="py-2 pr-4">
                <span
                  :class="provider.enabled
                    ? 'inline-block px-2 py-0.5 rounded text-xs bg-tropical-mint/20 text-space-indigo'
                    : 'inline-block px-2 py-0.5 rounded text-xs bg-gray-200 text-gray-600'"
                >
                  {{ provider.enabled ? 'Enabled' : 'Disabled' }}
                </span>
              </td>
              <td class="py-2 pr-0 text-right whitespace-nowrap">
                <Button variant="ghost" size="sm" @click="openEditModal(provider)">Edit</Button>
                <Button variant="danger" size="sm" @click="deleteProvider(provider)">Delete</Button>
              </td>
            </tr>
          </tbody>
        </table>
      </Card>
    </template>

    <!-- Provider create/edit modal -->
    <Modal :is-open="showProviderModal" :title="editingProviderId === null ? 'Add SSO provider' : 'Edit SSO provider'" @close="closeProviderModal">
      <div class="space-y-4 max-h-[70vh] overflow-y-auto pr-2">
        <Select
          v-model="providerForm.provider_type"
          label="Provider type"
          :options="presetOptions"
          theme="reveals"
          @update:modelValue="applyPresetDefaults"
        />

        <Input
          v-model="providerForm.name"
          label="Identifier (slug)"
          placeholder="google-corp"
          theme="reveals"
          :disabled="editingProviderId !== null"
        />
        <p class="text-xs text-space-indigo/50 -mt-3">URL-safe slug, used internally and in the OAuth callback URL.</p>

        <Input
          v-model="providerForm.display_name"
          label="Display name"
          placeholder="Google Workspace"
          theme="reveals"
        />

        <Input
          v-model="providerForm.client_id"
          label="Client ID"
          theme="reveals"
        />

        <Input
          v-model="providerForm.client_secret"
          type="password"
          label="Client secret"
          :placeholder="editingProviderId !== null ? '•••••• (leave blank to keep existing)' : ''"
          theme="reveals"
        />

        <Input
          v-if="showField('tenant_id')"
          v-model="providerForm.tenant_id"
          label="Azure tenant ID"
          placeholder="common, organizations, or a GUID"
          theme="reveals"
        />

        <Input
          v-if="showField('issuer')"
          v-model="providerForm.issuer"
          label="Issuer URL"
          placeholder="https://your-org.okta.com/oauth2/default"
          theme="reveals"
        />

        <Input
          v-if="showField('authorization_url')"
          v-model="providerForm.authorization_url"
          label="Authorization URL"
          theme="reveals"
        />
        <Input
          v-if="showField('token_url')"
          v-model="providerForm.token_url"
          label="Token URL"
          theme="reveals"
        />
        <Input
          v-if="showField('userinfo_url')"
          v-model="providerForm.userinfo_url"
          label="UserInfo URL"
          theme="reveals"
        />
        <Input
          v-if="showField('jwks_url')"
          v-model="providerForm.jwks_url"
          label="JWKS URL"
          theme="reveals"
        />

        <Input
          v-model="providerForm.scopes"
          label="Scopes"
          placeholder="openid email profile"
          theme="reveals"
        />

        <Input
          v-model="providerForm.button_color"
          label="Button border color (optional)"
          placeholder="#4285F4"
          theme="reveals"
        />

        <div>
          <label class="text-sm font-medium text-space-indigo">Icon SVG (optional)</label>
          <textarea
            v-model="providerForm.icon"
            rows="3"
            placeholder="<svg ...>...</svg>"
            class="mt-2 w-full px-3 py-2 text-sm rounded border border-space-indigo/30 focus:outline-none focus:ring-2 focus:ring-tropical-mint font-mono"
          ></textarea>
        </div>

        <div class="grid grid-cols-2 gap-4">
          <Input
            v-model.number="providerForm.order"
            type="number"
            label="Display order"
            theme="reveals"
          />
          <div class="flex items-end pb-2">
            <Checkbox v-model="providerForm.enabled" label="Enabled" theme="reveals" />
          </div>
        </div>
      </div>

      <template #footer>
        <Button variant="reveals-primary" :disabled="isSavingProvider" @click="saveProvider">
          <span v-if="isSavingProvider">Saving...</span>
          <span v-else>Save</span>
        </Button>
        <Button variant="secondary" @click="closeProviderModal">Cancel</Button>
      </template>
    </Modal>
  </div>
</template>

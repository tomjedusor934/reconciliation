<script setup lang="ts">
import { ref, onMounted } from 'vue';
import Card from '@/components/ui/Card.vue';
import Input from '@/components/ui/Input.vue';
import Button from '@/components/ui/Button.vue';
import Checkbox from '@/components/ui/Checkbox.vue';
import Loader from '@/components/ui/Loader.vue';
import settingsService from '@/services/settingsService';
import { success, error } from '@/utils/toaster';

const isLoading = ref(false);
const isSaving = ref(false);

const settings = ref({
  password_min_length: 8,
  password_require_uppercase: true,
  password_require_lowercase: true,
  password_require_numbers: true,
  password_require_special: true,
  session_timeout_minutes: 60,
  password_expiry_days: 90,
  max_login_attempts: 5
});

const originalSettings = ref({ ...settings.value });

const fetchSettings = async () => {
  isLoading.value = true;
  try {
    const { data } = await settingsService.getPasswordSettings();
    settings.value = {
      password_min_length: data.password_min_length ?? 8,
      password_require_uppercase: data.password_require_uppercase ?? true,
      password_require_lowercase: data.password_require_lowercase ?? true,
      password_require_numbers: data.password_require_numbers ?? true,
      password_require_special: data.password_require_special ?? true,
      session_timeout_minutes: data.session_timeout_minutes ?? 60,
      password_expiry_days: data.password_expiry_days ?? 90,
      max_login_attempts: data.max_login_attempts ?? 5
    };
    originalSettings.value = { ...settings.value };
  } catch (err) {
    console.error('Error loading settings', err);
    error('Unable to load security settings');
  } finally {
    isLoading.value = false;
  }
};

const handleSave = async () => {
  isSaving.value = true;
  try {
    await settingsService.updatePasswordSettings(settings.value);
    success('Security settings saved successfully');
    originalSettings.value = { ...settings.value };
  } catch (err: any) {
    error('Error saving settings');
    console.error(err);
  } finally {
    isSaving.value = false;
  }
};

const handleReset = () => {
  settings.value = { ...originalSettings.value };
};

onMounted(fetchSettings);
</script>

<template>
  <div class="space-y-8">
    <!-- SECURITY SECTION -->
    <div class="space-y-4">
      <div class="mb-6">
        <h2 class="text-2xl font-bold text-space-indigo">Security Settings</h2>
        <p class="text-space-indigo/60 mt-2">Set password and session security rules</p>
      </div>
      
      <div class="grid grid-cols-1 gap-6">
        <!-- Password Requirements Card -->
        <Card title="Password Requirements">
          <Loader v-if="isLoading" />
          <div v-else class="space-y-4">
            <Input 
              v-model.number="settings.password_min_length"
              label="Minimum Length"
              type="number"
              :min="4"
              :max="20"
              theme="reveals"
            />
            
            <div class="space-y-3">
              <Checkbox
                v-model="settings.password_require_uppercase"
                label="Require Uppercase"
                theme="reveals"
              />
              
              <Checkbox
                v-model="settings.password_require_lowercase"
                label="Require Lowercase"
                theme="reveals"
              />
              
              <Checkbox
                v-model="settings.password_require_numbers"
                label="Require Numbers"
                theme="reveals"
              />
              
              <Checkbox
                v-model="settings.password_require_special"
                label="Require Special Characters"
                theme="reveals"
              />
            </div>
          </div>
        </Card>

        <!-- Session & Expiry Card -->
        <Card title="Session and Expiration">
          <div class="space-y-4">
            <Input 
              v-model.number="settings.session_timeout_minutes"
              label="Session Duration (minutes)"
              type="number"
              :min="5"
              :max="480"
              placeholder="60"
              theme="reveals"
            />
            <p class="text-xs text-space-indigo/50">After this inactivity timeout, the user will be logged out</p>

            <Input 
              v-model.number="settings.password_expiry_days"
              label="Password Expiration (days)"
              type="number"
              :min="0"
              :max="365"
              placeholder="90"
              theme="reveals"
            />
            <p class="text-xs text-space-indigo/50">{{ settings.password_expiry_days === 0 ? "No expiration" : "Expires after " + settings.password_expiry_days + " days" }}</p>

            <Input 
              v-model.number="settings.max_login_attempts"
              label="Max Login Attempts"
              type="number"
              :min="1"
              :max="20"
              placeholder="5"
              theme="reveals"
            />
            <p class="text-xs text-space-indigo/50">Number of attempts before account lockout</p>
          </div>
        </Card>
      </div>
    </div>

    <!-- Actions -->
    <div class="flex justify-end gap-3 pt-6 border-t border-space-indigo/10">
      <Button variant="secondary" @click="handleReset">Reset</Button>
      <Button :disabled="isSaving" variant="reveals-primary" @click="handleSave">
        <span v-if="isSaving">Saving...</span>
        <span v-else>Save</span>
      </Button>
    </div>
  </div>
</template>

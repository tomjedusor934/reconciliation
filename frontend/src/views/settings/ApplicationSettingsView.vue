<script setup lang="ts">
import { ref, onMounted } from 'vue';
import Card from '@/components/ui/Card.vue';
import Input from '@/components/ui/Input.vue';
import Button from '@/components/ui/Button.vue';
import Loader from '@/components/ui/Loader.vue';
import settingsService from '@/services/settingsService';
import { success, error } from '@/utils/toaster';

const isLoading = ref(false);
const isSaving = ref(false);
const isDragging = ref(false);
const appIconSvg = ref<string | null>(null);

const settings = ref({
  app_title: 'OrchestroTemplate',
  app_icon: null as string | null,
});

const readSvgFile = async (file: File) => {
  if (!file.type.includes('svg')) {
    error('Please select an SVG file');
    return;
  }
  
  const reader = new FileReader();
  reader.onload = (e) => {
    const content = e.target?.result as string;
    appIconSvg.value = content;
    settings.value.app_icon = content;
    success('SVG loaded successfully');
  };
  reader.readAsText(file);
};

const handleDrop = (e: DragEvent) => {
  e.preventDefault();
  isDragging.value = false;
  
  const files = e.dataTransfer?.files;
  if (files && files.length > 0) {
    const file = files[0];
    readSvgFile(file);
  }
};

const handleDragOver = (e: DragEvent) => {
  e.preventDefault();
  isDragging.value = true;
};

const handleDragLeave = () => {
  isDragging.value = false;
};

const handleFileInput = (e: Event) => {
  const input = e.target as HTMLInputElement;
  if (input.files && input.files.length > 0) {
    readSvgFile(input.files[0]);
  }
};

const fetchSettings = async () => {
  isLoading.value = true;
  try {
    const { data: titleData } = await settingsService.getAppName();
    settings.value.app_title = titleData.value;
    
    // Load SVG icon if it exists
    try {
      const { data: iconData } = await settingsService.getAppIcon();
      if (iconData.value) {
        appIconSvg.value = iconData.value;
        settings.value.app_icon = iconData.value;
      }
    } catch (err) {
      console.warn('No icon configured yet', err);
    }
  } catch (err) {
    console.error('Error loading settings', err);
  } finally {
    isLoading.value = false;
  }
};

const handleSave = async () => {
  isSaving.value = true;
  try {
    // Save the application title
    await settingsService.updateAppName({ value: settings.value.app_title });
    
    // Save SVG icon if it was modified
    if (settings.value.app_icon) {
      await settingsService.updateAppIcon({ value: settings.value.app_icon });
    }
    
    success('Settings saved successfully');
    setTimeout(() => {
      window.location.reload();
    }, 1000);
  } catch (err: any) {
    error('Error saving settings');
    console.error(err);
  } finally {
    isSaving.value = false;
  }
};

const resetAppIcon = async () => {
  try {
    await settingsService.updateAppIcon({ value: '' });
    appIconSvg.value = null;
    settings.value.app_icon = null;
    success('Icon reset successfully');
    setTimeout(() => {
      window.location.reload();
    }, 1000);
  } catch (err: any) {
    error('Error resetting icon');
    console.error(err);
  }
};

onMounted(fetchSettings);
</script>

<template>
  <div class="space-y-8">
    <!-- APP SECTION -->
    <div class="space-y-4">
      <div class="mb-6">
        <h2 class="text-2xl font-bold text-space-indigo">Application Settings</h2>
        <p class="text-space-indigo/60 mt-2">Manage your application's identity and appearance</p>
      </div>
      <div class="grid grid-cols-1 gap-6">
        <!-- App Identity Card -->
        <Card title="Application Identity">
          <Loader v-if="isLoading" />
          <div v-else class="space-y-4">
            <Input 
              v-model="settings.app_title" 
              label="Application Title"
              placeholder="OrchestroTemplate"
              theme="reveals"
            />
            
            <!-- SVG Upload Zone -->
            <div>
              <label class="block text-sm font-medium text-space-indigo mb-2">Icon (SVG)</label>
              <div 
                @drop="handleDrop"
                @dragover="handleDragOver"
                @dragleave="handleDragLeave"
                :class="[
                  'relative flex flex-col items-center justify-center w-full p-6 rounded-lg border-2 border-dashed transition-colors cursor-pointer',
                  isDragging ? 'border-tropical-mint bg-tropical-mint-50' : 'border-space-indigo/20 bg-gray-50 hover:bg-gray-100'
                ]"
              >
                <input 
                  type="file" 
                  accept=".svg" 
                  class="absolute inset-0 opacity-0 cursor-pointer"
                  @change="handleFileInput"
                />
                <div v-if="!appIconSvg" class="text-center">
                  <p class="text-sm font-medium text-space-indigo/70">Drag an SVG file here</p>
                  <p class="text-xs text-space-indigo/50 mt-1">or click to select</p>
                </div>
                <div v-else class="text-center">
                  <p class="text-sm font-medium text-tropical-mint">SVG loaded</p>
                  <p class="text-xs text-space-indigo/60 mt-1">Drag a new file to replace</p>
                </div>
              </div>
              <Button 
                    size="sm" 
                    variant="secondary"
                    class="mt-3"
                    @click="resetAppIcon"
                  >
                    Reset Icon
              </Button>
            </div>
          </div>
        </Card>
      </div>
    </div>

    <!-- Actions -->
    <div class="flex justify-end gap-3 pt-6 border-t border-space-indigo/10">
      <Button variant="secondary" @click="fetchSettings">Reset</Button>
      <Button :disabled="isSaving" variant="reveals-primary" @click="handleSave">
        <span v-if="isSaving">Saving...</span>
        <span v-else>Save</span>
      </Button>
    </div>
  </div>
</template>

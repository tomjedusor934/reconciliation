<script setup lang="ts">
import { ref, onMounted } from 'vue';
import { useAuthStore } from '@/stores/auth';
import Card from '@/components/ui/Card.vue';
import Input from '@/components/ui/Input.vue';
import Button from '@/components/ui/Button.vue';
import Loader from '@/components/ui/Loader.vue';
import api from '@/api/axios';
import { success, error } from '@/utils/toaster';

const authStore = useAuthStore();
const isLoading = ref(false);
const isSaving = ref(false);
const isDragging = ref(false);
const appIconSvg = ref<string | null>(null);

const settings = ref({
  // App Settings
  app_name: 'ComPanion',
  app_icon: null as string | null,
  
  // Security Settings
  password_min_length: 8,
  password_require_uppercase: true,
  password_require_lowercase: true,
  password_require_numbers: true,
  password_require_special: true,
  session_timeout_minutes: 60,
  password_expiry_days: 90
});

const readSvgFile = async (file: File) => {
  if (!file.type.includes('svg')) {
    error('Veuillez sélectionner un fichier SVG');
    return;
  }
  
  const reader = new FileReader();
  reader.onload = (e) => {
    const content = e.target?.result as string;
    appIconSvg.value = content;
    settings.value.app_icon = content;
    success('SVG chargé avec succès');
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
    const { data } = await api.get('/admin/settings');
    settings.value = data;
  } catch (err) {
    console.info('Settings endpoint not yet implemented, using defaults');
  } finally {
    isLoading.value = false;
  }
};

const handleSave = async () => {
  isSaving.value = true;
  try {
    await api.put('/admin/settings', settings.value);
    success('Réglages sauvegardés avec succès');
  } catch (err: any) {
    error('Erreur lors de la sauvegarde des réglages');
    console.error(err);
  } finally {
    isSaving.value = false;
  }
};

onMounted(fetchSettings);
</script>

<template>
    <div class="mb-8">
    <h1 class="text-3xl font-bold text-gray-900">Réglages Globaux</h1>
    <p class="text-gray-600 mt-2">Gérez les paramètres généraux de l'application</p>
    </div>

    <Loader v-if="isLoading" />

    <div v-else class="space-y-8">
    <!-- APP SECTION -->
    <div class="space-y-4">
        <h2 class="text-xl font-semibold text-gray-900">Application</h2>
        <div class="grid grid-cols-1 gap-6">
        <!-- App Identity Card -->
        <Card title="Identité de l'Application">
            <div class="space-y-4">
            <Input 
                v-model="settings.app_name" 
                label="Nom de l'application"
                placeholder="ComPanion"
            />
            
            <!-- SVG Upload Zone -->
            <div>
                <label class="block text-sm font-medium text-gray-700 mb-2">Icône (SVG)</label>
                <div 
                @drop="handleDrop"
                @dragover="handleDragOver"
                @dragleave="handleDragLeave"
                :class="[
                    'relative flex flex-col items-center justify-center w-full p-6 rounded-lg border-2 border-dashed transition-colors cursor-pointer',
                    isDragging ? 'border-tropical-mint bg-tropical-mint-50' : 'border-gray-300 bg-gray-50 hover:bg-gray-100'
                ]"
                >
                <input 
                    type="file" 
                    accept=".svg" 
                    class="absolute inset-0 opacity-0 cursor-pointer"
                    @change="handleFileInput"
                />
                <div v-if="!appIconSvg" class="text-center">
                    <p class="text-sm font-medium text-gray-700">Glissez un fichier SVG ici</p>
                    <p class="text-xs text-gray-500 mt-1">ou cliquez pour sélectionner</p>
                </div>
                <div v-else class="text-center">
                    <p class="text-sm font-medium text-tropical-mint">SVG chargé</p>
                    <p class="text-xs text-gray-600 mt-1">Faites glisser un nouveau fichier pour remplacer</p>
                </div>
                </div>
            </div>
            </div>
        </Card>
        </div>
    </div>

    <!-- SECURITY SECTION -->
    <div class="space-y-4">
        <h2 class="text-xl font-semibold text-gray-900">Sécurité des Utilisateurs</h2>
        <div class="grid grid-cols-1 gap-6">
        <!-- Password Requirements Card -->
        <Card title="Exigences du Mot de Passe">
            <div class="space-y-4">
            <Input 
                v-model.number="settings.password_min_length"
                label="Longueur minimale"
                type="number"
                :min="4"
                :max="20"
            />
            
            <div class="space-y-3">
                <label class="flex items-center gap-3 cursor-pointer">
                <input 
                    v-model="settings.password_require_uppercase"
                    type="checkbox" 
                    class="w-4 h-4 text-tropical-mint bg-gray-100 rounded focus:ring-2 focus:ring-tropical-mint"
                />
                <span class="text-sm font-medium text-gray-700">Majuscules requises</span>
                </label>
                
                <label class="flex items-center gap-3 cursor-pointer">
                <input 
                    v-model="settings.password_require_lowercase"
                    type="checkbox" 
                    class="w-4 h-4 text-tropical-mint bg-gray-100 rounded focus:ring-2 focus:ring-tropical-mint"
                />
                <span class="text-sm font-medium text-gray-700">Minuscules requises</span>
                </label>
                
                <label class="flex items-center gap-3 cursor-pointer">
                <input 
                    v-model="settings.password_require_numbers"
                    type="checkbox" 
                    class="w-4 h-4 text-tropical-mint bg-gray-100 rounded focus:ring-2 focus:ring-tropical-mint"
                />
                <span class="text-sm font-medium text-gray-700">Chiffres requis</span>
                </label>
                
                <label class="flex items-center gap-3 cursor-pointer">
                <input 
                    v-model="settings.password_require_special"
                    type="checkbox" 
                    class="w-4 h-4 text-tropical-mint bg-gray-100 rounded focus:ring-2 focus:ring-tropical-mint"
                />
                <span class="text-sm font-medium text-gray-700">Caractères spéciaux requis</span>
                </label>
            </div>
            </div>
        </Card>

        <!-- Session & Expiry Card -->
        <Card title="Session et Expiration">
            <div class="space-y-4">
            <Input 
                v-model.number="settings.session_timeout_minutes"
                label="Durée de session (minutes)"
                type="number"
                :min="5"
                :max="480"
                placeholder="60"
            />
            <p class="text-xs text-gray-500">Après ce délai d'inactivité, l'utilisateur sera déconnecté</p>

            <Input 
                v-model.number="settings.password_expiry_days"
                label="Expiration du mot de passe (jours)"
                type="number"
                :min="0"
                :max="365"
                placeholder="90"
            />
            <p class="text-xs text-gray-500">0 = pas d'expiration</p>
            </div>
        </Card>
        </div>
    </div>

    <!-- Actions -->
    <div class="flex justify-end gap-3 pt-6 border-t border-gray-200">
        <Button variant="secondary" @click="fetchSettings">Réinitialiser</Button>
        <Button :disabled="isSaving" @click="handleSave">
        <span v-if="isSaving">Enregistrement...</span>
        <span v-else>Enregistrer les réglages</span>
        </Button>
    </div>
    </div>
</template>

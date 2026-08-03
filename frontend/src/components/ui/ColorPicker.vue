<script setup lang="ts">
import { ref, computed, watch } from 'vue'
import { useRoute } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import { cn } from '@/utils/cn'

const route = useRoute()
const authStore = useAuthStore()

const props = defineProps<{
  label?: string
  id?: string
  placeholder?: string
  error?: string
  class?: string
  theme?: 'default' | 'reveals' | 'modern' | 'modern-reveals'
  disabled?: boolean
  presets?: string[]
}>()

const model = defineModel<string | null | undefined>()

const isReadOnly = computed(() => {
  if (route.path === '/login' || route.path === '/change-password') return false
  const level = authStore.getPermissionLevel(route.path)
  return level === 'NONE'
})

const isDisabled = computed(() => props.disabled || isReadOnly.value)

// Internal color state for the native picker
const pickerColor = ref(model.value || '#000000')

watch(model, (val: string | null | undefined) => {
  if (val && /^#[0-9A-Fa-f]{6}$/.test(val)) {
    pickerColor.value = val
  }
})

function onPickerChange(event: Event) {
  const target = event.target as HTMLInputElement
  const hex = target.value.toUpperCase()
  model.value = hex
  pickerColor.value = hex
}

function onInputChange(val: string | number | undefined) {
  const str = String(val || '')
  if (/^#[0-9A-Fa-f]{6}$/.test(str)) {
    pickerColor.value = str
  }
  model.value = str || null
}

function selectPreset(color: string) {
  if (isDisabled.value) return
  model.value = color
  pickerColor.value = color
}

function clearColor() {
  if (isDisabled.value) return
  model.value = null
}

const defaultPresets = [
  '#EF4444', '#F97316', '#EAB308', '#22C55E', '#06B6D4',
  '#3B82F6', '#8B5CF6', '#EC4899', '#6B7280', '#2B2D42',
]

const colorPresets = computed(() => props.presets || defaultPresets)

const labelClasses = computed(() => {
  switch (props.theme) {
    case 'reveals':
      return 'text-sm font-medium text-space-indigo font-inter'
    case 'modern':
      return 'text-sm font-medium text-zinc-700 font-jakarta'
    case 'modern-reveals':
      return 'text-sm font-medium text-space-indigo font-jakarta'
    default:
      return 'text-sm font-medium text-space-indigo'
  }
})

const inputClasses = computed(() => {
  const base = 'w-full px-4 py-2.5 text-sm transition-all duration-200 focus:outline-none disabled:cursor-not-allowed disabled:bg-gray-100 disabled:text-space-indigo/50'
  switch (props.theme) {
    case 'reveals':
      return `${base} border rounded border-space-indigo/30 text-space-indigo placeholder:text-space-indigo/40 focus:ring-2 focus:ring-tropical-mint font-inter`
    case 'modern':
      return `${base} border rounded-xl border-zinc-300 text-zinc-900 placeholder:text-zinc-400 focus:border-zinc-500 focus:ring-4 focus:ring-zinc-500/10 font-jakarta bg-white`
    case 'modern-reveals':
      return `${base} border rounded-xl border-space-indigo/30 text-space-indigo placeholder:text-space-indigo/40 focus:border-tropical-mint focus:ring-4 focus:ring-tropical-mint/20 font-jakarta bg-white`
    default:
      return `${base} border rounded border-gray-300 focus:ring-2 focus:ring-blue-500`
  }
})
</script>

<template>
  <div :class="cn('flex flex-col gap-1', $props.class)">
    <label v-if="label" :for="id" :class="labelClasses">{{ label }}</label>

    <div class="flex items-center gap-2">
      <!-- Native color picker -->
      <div class="relative flex-shrink-0">
        <input
          type="color"
          :value="pickerColor"
          :disabled="isDisabled"
          class="w-10 h-10 rounded border border-gray-200 cursor-pointer disabled:cursor-not-allowed disabled:opacity-50 p-0.5"
          @input="onPickerChange"
        />
      </div>

      <!-- Hex input -->
      <input
        :id="id"
        :value="model || ''"
        :class="cn(inputClasses, error ? 'border-red-500' : '')"
        :placeholder="placeholder || '#FF5733'"
        :disabled="isDisabled"
        maxlength="7"
        @input="onInputChange(($event.target as HTMLInputElement).value)"
      />

      <!-- Clear button -->
      <button
        v-if="model && !isDisabled"
        type="button"
        class="flex-shrink-0 text-gray-400 hover:text-gray-600 transition-colors p-1"
        title="Effacer la couleur"
        @click="clearColor"
      >
        <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
        </svg>
      </button>
    </div>

    <!-- Color presets -->
    <div class="flex flex-wrap gap-1.5 mt-1">
      <button
        v-for="color in colorPresets"
        :key="color"
        type="button"
        :disabled="isDisabled"
        class="w-6 h-6 rounded-full border-2 transition-all duration-150 disabled:cursor-not-allowed disabled:opacity-50 hover:scale-110"
        :class="model === color ? 'border-space-indigo ring-2 ring-space-indigo/30' : 'border-gray-200 hover:border-gray-400'"
        :style="{ backgroundColor: color }"
        :title="color"
        @click="selectPreset(color)"
      />
    </div>

    <span v-if="error" class="text-xs text-red-500">{{ error }}</span>
  </div>
</template>

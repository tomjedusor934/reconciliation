<script setup lang="ts">
export interface WorkflowAuditStep {
  date: string
  label: string
  user: string
  color: 'blue' | 'green' | 'red' | 'orange'
  icon?: string
}

defineProps<{
  steps: WorkflowAuditStep[]
}>()

const dotColors: Record<string, string> = {
  blue: 'bg-blue-500 ring-blue-100',
  green: 'bg-emerald-500 ring-emerald-100',
  red: 'bg-red-500 ring-red-100',
  orange: 'bg-amber-500 ring-amber-100',
}

const lineColors: Record<string, string> = {
  blue: 'bg-blue-200',
  green: 'bg-emerald-200',
  red: 'bg-red-200',
  orange: 'bg-amber-200',
}

const textColors: Record<string, string> = {
  blue: 'text-blue-700',
  green: 'text-emerald-700',
  red: 'text-red-700',
  orange: 'text-amber-700',
}
</script>

<template>
  <div v-if="steps.length === 0" class="text-sm text-gray-400 italic py-4">
    Aucune action enregistrée pour le moment.
  </div>
  <ol v-else class="relative ml-4 py-2">
    <li
      v-for="(step, index) in steps"
      :key="index as number"
      class="relative pl-8 pb-8 last:pb-0"
    >
      <!-- Vertical line connecting dots -->
      <div
        v-if="(index as number) < steps.length - 1"
        class="absolute left-[7px] top-[18px] w-[2px] h-[calc(100%-6px)]"
        :class="lineColors[step.color]"
      />

      <!-- Dot -->
      <div
        class="absolute left-0 top-[6px] w-[16px] h-[16px] rounded-full ring-4 shadow-sm"
        :class="dotColors[step.color]"
      />

      <!-- Content -->
      <div class="min-w-0">
        <p class="text-xs font-medium text-gray-400 tabular-nums">
          {{ step.date }}
        </p>
        <p class="mt-0.5 text-sm font-semibold" :class="textColors[step.color]">
          {{ step.label }}
        </p>
        <p class="text-sm text-gray-600">
          par <span class="font-medium text-gray-800">{{ step.user }}</span>
        </p>
      </div>
    </li>
  </ol>
</template>

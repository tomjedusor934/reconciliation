<script setup lang="ts">
import { computed } from 'vue'
import { formatAmount, formatCompactAmount, isNegativeAmount } from '../../utils/formatAmount'

const props = withDefaults(defineProps<{
  value: string | number | null | undefined
  currency?: string | null
  compact?: boolean
}>(), {
  currency: 'EUR',
  compact: false,
})

const display = computed(() =>
  props.compact
    ? formatCompactAmount(props.value, props.currency)
    : formatAmount(props.value, props.currency),
)

// When compact, expose the full precise value as a native tooltip.
const fullValue = computed(() =>
  props.compact ? formatAmount(props.value, props.currency) : undefined,
)

const negative = computed(() => isNegativeAmount(props.value))
</script>

<template>
  <span
    class="tabular-nums whitespace-nowrap"
    :class="negative ? 'text-red-600' : ''"
    :title="fullValue"
  >{{ display }}</span>
</template>

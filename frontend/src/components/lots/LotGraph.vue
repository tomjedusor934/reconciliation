<script setup lang="ts">
import { computed, markRaw } from 'vue';
import { VueFlow, type NodeTypesObject } from '@vue-flow/core';
import { Background } from '@vue-flow/background';
import { Controls } from '@vue-flow/controls';
import { MiniMap } from '@vue-flow/minimap';
import '@vue-flow/core/dist/style.css';
import '@vue-flow/core/dist/theme-default.css';
import '@vue-flow/controls/dist/style.css';
import '@vue-flow/minimap/dist/style.css';
import LotMovementNode from './LotMovementNode.vue';
import LotKeyNode from './LotKeyNode.vue';
import { buildLotGraph, KEY_TYPE_COLORS } from '@/utils/lotGraph';
import type { LotKey, LotMember } from '@/types';

const props = defineProps<{
  members: LotMember[];
  keys: LotKey[];
}>();

const graph = computed(() => buildLotGraph(props.members, props.keys));

const nodeTypes = {
  movement: markRaw(LotMovementNode),
  key: markRaw(LotKeyNode),
} as unknown as NodeTypesObject;
</script>

<template>
  <div>
    <div class="h-[600px] rounded-lg border border-gray-200 bg-white overflow-hidden">
      <VueFlow
        :nodes="graph.nodes"
        :edges="graph.edges"
        :node-types="nodeTypes"
        :nodes-draggable="false"
        :nodes-connectable="false"
        :edges-updatable="false"
        :min-zoom="0.15"
        fit-view-on-init
      >
        <Background />
        <Controls :show-interactive="false" />
        <MiniMap pannable zoomable />
      </VueFlow>
    </div>
    <div class="mt-2 flex items-center gap-4 text-xs text-gray-500">
      <span class="font-medium">Paysis (SP) on the left · Finacle (NDGB) on the right · keys in between:</span>
      <span v-for="(color, type) in KEY_TYPE_COLORS" :key="type" class="inline-flex items-center gap-1">
        <span class="inline-block h-2 w-2 rounded-full" :style="{ backgroundColor: color }" />
        {{ type }}
      </span>
      <span class="inline-flex items-center gap-1">
        <span class="inline-block h-0.5 w-4 bg-gray-400 animate-pulse" />
        animated edge = pending entry
      </span>
    </div>
  </div>
</template>

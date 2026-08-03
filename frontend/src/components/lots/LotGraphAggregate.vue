<script setup lang="ts">
import { computed, markRaw } from 'vue';
import { VueFlow, type NodeMouseEvent, type NodeTypesObject } from '@vue-flow/core';
import { Background } from '@vue-flow/background';
import { Controls } from '@vue-flow/controls';
import { MiniMap } from '@vue-flow/minimap';
import '@vue-flow/core/dist/style.css';
import '@vue-flow/core/dist/theme-default.css';
import '@vue-flow/controls/dist/style.css';
import '@vue-flow/minimap/dist/style.css';
import LotGroupNode from './LotGroupNode.vue';
import LotHubKeyNode from './LotHubKeyNode.vue';
import LotPendingNode from './LotPendingNode.vue';
import { KEY_TYPE_COLORS } from '@/utils/lotGraph';
import {
  buildLotAggregateGraph,
  type GroupNodeData,
  type KeyTypeNodeData,
  type LotGraphFocus,
} from '@/utils/lotAggregateGraph';
import type { LotGraphData, LotTypeDirectionGroup } from '@/types';

const props = defineProps<{ graph: LotGraphData; focus?: LotGraphFocus | null }>();

const emit = defineEmits<{
  (e: 'select-group', group: LotTypeDirectionGroup): void;
  (e: 'open-key-type', keyType: string): void;
  (e: 'clear-focus'): void;
}>();

const built = computed(() => buildLotAggregateGraph(props.graph, props.focus));

const nodeTypes = {
  lotGroup: markRaw(LotGroupNode),
  keyType: markRaw(LotHubKeyNode),
  pendingPayments: markRaw(LotPendingNode),
} as unknown as NodeTypesObject;

const onNodeClick = ({ node }: NodeMouseEvent) => {
  if (node.type === 'lotGroup') {
    emit('select-group', (node.data as GroupNodeData).group);
  } else if (node.type === 'keyType') {
    // In focus mode the center node is the picked value — clicking it re-opens
    // the drawer for that key type.
    emit('open-key-type', (node.data as KeyTypeNodeData).keyType);
  }
};
</script>

<template>
  <div>
    <div
      class="h-[600px] rounded-lg border border-gray-200 bg-gradient-to-br from-slate-50 to-indigo-50/40 overflow-hidden"
    >
      <VueFlow
        :nodes="built.nodes"
        :edges="built.edges"
        :node-types="nodeTypes"
        :nodes-draggable="false"
        :nodes-connectable="false"
        :edges-updatable="false"
        :min-zoom="0.1"
        fit-view-on-init
        @node-click="onNodeClick"
      >
        <Background :gap="24" />
        <Controls :show-interactive="false" />
        <MiniMap pannable zoomable />
      </VueFlow>
    </div>
    <div class="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-gray-500">
      <template v-if="focus">
        <span class="font-medium text-indigo-700">
          Focused on {{ focus.keyType }}
          <span class="font-mono">{{ focus.keyValue }}</span> — showing related movement nodes only.
        </span>
        <button class="text-indigo-500 underline hover:text-indigo-700" @click="emit('clear-focus')">
          Show all
        </button>
      </template>
      <template v-else>
        <span class="font-medium">
          Mega aggregate — {{ graph.meta.member_count.toLocaleString() }} movements grouped by type &
          direction. Click a link node (PACS008 / MSGID / PO) to explore its values.
        </span>
        <span
          v-for="(color, type) in KEY_TYPE_COLORS"
          :key="type"
          class="inline-flex items-center gap-1"
        >
          <span class="inline-block h-2 w-2 rounded-full" :style="{ backgroundColor: color }" />
          {{ type }}
        </span>
      </template>
      <span class="inline-flex items-center gap-1">
        <span class="inline-block h-0.5 w-4 bg-gray-400 animate-pulse" />
        animated edge = pending movements
      </span>
    </div>
  </div>
</template>

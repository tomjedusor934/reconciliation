import type { Edge, Node } from '@vue-flow/core';
import type { LotGraphData, LotTypeDirectionGroup } from '@/types';
import { KEY_TYPE_COLORS, movementSide } from '@/utils/lotGraph';

/**
 * Pure builder for the MEGA-aggregate lot graph (VueFlow): one movement node
 * per (movement_type, direction) with summed amount, and exactly 3 link nodes
 * (PACS008 / MSGID / PO) each carrying its distinct-value count. Same bipartite
 * column layout as buildLotGraph (SP left, links center, CP right) — an 800M
 * lot with tens of thousands of keys collapses into a handful of nodes.
 *
 * When ``focus`` is set (a value picked in the drawer), only the scoped
 * movement nodes + the single value node are rendered — the "related nodes".
 */

export interface GroupNodeData {
  group: LotTypeDirectionGroup;
  side: 'SP' | 'CP';
  showPending: boolean; // show this group's pending-payment amount on its card (focus view)
}

export interface KeyTypeNodeData {
  keyType: string;
  distinctCount: number;
  memberLinkCount: number;
  keyValue?: string; // set in focus mode = the picked value
}

export interface PendingNodeData {
  amount: string; // lot-wide Σ of members carrying a PDNG payment
  count: number;
}

export type LotAggregateNode = Node<GroupNodeData | KeyTypeNodeData | PendingNodeData>;

export const PENDING_PAYMENT_COLOR = '#f97316'; // orange-500 — pending payments

export interface LotGraphFocus {
  keyType: string;
  keyValue: string;
  groups: LotTypeDirectionGroup[]; // movement nodes scoped to keyValue
}

// Tailwind classes per movement type (chip on group cards) — same hues as
// LotMovementNode so both graph modes read as one system.
export const MOVEMENT_TYPE_CLASSES: Record<string, string> = {
  SCTXB: 'bg-indigo-100 text-indigo-800',
  SDDXB: 'bg-violet-100 text-violet-800',
  SDXBB: 'bg-purple-100 text-purple-800',
  NDGB: 'bg-teal-100 text-teal-800',
  NDRJ: 'bg-rose-100 text-rose-800',
  SWIFT: 'bg-sky-100 text-sky-800',
  BKRTP: 'bg-amber-100 text-amber-800',
};

const KEY_TYPE_ORDER: Record<string, number> = { PACS008: 0, MSGID: 1, PO: 2 };
const COL_X = { SP: 0, KEY: 460, CP: 780 } as const;
const ROW_H = { GROUP: 160, KEY: 92 } as const;

export const groupNodeId = (g: LotTypeDirectionGroup) => `g-${g.movement_type}-${g.direction}`;
export const keyTypeNodeId = (keyType: string) => `k-${keyType}`;
export const PENDING_NODE_ID = 'pending-payments';

/** Edge thickness grows with the log of the member volume (1.5px → ~7px). */
export const edgeWidth = (memberCount: number) =>
  Math.min(7, 1.5 + Math.log10(Math.max(memberCount, 1)) * 1.2);

export function buildLotAggregateGraph(
  graph: LotGraphData,
  focus?: LotGraphFocus | null
): { nodes: LotAggregateNode[]; edges: Edge[] } {
  const groups = focus ? focus.groups : graph.groups;
  // In focus mode a single value node stands in for the 3 link nodes.
  const keyTypes = focus
    ? [{ key_type: focus.keyType, distinct_count: 1, member_link_count: 0 }]
    : [...graph.key_types].sort(
        (a, b) => (KEY_TYPE_ORDER[a.key_type] ?? 9) - (KEY_TYPE_ORDER[b.key_type] ?? 9)
      );

  const bySide = (side: 'SP' | 'CP') =>
    groups
      .filter((g) => movementSide(g.movement_type) === side)
      .sort(
        (a, b) =>
          b.member_count - a.member_count ||
          a.movement_type.localeCompare(b.movement_type) ||
          a.direction.localeCompare(b.direction)
      );
  const sp = bySide('SP');
  const cp = bySide('CP');

  // Vertical centering per column (same scheme as buildLotGraph).
  const heights = {
    SP: sp.length * ROW_H.GROUP,
    KEY: keyTypes.length * ROW_H.KEY,
    CP: cp.length * ROW_H.GROUP,
  };
  const maxH = Math.max(heights.SP, heights.KEY, heights.CP, 1);
  const yOf = (col: keyof typeof heights, index: number, rowH: number) =>
    (maxH - heights[col]) / 2 + index * rowH;

  const nodes: LotAggregateNode[] = [
    ...sp.map((g, i) => ({
      id: groupNodeId(g),
      type: 'lotGroup',
      position: { x: COL_X.SP, y: yOf('SP', i, ROW_H.GROUP) },
      data: { group: g, side: 'SP' as const, showPending: !!focus },
      draggable: false,
    })),
    ...keyTypes.map((k, i) => ({
      id: keyTypeNodeId(k.key_type),
      type: 'keyType',
      position: { x: COL_X.KEY, y: yOf('KEY', i, ROW_H.KEY) },
      data: {
        keyType: k.key_type,
        distinctCount: k.distinct_count,
        memberLinkCount: k.member_link_count,
        keyValue: focus?.keyValue,
      },
      draggable: false,
    })),
    ...cp.map((g, i) => ({
      id: groupNodeId(g),
      type: 'lotGroup',
      position: { x: COL_X.CP, y: yOf('CP', i, ROW_H.GROUP) },
      data: { group: g, side: 'CP' as const, showPending: !!focus },
      draggable: false,
    })),
  ];

  // Base (unfocused) view only: a single aggregate "Pending payments" node
  // carrying the lot-wide total, linked to the movement groups that contribute.
  const showPendingNode = !focus && graph.meta.pending_payment_count > 0;
  if (showPendingNode) {
    nodes.push({
      id: PENDING_NODE_ID,
      type: 'pendingPayments',
      position: { x: COL_X.KEY, y: maxH + 60 },
      data: {
        amount: graph.meta.pending_payment_amount,
        count: graph.meta.pending_payment_count,
      },
      draggable: false,
    });
  }

  const groupById = new Map(groups.map((g) => [groupNodeId(g), g]));
  const edges: Edge[] = [];

  if (focus) {
    // Every scoped movement node connects to the single value node.
    const targetId = keyTypeNodeId(focus.keyType);
    for (const g of [...sp, ...cp]) {
      const side = movementSide(g.movement_type);
      edges.push({
        id: `e-${groupNodeId(g)}`,
        source: groupNodeId(g),
        target: targetId,
        targetHandle: side === 'SP' ? 'tl' : 'tr',
        animated: g.pending_count > 0,
        style: {
          stroke: KEY_TYPE_COLORS[focus.keyType] ?? '#94a3b8',
          strokeWidth: edgeWidth(g.member_count),
          opacity: 0.85,
        },
      });
    }
  } else {
    // Lot-wide skeleton: each (type, direction, key_type) triple → an edge from
    // that movement node to its key-type node.
    for (const e of graph.edges) {
      const gid = `g-${e.movement_type}-${e.direction}`;
      const g = groupById.get(gid);
      if (!g) continue; // group not materialized — skip dangling edge
      const side = movementSide(e.movement_type);
      edges.push({
        id: `e-${gid}-${e.key_type}`,
        source: gid,
        target: keyTypeNodeId(e.key_type),
        targetHandle: side === 'SP' ? 'tl' : 'tr',
        animated: g.pending_count > 0,
        style: {
          stroke: KEY_TYPE_COLORS[e.key_type] ?? '#94a3b8',
          strokeWidth: edgeWidth(g.member_count),
          opacity: 0.8,
        },
      });
    }
  }

  if (showPendingNode) {
    for (const g of [...sp, ...cp]) {
      if (Number(g.pending_payment_amount) === 0) continue;
      const side = movementSide(g.movement_type);
      edges.push({
        id: `e-pending-${groupNodeId(g)}`,
        source: groupNodeId(g),
        target: PENDING_NODE_ID,
        targetHandle: side === 'SP' ? 'tl' : 'tr',
        animated: true,
        style: {
          stroke: PENDING_PAYMENT_COLOR,
          strokeWidth: edgeWidth(g.member_count),
          opacity: 0.85,
        },
      });
    }
  }

  return { nodes, edges };
}

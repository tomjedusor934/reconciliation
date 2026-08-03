import type { Edge, Node } from '@vue-flow/core';
import type { LotKey, LotMember } from '@/types';

/**
 * Pure builder for the bipartite lot graph (VueFlow):
 * Paysis-side movements (SP) on the left, shared keys (PACS008/MSGID/PO) as
 * small badge nodes in the middle, Finacle NDGB aggregates (CP) on the right.
 * Deterministic column layout — no external layout dependency.
 */

export interface MovementNodeData {
  member: LotMember;
  side: 'SP' | 'CP';
}

export interface KeyNodeData {
  keyType: string;
  keyValue: string;
}

export type LotGraphNode = Node<MovementNodeData | KeyNodeData>;

const SP_TYPES = ['SCTXB', 'SDDXB', 'SDXBB', 'NDRJ', 'SWIFT', 'BKRTP'];
const KEY_TYPE_ORDER: Record<string, number> = { PACS008: 0, MSGID: 1, PO: 2 };

export const KEY_TYPE_COLORS: Record<string, string> = {
  PACS008: '#6366f1', // indigo-500
  MSGID: '#14b8a6',   // teal-500
  PO: '#f59e0b',      // amber-500
};

const COL_X = { SP: 0, KEY: 430, CP: 720 } as const;
const ROW_H = { SP: 150, KEY: 64, CP: 150 } as const;

export const movementSide = (movementType: string): 'SP' | 'CP' =>
  SP_TYPES.includes(movementType) ? 'SP' : 'CP';

const keyNodeId = (keyType: string, keyValue: string) => `k-${keyType}:${keyValue}`;

export function buildLotGraph(
  members: LotMember[],
  keys: LotKey[]
): { nodes: LotGraphNode[]; edges: Edge[] } {
  const sp = members
    .filter((m) => movementSide(m.movement_type) === 'SP')
    .sort(
      (a, b) =>
        a.movement_type.localeCompare(b.movement_type) ||
        a.value_date.localeCompare(b.value_date) ||
        a.id - b.id
    );
  const cp = members
    .filter((m) => movementSide(m.movement_type) === 'CP')
    .sort((a, b) => a.value_date.localeCompare(b.value_date) || a.id - b.id);

  // Dedupe keys shared by several members into a single badge node.
  const uniqueKeys = new Map<string, KeyNodeData>();
  for (const k of keys) {
    uniqueKeys.set(keyNodeId(k.key_type, k.key_value), {
      keyType: k.key_type,
      keyValue: k.key_value,
    });
  }
  const keyNodes = [...uniqueKeys.entries()].sort(
    ([, a], [, b]) =>
      (KEY_TYPE_ORDER[a.keyType] ?? 9) - (KEY_TYPE_ORDER[b.keyType] ?? 9) ||
      a.keyValue.localeCompare(b.keyValue)
  );

  // Vertical centering per column.
  const heights = {
    SP: sp.length * ROW_H.SP,
    KEY: keyNodes.length * ROW_H.KEY,
    CP: cp.length * ROW_H.CP,
  };
  const maxH = Math.max(heights.SP, heights.KEY, heights.CP, 1);
  const yOf = (col: keyof typeof heights, index: number, rowH: number) =>
    (maxH - heights[col]) / 2 + index * rowH;

  const nodes: LotGraphNode[] = [
    ...sp.map((m, i) => ({
      id: `m-${m.id}`,
      type: 'movement',
      position: { x: COL_X.SP, y: yOf('SP', i, ROW_H.SP) },
      data: { member: m, side: 'SP' as const },
      draggable: false,
    })),
    ...keyNodes.map(([id, data], i) => ({
      id,
      type: 'key',
      position: { x: COL_X.KEY, y: yOf('KEY', i, ROW_H.KEY) },
      data,
      draggable: false,
    })),
    ...cp.map((m, i) => ({
      id: `m-${m.id}`,
      type: 'movement',
      position: { x: COL_X.CP, y: yOf('CP', i, ROW_H.CP) },
      data: { member: m, side: 'CP' as const },
      draggable: false,
    })),
  ];

  const membersById = new Map(members.map((m) => [m.id, m]));
  const edges: Edge[] = [];
  for (const k of keys) {
    const member = membersById.get(k.member_id);
    if (!member) continue; // key of a member outside this lot payload — skip
    const side = movementSide(member.movement_type);
    edges.push({
      id: `e-${k.id}`,
      source: `m-${member.id}`,
      target: keyNodeId(k.key_type, k.key_value),
      targetHandle: side === 'SP' ? 'tl' : 'tr',
      animated: member.entry_status === 'pending',
      style: { stroke: KEY_TYPE_COLORS[k.key_type] ?? '#94a3b8', strokeWidth: 1.5 },
    });
  }

  return { nodes, edges };
}

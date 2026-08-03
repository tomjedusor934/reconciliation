import { describe, expect, it } from 'vitest';
import { buildLotAggregateGraph, edgeWidth } from '@/utils/lotAggregateGraph';
import type { LotGraphData, LotGraphEdge, LotKeyTypeSummary, LotTypeDirectionGroup } from '@/types';

const keyType = (
  kt: LotKeyTypeSummary['key_type'],
  distinct: number
): LotKeyTypeSummary => ({ key_type: kt, distinct_count: distinct, member_link_count: distinct });

const group = (overrides: Partial<LotTypeDirectionGroup>): LotTypeDirectionGroup => ({
  movement_type: 'NDGB',
  direction: 'credit',
  member_count: 1,
  total_amount: '0.00',
  pending_count: 0,
  matched_count: 0,
  excluded_count: 0,
  pending_payment_amount: '0',
  ...overrides,
});

const edge = (movement_type: string, direction: string, key_type: LotGraphEdge['key_type']): LotGraphEdge => ({
  movement_type,
  direction,
  key_type,
});

const fixture = (): LotGraphData => ({
  lot_id: 'LOT1',
  key_types: [keyType('PACS008', 51994), keyType('PO', 2)],
  groups: [
    group({ movement_type: 'SCTXB', direction: 'debit', member_count: 4, total_amount: '-1000.00', pending_count: 4 }),
    group({ movement_type: 'NDGB', direction: 'credit', member_count: 51990, total_amount: '1000.00', matched_count: 51990 }),
    group({ movement_type: 'SWIFT', direction: 'credit', member_count: 10 }), // isolated (no edge)
  ],
  edges: [edge('SCTXB', 'debit', 'PACS008'), edge('NDGB', 'credit', 'PACS008')],
  meta: {
    member_count: 52004,
    type_counts: { SCTXB: 4, NDGB: 51990, SWIFT: 10 },
    pending_payment_amount: '0',
    pending_payment_count: 0,
  },
});

describe('buildLotAggregateGraph', () => {
  it('lays out three x bands: SP groups < key nodes < CP groups', () => {
    const { nodes } = buildLotAggregateGraph(fixture());
    const xs = (type: string, side?: string) =>
      nodes
        .filter((n) => n.type === type && (side ? (n.data as any).side === side : true))
        .map((n) => n.position.x);
    expect(Math.max(...xs('lotGroup', 'SP'))).toBeLessThan(Math.min(...xs('keyType')));
    expect(Math.max(...xs('keyType'))).toBeLessThan(Math.min(...xs('lotGroup', 'CP')));
  });

  it('creates one node per (type, direction) group and exactly one per key type', () => {
    const { nodes } = buildLotAggregateGraph(fixture());
    expect(nodes.filter((n) => n.type === 'lotGroup').length).toBe(3);
    expect(nodes.filter((n) => n.type === 'keyType').length).toBe(2);
  });

  it('draws one edge per triple with side-correct handles and animation', () => {
    const { edges } = buildLotAggregateGraph(fixture());
    expect(edges.length).toBe(2); // isolated SWIFT group has no edge
    const spEdge = edges.find((e) => e.source === 'g-SCTXB-debit')!;
    expect(spEdge.target).toBe('k-PACS008');
    expect(spEdge.targetHandle).toBe('tl');
    expect(spEdge.animated).toBe(true); // pending movements in the group
    const cpEdge = edges.find((e) => e.source === 'g-NDGB-credit')!;
    expect(cpEdge.targetHandle).toBe('tr');
    expect(cpEdge.animated).toBe(false);
  });

  it('scales edge width with volume, capped', () => {
    const { edges } = buildLotAggregateGraph(fixture());
    const widths = edges.map((e) => Number((e.style as any).strokeWidth));
    expect(Math.max(...widths)).toBeGreaterThan(Math.min(...widths));
    expect(edgeWidth(1)).toBeCloseTo(1.5);
    expect(edgeWidth(52000)).toBeLessThanOrEqual(7);
  });

  it('skips edges whose group is not materialized', () => {
    const graph = fixture();
    graph.edges = [...graph.edges, edge('XXX', 'debit', 'MSGID')];
    const { edges } = buildLotAggregateGraph(graph);
    expect(edges.every((e) => e.source !== 'g-XXX-debit')).toBe(true);
  });

  it('focus mode shows only the scoped groups + a single value node', () => {
    const graph = fixture();
    const scoped = [
      group({ movement_type: 'SCTXB', direction: 'debit', member_count: 1, total_amount: '-250.00', pending_count: 1 }),
      group({ movement_type: 'NDGB', direction: 'credit', member_count: 1, total_amount: '250.00' }),
    ];
    const { nodes, edges } = buildLotAggregateGraph(graph, {
      keyType: 'PACS008',
      keyValue: 'PACS1',
      groups: scoped,
    });
    expect(nodes.filter((n) => n.type === 'keyType').length).toBe(1);
    expect(nodes.filter((n) => n.type === 'lotGroup').length).toBe(2);
    expect((nodes.find((n) => n.type === 'keyType')!.data as any).keyValue).toBe('PACS1');
    expect(edges.every((e) => e.target === 'k-PACS008')).toBe(true);
  });

  it('is stable: same input produces identical node ordering', () => {
    const a = buildLotAggregateGraph(fixture());
    const b = buildLotAggregateGraph(fixture());
    expect(a.nodes.map((n) => n.id)).toEqual(b.nodes.map((n) => n.id));
  });

  it('adds no pending-payments node when the lot has none', () => {
    const { nodes } = buildLotAggregateGraph(fixture()); // meta pending count = 0
    expect(nodes.some((n) => n.type === 'pendingPayments')).toBe(false);
    expect(nodes.every((n) => (n.data as any).showPending !== true)).toBe(true);
  });

  it('base view: pending node carries the lot total and links contributing groups', () => {
    const graph = fixture();
    graph.groups = [
      group({ movement_type: 'SCTXB', direction: 'debit', member_count: 4, total_amount: '-1000', pending_payment_amount: '-250' }),
      group({ movement_type: 'NDGB', direction: 'credit', member_count: 3, total_amount: '1000' }), // no pending
    ];
    graph.edges = [];
    graph.meta.pending_payment_amount = '-250';
    graph.meta.pending_payment_count = 2;
    const { nodes, edges } = buildLotAggregateGraph(graph);
    const pending = nodes.find((n) => n.type === 'pendingPayments')!;
    expect((pending.data as any).amount).toBe('-250');
    expect((pending.data as any).count).toBe(2);
    // only the group with a non-zero pending amount links to it
    const pendingEdges = edges.filter((e) => e.target === 'pending-payments');
    expect(pendingEdges.map((e) => e.source)).toEqual(['g-SCTXB-debit']);
  });

  it('focus view: no pending node, group cards flagged showPending', () => {
    const graph = fixture();
    graph.meta.pending_payment_count = 5; // would show in base, must NOT in focus
    const { nodes } = buildLotAggregateGraph(graph, {
      keyType: 'PACS008',
      keyValue: 'PACS1',
      groups: [group({ movement_type: 'SCTXB', direction: 'debit', pending_payment_amount: '-10' })],
    });
    expect(nodes.some((n) => n.type === 'pendingPayments')).toBe(false);
    expect(nodes.filter((n) => n.type === 'lotGroup').every((n) => (n.data as any).showPending === true)).toBe(true);
  });
});

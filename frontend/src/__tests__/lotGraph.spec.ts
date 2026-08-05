import { describe, expect, it } from 'vitest';
import { buildLotGraph, movementSide } from '@/utils/lotGraph';
import type { LotKey, LotMember } from '@/types';

let nextId = 1;
const member = (overrides: Partial<LotMember>): LotMember => ({
  id: nextId++,
  source_hash: `hash-${nextId}`,
  movement_type: 'SCTXB',
  external_ref: `S${nextId}`,
  account: '0010130015001',
  currency: 'EUR',
  amount: '-100.00',
  direction: 'debit',
  value_date: '2026-07-01T00:00:00Z',
  operation_date: null,
  transaction_particulars: null,
  ref_no: null,
  remarks_1: null,
  entry_status: 'pending',
  entry_id: null,
  match_group_id: null,
  ...overrides,
});

const key = (memberId: number, keyType: LotKey['key_type'], keyValue: string): LotKey => ({
  id: nextId++,
  member_id: memberId,
  key_type: keyType,
  key_value: keyValue,
});

const fixture = () => {
  const sp1 = member({ movement_type: 'SCTXB', amount: '-100.00' });
  const sp2 = member({ movement_type: 'SDDXB', amount: '-90.00' });
  const swift = member({ movement_type: 'SWIFT', amount: '-10.00' });
  const ndgb1 = member({ movement_type: 'NDGB', amount: '60.00', entry_status: 'matched' });
  const ndgb2 = member({ movement_type: 'NDGB', amount: '140.00' });
  const members = [sp1, sp2, swift, ndgb1, ndgb2];
  const keys = [
    key(sp1.id, 'PACS008', 'PACS1'),
    key(sp2.id, 'PACS008', 'PACS2'),
    key(ndgb1.id, 'MSGID', 'AGG1'),
    key(ndgb1.id, 'PACS008', 'PACS1'),
    key(ndgb2.id, 'MSGID', 'AGG2'),
    key(ndgb2.id, 'PACS008', 'PACS1'),
    key(ndgb2.id, 'PACS008', 'PACS2'),
    key(swift.id, 'PO', 'R1'),
    key(ndgb2.id, 'PO', 'R1'),
  ];
  return { members, keys, sp1, sp2, swift, ndgb1, ndgb2 };
};

describe('movementSide', () => {
  it('routes SP types left and NDGB (or unknown aggregates) right', () => {
    for (const t of ['SCTXB', 'SDDXB', 'SDXBB', 'NDRJ', 'SWIFT', 'BKRTP']) {
      expect(movementSide(t)).toBe('SP');
    }
    expect(movementSide('NDGB')).toBe('CP');
  });
});

describe('buildLotGraph', () => {
  it('lays out three x bands: SP < keys < CP', () => {
    const { members, keys } = fixture();
    const { nodes } = buildLotGraph(members, keys);
    const xs = (type: string, side?: string) =>
      nodes
        .filter((n) => n.type === type && (side ? (n.data as any).side === side : true))
        .map((n) => n.position.x);
    const spXs = xs('movement', 'SP');
    const cpXs = xs('movement', 'CP');
    const keyXs = xs('key');
    expect(spXs.length).toBe(3);
    expect(cpXs.length).toBe(2);
    expect(Math.max(...spXs)).toBeLessThan(Math.min(...keyXs));
    expect(Math.max(...keyXs)).toBeLessThan(Math.min(...cpXs));
  });

  it('dedupes shared keys into a single node', () => {
    const { members, keys } = fixture();
    const { nodes } = buildLotGraph(members, keys);
    const keyNodes = nodes.filter((n) => n.type === 'key');
    // PACS1, PACS2, AGG1, AGG2, R1 → 5 unique badges (PACS1/PACS2/R1 are shared)
    expect(keyNodes.length).toBe(5);
    expect(new Set(keyNodes.map((n) => n.id)).size).toBe(5);
  });

  it('draws one edge per key row with side-correct target handles', () => {
    const { members, keys, sp1, ndgb1 } = fixture();
    const { edges } = buildLotGraph(members, keys);
    expect(edges.length).toBe(keys.length);
    const spEdge = edges.find((e) => e.source === `m-${sp1.id}`)!;
    expect(spEdge.target).toBe('k-PACS008:PACS1');
    expect(spEdge.targetHandle).toBe('tl');
    const cpEdge = edges.find(
      (e) => e.source === `m-${ndgb1.id}` && e.target === 'k-PACS008:PACS1'
    )!;
    expect(cpEdge.targetHandle).toBe('tr');
  });

  it('animates edges of pending members only', () => {
    const { members, keys, ndgb1, sp1 } = fixture();
    const { edges } = buildLotGraph(members, keys);
    expect(edges.find((e) => e.source === `m-${sp1.id}`)!.animated).toBe(true);
    expect(edges.find((e) => e.source === `m-${ndgb1.id}`)!.animated).toBe(false);
  });

  it('centers the shorter columns vertically', () => {
    const { members, keys } = fixture();
    const { nodes } = buildLotGraph(members, keys);
    const ys = (type: string, side?: string) =>
      nodes
        .filter((n) => n.type === type && (side ? (n.data as any).side === side : true))
        .map((n) => n.position.y);
    const mid = (arr: number[]) => (Math.min(...arr) + Math.max(...arr)) / 2;
    // CP column (2 nodes) is shorter than SP (3 nodes): both roughly share a midline
    expect(Math.abs(mid(ys('movement', 'SP')) - mid(ys('movement', 'CP')))).toBeLessThan(80);
  });

  it('is stable: same input produces identical node ordering', () => {
    const { members, keys } = fixture();
    const a = buildLotGraph([...members].reverse(), [...keys]);
    const b = buildLotGraph(members, keys);
    expect(a.nodes.map((n) => n.id)).toEqual(b.nodes.map((n) => n.id));
  });

  it('skips keys referencing unknown members', () => {
    const { members, keys } = fixture();
    const { edges } = buildLotGraph(members, [
      ...keys,
      { id: 999999, member_id: 424242, key_type: 'PO', key_value: 'GHOST' },
    ]);
    expect(edges.length).toBe(keys.length);
  });

  it('carries the split link through to the node, so ghosts can be told apart', () => {
    // A ghost is a slice of a batch movement, not a booking of its own; the node
    // renders it dashed and offers the way back to the real movement.
    const ghost = member({
      movement_type: 'SCTXB',
      amount: '-700.00',
      split_parent_hash: 'a'.repeat(64),
      split_parent_external_ref: 'PF0051006#2',
      split_parent_amount: '-1000.00',
      payment_count: 2,
    });
    const real = member({ movement_type: 'NDGB', amount: '700.00' });
    const { nodes } = buildLotGraph([ghost, real], [key(ghost.id, 'PACS008', 'PACS1')]);

    const ghostNode = nodes.find((n) => n.id === `m-${ghost.id}`);
    const realNode = nodes.find((n) => n.id === `m-${real.id}`);
    expect((ghostNode!.data as any).member.split_parent_hash).toBe('a'.repeat(64));
    expect((ghostNode!.data as any).member.split_parent_external_ref).toBe('PF0051006#2');
    expect((ghostNode!.data as any).member.payment_count).toBe(2);
    expect((realNode!.data as any).member.split_parent_hash).toBeUndefined();
  });
});

// The ledger rules of the build spec ("Rules enforced by Atlas"), written out so
// the fixture refuses the same writes Atlas would. In a live run none of this
// code decides anything: Atlas does, and the wall shows what Atlas returned.

import { BATCH } from './scenario.js';

const BUCKETS = ['sold', 'onShelf', 'held', 'returned', 'forwarded'];
const SNAKE = {
  sold: 'sold', onShelf: 'on_shelf', held: 'held', returned: 'returned', forwarded: 'forwarded',
  unresolved: 'unresolved', shippedIn: 'shipped_in',
};

export const reportedTotal = (c) => BUCKETS.reduce((n, k) => n + (c[k] || 0), 0);

// Rule 1 (conservation) and rule 2 (removal needs evidence) on `parties`.
// Returns null when the document would be accepted.
export function validateParty(doc) {
  for (const k of [...BUCKETS, 'unresolved', 'shippedIn']) {
    if (!Number.isInteger(doc[k]) || doc[k] < 0) {
      return {
        rule: 'conservation',
        errInfo: {
          failingDocumentId: doc.id,
          details: {
            operatorName: '$jsonSchema',
            schemaRulesNotSatisfied: [{
              operatorName: 'properties',
              propertiesNotSatisfied: [{
                propertyName: SNAKE[k],
                details: [{ operatorName: 'minimum', specifiedAs: { minimum: 0 }, reason: 'comparison failed', consideredValue: doc[k] }],
              }],
            }],
          },
        },
      };
    }
  }
  if (doc.shippedIn !== reportedTotal(doc) + doc.unresolved) {
    return { rule: 'conservation', errInfo: exprInfo(doc, { $eq: ['$shipped_in', { $add: ['$sold', '$on_shelf', '$held', '$returned', '$forwarded', '$unresolved'] }] }) };
  }
  if (doc.onShelf < doc.systemOnShelf) {
    return { rule: 'till outranks phone', errInfo: exprInfo(doc, { $gte: ['$on_shelf', '$system_on_shelf'] }) };
  }
  if (doc.held > doc.heldBySystem && !(doc.batchSeen === BATCH && doc.blockTest === true)) {
    return {
      rule: 'removal needs evidence',
      errInfo: exprInfo(doc, { $or: [{ $lte: ['$held', '$held_by_system'] }, { $and: [{ $eq: ['$batch_seen', '$batch_code'] }, { $eq: ['$block_test', true] }] }] }),
    };
  }
  if (doc.sold > doc.soldByTill && doc.soldSource !== 'till') {
    return { rule: 'sales are counted', errInfo: exprInfo(doc, { $or: [{ $lte: ['$sold', '$sold_by_till'] }, { $eq: ['$sold_source', 'till'] }] }) };
  }
  return null;
}

function exprInfo(doc, expr) {
  return {
    failingDocumentId: doc.id,
    details: { operatorName: '$expr', specifiedAs: { $expr: expr }, reason: 'expression did not match', expressionResult: false },
  };
}

// The driver's error, as PyMongo prints it.
export const writeError = (errInfo) =>
  `WriteError: Document failed validation, full error: ${JSON.stringify({ index: 0, code: 121, errmsg: 'Document failed validation', errInfo })}`;

// One claim, one transaction. Either every change below is written or none is.
export function postClaim(parties, edges, p, payload, makeParty) {
  const cand = { ...p };
  let inbound = null;
  let inboundNext = null;

  if (payload.received != null) {
    inbound = edges.find((e) => e.to === p.id);
    if (inbound) {
      const corroborated = Math.min(inbound.senderQty, payload.received);
      inboundNext = {
        recipientQty: payload.received,
        corroboratedQty: corroborated,
        disputedQty: Math.abs(inbound.senderQty - payload.received),
        state: inbound.senderQty === payload.received ? 'corroborated' : 'disputed',
      };
      cand.shippedIn = corroborated;
    }
  }

  for (const k of BUCKETS) cand[k] = payload[k] ?? 0;
  for (const k of ['batchSeen', 'blockTest', 'soldSource', 'heldBySystem', 'soldByTill', 'systemOnShelf']) {
    if (payload[k] !== undefined) cand[k] = payload[k];
  }
  const reported = reportedTotal(cand);
  cand.unresolved = cand.shippedIn - reported;

  const refusal = validateParty(cand);
  if (refusal) {
    return { accepted: false, reported, shippedIn: cand.shippedIn, rule: refusal.rule, raw: writeError(refusal.errInfo) };
  }

  // Commit.
  const out = { accepted: true, reported, shippedIn: cand.shippedIn, created: null, disputed: null };
  Object.assign(p, cand);
  if (inbound) {
    Object.assign(inbound, inboundNext);
    if (inbound.state === 'disputed') {
      out.disputed = { from: inbound.from, senderQty: inbound.senderQty, recipientQty: inbound.recipientQty, qty: inbound.disputedQty };
    }
  }
  if (payload.forwardedTo && payload.forwarded > 0) {
    const norm = (s) => s.toLowerCase().replace(/[^a-z0-9]/g, '');
    let to = parties.find((x) => norm(x.name) === norm(payload.forwardedTo));
    if (!to) {
      to = makeParty(payload.forwardedTo, p);
      parties.push(to);
      out.created = to;
    }
    if (!edges.some((e) => e.from === p.id && e.to === to.id)) {
      edges.push({
        from: p.id, to: to.id, senderQty: payload.forwarded, recipientQty: null,
        corroboratedQty: 0, disputedQty: 0, state: 'awaiting_recipient',
      });
    }
  }
  settle(parties, edges);
  return out;
}

// claimed -> reconciled once nothing at the party is open and every transfer agrees.
export function settle(parties, edges) {
  for (const p of parties) {
    if (p.state !== 'claimed' && p.state !== 'reached' && p.state !== 'reconciled') continue;
    const mine = edges.filter((e) => e.from === p.id || e.to === p.id);
    const done = p.unresolved === 0 && p.onShelf === 0 && mine.every((e) => e.state === 'corroborated');
    p.state = done ? 'reconciled' : 'claimed';
  }
}

export function totals(parties, edges) {
  const t = { sold: 0, onShelf: 0, held: 0, returned: 0, unresolved: 0, edgeOpen: 0, disputed: 0, awaiting: 0, checked: 0 };
  for (const p of parties) {
    t.sold += p.sold;
    t.onShelf += p.onShelf;
    t.held += p.held;
    t.returned += p.returned;
    t.unresolved += p.unresolved;
    if (p.state === 'claimed' || p.state === 'reconciled') t.checked += 1;
  }
  for (const e of edges) {
    const gap = e.senderQty - e.corroboratedQty;
    t.edgeOpen += gap;
    if (e.state === 'disputed') t.disputed += gap;
    if (e.state === 'awaiting_recipient') t.awaiting += gap;
  }
  // units_total = sum over parties (sold + on_shelf + held + returned + unresolved)
  //             + sum over edges (sender_qty - corroborated_qty)
  t.balance = t.sold + t.onShelf + t.held + t.returned + t.unresolved + t.edgeOpen;
  t.notLocated = t.unresolved + t.edgeOpen;
  t.open = t.notLocated + t.onShelf;
  t.located = t.sold + t.onShelf + t.held + t.returned;
  t.removed = t.held + t.returned;
  return t;
}

// Rule 4: `status` can be `closed` only when `open_units` is 0.
export function tryClose(openUnits) {
  if (openUnits === 0) return { closed: true };
  return {
    closed: false,
    raw: writeError({
      failingDocumentId: 'r-1',
      details: {
        operatorName: '$expr',
        specifiedAs: { $expr: { $or: [{ $ne: ['$status', 'closed'] }, { $eq: ['$open_units', 0] }] } },
        reason: 'expression did not match',
        expressionResult: false,
      },
    }),
  };
}

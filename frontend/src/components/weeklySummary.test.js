// How /api/weekly-summary responses become card content.
//
//   npx craco test --watchAll=false --testPathPattern weeklySummary
//
// Three cases decide whether the card reads as designed:
//   - the three findings are optional, because the prompt asks for them but
//     cannot force the model to have anything to say;
//   - "no trades this week" comes back as 200 with an `error` key, so it has to
//     be a readable sentence rather than an empty card;
//   - the week range is taken from the endpoint, which is what keys the cache.
import { readResult, rangeLabel } from './weeklySummary';

const FULL = {
  week_label: '2026-W40',
  week_from: '2026-09-28',
  week_to: '2026-10-02',
  week_narrative: '  A week of two halves.  ',
  anchor_mistake: 'Sized up after the first loss.',
  weekly_edge: 'Waited for the opening range.',
  next_week_rule: 'No adds before the first green day.',
  behavioral_patterns: ['revenge'],
};

test('a full synthesis becomes a narrative plus three labelled blocks', () => {
  const r = readResult(FULL);
  expect(r.kind).toBe('summary');
  expect(r.narrative).toBe('A week of two halves.');
  expect(r.blocks.map(b => b.key)).toEqual([
    'anchor_mistake', 'weekly_edge', 'next_week_rule',
  ]);
  expect(r.blocks.map(b => b.label)).toEqual([
    'Anchor mistake', 'Weekly edge', 'Rule for next week',
  ]);
  expect(r.blocks.map(b => b.tone)).toEqual(['bad', 'good', 'rule']);
  expect(r.blocks[2].text).toBe('No adds before the first green day.');
});

test('findings the model left empty are dropped, not rendered as blank headings', () => {
  const r = readResult({
    ...FULL,
    anchor_mistake: '',
    weekly_edge: '   ',
    next_week_rule: 'Stick to the plan.',
  });
  expect(r.kind).toBe('summary');
  expect(r.blocks.map(b => b.key)).toEqual(['next_week_rule']);
});

test('a synthesis with no findings still shows its narrative', () => {
  const r = readResult({ ...FULL, anchor_mistake: '', weekly_edge: '', next_week_rule: '' });
  expect(r.kind).toBe('summary');
  expect(r.blocks).toEqual([]);
  expect(r.narrative).toBe('A week of two halves.');
});

test('a payload with nothing to say is not treated as a synthesis', () => {
  // An empty card looks like a bug; this is the shape of a truncated response.
  expect(readResult({
    week_label: '2026-W40', week_from: '2026-09-28', week_to: '2026-10-02',
    week_narrative: '   ', anchor_mistake: '', weekly_edge: '', next_week_rule: '',
  }).kind).toBe('none');
});

test('"no trades this week" is a readable refusal, with the week it looked at', () => {
  const r = readResult({
    error: 'No trades found for this week',
    week_label: '2026-W40',
    week_from: '2026-09-28',
    week_to: '2026-10-02',
  });
  expect(r.kind).toBe('refusal');
  expect(r.message).toBe('No trades found for this week');
  expect(r.blocks).toEqual([]);
  expect(r.label).toBe('2026-W40');
  expect(r.from).toBe('2026-09-28');
});

test('an error wins over a narrative in the same payload', () => {
  // Defensive: the endpoint does not do this today, but a refusal must never
  // be half-rendered as a synthesis.
  const r = readResult({ ...FULL, error: 'weekly feature is turned off' });
  expect(r.kind).toBe('refusal');
  expect(r.narrative).toBe('');
});

test('a missing body is "no result", never a crash', () => {
  for (const data of [null, undefined, {}, '']) {
    const r = readResult(data);
    expect(r.kind).toBe('none');
    expect(r.blocks).toEqual([]);
  }
});

test('a narrative with no week range still renders', () => {
  const r = readResult({ week_narrative: 'Only prose.' });
  expect(r.kind).toBe('summary');
  expect(r.from).toBeNull();
});

test('the week range comes back as prose', () => {
  expect(rangeLabel({ from: '2026-09-28', to: '2026-10-02', label: '2026-W40' }))
    .toBe('Week of Sep 28 – Oct 2');
});

test('a one-day range does not repeat the same date', () => {
  expect(rangeLabel({ from: '2026-09-28', to: '2026-09-28' })).toBe('Week of Sep 28');
  expect(rangeLabel({ from: '2026-09-28', to: null })).toBe('Week of Sep 28');
});

test('no range means no subtitle rather than a malformed one', () => {
  expect(rangeLabel({ from: null, to: null, label: '2026-W40' })).toBe('');
  expect(rangeLabel(null)).toBe('');
  // A malformed date is shown as written instead of "Invalid Date".
  expect(rangeLabel({ from: 'not-a-date' })).toBe('Week of not-a-date');
});

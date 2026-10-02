// Weekly Summary: how a response from /api/weekly-summary becomes renderable
// blocks. Kept out of DailySummary.js so it can be tested without a DOM, the
// same arrangement as diaryReview.js and aiUsage.js.
//
// Two shapes come back from that endpoint and both have to be handled:
//
//   1. A real synthesis — narrative plus three labelled findings. The model is
//      asked for all three but the prompt cannot force it to have anything to
//      say, so any of them may be an empty string. An empty heading would read
//      as a broken card, so those blocks are dropped rather than rendered.
//   2. `{error: "No trades found for this week"}` with 200 — a refusal, not a
//      failure. It still carries week_from/week_to/week_label so the card can
//      say which week it looked at.
//
// The endpoint also computes the week itself (week_label, week_from, week_to)
// from the date it is given. That is deliberately read back from the response
// instead of recalculated here: two implementations of "which week is this"
// would drift, and the backend's is the one that keys the cache.

/** The three labelled findings, in reading order. */
const BLOCKS = [
  { key: 'anchor_mistake', label: 'Anchor mistake', tone: 'bad' },
  { key: 'weekly_edge', label: 'Weekly edge', tone: 'good' },
  { key: 'next_week_rule', label: 'Rule for next week', tone: 'rule' },
];

/**
 * What kind of thing came back, and the pieces worth rendering.
 *
 * @param {object|null} data - the response body
 * @returns {object} `{kind: 'summary'|'refusal'|'none', from, to, label, narrative, blocks}`
 */
export function readResult(data) {
  const base = {
    kind: 'none',
    from: data?.week_from || null,
    to: data?.week_to || null,
    label: data?.week_label || null,
    narrative: '',
    blocks: [],
  };
  if (!data || typeof data !== 'object') return base;

  if (data.error) return { ...base, kind: 'refusal', message: data.error };

  const narrative = typeof data.week_narrative === 'string' ? data.week_narrative.trim() : '';
  const blocks = BLOCKS
    .map(({ key, label, tone }) => ({
      key,
      label,
      tone,
      text: typeof data[key] === 'string' ? data[key].trim() : '',
    }))
    .filter((b) => b.text);

  // A payload with neither a narrative nor a findings is not a synthesis —
  // treating it as one would render an empty card that looks like a bug.
  if (!narrative && !blocks.length) return base;
  return { ...base, kind: 'summary', narrative, blocks };
}

/**
 * "Week of Sep 28 – Oct 2" from the endpoint's own range, so the card says
 * which week it is about without re-deriving anything.
 *
 * @param {{from: string|null, to: string|null, label: string|null}} range
 * @returns {string}
 */
export function rangeLabel(range) {
  if (!range?.from) return '';
  const short = (iso) => {
    const d = new Date(`${iso}T12:00:00`);
    return Number.isNaN(d.getTime()) ? iso
      : d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
  };
  if (!range.to || range.to === range.from) return `Week of ${short(range.from)}`;
  return `Week of ${short(range.from)} – ${short(range.to)}`;
}

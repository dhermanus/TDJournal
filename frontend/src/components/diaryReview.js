// Pure pieces of the diary match-review UI, kept out of the component so they
// can be tested without a DOM. Diary.js is the only caller.
//
// Three things here can go wrong in ways a screenshot will not show:
//
//   1. An empty form field must reach the API as `null`, not `""` — clearing a
//      value is a real action, and an empty string fails validation instead.
//   2. FastAPI validation failures arrive as an array of objects, and React
//      refuses to render an object as a child, so the whole card throws.
//   3. Saving one extraction must replace exactly that extraction, so the card
//      can update in place without re-fetching and collapsing.

/** '' / null / undefined -> null; otherwise a number when it parses. */
export const numOr = (v) => (v === '' || v == null ? null
  : (Number.isFinite(Number(v)) ? Number(v) : v));

/** A server detail only when it is a string React can print. */
export const detailOf = (error, fallback) => {
  const d = error?.response?.data?.detail;
  return typeof d === 'string' && d ? d : fallback;
};

/** Return the analysis with entry `index` merged with `fields`; unchanged otherwise. */
export const patchAnalysis = (analysis, index, fields) => {
  if (!analysis || !Array.isArray(analysis.trade_analyses)) return analysis;
  if (!Number.isInteger(index) || index < 0 || index >= analysis.trade_analyses.length) {
    return analysis;
  }
  const tradeAnalyses = [...analysis.trade_analyses];
  tradeAnalyses[index] = { ...tradeAnalyses[index], ...fields };
  return { ...analysis, trade_analyses: tradeAnalyses };
};

// What an AI action spent, as one short line.
//
// Kept out of DailySummary.js so it can be tested without a DOM — the same
// arrangement as diaryReview.js. Three things it must get right:
//
//   1. A cache hit spent nothing. Saying "cost $0.00" would imply a request
//      happened; this returns a sentence instead.
//   2. The number is an estimate. The configured endpoint bills its own rates,
//      so the label never presents it as an invoice.
//   3. The cache fields are 0 on this deployment (the proxy strips them rather
//      than reporting them), so they are omitted rather than shown as zeros,
//      which would read as "no caching happened".

export const EMPTY = null;

/** '1,000' — thousands separators for token counts. */
export const fmtTokens = (n) =>
  (Number(n) || 0).toLocaleString('en-US');

/** '$0.03' for small numbers, '$12' above $10. */
export const fmtCost = (usd) => {
  const value = Number(usd) || 0;
  if (value >= 10) return `$${value.toFixed(0)}`;
  if (value >= 1) return `$${value.toFixed(2)}`;
  if (value > 0) return `$${value.toFixed(3)}`;
  return '$0';
};

/**
 * One line of prose for a `ai_usage` payload, or null when there is nothing
 * useful to say.
 *
 * @param {object|null} usage - the ai_usage block from an AI endpoint
 * @returns {string|null}
 */
export const usageLine = (usage) => {
  if (!usage || typeof usage !== 'object') return null;

  if (usage.spent === false) {
    const reason = usage.reason;
    if (reason === 'cached') return 'Served from the saved review — no request sent.';
    if (reason) return `No request sent: ${reason}.`;
    return 'No request sent.';
  }

  const parts = [
    `${fmtTokens(usage.input_tokens)} in, ${fmtTokens(usage.output_tokens)} out`,
  ];

  const cached = (usage.cache_read_input_tokens || 0) + (usage.cache_creation_input_tokens || 0);
  if (cached > 0) parts.push(`${fmtTokens(cached)} cached`);

  parts.push(`~${fmtCost(usage.estimated_cost_usd)}`);
  if (usage.model) parts.push(usage.model);

  return `Est. cost: ${parts.join(' · ')}`;
};

// Cost line shown under Day Review and Brain.
//
// The endpoint bills its own published rate, so our number is an estimate,
// never an invoice. The proxy in front of this deployment drops both cache
// token counters; when they are absent/zero, omit the claim entirely rather
// than writing "0 cached" (which could read as "caching is off").
import { fmtTokens, fmtCost, usageLine } from './aiUsage';

describe('AI token cost line', () => {
  test('a real action labels the cost as an estimate', () => {
    expect(usageLine({
      spent: true, input_tokens: 6118, output_tokens: 400,
      estimated_cost_usd: 0.04, model: 'claude-opus-5',
      cache_read_input_tokens: 0, cache_creation_input_tokens: 0,
    })).toBe('Est. cost: 6,118 in, 400 out · ~$0.040 · claude-opus-5');
  });

  test('a cache hit says no request was sent, not a cost of zero', () => {
    expect(usageLine({ spent: false, reason: 'cached' }))
      .toBe('Served from the saved review — no request sent.');
  });

  test('a refusal says it made no request', () => {
    expect(usageLine({ spent: false, reason: 'feature off' }))
      .toBe('No request sent: feature off.');
  });

  test('missing usage renders nothing instead of lying about cost', () => {
    expect(usageLine(null)).toBeNull();
    expect(usageLine(undefined)).toBeNull();
    expect(usageLine('bad payload')).toBeNull();
  });

  test('cache tokens are shown only when the proxy actually reports them', () => {
    const line = usageLine({ spent: true, input_tokens: 100, output_tokens: 20,
      cache_read_input_tokens: 50, cache_creation_input_tokens: 10,
      estimated_cost_usd: 0.01, model: 'model-x' });
    expect(line.includes('60 cached')).toBe(true);
    const noCacheFields = usageLine({ spent: true, input_tokens: 100, output_tokens: 20,
      estimated_cost_usd: 0.01, model: 'model-x' });
    expect(noCacheFields.includes('cached')).toBe(false);
  });

  test('small costs keep three decimals, larger ones stay compact', () => {
    expect(fmtCost(0.0037)).toBe('$0.004');
    expect(fmtCost(1.234)).toBe('$1.23');
    expect(fmtCost(12.99)).toBe('$13');
    expect(fmtTokens(1000000)).toBe('1,000,000');
  });
});

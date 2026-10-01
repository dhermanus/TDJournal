// The pure helpers behind the diary match-review card.
//
// Diary.js is a component and this sandbox has no DOM test runner, so the three
// pieces that carry real failure modes live in diaryReview.js and are tested
// here directly — the same arrangement tradeMetrics.js has.
import { numOr, detailOf, patchAnalysis } from './diaryReview';

describe('form values reach the API as data', () => {
  test('clearing a field sends null, not an empty string', () => {
    expect(numOr('')).toBeNull();
    expect(numOr(null)).toBeNull();
    expect(numOr(undefined)).toBeNull();
    // an empty stop is a cleared stop; "" would fail server validation
    expect(JSON.stringify({ stop_loss: numOr('') })).toBe('{"stop_loss":null}');
  });

  test('a typed number becomes a number, and junk stays distinguishable', () => {
    expect(numOr('-1.5')).toBe(-1.5);
    expect(numOr(2)).toBe(2);
    expect(numOr(0)).toBe(0, 'zero is a value, not an empty field');
    // passes through for the server to reject with a readable message
    expect(numOr('a lot')).toBe('a lot');
  });
});

describe('server failures are rendered as text', () => {
  test('a string detail is shown as-is', () => {
    expect(detailOf({ response: { data: { detail: 'Choose a trade' } } }, 'nope'))
      .toBe('Choose a trade');
  });

  test('a validation list never reaches the DOM', () => {
    // FastAPI 422: detail is an array of objects, which React cannot render as
    // a child — the fallback is what gets shown instead.
    const e = { response: { data: { detail: [{ msg: 'field required' }] } } };
    expect(detailOf(e, 'Could not save these fields.')).toBe('Could not save these fields.');
    expect(detailOf({}, 'Could not save these fields.')).toBe('Could not save these fields.');
    expect(detailOf({ response: { data: { detail: '' } } }, 'x')).toBe('x');
  });
});

describe('one extraction is patched in place', () => {
  const analysis = {
    overall_summary: 'Steady day.',
    trade_analyses: [
      { ticker: 'NVDA', r_multiple: 0.8, match_confidence: 'high' },
      { ticker: 'AMD', r_multiple: null, match_confidence: 'unmatched' },
    ],
  };

  test('the confirmed match is the one that changes', () => {
    const out = patchAnalysis(analysis, 0, { match_confidence: 'manual' });
    expect(out.trade_analyses[0].match_confidence).toBe('manual');
    expect(out.trade_analyses[1].match_confidence).toBe('unmatched');
    expect(out.trade_analyses[1].r_multiple).toBeNull();
    // the rest of the analysis survives
    expect(out.overall_summary).toBe('Steady day.');
  });

  test('other fields on the same entry are preserved', () => {
    const out = patchAnalysis(analysis, 0, { r_multiple: -1.5 });
    expect(out.trade_analyses[0].r_multiple).toBe(-1.5);
    expect(out.trade_analyses[0].ticker).toBe('NVDA');
  });

  test('an out-of-range patch leaves the analysis alone', () => {
    expect(patchAnalysis(analysis, 5, { r_multiple: 1 })).toBe(analysis);
    expect(patchAnalysis(analysis, -1, { r_multiple: 1 })).toBe(analysis);
    expect(patchAnalysis(analysis, 0.5, { r_multiple: 1 })).toBe(analysis);
    expect(patchAnalysis(analysis, 0, {})).not.toBe(analysis, 'a valid patch always returns a copy');
  });

  test('a missing analysis does not throw', () => {
    expect(patchAnalysis(null, 0, { r_multiple: 1 })).toBeNull();
    expect(patchAnalysis({ trade_analyses: 'not a list' }, 0, {})).toEqual({ trade_analyses: 'not a list' });
  });
});

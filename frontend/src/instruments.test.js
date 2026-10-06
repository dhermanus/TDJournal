// Price precision per instrument.
//
// A EURUSD entry of 1.15298 and an exit of 1.15283 both printed "$1.15" through
// `toFixed(2)` — identical numbers in two columns whose entire purpose is the
// difference between them. That is a display bug with a wrong answer, not a
// rounding preference, so the precision is asserted rather than eyeballed.
//
// `priceStep` is here for a stricter reason: `<input type="number" step="0.01">`
// makes the browser reject the whole form when the value is not a multiple of
// the step. 1.15298 is not a multiple of 0.01, so an FX trade could not be
// added through Add Trade at all.
import { priceDecimals, fmtPrice, priceStep } from './instruments';

const fx = { instrument_type: 'FX', ticker: 'EURUSD' };
const jpy = { instrument_type: 'FX', ticker: 'USDJPY' };
const stock = { instrument_type: 'STOCK', ticker: 'AAPL' };
const metal = { instrument_type: 'METAL', ticker: 'XAUUSD' };

describe('how many digits a quote carries', () => {
  test('FX is five, except the yen pair', () => {
    expect(priceDecimals('FX', 'EURUSD')).toBe(5);
    expect(priceDecimals('FX', 'GBPUSD')).toBe(5);
    expect(priceDecimals('FX', 'USDJPY')).toBe(3);
    expect(priceDecimals('FX', 'EURJPY')).toBe(3);
  });

  test('everything else keeps cents', () => {
    expect(priceDecimals('STOCK', 'AAPL')).toBe(2);
    expect(priceDecimals('INDEX', 'SPX')).toBe(2);
    expect(priceDecimals('OPTION', 'AAPL250117C00100000')).toBe(2);
    expect(priceDecimals('METAL', 'XAUUSD')).toBe(2);
    expect(priceDecimals('FUTURE', '/ES')).toBe(2);
  });

  test('a missing type falls back to cents rather than throwing', () => {
    expect(priceDecimals(null, 'EURUSD')).toBe(2);
    expect(priceDecimals(undefined, undefined)).toBe(2);
  });

  test('the pair decides for FX even when the type is written loosely', () => {
    expect(priceDecimals('fx', 'usdjpy')).toBe(3);
    expect(priceDecimals('FX', '')).toBe(5);      // unknown pair: five, not two
    expect(priceDecimals('FX', null)).toBe(5);
  });
});

describe('a price is printed at the precision it trades at', () => {
  test('two FX fills that differ below the cent stay distinguishable', () => {
    // The defect: both of these rendered "$1.15".
    expect(fmtPrice(1.15298, fx)).toBe('$1.15298');
    expect(fmtPrice(1.15283, fx)).toBe('$1.15283');
    expect(fmtPrice(1.15298, fx)).not.toBe(fmtPrice(1.15283, fx));
    // ...and rendered the old way they were the same string.
    expect((1.15298).toFixed(2)).toBe((1.15283).toFixed(2));
  });

  test('a stock still reads in cents', () => {
    expect(fmtPrice(193.42, stock)).toBe('$193.42');
    expect(fmtPrice(193.427, stock)).toBe('$193.43');
  });

  test('a yen pair reads three digits, not five', () => {
    expect(fmtPrice(148.25, jpy)).toBe('$148.250');
    expect(fmtPrice(148.253, jpy)).toBe('$148.253');
  });

  test('a value with nothing to show is a dash, never $0.00', () => {
    // $0.00 looks like a fill at zero — a price we actually have.
    expect(fmtPrice(null, fx)).toBe('—');
    expect(fmtPrice(undefined, fx)).toBe('—');
    expect(fmtPrice(NaN, fx)).toBe('—');
    expect(fmtPrice(0, fx)).toBe('$0.00000');   // a real zero is still a value
  });

  test('strings coming out of an input are accepted', () => {
    expect(fmtPrice('1.15298', fx)).toBe('$1.15298');
    expect(fmtPrice('', fx)).toBe('—');
  });
});

describe('a price input cannot reject the price it was asked for', () => {
  test('the step is the smallest unit of the quote', () => {
    expect(priceStep('FX', 'EURUSD')).toBeCloseTo(0.00001, 10);
    expect(priceStep('FX', 'USDJPY')).toBeCloseTo(0.001, 10);
    expect(priceStep('STOCK', 'AAPL')).toBeCloseTo(0.01, 10);
  });

  test('the step is exact, not a float the browser sees as 9.999e-6', () => {
    // `10 ** -5` evaluates to 0.000009999999999999999, and that literal is what
    // reaches the input's `step` attribute. `1 / 10 ** 5` is exact, and a step
    // that does not divide the price evenly is precisely the failure this field
    // exists to avoid. Strict equality, so a regression cannot pass as "close".
    expect(priceStep('FX', 'EURUSD')).toBe(0.00001);
    expect(String(priceStep('FX', 'EURUSD'))).toBe('0.00001');
    expect(priceStep('FX', 'USDJPY')).toBe(0.001);
    expect(priceStep('STOCK', 'AAPL')).toBe(0.01);
    expect(priceStep('FX', 'EURUSD') * 1e5).toBe(1);   // whole multiples round-trip
  });

  test('the step and the displayed precision cannot disagree', () => {
    // Derived from the same number, so one cannot drift from the other.
    for (const t of [fx, jpy, stock, metal]) {
      expect(Math.round(-Math.log10(priceStep(t.instrument_type, t.ticker))))
        .toBe(priceDecimals(t.instrument_type, t.ticker));
    }
  });

  test('an FX quote is a whole multiple of its own step', () => {
    // This is the browser's own constraint. The old fixed 0.01 failed it.
    const price = 1.15298;
    const step = priceStep('FX', 'EURUSD');
    expect(price / step).toBeCloseTo(Math.round(price / step), 6);
    expect(1.15298 / 0.01).not.toBeCloseTo(Math.round(1.15298 / 0.01), 6);
  });
});

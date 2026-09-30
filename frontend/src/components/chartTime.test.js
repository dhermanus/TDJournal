// Timestamp handling for the price chart.

// lightweight-charts plots and labels every timestamp with UTC getters and has
// no timezone option, so the chart's axis is whatever we put in the values.
// This file pins the two rules that keep a fill on its own candle:
//
//   1. the bar feed and the fill feed for a given source use the *same*
//      timeline (the local/UTC one, or the Alpaca/ET one), and
//   2. switching feed must shift both, never just one.
//
// A fill four hours off its candle is invisible as a crash — the markers simply
// land on the wrong bars — so these are asserted numerically rather than left
// to a screenshot.
import { toTs, execToTs, fillTs, axisLabel } from './chartTime';

const utc = (iso) => Math.floor(new Date(iso).getTime() / 1000);

describe('toTs', () => {
  test('leaves an imported (local) UTC timestamp untouched', () => {
    // The endpoint returns naive UTC with a Z suffix; that is already the
    // timeline lightweight-charts draws, so it must pass straight through.
    expect(toTs('2026-01-07T10:30:00Z', true)).toBe(utc('2026-01-07T10:30:00Z'));
  });

  test('shifts the remote feed down to ET wall-clock', () => {
    // Alpaca stamps its intraday bars in UTC; the chart keeps its historical
    // ET reading, so the value is moved back four hours.
    expect(toTs('2026-09-10T13:35:00Z', false)).toBe(utc('2026-09-10T13:35:00Z') - 4 * 3600);
    expect(toTs('2026-09-10T13:35:00Z', false)).toBe(utc('2026-09-10T09:35:00Z'));
  });

  test('the source flag is what moves the axis, not the data', () => {
    const stamp = '2026-01-07T10:30:00Z';
    expect(toTs(stamp, false) - toTs(stamp, true)).toBe(-4 * 3600);
  });
});

describe('execToTs', () => {
  test('returns null for a fill without a time', () => {
    expect(execToTs('2026-01-07', '')).toBeNull();
    expect(execToTs('2026-01-07', null)).toBeNull();
  });

  test('snaps a fill to the bucket its candle opens in', () => {
    // A fill at 10:07 sits in the 10:05 candle on a 5-minute chart; using the
    // raw second would give it a timestamp no bar owns.
    expect(execToTs('2026-01-07', '10:07:42', 5)).toBe(utc('2026-01-07T10:05:00Z'));
    expect(execToTs('2026-01-07', '10:00:00', 5)).toBe(utc('2026-01-07T10:00:00Z'));
    expect(execToTs('2026-01-07', '10:05:59', 5)).toBe(utc('2026-01-07T10:05:00Z'));
  });

  test('uses the fill’s own date, not the trade’s', () => {
    // `date` is the trade's close date; an entry from the day before has to be
    // plotted on the day it happened. A missing fill date still uses the trade
    // date as a stable fallback.
    const fill = { date: '2026-01-06', time: '10:05:00' };
    expect(fillTs(fill, '2026-01-07', 5)).toBe(utc('2026-01-06T10:05:00Z'));
    expect(fillTs({ time: '10:05:00' }, '2026-01-07', 5)).toBe(utc('2026-01-07T10:05:00Z'));
  });
});

describe('axis label', () => {
  test('names the basis of each feed', () => {
    expect(axisLabel('local')).toBe('UTC');
    expect(axisLabel('remote')).toBe('ET');
    expect(axisLabel(null)).toBe('ET');   // holding back reads as the default feed
  });

  test('the local chart both passes its bars through and says UTC', () => {
    // The label and the transform are set independently in different files;
    // this asserts they still describe the same axis.
    const stamp = '2026-01-07T10:05:00Z';
    const untouched = toTs(stamp, true) === utc(stamp);
    expect(axisLabel('local') === 'UTC').toBe(untouched);
    expect(untouched).toBe(true);
  });
});

describe('a fill lands on its own candle', () => {
  test('imported bars: marker and candle share the UTC timeline', () => {
    const bar = '2026-01-07T10:05:00Z';   // what the local endpoint returned
    const barTs = toTs(bar, true);
    const markerTs = execToTs('2026-01-07', '10:07:42', 5);
    expect(markerTs).toBe(barTs);
  });

  test('remote bars: the ET shift is applied to both sides, so they still meet', () => {
    const bar = '2026-09-10T13:35:00Z';   // Alpaca's 9:35 ET candle, in UTC
    const barTs = toTs(bar, false);
    const markerTs = execToTs('2026-09-10', '09:35:10', 5);  // stored as ET wall-clock
    expect(markerTs).toBe(barTs);
  });

  test('a local marker is four hours from where an ET-shifted one would be', () => {
    // Guards the failure this split exists to prevent: shifting markers while
    // bars pass through (or the reverse) moves every fill off its candle.
    const bar = '2026-01-07T10:05:00Z';
    const markerTs = execToTs('2026-01-07', '10:05:00', 5);
    expect(markerTs).toBe(toTs(bar, true));
    expect(markerTs).not.toBe(toTs(bar, false));
    expect(toTs(bar, false)).toBe(markerTs - 4 * 3600);
  });

  test('minute offsets stay a whole number of buckets', () => {
    expect(execToTs('2026-01-07', '10:07:00', 15)).toBe(utc('2026-01-07T10:00:00Z'));
    expect(execToTs('2026-01-07', '10:00:00', 60)).toBe(utc('2026-01-07T10:00:00Z'));
    expect(execToTs('2026-01-07', '10:59:00', 60)).toBe(utc('2026-01-07T10:00:00Z'));
    // The last minute of a day still floors backwards, never rolls into the next.
    expect(execToTs('2026-01-07', '23:59:00', 5)).toBe(utc('2026-01-07T23:55:00Z'));
  });
});

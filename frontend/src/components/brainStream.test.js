// Reading Brain's NDJSON stream, without a network.
//
// The parser is where a streaming client breaks quietly: one fetch chunk can
// hold three lines or a third of one, a body can end with no trailing newline,
// and a multi-byte character can be cut in half across chunks. None of that is
// visible in a screenshot, so it is tested here instead.
import { consumeBrainStream, bodyChunks } from './brainStream';

const enc = (s) => new TextEncoder().encode(s);

async function* chunks(list) {
  for (const c of list) yield c;
}

describe('Brain stream events', () => {
  test('deltas are concatenated in order and reported as they land', async () => {
    const seen = [];
    const out = await consumeBrainStream(
      chunks([enc('{"type":"delta","text":"Hello "}\n')]),
      { onDelta: (t) => seen.push(t) },
    );
    expect(out.text).toBe('Hello ');
    expect(seen).toEqual(['Hello ']);
    expect(out.failure).toBeNull();
  });

  test('one network chunk carrying several lines parses as several events', async () => {
    const out = await consumeBrainStream(chunks([
      enc('{"type":"delta","text":"A"}\n{"type":"delta","text":"B"}\n{"type":"delta","text":"C"}\n'),
    ]));
    expect(out.text).toBe('ABC');
  });

  test('a chunk split in the middle of a line waits for the rest', async () => {
    // The exact failure a naive parser hits: half a JSON object is not JSON.
    const out = await consumeBrainStream(chunks([
      enc('{"type":"delta","text":"Hel'),
      enc('lo there"}\n'),
    ]));
    expect(out.text).toBe('Hello there');
  });

  test('a body with no trailing newline still yields its last line', async () => {
    const out = await consumeBrainStream(chunks([enc('{"type":"delta","text":"end"}')]));
    expect(out.text).toBe('end');
  });

  test('usage and error events are surfaced, not folded into the text', async () => {
    const out = await consumeBrainStream(chunks([
      enc('{"type":"delta","text":"Half"}\n{"type":"error","status":429,"detail":"rate-limited"}\n'),
      enc('{"type":"usage","ai_usage":{"spent":true,"input_tokens":12}}\n'),
    ]));
    expect(out.text).toBe('Half');
    expect(out.failure).toEqual({ status: 429, detail: 'rate-limited' });
    expect(out.ai_usage).toEqual({ spent: true, input_tokens: 12 });
  });

  test('malformed lines are ignored rather than thrown mid-answer', async () => {
    const out = await consumeBrainStream(chunks([
      enc('this is not json\n{"type":"delta","text":"ok"}\n\n' + 'x'.repeat(3) + '\n'),
    ]));
    expect(out.text).toBe('ok');
  });

  test('a multi-byte character split across chunks arrives intact', async () => {
    const bytes = enc('{"type":"delta","text":"résumé"}\n');
    const cut = bytes.length - 6;          // inside the UTF-8 sequence
    const out = await consumeBrainStream(chunks([bytes.slice(0, cut), bytes.slice(cut)]));
    expect(out.text).toBe('résumé');
  });

  test('an empty stream resolves to an empty answer rather than hanging', async () => {
    const out = await consumeBrainStream(chunks([]));
    expect(out.text).toBe('');
    expect(out.ai_usage).toBeNull();
    expect(out.failure).toBeNull();
  });
});

describe('bodyChunks', () => {
  test('reads a fetch body through to completion', async () => {
    const sent = [];
    let i = 0;
    const fake = {
      body: {
        getReader: () => ({
          read: async () => (i++ < 2
            ? { done: false, value: enc('chunk\n') }
            : { done: true, value: undefined }),
        }),
      },
    };
    const gen = bodyChunks(fake);
    for await (const c of gen) sent.push(c.length);
    expect(sent).toEqual([6, 6]);
  });

  test('returns null when the browser cannot stream, so callers can say so', () => {
    expect(bodyChunks({ ok: true })).toBeNull();
    expect(bodyChunks({ body: {} })).toBeNull();
  });
});

// Reading Brain's newline-delimited JSON stream.
//
// Kept out of api.js so it can be tested without fetch, a network, or a DOM —
// the same arrangement as aiUsage.js and diaryReview.js.
//
// Two things here are easy to get wrong and impossible to see in a screenshot:
// one network chunk can contain several lines *or half of one*, so a partial
// line must stay buffered rather than parsed; and a buffer split in the middle
// of a multi-byte character must not produce a mojibake token. The decoder runs
// with `{ stream: true }` for exactly that reason.

/**
 * Consume the body of a Brain stream.
 *
 * @param {AsyncIterable<Uint8Array>} chunks  e.g. the reader from res.body
 * @param {object} [options]
 * @param {(text: string) => void} [options.onDelta] called per delta as it lands
 * @param {TextDecoder} [options.decoder] injectable for tests
 * @returns {Promise<{text: string, ai_usage: object|null, failure: object|null}>}
 */
export async function consumeBrainStream(chunks, { onDelta, decoder } = {}) {
  const dec = decoder || new TextDecoder();
  let buffer = '';
  let text = '';
  let usage = null;
  let failure = null;

  const handle = (raw) => {
    const line = raw.trim();
    if (!line) return;
    let event;
    try { event = JSON.parse(line); } catch (e) { return; }
    if (event.type === 'delta') {
      text += event.text;
      if (onDelta) onDelta(event.text);
    } else if (event.type === 'usage') {
      usage = event.ai_usage || null;
    } else if (event.type === 'error') {
      failure = { status: event.status, detail: event.detail };
    }
  };

  for await (const chunk of chunks) {
    buffer += dec.decode(chunk, { stream: true });
    const cut = buffer.lastIndexOf('\n');
    if (cut === -1) continue;
    for (const line of buffer.slice(0, cut).split('\n')) handle(line);
    buffer = buffer.slice(cut + 1);
  }
  // The body can end without a trailing newline; that line still counts.
  handle(buffer);

  return { text, ai_usage: usage, failure };
}

/** Pull the readable side of a fetch response, or null where unavailable. */
export function bodyChunks(res) {
  return res.body && typeof res.body.getReader === 'function'
    ? async function* () {
        const reader = res.body.getReader();
        for (;;) {
          const { done, value } = await reader.read();
          if (done) return;
          yield value;
        }
      }()
    : null;
}

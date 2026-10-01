import { useState, useRef, useEffect } from 'react';
import { X, Send, Brain as BrainIcon } from 'lucide-react';
import { brainApi } from '../api';
import { usageLine } from './aiUsage';

// ── Markdown renderer ─────────────────────────────────────────────────────────

function parseLine(text) {
  const parts = [];
  let remaining = text;
  let key = 0;
  while (remaining.length) {
    const bold = remaining.match(/^([\s\S]*?)\*\*([\s\S]*?)\*\*([\s\S]*)$/);
    const code = remaining.match(/^([\s\S]*?)`([^`]+)`([\s\S]*)$/);
    if (bold && (!code || bold[1].length <= code[1].length)) {
      if (bold[1]) parts.push(bold[1]);
      parts.push(<strong key={key++}>{bold[2]}</strong>);
      remaining = bold[3];
    } else if (code) {
      if (code[1]) parts.push(code[1]);
      parts.push(
        <code key={key++} style={{ background: 'var(--surface-control)', padding: '1px 5px', borderRadius: 3, fontFamily: 'var(--font-mono)', fontSize: 12 }}>
          {code[2]}
        </code>
      );
      remaining = code[3];
    } else {
      parts.push(remaining);
      break;
    }
  }
  return parts.length === 1 && typeof parts[0] === 'string' ? parts[0] : parts;
}

function Markdown({ text }) {
  return (
    <div style={{ fontSize: 14, lineHeight: 1.6 }}>
      {(text || '').split('\n').map((line, i) => {
        if (line.startsWith('### ')) return <div key={i} style={{ fontWeight: 700, marginTop: 10, marginBottom: 3 }}>{parseLine(line.slice(4))}</div>;
        if (line.startsWith('## ')) return <div key={i} style={{ fontWeight: 700, fontSize: 14, marginTop: 12, marginBottom: 4, color: 'var(--purple)' }}>{parseLine(line.slice(3))}</div>;
        if (line.startsWith('# ')) return <div key={i} style={{ fontWeight: 800, fontSize: 15, marginTop: 12, marginBottom: 4, color: 'var(--purple)' }}>{parseLine(line.slice(2))}</div>;
        if (line.startsWith('- ') || line.startsWith('* ')) return <div key={i} style={{ paddingLeft: 12, marginTop: 2 }}>• {parseLine(line.slice(2))}</div>;
        if (/^\d+\. /.test(line)) {
          const num = line.match(/^\d+/)[0];
          return <div key={i} style={{ paddingLeft: 12, marginTop: 2 }}>{num}. {parseLine(line.replace(/^\d+\. /, ''))}</div>;
        }
        if (line === '') return <div key={i} style={{ height: 6 }} />;
        return <div key={i} style={{ marginTop: 2 }}>{parseLine(line)}</div>;
      })}
    </div>
  );
}

// ── Suggested prompts ─────────────────────────────────────────────────────────

const SUGGESTIONS = [
  'What is my best performing strategy?',
  'Show my win rate by ticker',
  'Where am I losing the most money?',
  'How is my risk management?',
];

// ── Brain component ───────────────────────────────────────────────────────────

export default function Brain({ accountId, open: openProp, onOpenChange }) {
  // Controlled by the app header when it passes `open`; falls back to its own state.
  const [openLocal, setOpenLocal] = useState(false);
  const open = openProp ?? openLocal;
  const setOpen = (v) => { if (onOpenChange) onOpenChange(v); else setOpenLocal(v); };
  const [messages, setMessages] = useState([]);
  const [input, setInput] = useState('');
  const [loading, setLoading] = useState(false);
  // What the most recent answer cost, shown under the composer: a per-turn
  // figure rather than a running total, so it always describes what is on screen.
  const [lastUsage, setLastUsage] = useState(null);
  const bottomRef = useRef(null);
  const inputRef = useRef(null);
  const launcherRef = useRef(null);
  const wasOpen = useRef(false);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages, loading]);

  useEffect(() => {
    if (open) inputRef.current?.focus();
    else if (wasOpen.current) launcherRef.current?.focus();
    wasOpen.current = open;
  }, [open]);

  const send = async (text) => {
    const msg = (text || input).trim();
    if (!msg || loading) return;
    setInput('');

    const userMsg = { role: 'user', content: msg };
    const nextMessages = [...messages, userMsg];
    setMessages(nextMessages);
    setLoading(true);

    try {
      const res = await brainApi.chat(nextMessages, accountId);
      setMessages(prev => [...prev, { role: 'assistant', content: res.data.response }]);
      setLastUsage(res.data.ai_usage || null);
    } catch (e) {
      setMessages(prev => [...prev, { role: 'assistant', content: `Error: ${e.response?.data?.detail || e.message}` }]);
    } finally {
      setLoading(false);
    }
  };

  return (
    <>
      {open ? (
        <div
          className="brain-panel"
          role="dialog"
          aria-label="Brain, AI trading coach"
          onKeyDown={e => { if (e.key === 'Escape') setOpen(false); }}
        >
          {/* Header */}
          <div style={{ padding: '14px 16px', borderBottom: '1px solid var(--divider)', display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
              <BrainIcon size={20} className="text-purple" aria-hidden="true" />
              <div>
                <div className="section-title" style={{ fontSize: 16 }}>Brain</div>
                <div style={{ fontSize: 12, color: 'var(--text-secondary)' }}>AI Trading Coach</div>
              </div>
            </div>
            <button type="button" className="btn btn-ghost btn-icon" onClick={() => setOpen(false)} aria-label="Close Brain">
              <X size={16} />
            </button>
          </div>

          {/* Messages */}
          <div style={{ flex: 1, overflowY: 'auto', padding: '14px 16px', display: 'flex', flexDirection: 'column', gap: 10 }} aria-live="polite">
            {messages.length === 0 && (
              <div>
                <div style={{ color: 'var(--text-secondary)', fontSize: 14, marginBottom: 12, lineHeight: 1.5 }}>
                  Hi! I'm Brain, your AI trading coach. Ask me anything about your performance, patterns, or strategy.
                </div>
                <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                  {SUGGESTIONS.map((s, i) => (
                    <button key={i} type="button" className="brain-suggestion" onClick={() => send(s)}>
                      {s}
                    </button>
                  ))}
                </div>
              </div>
            )}

            {messages.map((msg, i) => (
              <div key={i} style={{ display: 'flex', flexDirection: 'column', alignItems: msg.role === 'user' ? 'flex-end' : 'flex-start' }}>
                <div style={{
                  maxWidth: '88%',
                  padding: '9px 12px',
                  borderRadius: msg.role === 'user' ? '10px 10px 3px 10px' : '10px 10px 10px 3px',
                  background: msg.role === 'user' ? 'var(--surface-selected)' : 'var(--surface-inset)',
                  border: '1px solid var(--divider-soft)',
                  color: 'var(--text-primary)',
                }}>
                  {msg.role === 'assistant' ? <Markdown text={msg.content} /> : <div style={{ fontSize: 14 }}>{msg.content}</div>}
                </div>
              </div>
            ))}

            {loading && (
              <div style={{ alignSelf: 'flex-start', padding: '10px 14px', background: 'var(--surface-inset)', border: '1px solid var(--divider-soft)', borderRadius: '10px 10px 10px 3px', display: 'flex', gap: 4, alignItems: 'center' }} aria-label="Brain is thinking">
                {[0, 1, 2].map(j => (
                  <div key={j} style={{ width: 6, height: 6, borderRadius: '50%', background: 'var(--accent-line)', animation: `pulse 1s ${j * 0.2}s infinite` }} />
                ))}
              </div>
            )}
            <div ref={bottomRef} />
          </div>

          {/* Input */}
          {usageLine(lastUsage) && (
            <div style={{
              padding: '8px 14px 0',
              color: 'var(--text-secondary)',
              fontSize: 11.5,
            }} role="status" aria-label="AI token usage">
              {usageLine(lastUsage)}
            </div>
          )}
          <div style={{ padding: '10px 14px 14px', borderTop: '1px solid var(--divider)', display: 'flex', gap: 8 }}>
            <input
              ref={inputRef}
              value={input}
              onChange={e => setInput(e.target.value)}
              onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); } }}
              placeholder="Ask Brain anything..."
              aria-label="Message Brain"
              disabled={loading}
              style={{ flex: 1, fontSize: 14 }}
            />
            <button
              type="button"
              className="btn btn-primary btn-icon"
              onClick={() => send()}
              disabled={loading || !input.trim()}
              aria-label="Send message"
            >
              <Send size={15} />
            </button>
          </div>
        </div>
      ) : (
        <button
          ref={launcherRef}
          type="button"
          className="brain-launcher"
          onClick={() => setOpen(true)}
          title="Open Brain, AI Trading Coach"
        >
          <BrainIcon size={18} className="text-purple" aria-hidden="true" />
          Brain
        </button>
      )}
    </>
  );
}

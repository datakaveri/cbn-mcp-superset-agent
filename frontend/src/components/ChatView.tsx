import { useEffect, useRef, useState } from 'react';
import { fetchSuggestions } from '../api';
import { LOGO_URL } from '../config';
import { usePipeline } from '../hooks/usePipeline';
import { EXAMPLE_QUERIES } from '../types';
import { Composer } from './Composer';
import { Message } from './Message';

export function ChatView() {
  const { messages, running, run } = usePipeline();
  const endRef = useRef<HTMLDivElement>(null);
  const [starters, setStarters] = useState<string[]>(EXAMPLE_QUERIES);

  // Dataset-grounded starter suggestions (fall back to the static defaults).
  useEffect(() => {
    let cancelled = false;
    fetchSuggestions()
      .then((s) => {
        if (!cancelled && s.length) setStarters(s);
      })
      .catch(() => {/* keep defaults */});
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages]);

  const empty = messages.length === 0;

  return (
    <div className="chat">
      <div className="chat-scroll">
        {empty ? (
          <div className="welcome">
            <img className="welcome-logo" src={LOGO_URL} alt="CBN" />
            <h1>What would you like to see?</h1>
            <p>Describe a chart or dashboard in plain language — I’ll build it and show it inline.</p>
            <div className="welcome-examples">
              {starters.map((q) => (
                <button key={q} className="ex-chip" onClick={() => run(q)}>
                  {q}
                </button>
              ))}
            </div>
          </div>
        ) : (
          <div className="messages">
            {messages.map((m, i) => (
              <Message
                key={m.id}
                m={m}
                isLast={i === messages.length - 1}
                onSuggest={run}
              />
            ))}
            <div ref={endRef} />
          </div>
        )}
      </div>
      <Composer running={running} onSend={run} />
    </div>
  );
}

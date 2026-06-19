import { useEffect, useRef } from 'react';
import { LOGO_URL } from '../config';
import { usePipeline } from '../hooks/usePipeline';
import { EXAMPLE_QUERIES } from '../types';
import { Composer } from './Composer';
import { Message } from './Message';

export function ChatView() {
  const { messages, running, run } = usePipeline();
  const endRef = useRef<HTMLDivElement>(null);

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
              {EXAMPLE_QUERIES.map((q) => (
                <button key={q} className="ex-chip" onClick={() => run(q)}>
                  {q}
                </button>
              ))}
            </div>
          </div>
        ) : (
          <div className="messages">
            {messages.map((m) => (
              <Message key={m.id} m={m} />
            ))}
            <div ref={endRef} />
          </div>
        )}
      </div>
      <Composer running={running} onSend={run} showExamples={!empty} />
    </div>
  );
}

import { useState } from 'react';
import { EXAMPLE_QUERIES } from '../types';

interface Props {
  running: boolean;
  onSend: (q: string) => void;
  showExamples: boolean;
}

export function Composer({ running, onSend, showExamples }: Props) {
  const [value, setValue] = useState('');

  const submit = () => {
    const q = value.trim();
    if (!q || running) return;
    onSend(q);
    setValue('');
  };

  return (
    <div className="composer">
      {showExamples && (
        <div className="examples">
          {EXAMPLE_QUERIES.map((q) => (
            <button key={q} className="ex-chip" onClick={() => onSend(q)} disabled={running}>
              {q}
            </button>
          ))}
        </div>
      )}
      <div className="composer-row">
        <textarea
          className="composer-input"
          rows={1}
          placeholder="Ask for a chart or dashboard…"
          value={value}
          disabled={running}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
              e.preventDefault();
              submit();
            }
          }}
        />
        <button className="send-btn" onClick={submit} disabled={running || !value.trim()}>
          {running ? <span className="spinner" /> : 'Send'}
        </button>
      </div>
    </div>
  );
}

import { apiUrl } from './config';
import { getToken } from './auth/keycloak';
import type { AuthConfig, PipelineEvent } from './types';

/** Thrown when the backend rejects the request for auth reasons (401/403). */
export class AuthError extends Error {
  status: number;
  constructor(status: number) {
    super(`Authentication required (${status})`);
    this.status = status;
  }
}

export async function fetchAuthConfig(): Promise<AuthConfig> {
  const r = await fetch(apiUrl('auth-config'));
  if (!r.ok) throw new Error(`auth-config failed (${r.status})`);
  return r.json();
}

/** Mint a Superset guest token (scoped to the dashboard) via our backend. */
export async function fetchGuestToken(uuid: string): Promise<string> {
  const token = await getToken();
  const r = await fetch(apiUrl('guest-token'), {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    body: JSON.stringify({ uuid }),
  });
  if (!r.ok) {
    let reason = '';
    try {
      const j = await r.json();
      reason = j.reason || j.error || '';
    } catch {
      /* no body */
    }
    throw new Error(`guest token request failed (${r.status})${reason ? ': ' + reason : ''}`);
  }
  const data = await r.json().catch(() => null);
  if (typeof data === 'string') return data.trim().replace(/^["']|["']$/g, '');
  return (data && (data.token || data.guest_token)) || '';
}

/** Run the pipeline and stream parsed SSE events as they arrive. */
export async function* runPipeline(
  query: string,
  signal?: AbortSignal,
): AsyncGenerator<PipelineEvent> {
  const token = await getToken();
  const resp = await fetch(apiUrl('run'), {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    body: JSON.stringify({ query }),
    signal,
  });

  if (resp.status === 401 || resp.status === 403) throw new AuthError(resp.status);
  if (!resp.ok) {
    const t = await resp.text().catch(() => '');
    throw new Error(`Server returned ${resp.status}${t ? ': ' + t.slice(0, 160) : ''}`);
  }
  if (!resp.body) throw new Error('No response stream');

  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split('\n');
    buffer = lines.pop() ?? '';
    for (const line of lines) {
      if (line.startsWith('data: ')) {
        try {
          yield JSON.parse(line.slice(6)) as PipelineEvent;
        } catch {
          /* ignore keep-alive / malformed lines */
        }
      }
    }
  }
}

import { useEffect, useState } from 'react';
import { fetchAuthConfig } from './api';
import { initAuth } from './auth/keycloak';
import { ChatView } from './components/ChatView';
import { Header } from './components/Header';
import { AppConfigProvider, type AppConfig } from './context';

type BootState =
  | { phase: 'loading' }
  | { phase: 'error'; message: string }
  | { phase: 'ready'; config: AppConfig };

export default function App() {
  const [boot, setBoot] = useState<BootState>({ phase: 'loading' });

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const cfg = await fetchAuthConfig();
        const { authenticated, username } = await initAuth(cfg);
        if (cancelled) return;
        if (!authenticated) {
          setBoot({ phase: 'error', message: 'Not authenticated.' });
          return;
        }
        setBoot({ phase: 'ready', config: { embed: cfg.embed, username } });
      } catch (e) {
        if (!cancelled) {
          setBoot({ phase: 'error', message: e instanceof Error ? e.message : String(e) });
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  if (boot.phase === 'loading') {
    return (
      <div className="overlay">
        <span className="spinner large" />
        <p>Signing you in…</p>
      </div>
    );
  }
  if (boot.phase === 'error') {
    return (
      <div className="overlay">
        <p className="overlay-error">{boot.message}</p>
        <button className="send-btn" onClick={() => location.reload()}>
          Retry
        </button>
      </div>
    );
  }

  return (
    <AppConfigProvider value={boot.config}>
      <div className="app">
        <Header />
        <ChatView />
      </div>
    </AppConfigProvider>
  );
}

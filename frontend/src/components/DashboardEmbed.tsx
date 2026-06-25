import { useEffect, useRef, useState } from 'react';
import { embedDashboard } from '@superset-ui/embedded-sdk';
import { fetchGuestToken } from '../api';
import { useAppConfig } from '../context';

/**
 * Inline Superset dashboard via the embedded SDK + a backend-minted guest token.
 * No outbound Superset link — the dashboard is only ever viewed in-app.
 */
export function DashboardEmbed({ uuid }: { uuid: string }) {
  const { embed } = useAppConfig();
  const ref = useRef<HTMLDivElement>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const mount = ref.current;
    if (!mount || !embed.enabled || !embed.supersetDomain) return;

    let unmount: (() => void) | undefined;
    let cancelled = false;
    setError(null);
    mount.innerHTML = '';

    embedDashboard({
      id: uuid,
      supersetDomain: embed.supersetDomain,
      mountPoint: mount,
      fetchGuestToken: () => fetchGuestToken(uuid),
      dashboardUiConfig: { hideTitle: true, filters: { visible: false, expanded: false } },
    })
      .then((instance) => {
        if (cancelled) return;
        unmount = (instance as { unmount?: () => void })?.unmount;
        const iframe = mount.querySelector('iframe');
        if (iframe) {
          iframe.style.width = '100%';
          iframe.style.height = '100%';
          iframe.style.border = '0';
        }
      })
      .catch((e: unknown) => {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      });

    return () => {
      cancelled = true;
      try {
        unmount?.();
      } catch {
        /* ignore */
      }
      mount.innerHTML = '';
    };
  }, [uuid, embed.enabled, embed.supersetDomain]);

  if (!embed.enabled) return null;
  if (error) {
    return (
      <div className="embed-error">
        Dashboard preview couldn’t load — {error}
      </div>
    );
  }
  return <div className="embed-mount" ref={ref} aria-label="Dashboard preview" />;
}

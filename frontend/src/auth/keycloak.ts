import Keycloak from 'keycloak-js';
import type { AuthConfig } from '../types';

// Single Keycloak instance for the app. Mirrors the ui-cbn Angular client
// (same realm/clientId) so the same SSO session works across apps. When auth is
// disabled (local dev: KEYCLOAK_ENABLED=false) everything no-ops and getToken()
// returns null.

let kc: Keycloak | null = null;
let enabled = false;

export interface AuthResult {
  authenticated: boolean;
  username?: string;
}

export async function initAuth(cfg: AuthConfig): Promise<AuthResult> {
  if (!cfg.enabled) {
    enabled = false;
    return { authenticated: true };
  }
  enabled = true;
  kc = new Keycloak({ url: cfg.url!, realm: cfg.realm!, clientId: cfg.clientId! });

  const authenticated = await kc.init({
    onLoad: 'login-required',
    pkceMethod: 'S256',
    checkLoginIframe: false,
  });

  if (authenticated && cfg.requiredRole) {
    const roles = kc.realmAccess?.roles ?? [];
    if (!roles.includes(cfg.requiredRole)) {
      throw new Error(`Your account lacks the required role "${cfg.requiredRole}".`);
    }
  }

  // Keep the token fresh; on failure, bounce to login.
  setInterval(() => {
    kc?.updateToken(60).catch(() => kc?.login());
  }, 30_000);

  const username =
    (kc.tokenParsed as { preferred_username?: string } | undefined)?.preferred_username;
  return { authenticated, username };
}

/** Current access token (refreshed if near expiry), or null when auth is off. */
export async function getToken(): Promise<string | null> {
  if (!enabled || !kc) return null;
  try {
    await kc.updateToken(30);
  } catch {
    /* a subsequent 401 triggers re-login via relogin() */
  }
  return kc.token ?? null;
}

export function relogin(): void {
  kc?.login();
}

export function logout(): void {
  kc?.logout();
}

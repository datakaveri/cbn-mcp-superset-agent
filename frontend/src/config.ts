// Runtime config. BASE is injected by Vite (import.meta.env.BASE_URL) and equals
// '/' in dev or '/chatbot/' for the sub-path production build, so all API calls
// resolve correctly behind the reverse proxy without hardcoding the prefix.
export const BASE: string = import.meta.env.BASE_URL || '/';

/** Build an API URL under the app's base path. apiUrl('run') → '/run' or '/chatbot/run'. */
export function apiUrl(path: string): string {
  return BASE.replace(/\/?$/, '/') + path.replace(/^\//, '');
}

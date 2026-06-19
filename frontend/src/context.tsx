import { createContext, useContext } from 'react';
import type { EmbedConfig } from './types';

/** App-wide config available to any component (currently the embed settings). */
export interface AppConfig {
  embed: EmbedConfig;
  username?: string;
}

const AppConfigContext = createContext<AppConfig>({
  embed: { enabled: false, supersetDomain: '' },
});

export const AppConfigProvider = AppConfigContext.Provider;

export function useAppConfig(): AppConfig {
  return useContext(AppConfigContext);
}

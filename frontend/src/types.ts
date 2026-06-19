export type LogLevel = 'info' | 'success' | 'warning' | 'error';
export type PhaseStatus = 'pending' | 'running' | 'done' | 'error';

export interface EmbedConfig {
  enabled: boolean;
  supersetDomain: string;
}

export interface AuthConfig {
  enabled: boolean;
  url?: string;
  realm?: string;
  clientId?: string;
  requiredRole?: string | null;
  embed: EmbedConfig;
}

/** A single SSE event emitted by the backend pipeline (POST /run). */
export interface PipelineEvent {
  phase?: string;
  level?: LogLevel;
  message?: string;
  done?: boolean;
  success?: boolean;
  dashboard_url?: string;
  dashboard_id?: number;
  dashboard_uuid?: string;
  charts?: number;
  charts_total?: number;
  errors?: string[];
}

export interface LogLine {
  ts: string;
  phase?: string;
  level: LogLevel;
  message: string;
}

export interface UserMessage {
  id: string;
  role: 'user';
  text: string;
}

export interface AssistantMessage {
  id: string;
  role: 'assistant';
  query: string;
  logs: LogLine[];
  phases: Record<string, PhaseStatus>;
  running: boolean;
  success?: boolean;
  dashboardUuid?: string;
  dashboardUrl?: string;
  chartCount?: number;
  elapsed?: number;
  error?: string;
}

export type ChatMessage = UserMessage | AssistantMessage;

/** Ordered pipeline phases with display labels (keys match backend Phase values). */
export const PHASES: { key: string; label: string }[] = [
  { key: 'health_check', label: 'Health' },
  { key: 'plan_generation', label: 'Plan' },
  { key: 'dataset_discovery', label: 'Dataset' },
  { key: 'plan_refinement', label: 'Refine' },
  { key: 'sql_validation', label: 'Validate' },
  { key: 'chart_creation', label: 'Charts' },
  { key: 'dashboard_assembly', label: 'Assemble' },
  { key: 'result_reporting', label: 'Report' },
];

export const EXAMPLE_QUERIES: string[] = [
  'show me a time series chart of inflow and outflow of cash',
  'show the top 10 banks by total transaction amount as a bar chart grouped by bank_name',
  'Create a pie chart of number of transactions done by each channel',
];

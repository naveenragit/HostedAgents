/**
 * Typed HTTP client for the conversation API.
 *
 * All requests are same-origin: the Vite dev server proxies `/api` to FastAPI, so
 * the browser never makes a cross-origin call during development.
 *
 * The user identity is NOT sent by this client in production. `x-dev-user-id` is
 * attached only while the backend runs with AUTH_MODE=dev; with AUTH_MODE=entra
 * the backend derives the user from a validated bearer token and ignores the header.
 */

import type {
  AgentSummary,
  Conversation,
  Message,
  SendMessageResult,
} from './types';

const DEV_USER_ID = import.meta.env.VITE_DEV_USER_ID ?? 'local-dev-user';

export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
  }
}

interface RequestOptions {
  method?: 'GET' | 'POST' | 'DELETE';
  body?: unknown;
  signal?: AbortSignal;
}

async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { method = 'GET', body, signal } = options;

  const headers: Record<string, string> = {
    Accept: 'application/json',
    'x-dev-user-id': DEV_USER_ID,
  };
  if (body !== undefined) {
    headers['Content-Type'] = 'application/json';
  }

  const init: RequestInit = { method, headers };
  if (body !== undefined) {
    init.body = JSON.stringify(body);
  }
  if (signal) {
    init.signal = signal;
  }

  const response = await fetch(path, init);

  if (!response.ok) {
    throw new ApiError(response.status, await readErrorDetail(response));
  }

  if (response.status === 204) {
    return undefined as T;
  }

  return (await response.json()) as T;
}

async function readErrorDetail(response: Response): Promise<string> {
  try {
    const payload: unknown = await response.json();
    if (typeof payload === 'object' && payload !== null && 'detail' in payload) {
      const { detail } = payload;
      if (typeof detail === 'string') {
        return detail;
      }
    }
  } catch {
    // Non-JSON body — fall through to the generic message.
  }
  return `Request failed with status ${response.status}.`;
}

export const api = {
  listAgents: (signal?: AbortSignal) =>
    request<AgentSummary[]>('/api/agents', signal ? { signal } : {}),

  listConversations: (signal?: AbortSignal) =>
    request<Conversation[]>('/api/conversations', signal ? { signal } : {}),

  createConversation: (agentName: string, title?: string) =>
    request<Conversation>('/api/conversations', {
      method: 'POST',
      body: title ? { agentName, title } : { agentName },
    }),

  deleteConversation: (conversationId: string) =>
    request<void>(`/api/conversations/${encodeURIComponent(conversationId)}`, {
      method: 'DELETE',
    }),

  listMessages: (conversationId: string, signal?: AbortSignal) =>
    request<Message[]>(
      `/api/conversations/${encodeURIComponent(conversationId)}/messages`,
      signal ? { signal } : {},
    ),

  sendMessage: (conversationId: string, content: string) =>
    request<SendMessageResult>(
      `/api/conversations/${encodeURIComponent(conversationId)}/messages`,
      { method: 'POST', body: { content } },
    ),
};

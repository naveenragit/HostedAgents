/**
 * Typed HTTP client for the conversation API.
 *
 * All requests are same-origin: the Vite dev server proxies `/api` to FastAPI, so
 * the browser never makes a cross-origin call during development.
 *
 * This local POC has no application-user authentication. The API assigns one
 * server-owned identity and accepts only loopback clients, so this client sends
 * neither bearer tokens nor caller-selected identity headers.
 */

import type {
  AgentSummary,
  ApiCapabilities,
  Conversation,
  CursorPage,
  Message,
  SendMessageResult,
} from './types';

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
  headers?: HeadersInit;
  signal?: AbortSignal | undefined;
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

export function createApiClient() {
  async function request<T>(path: string, requestOptions: RequestOptions = {}): Promise<T> {
    const { method = 'GET', body, signal } = requestOptions;
    const headers = new Headers(requestOptions.headers);
    headers.set('Accept', 'application/json');

    if (body !== undefined) {
      headers.set('Content-Type', 'application/json');
    }

    const init: RequestInit = { method, headers };
    if (body !== undefined) {
      init.body = JSON.stringify(body);
    }
    if (signal !== undefined) {
      init.signal = signal;
    }

    const response = await fetch(path, init);

    if (!response.ok) {
      const detail = await readErrorDetail(response);
      throw new ApiError(response.status, detail);
    }

    if (response.status === 204) {
      return undefined as T;
    }

    return (await response.json()) as T;
  }

  const withCursor = (path: string, cursor?: string | null) => {
    if (!cursor) {
      return path;
    }
    const query = new URLSearchParams({ cursor });
    return `${path}?${query.toString()}`;
  };

  return {
    listAgents: (signal?: AbortSignal) =>
      request<AgentSummary[]>('/api/agents', signal ? { signal } : {}),

    getCapabilities: (signal?: AbortSignal) =>
      request<ApiCapabilities>('/api/capabilities', signal ? { signal } : {}),

    listConversations: (cursor?: string | null, signal?: AbortSignal) =>
      request<CursorPage<Conversation>>(
        withCursor('/api/conversations', cursor),
        signal ? { signal } : {},
      ),

    createConversation: (agentName: string, title?: string, signal?: AbortSignal) =>
      request<Conversation>('/api/conversations', {
        method: 'POST',
        body: title ? { agentName, title } : { agentName },
        signal,
      }),

    deleteConversation: (conversationId: string, signal?: AbortSignal) =>
      request<void>(`/api/conversations/${encodeURIComponent(conversationId)}`, {
        method: 'DELETE',
        signal,
      }),

    listMessages: (
      conversationId: string,
      cursor?: string | null,
      signal?: AbortSignal,
    ) =>
      request<CursorPage<Message>>(
        withCursor(
          `/api/conversations/${encodeURIComponent(conversationId)}/messages`,
          cursor,
        ),
        signal ? { signal } : {},
      ),

    sendMessage: (
      conversationId: string,
      content: string,
      idempotencyKey: string,
      signal?: AbortSignal,
    ) =>
      request<SendMessageResult>(
        `/api/conversations/${encodeURIComponent(conversationId)}/messages`,
        {
          method: 'POST',
          body: { content },
          headers: { 'Idempotency-Key': idempotencyKey },
          signal,
        },
      ),
  };
}

export type ApiClient = ReturnType<typeof createApiClient>;

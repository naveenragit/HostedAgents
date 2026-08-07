import { act, renderHook, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { ApiError, type ApiClient } from '../api/client';
import type {
  Conversation,
  CursorPage,
  Message,
  SendMessageResult,
} from '../api/types';
import { useChat } from './useChat';

interface Deferred<T> {
  promise: Promise<T>;
  resolve: (value: T) => void;
}

function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((complete) => {
    resolve = complete;
  });
  return { promise, resolve };
}

function conversation(
  id: string,
  overrides: Partial<Conversation> = {},
): Conversation {
  return {
    id,
    agentName: 'triage-agent',
    title: `Conversation ${id}`,
    createdAt: '2026-08-06T10:00:00Z',
    updatedAt: '2026-08-06T10:00:00Z',
    messageCount: 0,
    conversationManager: 'cosmos',
    ...overrides,
  };
}

function message(
  id: string,
  conversationId: string,
  overrides: Partial<Message> = {},
): Message {
  return {
    id,
    conversationId,
    role: 'assistant',
    content: id,
    createdAt: '2026-08-06T10:00:00Z',
    ...overrides,
  };
}

function sendResult(
  conversationId: string,
  overrides: Partial<Conversation> = {},
): SendMessageResult {
  return {
    conversation: conversation(conversationId, {
      messageCount: 2,
      updatedAt: '2026-08-06T11:00:00Z',
      ...overrides,
    }),
    userMessage: message('user-message', conversationId, {
      role: 'user',
      content: 'question',
    }),
    assistantMessage: message('assistant-message', conversationId, {
      content: 'answer',
      createdAt: '2026-08-06T11:00:00Z',
    }),
  };
}

function createMockApi(overrides: Partial<ApiClient> = {}): ApiClient {
  return {
    listAgents: () => Promise.resolve([{ name: 'triage-agent' }]),
    getCapabilities: () => Promise.resolve({ maxInputChars: 120 }),
    listConversations: () => Promise.resolve({
      items: [conversation('a'), conversation('b')],
      nextCursor: null,
    }),
    createConversation: (agentName) =>
      Promise.resolve(conversation('created', { agentName })),
    deleteConversation: () => Promise.resolve(undefined),
    listMessages: () => Promise.resolve({ items: [], nextCursor: null }),
    sendMessage: (conversationId) => Promise.resolve(sendResult(conversationId)),
    ...overrides,
  };
}

async function waitForInitialLoad(
  result: { current: ReturnType<typeof useChat> },
): Promise<void> {
  await waitFor(() => expect(result.current.conversations).toHaveLength(2));
}

describe('useChat request isolation', () => {
  it('ignores a late transcript response after switching conversations', async () => {
    const latePage = deferred<CursorPage<Message>>();
    const listMessages = vi.fn((conversationId: string) =>
      conversationId === 'a'
        ? latePage.promise
        : Promise.resolve({
            items: [message('b-message', 'b')],
            nextCursor: null,
          }),
    );
    const api = createMockApi({ listMessages });
    const { result } = renderHook(() => useChat(api, 80));
    await waitForInitialLoad(result);

    act(() => result.current.selectConversation('a'));
    await waitFor(() => expect(listMessages).toHaveBeenCalledWith('a', null, expect.any(AbortSignal)));

    act(() => result.current.selectConversation('b'));
    await waitFor(() =>
      expect(result.current.messages.map((item) => item.id)).toEqual(['b-message']),
    );

    await act(async () => {
      latePage.resolve({
        items: [message('late-a-message', 'a')],
        nextCursor: null,
      });
      await latePage.promise;
    });

    expect(result.current.selectedId).toBe('b');
    expect(result.current.messages.map((item) => item.id)).toEqual(['b-message']);
  });

  it('tracks sends per conversation and never applies an aborted send to another transcript', async () => {
    const aSend = deferred<SendMessageResult>();
    const bSend = deferred<SendMessageResult>();
    const sendMessage = vi.fn<ApiClient['sendMessage']>(
      (conversationId) =>
        conversationId === 'a' ? aSend.promise : bSend.promise,
    );
    const api = createMockApi({ sendMessage });
    const { result } = renderHook(() => useChat(api, 80));
    await waitForInitialLoad(result);

    act(() => result.current.selectConversation('a'));
    await waitFor(() => expect(result.current.isLoadingMessages).toBe(false));

    let firstSend!: Promise<boolean>;
    act(() => {
      firstSend = result.current.send('question for a');
    });
    await waitFor(() => expect(result.current.sendingConversationIds.has('a')).toBe(true));

    act(() => result.current.selectConversation('b'));
    await waitFor(() => expect(result.current.selectedId).toBe('b'));
    expect(result.current.isSending).toBe(false);
    expect(sendMessage.mock.calls[0]?.[3]?.aborted).toBe(true);

    let secondSend!: Promise<boolean>;
    act(() => {
      secondSend = result.current.send('question for b');
    });
    await waitFor(() => {
      expect(result.current.sendingConversationIds.has('a')).toBe(true);
      expect(result.current.sendingConversationIds.has('b')).toBe(true);
    });

    await act(async () => {
      bSend.resolve(sendResult('b'));
      await secondSend;
    });
    expect(result.current.messages.map((item) => item.conversationId)).toEqual(['b', 'b']);

    await act(async () => {
      aSend.resolve(sendResult('a'));
      await firstSend;
    });
    expect(result.current.messages.map((item) => item.conversationId)).toEqual(['b', 'b']);
  });

  it('reuses the same idempotency key after an ambiguous network failure', async () => {
    const sendMessage = vi
      .fn<ApiClient['sendMessage']>()
      .mockRejectedValueOnce(new TypeError('Failed to fetch'))
      .mockResolvedValueOnce(sendResult('a'));
    const api = createMockApi({ sendMessage });
    const { result } = renderHook(() => useChat(api, 80));
    await waitForInitialLoad(result);
    act(() => result.current.selectConversation('a'));
    await waitFor(() => expect(result.current.isLoadingMessages).toBe(false));

    let firstAccepted = true;
    await act(async () => {
      firstAccepted = await result.current.send('retry me');
    });
    expect(firstAccepted).toBe(false);

    let secondAccepted = false;
    await act(async () => {
      secondAccepted = await result.current.send('retry me');
    });
    expect(secondAccepted).toBe(true);

    const firstKey = sendMessage.mock.calls[0]?.[2];
    const secondKey = sendMessage.mock.calls[1]?.[2];
    expect(firstKey).toMatch(/^[0-9a-f-]{36}$/i);
    expect(secondKey).toBe(firstKey);
  });

  it('reuses the same idempotency key after a pending conflict', async () => {
    const sendMessage = vi
      .fn<ApiClient['sendMessage']>()
      .mockRejectedValueOnce(new ApiError(409, 'Turn is already pending.'))
      .mockResolvedValueOnce(sendResult('a'));
    const api = createMockApi({ sendMessage });
    const { result } = renderHook(() => useChat(api, 80));
    await waitForInitialLoad(result);
    act(() => result.current.selectConversation('a'));
    await waitFor(() => expect(result.current.isLoadingMessages).toBe(false));

    await act(async () => {
      expect(await result.current.send('retry pending')).toBe(false);
    });
    await act(async () => {
      expect(await result.current.send('retry pending')).toBe(true);
    });

    expect(sendMessage.mock.calls[1]?.[2]).toBe(sendMessage.mock.calls[0]?.[2]);
  });

  it('merges cursor pages without duplicates and preserves message chronology', async () => {
    const listConversations = vi.fn((cursor?: string | null) =>
      Promise.resolve(
        cursor === null || cursor === undefined
          ? { items: [conversation('a')], nextCursor: 'conversation-page-2' }
          : {
              items: [
                conversation('a'),
                conversation('b', { updatedAt: '2026-08-06T09:00:00Z' }),
              ],
              nextCursor: null,
            },
      ),
    );
    const listMessages = vi.fn((_conversationId: string, cursor?: string | null) =>
      Promise.resolve(
        cursor === null || cursor === undefined
          ? {
              items: [message('m3', 'a'), message('m4', 'a')],
              nextCursor: 'message-page-2',
            }
          : {
              items: [message('m1', 'a'), message('m2', 'a')],
              nextCursor: null,
            },
      ),
    );
    const api = createMockApi({ listConversations, listMessages });
    const { result } = renderHook(() => useChat(api, 80));
    await waitFor(() => expect(result.current.conversations.map((item) => item.id)).toEqual(['a']));

    await act(async () => result.current.loadMoreConversations());
    expect(result.current.conversations.map((item) => item.id)).toEqual(['a', 'b']);

    act(() => result.current.selectConversation('a'));
    await waitFor(() =>
      expect(result.current.messages.map((item) => item.id)).toEqual(['m3', 'm4']),
    );
    await act(async () => result.current.loadMoreMessages());
    expect(result.current.messages.map((item) => item.id)).toEqual([
      'm1',
      'm2',
      'm3',
      'm4',
    ]);
  });

  it('uses authoritative conversation metadata returned by send', async () => {
    const api = createMockApi({
      sendMessage: () =>
        Promise.resolve(sendResult('a', {
          title: 'Server-generated title',
          messageCount: 9,
          updatedAt: '2026-08-06T12:00:00Z',
        })),
    });
    const { result } = renderHook(() => useChat(api, 80));
    await waitForInitialLoad(result);
    act(() => result.current.selectConversation('a'));
    await waitFor(() => expect(result.current.isLoadingMessages).toBe(false));

    await act(async () => result.current.send('refresh metadata'));

    expect(result.current.conversations[0]).toMatchObject({
      id: 'a',
      title: 'Server-generated title',
      messageCount: 9,
      updatedAt: '2026-08-06T12:00:00Z',
    });
  });
});
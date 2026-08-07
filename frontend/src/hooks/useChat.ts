import { useCallback, useEffect, useRef, useState } from 'react';

import { ApiError, type ApiClient } from '../api/client';
import type { Conversation, Message } from '../api/types';

interface ActiveRequest {
  id: number;
  controller: AbortController;
}

interface ConversationRequest extends ActiveRequest {
  conversationId: string;
}

interface RetryableSend {
  content: string;
  idempotencyKey: string;
}

export interface ChatState {
  conversations: Conversation[];
  conversationsNextCursor: string | null;
  selectedId: string | null;
  messages: Message[];
  messagesNextCursor: string | null;
  agents: string[];
  maxInputChars: number;
  isLoadingConversations: boolean;
  isLoadingMessages: boolean;
  isLoadingMoreMessages: boolean;
  isStarting: boolean;
  sendingConversationIds: ReadonlySet<string>;
  deletingConversationIds: ReadonlySet<string>;
  isSending: boolean;
  error: string | null;
  status: string;
  selectConversation: (id: string) => void;
  loadMoreConversations: () => Promise<void>;
  loadMoreMessages: () => Promise<void>;
  startConversation: (agentName: string) => Promise<void>;
  removeConversation: (id: string) => Promise<void>;
  send: (content: string) => Promise<boolean>;
  dismissError: () => void;
}

function isAbort(cause: unknown): boolean {
  return cause instanceof DOMException && cause.name === 'AbortError';
}

function mergeMessages(older: Message[], newer: Message[]): Message[] {
  const seen = new Set<string>();
  return [...older, ...newer].filter((message) => {
    if (seen.has(message.id)) {
      return false;
    }
    seen.add(message.id);
    return true;
  });
}

function sortConversations(conversations: Conversation[]): Conversation[] {
  return [...conversations].sort(
    (left, right) => Date.parse(right.updatedAt) - Date.parse(left.updatedAt),
  );
}

function mergeConversationPage(
  current: Conversation[],
  incoming: Conversation[],
): Conversation[] {
  const existingIds = new Set(current.map((conversation) => conversation.id));
  return sortConversations([
    ...current,
    ...incoming.filter((conversation) => !existingIds.has(conversation.id)),
  ]);
}

function upsertConversation(
  current: Conversation[],
  conversation: Conversation,
): Conversation[] {
  return sortConversations([
    conversation,
    ...current.filter((candidate) => candidate.id !== conversation.id),
  ]);
}

function isDefinitiveRejection(cause: unknown): boolean {
  return (
    cause instanceof ApiError &&
    cause.status >= 400 &&
    cause.status < 500 &&
    cause.status !== 408 &&
    cause.status !== 409 &&
    cause.status !== 429
  );
}

export function useChat(api: ApiClient, fallbackMaxInputChars: number): ChatState {
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [conversationsNextCursor, setConversationsNextCursor] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [messagesNextCursor, setMessagesNextCursor] = useState<string | null>(null);
  const [agents, setAgents] = useState<string[]>([]);
  const [maxInputChars, setMaxInputChars] = useState(fallbackMaxInputChars);
  const [isLoadingConversations, setLoadingConversations] = useState(false);
  const [isLoadingMessages, setLoadingMessages] = useState(false);
  const [isLoadingMoreMessages, setLoadingMoreMessages] = useState(false);
  const [isStarting, setStarting] = useState(false);
  const [sendingConversationIds, setSendingConversationIds] = useState<Set<string>>(
    () => new Set(),
  );
  const [deletingConversationIds, setDeletingConversationIds] = useState<Set<string>>(
    () => new Set(),
  );
  const [error, setError] = useState<string | null>(null);
  const [status, setStatus] = useState('');

  const selectedIdRef = useRef<string | null>(null);
  const nextRequestId = useRef(0);
  const initialRequest = useRef<ActiveRequest | null>(null);
  const conversationPageRequest = useRef<ActiveRequest | null>(null);
  const messagesRequest = useRef<ConversationRequest | null>(null);
  const startRequest = useRef<ActiveRequest | null>(null);
  const sendRequests = useRef(new Map<string, ActiveRequest>());
  const deleteRequests = useRef(new Map<string, ActiveRequest>());
  const retryableSends = useRef(new Map<string, RetryableSend>());

  const createRequest = useCallback(
    (): ActiveRequest => ({
      id: ++nextRequestId.current,
      controller: new AbortController(),
    }),
    [],
  );

  const report = useCallback((cause: unknown, fallback: string) => {
    if (isAbort(cause)) {
      return;
    }
    setError(cause instanceof ApiError ? cause.message : fallback);
  }, []);

  const abortConversationWork = useCallback((conversationId: string) => {
    if (messagesRequest.current?.conversationId === conversationId) {
      messagesRequest.current.controller.abort();
    }
    sendRequests.current.get(conversationId)?.controller.abort();
  }, []);

  useEffect(() => {
    const request = createRequest();
    initialRequest.current = request;

    void (async () => {
      setLoadingConversations(true);
      setStatus('Loading conversations.');
      try {
        const capabilitiesPromise = api
          .getCapabilities(request.controller.signal)
          .then((capabilities) =>
            Number.isSafeInteger(capabilities.maxInputChars) && capabilities.maxInputChars > 0
              ? capabilities.maxInputChars
              : fallbackMaxInputChars,
          )
          .catch(() => fallbackMaxInputChars);
        const [agentList, conversationPage, inputLimit] = await Promise.all([
          api.listAgents(request.controller.signal),
          api.listConversations(null, request.controller.signal),
          capabilitiesPromise,
        ]);

        if (initialRequest.current?.id !== request.id || request.controller.signal.aborted) {
          return;
        }
        setAgents(agentList.map((agent) => agent.name));
        setConversations(sortConversations(conversationPage.items));
        setConversationsNextCursor(conversationPage.nextCursor);
        setMaxInputChars(inputLimit);
        setStatus('Conversations loaded.');
      } catch (cause) {
        report(cause, 'Could not reach the API. Is the backend running?');
      } finally {
        if (initialRequest.current?.id === request.id) {
          initialRequest.current = null;
          setLoadingConversations(false);
        }
      }
    })();

    return () => request.controller.abort();
  }, [api, createRequest, fallbackMaxInputChars, report]);

  useEffect(() => {
    if (selectedId === null) {
      return;
    }

    const request: ConversationRequest = {
      ...createRequest(),
      conversationId: selectedId,
    };
    messagesRequest.current = request;

    void (async () => {
      setLoadingMessages(true);
      setStatus('Loading messages.');
      try {
        const page = await api.listMessages(
          selectedId,
          null,
          request.controller.signal,
        );
        if (
          messagesRequest.current?.id !== request.id ||
          request.controller.signal.aborted ||
          selectedIdRef.current !== selectedId
        ) {
          return;
        }
        setMessages(mergeMessages([], page.items));
        setMessagesNextCursor(page.nextCursor);
        setStatus('Messages loaded.');
      } catch (cause) {
        report(cause, 'Could not load this conversation.');
      } finally {
        if (messagesRequest.current?.id === request.id) {
          messagesRequest.current = null;
          if (selectedIdRef.current === selectedId) {
            setLoadingMessages(false);
          }
        }
      }
    })();

    return () => request.controller.abort();
  }, [api, createRequest, report, selectedId]);

  useEffect(
    () => () => {
      initialRequest.current?.controller.abort();
      conversationPageRequest.current?.controller.abort();
      messagesRequest.current?.controller.abort();
      startRequest.current?.controller.abort();
      sendRequests.current.forEach((request) => request.controller.abort());
      deleteRequests.current.forEach((request) => request.controller.abort());
    },
    [],
  );

  const selectConversation = useCallback(
    (id: string) => {
      const previousId = selectedIdRef.current;
      if (previousId === id) {
        return;
      }
      if (previousId !== null) {
        abortConversationWork(previousId);
      }
      startRequest.current?.controller.abort();
      selectedIdRef.current = id;
      setSelectedId(id);
      setMessages([]);
      setMessagesNextCursor(null);
      setLoadingMessages(false);
      setLoadingMoreMessages(false);
    },
    [abortConversationWork],
  );

  const loadMoreConversations = useCallback(async () => {
    if (conversationsNextCursor === null || conversationPageRequest.current !== null) {
      return;
    }

    const request = createRequest();
    conversationPageRequest.current = request;
    setLoadingConversations(true);
    try {
      const page = await api.listConversations(
        conversationsNextCursor,
        request.controller.signal,
      );
      if (
        conversationPageRequest.current?.id !== request.id ||
        request.controller.signal.aborted
      ) {
        return;
      }
      setConversations((current) => mergeConversationPage(current, page.items));
      setConversationsNextCursor(page.nextCursor);
      setStatus('More conversations loaded.');
    } catch (cause) {
      report(cause, 'Could not load more conversations.');
    } finally {
      if (conversationPageRequest.current?.id === request.id) {
        conversationPageRequest.current = null;
        setLoadingConversations(false);
      }
    }
  }, [api, conversationsNextCursor, createRequest, report]);

  const loadMoreMessages = useCallback(async () => {
    const conversationId = selectedIdRef.current;
    if (
      conversationId === null ||
      messagesNextCursor === null ||
      messagesRequest.current !== null
    ) {
      return;
    }

    const request: ConversationRequest = {
      ...createRequest(),
      conversationId,
    };
    messagesRequest.current = request;
    setLoadingMoreMessages(true);
    try {
      const page = await api.listMessages(
        conversationId,
        messagesNextCursor,
        request.controller.signal,
      );
      if (
        messagesRequest.current?.id !== request.id ||
        request.controller.signal.aborted ||
        selectedIdRef.current !== conversationId
      ) {
        return;
      }
      setMessages((current) => mergeMessages(page.items, current));
      setMessagesNextCursor(page.nextCursor);
      setStatus('Older messages loaded.');
    } catch (cause) {
      report(cause, 'Could not load older messages.');
    } finally {
      if (messagesRequest.current?.id === request.id) {
        messagesRequest.current = null;
        if (selectedIdRef.current === conversationId) {
          setLoadingMoreMessages(false);
        }
      }
    }
  }, [api, createRequest, messagesNextCursor, report]);

  const startConversation = useCallback(
    async (agentName: string) => {
      if (startRequest.current !== null) {
        return;
      }

      const request = createRequest();
      startRequest.current = request;
      setStarting(true);
      try {
        const created = await api.createConversation(
          agentName,
          undefined,
          request.controller.signal,
        );
        if (startRequest.current?.id !== request.id || request.controller.signal.aborted) {
          return;
        }
        const previousId = selectedIdRef.current;
        if (previousId !== null) {
          abortConversationWork(previousId);
        }
        setConversations((current) => upsertConversation(current, created));
        selectedIdRef.current = created.id;
        setSelectedId(created.id);
        setMessages([]);
        setMessagesNextCursor(null);
        setStatus(`Started ${created.title}.`);
      } catch (cause) {
        report(cause, 'Could not start a conversation.');
      } finally {
        if (startRequest.current?.id === request.id) {
          startRequest.current = null;
          setStarting(false);
        }
      }
    },
    [api, abortConversationWork, createRequest, report],
  );

  const removeConversation = useCallback(
    async (id: string) => {
      if (deleteRequests.current.has(id)) {
        return;
      }

      abortConversationWork(id);
      const request = createRequest();
      deleteRequests.current.set(id, request);
      setDeletingConversationIds((current) => new Set(current).add(id));
      try {
        await api.deleteConversation(id, request.controller.signal);
        if (deleteRequests.current.get(id)?.id !== request.id || request.controller.signal.aborted) {
          return;
        }
        setConversations((current) => current.filter((conversation) => conversation.id !== id));
        retryableSends.current.delete(id);
        if (selectedIdRef.current === id) {
          selectedIdRef.current = null;
          setSelectedId(null);
          setMessages([]);
          setMessagesNextCursor(null);
        }
        setStatus('Conversation deleted.');
      } catch (cause) {
        report(cause, 'Could not delete the conversation.');
      } finally {
        if (deleteRequests.current.get(id)?.id === request.id) {
          deleteRequests.current.delete(id);
          setDeletingConversationIds((current) => {
            const next = new Set(current);
            next.delete(id);
            return next;
          });
        }
      }
    },
    [api, abortConversationWork, createRequest, report],
  );

  const send = useCallback(
    async (content: string): Promise<boolean> => {
      const conversationId = selectedIdRef.current;
      if (conversationId === null || sendRequests.current.has(conversationId)) {
        return false;
      }

      const previousAttempt = retryableSends.current.get(conversationId);
      const attempt: RetryableSend =
        previousAttempt?.content === content
          ? previousAttempt
          : { content, idempotencyKey: crypto.randomUUID() };
      retryableSends.current.set(conversationId, attempt);

      if (messagesRequest.current?.conversationId === conversationId) {
        messagesRequest.current.controller.abort();
      }
      const request = createRequest();
      sendRequests.current.set(conversationId, request);
      setSendingConversationIds((current) => new Set(current).add(conversationId));
      setStatus('Sending message.');

      try {
        const result = await api.sendMessage(
          conversationId,
          content,
          attempt.idempotencyKey,
          request.controller.signal,
        );
        if (
          sendRequests.current.get(conversationId)?.id !== request.id ||
          request.controller.signal.aborted ||
          selectedIdRef.current !== conversationId
        ) {
          return false;
        }
        retryableSends.current.delete(conversationId);
        setMessages((current) =>
          mergeMessages(current, [result.userMessage, result.assistantMessage]),
        );
        setConversations((current) => upsertConversation(current, result.conversation));
        setStatus('Message sent and response received.');
        return true;
      } catch (cause) {
        if (isDefinitiveRejection(cause)) {
          retryableSends.current.delete(conversationId);
        }
        if (!isAbort(cause)) {
          report(
            cause,
            'Delivery could not be confirmed. Retry to safely reuse the same request.',
          );
        }
        return false;
      } finally {
        if (sendRequests.current.get(conversationId)?.id === request.id) {
          sendRequests.current.delete(conversationId);
          setSendingConversationIds((current) => {
            const next = new Set(current);
            next.delete(conversationId);
            return next;
          });
        }
      }
    },
    [api, createRequest, report],
  );

  const dismissError = useCallback(() => setError(null), []);

  return {
    conversations,
    conversationsNextCursor,
    selectedId,
    messages,
    messagesNextCursor,
    agents,
    maxInputChars,
    isLoadingConversations,
    isLoadingMessages,
    isLoadingMoreMessages,
    isStarting,
    sendingConversationIds,
    deletingConversationIds,
    isSending: selectedId !== null && sendingConversationIds.has(selectedId),
    error,
    status,
    selectConversation,
    loadMoreConversations,
    loadMoreMessages,
    startConversation,
    removeConversation,
    send,
    dismissError,
  };
}

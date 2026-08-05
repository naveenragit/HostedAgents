/**
 * Chat state for the selected conversation.
 *
 * Kept in one hook so the component tree stays presentational. Requests are
 * aborted on unmount / conversation switch, which prevents a slow response for a
 * previous conversation from overwriting the current transcript.
 */

import { useCallback, useEffect, useRef, useState } from 'react';

import { ApiError, api } from '../api/client';
import type { Conversation, Message } from '../api/types';

export interface ChatState {
  conversations: Conversation[];
  selectedId: string | null;
  messages: Message[];
  agents: string[];
  isLoadingMessages: boolean;
  isSending: boolean;
  error: string | null;
  selectConversation: (id: string) => void;
  startConversation: (agentName: string) => Promise<void>;
  removeConversation: (id: string) => Promise<void>;
  send: (content: string) => Promise<void>;
  dismissError: () => void;
}

export function useChat(): ChatState {
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [agents, setAgents] = useState<string[]>([]);
  const [isLoadingMessages, setLoadingMessages] = useState(false);
  const [isSending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const messagesRequest = useRef<AbortController | null>(null);

  const report = useCallback((cause: unknown, fallback: string) => {
    if (cause instanceof DOMException && cause.name === 'AbortError') {
      return;
    }
    setError(cause instanceof ApiError ? cause.message : fallback);
  }, []);

  // Initial load: agents and the conversation list.
  useEffect(() => {
    const controller = new AbortController();

    void (async () => {
      try {
        const [agentList, conversationList] = await Promise.all([
          api.listAgents(controller.signal),
          api.listConversations(controller.signal),
        ]);
        setAgents(agentList.map((a) => a.name));
        setConversations(conversationList);
      } catch (cause) {
        report(cause, 'Could not reach the API. Is the backend running?');
      }
    })();

    return () => controller.abort();
  }, [report]);

  // Load the transcript whenever the selection changes.
  //
  // Clearing on deselection is done by the handlers below, not here: calling
  // setState synchronously in an effect body triggers a cascading render.
  useEffect(() => {
    if (selectedId === null) {
      return;
    }

    messagesRequest.current?.abort();

    const controller = new AbortController();
    messagesRequest.current = controller;

    void (async () => {
      // Inside the async callback rather than the effect body: a synchronous
      // setState in an effect body triggers a cascading render.
      setLoadingMessages(true);
      try {
        setMessages(await api.listMessages(selectedId, controller.signal));
      } catch (cause) {
        report(cause, 'Could not load this conversation.');
      } finally {
        if (!controller.signal.aborted) {
          setLoadingMessages(false);
        }
      }
    })();

    return () => controller.abort();
  }, [selectedId, report]);

  const selectConversation = useCallback((id: string) => {
    setSelectedId((current) => {
      if (current !== id) {
        // Drop the previous transcript so a slow load can't briefly show the
        // wrong conversation's messages.
        setMessages([]);
      }
      return id;
    });
  }, []);

  const startConversation = useCallback(
    async (agentName: string) => {
      try {
        const created = await api.createConversation(agentName);
        setConversations((current) => [created, ...current]);
        setSelectedId(created.id);
      } catch (cause) {
        report(cause, 'Could not start a conversation.');
      }
    },
    [report],
  );

  const removeConversation = useCallback(
    async (id: string) => {
      try {
        await api.deleteConversation(id);
        setConversations((current) => current.filter((c) => c.id !== id));
        setSelectedId((current) => {
          if (current === id) {
            messagesRequest.current?.abort();
            setMessages([]);
            return null;
          }
          return current;
        });
      } catch (cause) {
        report(cause, 'Could not delete the conversation.');
      }
    },
    [report],
  );

  const send = useCallback(
    async (content: string) => {
      if (selectedId === null || isSending) {
        return;
      }

      setSending(true);
      try {
        const result = await api.sendMessage(selectedId, content);
        setMessages((current) => [...current, result.userMessage, result.assistantMessage]);
        setConversations((current) =>
          current.map((c) =>
            c.id === selectedId
              ? { ...c, messageCount: c.messageCount + 2, updatedAt: result.assistantMessage.createdAt }
              : c,
          ),
        );
      } catch (cause) {
        report(cause, 'The agent did not respond.');
      } finally {
        setSending(false);
      }
    },
    [selectedId, isSending, report],
  );

  const dismissError = useCallback(() => setError(null), []);

  return {
    conversations,
    selectedId,
    messages,
    agents,
    isLoadingMessages,
    isSending,
    error,
    selectConversation,
    startConversation,
    removeConversation,
    send,
    dismissError,
  };
}

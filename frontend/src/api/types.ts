/** Wire types shared with the FastAPI service. Field names match its JSON aliases. */

export type Role = 'user' | 'assistant';
export type ConversationManager = 'cosmos' | 'foundry';

export interface AgentSummary {
  name: string;
}

export interface CursorPage<T> {
  items: T[];
  nextCursor: string | null;
}

export interface ApiCapabilities {
  maxInputChars: number;
}

export interface Conversation {
  id: string;
  agentName: string;
  title: string;
  createdAt: string;
  updatedAt: string;
  messageCount: number;
  conversationManager: ConversationManager;
}

export interface Message {
  id: string;
  conversationId: string;
  role: Role;
  content: string;
  createdAt: string | null;
}

export interface SendMessageResult {
  conversation: Conversation;
  userMessage: Message;
  assistantMessage: Message;
}

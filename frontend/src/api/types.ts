/** Wire types shared with the FastAPI service. Field names match its JSON aliases. */

export type Role = 'user' | 'assistant';

export interface AgentSummary {
  name: string;
}

export interface Conversation {
  id: string;
  agentName: string;
  title: string;
  createdAt: string;
  updatedAt: string;
  messageCount: number;
}

export interface Message {
  id: string;
  conversationId: string;
  role: Role;
  content: string;
  createdAt: string;
}

export interface SendMessageResult {
  userMessage: Message;
  assistantMessage: Message;
}

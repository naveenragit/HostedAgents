import { Cloud, Database, LoaderCircle, MessageSquarePlus, Trash2 } from 'lucide-react';

import type { Conversation } from '../api/types';

interface Props {
  conversations: Conversation[];
  agents: string[];
  selectedId: string | null;
  isStarting: boolean;
  isLoadingMore: boolean;
  hasMore: boolean;
  sendingConversationIds: ReadonlySet<string>;
  deletingConversationIds: ReadonlySet<string>;
  onSelect: (id: string) => void;
  onStart: (agentName: string) => void;
  onDelete: (id: string) => void;
  onLoadMore: () => void;
}

export function ConversationList({
  conversations,
  agents,
  selectedId,
  isStarting,
  isLoadingMore,
  hasMore,
  sendingConversationIds,
  deletingConversationIds,
  onSelect,
  onStart,
  onDelete,
  onLoadMore,
}: Props) {
  return (
    <aside className="sidebar">
      <div className="sidebar__header">
        <h1 className="sidebar__title">Conversations</h1>
      </div>

      <div className="sidebar__actions">
        {agents.length === 0 ? (
          <p className="muted">No agents configured.</p>
        ) : (
          agents.map((agent) => (
            <button
              key={agent}
              type="button"
              className="button button--new"
              onClick={() => onStart(agent)}
              disabled={isStarting}
            >
              {isStarting ? (
                <LoaderCircle className="spin" aria-hidden="true" size={16} />
              ) : (
                <MessageSquarePlus aria-hidden="true" size={16} />
              )}
              New chat with {agent}
            </button>
          ))
        )}
      </div>

      <ul className="conversation-list">
        {conversations.map((conversation) => {
          const isSelected = conversation.id === selectedId;
          const isDeleting = deletingConversationIds.has(conversation.id);
          const isSending = sendingConversationIds.has(conversation.id);
          return (
            <li key={conversation.id}>
              <div className={`conversation${isSelected ? ' conversation--selected' : ''}`}>
                <button
                  type="button"
                  className="conversation__open"
                  onClick={() => onSelect(conversation.id)}
                  aria-current={isSelected}
                >
                  <span className="conversation__title">{conversation.title}</span>
                  <span className="conversation__meta">
                    {conversation.agentName} · {conversation.messageCount} messages ·{' '}
                    <span className="conversation__manager">
                      {conversation.conversationManager === 'foundry' ? (
                        <Cloud aria-hidden="true" size={12} />
                      ) : (
                        <Database aria-hidden="true" size={12} />
                      )}
                      {conversation.conversationManager === 'foundry'
                        ? 'Foundry history'
                        : 'Cosmos history'}
                    </span>
                    {isSending ? ' · sending' : ''}
                  </span>
                </button>
                <button
                  type="button"
                  className="conversation__delete"
                  aria-label={`Delete ${conversation.title}`}
                  title={`Delete ${conversation.title}`}
                  disabled={isDeleting}
                  onClick={() => onDelete(conversation.id)}
                >
                  {isDeleting ? (
                    <LoaderCircle className="spin" aria-hidden="true" size={16} />
                  ) : (
                    <Trash2 aria-hidden="true" size={16} />
                  )}
                </button>
              </div>
            </li>
          );
        })}
      </ul>

      {conversations.length === 0 && (
        <p className="muted sidebar__empty">Start a chat to see it here.</p>
      )}

      {hasMore && (
        <button
          type="button"
          className="button button--secondary sidebar__more"
          onClick={onLoadMore}
          disabled={isLoadingMore}
        >
          {isLoadingMore && <LoaderCircle className="spin" aria-hidden="true" size={16} />}
          Load more conversations
        </button>
      )}
    </aside>
  );
}

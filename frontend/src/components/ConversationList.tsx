import type { Conversation } from '../api/types';

interface Props {
  conversations: Conversation[];
  agents: string[];
  selectedId: string | null;
  onSelect: (id: string) => void;
  onStart: (agentName: string) => void;
  onDelete: (id: string) => void;
}

export function ConversationList({
  conversations,
  agents,
  selectedId,
  onSelect,
  onStart,
  onDelete,
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
            >
              + New chat with {agent}
            </button>
          ))
        )}
      </div>

      <ul className="conversation-list">
        {conversations.map((conversation) => {
          const isSelected = conversation.id === selectedId;
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
                    {conversation.agentName} · {conversation.messageCount} messages
                  </span>
                </button>
                <button
                  type="button"
                  className="conversation__delete"
                  aria-label={`Delete ${conversation.title}`}
                  onClick={() => onDelete(conversation.id)}
                >
                  ×
                </button>
              </div>
            </li>
          );
        })}
      </ul>

      {conversations.length === 0 && (
        <p className="muted sidebar__empty">Start a chat to see it here.</p>
      )}
    </aside>
  );
}

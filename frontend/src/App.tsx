import { Cloud, Database } from 'lucide-react';
import { useMemo } from 'react';

import { createApiClient } from './api/client';
import { ConversationList } from './components/ConversationList';
import { ErrorBanner } from './components/ErrorBanner';
import { MessageComposer } from './components/MessageComposer';
import { MessageList } from './components/MessageList';
import type { AppConfig } from './config';
import { useChat } from './hooks/useChat';

interface Props {
  config: AppConfig;
}

export default function App({ config }: Props) {
  const api = useMemo(() => createApiClient(), []);
  const chat = useChat(api, config.fallbackMaxInputChars);
  const selected = chat.conversations.find(
    (conversation) => conversation.id === chat.selectedId,
  ) ?? null;

  return (
    <div className="layout">
      <ConversationList
        conversations={chat.conversations}
        agents={chat.agents}
        selectedId={chat.selectedId}
        isStarting={chat.isStarting}
        isLoadingMore={chat.isLoadingConversations}
        hasMore={chat.conversationsNextCursor !== null}
        sendingConversationIds={chat.sendingConversationIds}
        deletingConversationIds={chat.deletingConversationIds}
        onSelect={chat.selectConversation}
        onStart={(agent) => void chat.startConversation(agent)}
        onDelete={(id) => void chat.removeConversation(id)}
        onLoadMore={() => void chat.loadMoreConversations()}
      />

      <main className="main">
        <header className="main__header">
          <div className="main__heading">
            <h2 className="main__title">{selected ? selected.title : 'Hosted Agents'}</h2>
            {selected && (
              <div className="main__meta muted">
                <span>{selected.agentName}</span>
                <span className="manager-badge">
                  {selected.conversationManager === 'foundry' ? (
                    <Cloud aria-hidden="true" size={14} />
                  ) : (
                    <Database aria-hidden="true" size={14} />
                  )}
                  {selected.conversationManager === 'foundry'
                    ? 'Foundry history'
                    : 'Cosmos history'}
                </span>
              </div>
            )}
          </div>
          <span className="account__label">Local POC</span>
        </header>

        {chat.error && (
          <ErrorBanner message={chat.error} onDismiss={chat.dismissError} />
        )}

        <MessageList
          conversationId={chat.selectedId}
          messages={chat.messages}
          isLoading={chat.isLoadingMessages}
          isLoadingMore={chat.isLoadingMoreMessages}
          hasMore={chat.messagesNextCursor !== null}
          isSending={chat.isSending}
          onLoadMore={() => void chat.loadMoreMessages()}
        />

        <MessageComposer
          conversationId={chat.selectedId}
          disabled={chat.selectedId === null}
          isSending={chat.isSending}
          maxChars={chat.maxInputChars}
          onSend={chat.send}
        />

        <div className="sr-only" role="status" aria-live="polite" aria-atomic="true">
          {chat.status}
        </div>
      </main>
    </div>
  );
}

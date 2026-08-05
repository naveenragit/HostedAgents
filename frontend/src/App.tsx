import { ConversationList } from './components/ConversationList';
import { ErrorBanner } from './components/ErrorBanner';
import { MessageComposer } from './components/MessageComposer';
import { MessageList } from './components/MessageList';
import { useChat } from './hooks/useChat';

export default function App() {
  const chat = useChat();
  const selected = chat.conversations.find((c) => c.id === chat.selectedId) ?? null;

  return (
    <div className="layout">
      <ConversationList
        conversations={chat.conversations}
        agents={chat.agents}
        selectedId={chat.selectedId}
        onSelect={chat.selectConversation}
        onStart={(agent) => void chat.startConversation(agent)}
        onDelete={(id) => void chat.removeConversation(id)}
      />

      <main className="main">
        <header className="main__header">
          <h2 className="main__title">{selected ? selected.title : 'Hosted Agents'}</h2>
          {selected && <span className="muted">{selected.agentName}</span>}
        </header>

        {chat.error && <ErrorBanner message={chat.error} onDismiss={chat.dismissError} />}

        <MessageList
          messages={chat.messages}
          isLoading={chat.isLoadingMessages}
          isSending={chat.isSending}
        />

        <MessageComposer
          disabled={chat.selectedId === null}
          isSending={chat.isSending}
          onSend={(content) => void chat.send(content)}
        />
      </main>
    </div>
  );
}

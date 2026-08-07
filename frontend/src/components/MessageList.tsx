import { LoaderCircle } from 'lucide-react';
import { useLayoutEffect, useRef } from 'react';

import type { Message } from '../api/types';

interface Props {
  conversationId: string | null;
  messages: Message[];
  isLoading: boolean;
  isLoadingMore: boolean;
  hasMore: boolean;
  isSending: boolean;
  onLoadMore: () => void;
}

export function MessageList({
  conversationId,
  messages,
  isLoading,
  isLoadingMore,
  hasMore,
  isSending,
  onLoadMore,
}: Props) {
  const transcriptRef = useRef<HTMLDivElement | null>(null);
  const previousConversationId = useRef<string | null>(conversationId);
  const previousMessageCount = useRef(messages.length);
  const previousFirstMessageId = useRef(messages[0]?.id);
  const previousScrollHeight = useRef(0);

  useLayoutEffect(() => {
    const transcript = transcriptRef.current;
    if (transcript === null) {
      return;
    }

    const conversationChanged = previousConversationId.current !== conversationId;
    const olderMessagesPrepended =
      !conversationChanged &&
      messages.length > previousMessageCount.current &&
      previousFirstMessageId.current !== undefined &&
      messages[0]?.id !== previousFirstMessageId.current;

    if (olderMessagesPrepended) {
      transcript.scrollTop += transcript.scrollHeight - previousScrollHeight.current;
    } else if (conversationChanged || messages.length > previousMessageCount.current || isSending) {
      transcript.scrollTop = transcript.scrollHeight;
    }

    previousConversationId.current = conversationId;
    previousMessageCount.current = messages.length;
    previousFirstMessageId.current = messages[0]?.id;
    previousScrollHeight.current = transcript.scrollHeight;
  }, [conversationId, isSending, messages]);

  return (
    <div className="transcript" ref={transcriptRef} aria-busy={isLoading}>
      {hasMore && !isLoading && (
        <button
          type="button"
          className="button button--secondary transcript__more"
          onClick={onLoadMore}
          disabled={isLoadingMore}
        >
          {isLoadingMore && <LoaderCircle className="spin" aria-hidden="true" size={16} />}
          Load older messages
        </button>
      )}

      {isLoading && (
        <div className="transcript__empty muted" role="status">Loading messages...</div>
      )}

      {!isLoading && messages.length === 0 && !isSending && (
        <div className="transcript__empty muted">
          {conversationId === null
            ? 'Select or start a conversation.'
            : 'No messages yet. Say something to get started.'}
        </div>
      )}

      {!isLoading &&
        messages.map((message) => (
          <article key={message.id} className={`bubble bubble--${message.role}`}>
            <header className="bubble__role">{message.role}</header>
            <p className="bubble__content">{message.content}</p>
            {message.createdAt !== null && (
              <time className="bubble__time" dateTime={message.createdAt}>
                {new Date(message.createdAt).toLocaleTimeString()}
              </time>
            )}
          </article>
        ))}

      {isSending && (
        <div className="bubble bubble--assistant muted" role="status">
          <LoaderCircle className="spin" aria-hidden="true" size={16} />
          Agent is responding...
        </div>
      )}
    </div>
  );
}

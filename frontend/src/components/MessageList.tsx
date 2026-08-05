import { useEffect, useRef } from 'react';

import type { Message } from '../api/types';

interface Props {
  messages: Message[];
  isLoading: boolean;
  isSending: boolean;
}

export function MessageList({ messages, isLoading, isSending }: Props) {
  const endRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages.length, isSending]);

  if (isLoading) {
    return <div className="transcript transcript--empty muted">Loading…</div>;
  }

  // `isSending` is checked here too: on the very first turn the transcript is
  // still empty while the agent is working, and the empty state would otherwise
  // hide the "Thinking…" indicator and make the app look unresponsive.
  if (messages.length === 0 && !isSending) {
    return (
      <div className="transcript transcript--empty muted">
        No messages yet. Say something to get started.
      </div>
    );
  }

  return (
    <div className="transcript">
      {messages.map((message) => (
        <article key={message.id} className={`bubble bubble--${message.role}`}>
          <header className="bubble__role">{message.role}</header>
          {/* React escapes text content, so agent output cannot inject markup. */}
          <p className="bubble__content">{message.content}</p>
          <time className="bubble__time" dateTime={message.createdAt}>
            {new Date(message.createdAt).toLocaleTimeString()}
          </time>
        </article>
      ))}
      {isSending && <div className="bubble bubble--assistant muted">Thinking…</div>}
      <div ref={endRef} />
    </div>
  );
}

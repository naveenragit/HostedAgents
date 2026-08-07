import { Send } from 'lucide-react';
import { useState, type FormEvent, type KeyboardEvent } from 'react';

interface Props {
  conversationId: string | null;
  disabled: boolean;
  isSending: boolean;
  maxChars: number;
  onSend: (content: string) => Promise<boolean>;
}

export function MessageComposer({
  conversationId,
  disabled,
  isSending,
  maxChars,
  onSend,
}: Props) {
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const draft = conversationId === null ? '' : (drafts[conversationId] ?? '');
  const content = draft.trim();
  const isOverLimit = content.length > maxChars;
  const validationId = 'message-validation';

  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (
      conversationId === null ||
      !content ||
      disabled ||
      isSending ||
      isOverLimit
    ) {
      return;
    }

    const capturedConversationId = conversationId;
    const capturedDraft = draft;
    void (async () => {
      const accepted = await onSend(content);
      if (accepted) {
        setDrafts((current) =>
          current[capturedConversationId] === capturedDraft
            ? { ...current, [capturedConversationId]: '' }
            : current,
        );
      }
    })();
  };

  const handleKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      event.currentTarget.form?.requestSubmit();
    }
  };

  return (
    <form className="composer" onSubmit={submit}>
      <textarea
        className="composer__input"
        value={draft}
        onChange={(event) => {
          if (conversationId !== null) {
            setDrafts((current) => ({
              ...current,
              [conversationId]: event.target.value,
            }));
          }
        }}
        onKeyDown={handleKeyDown}
        placeholder={disabled ? 'Select or start a conversation first' : 'Send a message...'}
        rows={3}
        disabled={disabled || isSending}
        aria-label="Message"
        aria-invalid={isOverLimit}
        aria-describedby={validationId}
      />
      <div className="composer__footer">
        <span
          id={validationId}
          className={isOverLimit ? 'composer__validation' : 'muted composer__count'}
          role={isOverLimit ? 'alert' : undefined}
        >
          {isOverLimit
            ? `Message is ${content.length - maxChars} characters over the ${maxChars} character limit.`
            : `${content.length} / ${maxChars}`}
        </span>
        <button
          type="submit"
          className="button button--primary"
          disabled={disabled || isSending || content.length === 0 || isOverLimit}
        >
          <Send aria-hidden="true" size={16} />
          {isSending ? 'Sending...' : 'Send'}
        </button>
      </div>
    </form>
  );
}

import { useState, type FormEvent, type KeyboardEvent } from 'react';

/** Mirrors API_MAX_INPUT_CHARS on the backend; the server re-enforces it. */
const MAX_CHARS = 8000;

interface Props {
  disabled: boolean;
  isSending: boolean;
  onSend: (content: string) => void;
}

export function MessageComposer({ disabled, isSending, onSend }: Props) {
  const [draft, setDraft] = useState('');

  const submit = (event: FormEvent) => {
    event.preventDefault();
    const content = draft.trim();
    if (!content || disabled || isSending) {
      return;
    }
    onSend(content);
    setDraft('');
  };

  const handleKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    // Enter sends; Shift+Enter inserts a newline.
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
        onChange={(event) => setDraft(event.target.value)}
        onKeyDown={handleKeyDown}
        placeholder={disabled ? 'Select or start a conversation first' : 'Send a message…'}
        rows={3}
        maxLength={MAX_CHARS}
        disabled={disabled || isSending}
        aria-label="Message"
      />
      <div className="composer__footer">
        <span className="muted composer__count">
          {draft.length} / {MAX_CHARS}
        </span>
        <button
          type="submit"
          className="button button--send"
          disabled={disabled || isSending || draft.trim().length === 0}
        >
          {isSending ? 'Sending…' : 'Send'}
        </button>
      </div>
    </form>
  );
}

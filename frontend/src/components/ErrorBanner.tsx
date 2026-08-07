import { X } from 'lucide-react';

interface Props {
  message: string;
  onDismiss: () => void;
}

export function ErrorBanner({ message, onDismiss }: Props) {
  return (
    <div className="error-banner" role="alert">
      <span>{message}</span>
      <button
        type="button"
        className="icon-button error-banner__dismiss"
        aria-label="Dismiss error"
        title="Dismiss error"
        onClick={onDismiss}
      >
        <X aria-hidden="true" size={17} />
      </button>
    </div>
  );
}

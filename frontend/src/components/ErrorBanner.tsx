interface Props {
  message: string;
  onDismiss: () => void;
}

export function ErrorBanner({ message, onDismiss }: Props) {
  return (
    <div className="error-banner" role="alert">
      <span>{message}</span>
      <button type="button" className="error-banner__dismiss" onClick={onDismiss}>
        Dismiss
      </button>
    </div>
  );
}

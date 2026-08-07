import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import { MessageComposer } from './MessageComposer';

describe('MessageComposer', () => {
  it('shows the API limit and retains the draft until a send is accepted', async () => {
    const user = userEvent.setup();
    const onSend = vi.fn().mockResolvedValueOnce(false).mockResolvedValueOnce(true);
    render(
      <MessageComposer
        conversationId="conversation-a"
        disabled={false}
        isSending={false}
        maxChars={5}
        onSend={onSend}
      />,
    );

    const input = screen.getByRole('textbox', { name: 'Message' });
    await user.type(input, '123456');
    expect(screen.getByRole('alert')).toHaveTextContent('1 characters over');
    expect(screen.getByRole('button', { name: 'Send' })).toBeDisabled();

    await user.clear(input);
    await user.type(input, 'hello');
    await user.click(screen.getByRole('button', { name: 'Send' }));
    expect(input).toHaveValue('hello');

    await user.click(screen.getByRole('button', { name: 'Send' }));
    expect(input).toHaveValue('');
  });
});
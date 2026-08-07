import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';

import App from './App';
import { loadAppConfig } from './config';
import './styles.css';

const container = document.getElementById('root');
if (!container) {
  throw new Error('Root element #root was not found in index.html.');
}

const config = loadAppConfig();

createRoot(container).render(
  <StrictMode>
    <App config={config} />
  </StrictMode>,
);

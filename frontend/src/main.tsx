import React from 'react'
import ReactDOM from 'react-dom/client'
import App from './App'
import './index.css'
import { initAgentRegistry } from './agentRegistry'

function renderApp(): void {
  ReactDOM.createRoot(document.getElementById('root')!).render(
    <React.StrictMode>
      <App />
    </React.StrictMode>,
  );
}

// Initialize the agent registry from the backend before rendering.
// Populates AGENT_ICONS, AGENT_LABELS, AGENT_COLORS, AGENT_ACCENTS
// from the single source of truth (AGENT_REGISTRY in config.py).
// Always renders the app — on failure the fallback registry is used.
initAgentRegistry()
  .catch((err: unknown) => {
    console.warn('[main] Agent registry init failed, using fallback:', err);
  })
  .finally(renderApp);

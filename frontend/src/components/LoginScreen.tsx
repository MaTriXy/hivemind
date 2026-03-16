import { useState, useRef, useEffect, useCallback } from 'react';
import { setAuthToken } from '../WebSocketContext';

const CODE_LENGTH = 8;

interface LoginScreenProps {
  onAuthenticated: () => void;
}

export default function LoginScreen({ onAuthenticated }: LoginScreenProps) {
  const [chars, setChars] = useState<string[]>(Array(CODE_LENGTH).fill(''));
  const [password, setPassword] = useState('');
  const [passwordRequired, setPasswordRequired] = useState(false);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const [shake, setShake] = useState(false);
  const inputRefs = useRef<(HTMLInputElement | null)[]>([]);
  const passwordRef = useRef<HTMLInputElement | null>(null);

  // Check if password is required
  useEffect(() => {
    fetch('/api/auth/status')
      .then(res => res.json())
      .then(data => {
        if (data.password_required) setPasswordRequired(true);
      })
      .catch(() => {});
  }, []);

  // Focus first input on mount
  useEffect(() => {
    inputRefs.current[0]?.focus();
  }, []);

  const triggerError = useCallback((msg: string) => {
    setError(msg);
    setShake(true);
    setTimeout(() => setShake(false), 600);
  }, []);

  const submitCode = useCallback(async (code: string) => {
    if (passwordRequired && !password) {
      passwordRef.current?.focus();
      triggerError('Password is required.');
      return;
    }
    setLoading(true);
    setError('');
    try {
      const res = await fetch('/api/auth/verify', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ code, password }),
      });
      if (res.ok) {
        const data = await res.json().catch(() => ({}));
        // Store device token for WebSocket auth
        if (data.device_token) {
          setAuthToken(data.device_token);
        }
        onAuthenticated();
      } else {
        const data = await res.json().catch(() => ({}));
        triggerError(data.detail || data.error || 'Invalid code. Try again.');
        setChars(Array(CODE_LENGTH).fill(''));
        inputRefs.current[0]?.focus();
      }
    } catch {
      triggerError('Connection error. Is the server running?');
    } finally {
      setLoading(false);
    }
  }, [onAuthenticated, password, passwordRequired, triggerError]);

  const handleInput = useCallback((index: number, value: string) => {
    // Allow letters and digits
    const char = value.replace(/[^a-zA-Z0-9]/g, '').slice(-1).toUpperCase();
    const newChars = [...chars];
    newChars[index] = char;
    setChars(newChars);
    setError('');

    if (char && index < CODE_LENGTH - 1) {
      inputRefs.current[index + 1]?.focus();
    }

    // Auto-submit when all chars are filled (if no password required)
    if (char && index === CODE_LENGTH - 1) {
      const code = newChars.join('');
      if (code.length === CODE_LENGTH) {
        if (passwordRequired) {
          passwordRef.current?.focus();
        } else {
          submitCode(code);
        }
      }
    }
  }, [chars, submitCode, passwordRequired]);

  const handleKeyDown = useCallback((index: number, e: React.KeyboardEvent) => {
    if (e.key === 'Backspace' && !chars[index] && index > 0) {
      inputRefs.current[index - 1]?.focus();
    }
    if (e.key === 'Enter') {
      const code = chars.join('');
      if (code.length === CODE_LENGTH) {
        submitCode(code);
      }
    }
  }, [chars, submitCode]);

  const handlePaste = useCallback((e: React.ClipboardEvent) => {
    e.preventDefault();
    const pasted = e.clipboardData.getData('text').replace(/[^a-zA-Z0-9]/g, '').toUpperCase().slice(0, CODE_LENGTH);
    if (pasted.length > 0) {
      const newChars = [...chars];
      for (let i = 0; i < pasted.length && i < CODE_LENGTH; i++) {
        newChars[i] = pasted[i];
      }
      setChars(newChars);
      if (pasted.length === CODE_LENGTH) {
        if (passwordRequired) {
          passwordRef.current?.focus();
        } else {
          submitCode(pasted);
        }
      } else {
        inputRefs.current[Math.min(pasted.length, CODE_LENGTH - 1)]?.focus();
      }
    }
  }, [chars, submitCode, passwordRequired]);

  const handlePasswordKeyDown = useCallback((e: React.KeyboardEvent) => {
    if (e.key === 'Enter') {
      const code = chars.join('');
      if (code.length === CODE_LENGTH) {
        submitCode(code);
      }
    }
  }, [chars, submitCode]);

  const inputStyle = (char: string): React.CSSProperties => ({
    width: '44px',
    height: '56px',
    borderRadius: '10px',
    border: `2px solid ${error ? 'var(--accent-red, #ef4444)' : char ? 'var(--accent-blue, #6366f1)' : 'var(--border-dim, #27272a)'}`,
    background: 'var(--bg-panel, #18181b)',
    color: 'var(--text-primary, #e4e4e7)',
    fontSize: '20px',
    fontWeight: 700,
    textAlign: 'center',
    outline: 'none',
    transition: 'border-color 0.2s, box-shadow 0.2s',
    caretColor: 'var(--accent-blue, #6366f1)',
    boxShadow: char ? '0 0 0 3px rgba(99, 102, 241, 0.1)' : 'none',
    textTransform: 'uppercase' as const,
    fontFamily: 'monospace',
  });

  return (
    <div style={{
      minHeight: '100vh',
      display: 'flex',
      alignItems: 'center',
      justifyContent: 'center',
      background: 'var(--bg-void, #0a0a0f)',
      padding: '20px',
    }}>
      <div style={{
        width: '100%',
        maxWidth: '480px',
        textAlign: 'center',
      }}>
        {/* Logo */}
        <div style={{
          width: '72px',
          height: '72px',
          margin: '0 auto 24px',
          borderRadius: '18px',
          background: 'linear-gradient(135deg, #6366f1 0%, #8b5cf6 50%, #a78bfa 100%)',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          boxShadow: '0 8px 32px rgba(99, 102, 241, 0.3)',
        }}>
          <svg width="36" height="36" viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
            <path d="M12 2L2 7l10 5 10-5-10-5z"/>
            <path d="M2 17l10 5 10-5"/>
            <path d="M2 12l10 5 10-5"/>
          </svg>
        </div>

        {/* Title */}
        <h1 style={{
          fontSize: '28px',
          fontWeight: 700,
          color: 'var(--text-primary, #e4e4e7)',
          margin: '0 0 8px',
          letterSpacing: '-0.02em',
        }}>
          Hivemind
        </h1>
        <p style={{
          fontSize: '15px',
          color: 'var(--text-muted, #71717a)',
          margin: '0 0 32px',
          lineHeight: 1.5,
        }}>
          Enter the access code from your terminal
        </p>

        {/* Code Input — 8 chars */}
        <div
          style={{
            display: 'flex',
            gap: '6px',
            justifyContent: 'center',
            marginBottom: passwordRequired ? '16px' : '24px',
            animation: shake ? 'shake 0.5s ease-in-out' : 'none',
          }}
          onPaste={handlePaste}
        >
          {chars.map((char, i) => (
            <input
              key={i}
              ref={el => { inputRefs.current[i] = el; }}
              type="text"
              inputMode="text"
              maxLength={1}
              value={char}
              onChange={e => handleInput(i, e.target.value)}
              onKeyDown={e => handleKeyDown(i, e)}
              disabled={loading}
              autoComplete="one-time-code"
              style={inputStyle(char)}
              onFocus={e => {
                e.target.style.borderColor = 'var(--accent-blue, #6366f1)';
                e.target.style.boxShadow = '0 0 0 3px rgba(99, 102, 241, 0.15)';
              }}
              onBlur={e => {
                if (!char) {
                  e.target.style.borderColor = error ? 'var(--accent-red, #ef4444)' : 'var(--border-dim, #27272a)';
                  e.target.style.boxShadow = 'none';
                }
              }}
            />
          ))}
        </div>

        {/* Password field (only if required) */}
        {passwordRequired && (
          <div style={{ marginBottom: '24px' }}>
            <input
              ref={passwordRef}
              type="password"
              placeholder="Password"
              value={password}
              onChange={e => setPassword(e.target.value)}
              onKeyDown={handlePasswordKeyDown}
              disabled={loading}
              style={{
                width: '100%',
                maxWidth: '320px',
                height: '48px',
                borderRadius: '12px',
                border: `2px solid ${error && !password ? 'var(--accent-red, #ef4444)' : 'var(--border-dim, #27272a)'}`,
                background: 'var(--bg-panel, #18181b)',
                color: 'var(--text-primary, #e4e4e7)',
                fontSize: '16px',
                padding: '0 16px',
                outline: 'none',
                transition: 'border-color 0.2s',
              }}
              onFocus={e => {
                e.target.style.borderColor = 'var(--accent-blue, #6366f1)';
              }}
              onBlur={e => {
                e.target.style.borderColor = 'var(--border-dim, #27272a)';
              }}
            />
          </div>
        )}

        {/* Submit button */}
        {(passwordRequired || chars.every(c => c)) && (
          <button
            onClick={() => {
              const code = chars.join('');
              if (code.length === CODE_LENGTH) submitCode(code);
            }}
            disabled={loading || chars.some(c => !c)}
            style={{
              width: '100%',
              maxWidth: '320px',
              height: '48px',
              borderRadius: '12px',
              border: 'none',
              background: 'linear-gradient(135deg, #6366f1, #8b5cf6)',
              color: 'white',
              fontSize: '16px',
              fontWeight: 600,
              cursor: 'pointer',
              marginBottom: '24px',
              opacity: loading || chars.some(c => !c) ? 0.5 : 1,
              transition: 'opacity 0.2s',
            }}
          >
            {loading ? 'Verifying...' : 'Connect'}
          </button>
        )}

        {/* Error message */}
        {error && (
          <p style={{
            fontSize: '14px',
            color: 'var(--accent-red, #ef4444)',
            margin: '0 0 16px',
            animation: 'fadeIn 0.3s ease',
          }}>
            {error}
          </p>
        )}

        {/* Loading indicator */}
        {loading && (
          <div style={{
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            gap: '8px',
            color: 'var(--text-muted, #71717a)',
            fontSize: '14px',
            marginBottom: '16px',
          }}>
            <div style={{
              width: '16px',
              height: '16px',
              border: '2px solid var(--border-dim, #27272a)',
              borderTopColor: 'var(--accent-blue, #6366f1)',
              borderRadius: '50%',
              animation: 'spin 0.8s linear infinite',
            }} />
            Connecting...
          </div>
        )}

        {/* Help text */}
        <div style={{
          padding: '16px 20px',
          borderRadius: '12px',
          background: 'var(--bg-panel, #18181b)',
          border: '1px solid var(--border-dim, #27272a)',
          textAlign: 'left',
        }}>
          <p style={{
            fontSize: '13px',
            color: 'var(--text-muted, #71717a)',
            margin: '0 0 8px',
            lineHeight: 1.6,
          }}>
            The access code is displayed in the terminal where the server is running.
            Look for:
          </p>
          <code style={{
            display: 'block',
            padding: '10px 14px',
            borderRadius: '8px',
            background: 'var(--bg-void, #0a0a0f)',
            color: 'var(--accent-green, #22c55e)',
            fontSize: '13px',
            fontFamily: 'monospace',
            letterSpacing: '0.1em',
          }}>
            ACCESS CODE: ????????
          </code>
          <p style={{
            fontSize: '12px',
            color: 'var(--text-muted, #71717a)',
            margin: '10px 0 0',
            opacity: 0.7,
          }}>
            Once entered, this device will be remembered permanently.
          </p>
        </div>

        {/* CSS Animations */}
        <style>{`
          @keyframes shake {
            0%, 100% { transform: translateX(0); }
            10%, 30%, 50%, 70%, 90% { transform: translateX(-4px); }
            20%, 40%, 60%, 80% { transform: translateX(4px); }
          }
          @keyframes spin {
            to { transform: rotate(360deg); }
          }
          @keyframes fadeIn {
            from { opacity: 0; transform: translateY(-4px); }
            to { opacity: 1; transform: translateY(0); }
          }
        `}</style>
      </div>
    </div>
  );
}

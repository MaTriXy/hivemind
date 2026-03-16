# Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability in Hivemind, please report it responsibly. **Do not open a public GitHub issue.**

Please report vulnerabilities via [GitHub Security Advisories](https://github.com/cohen-liel/hivemind/security/advisories/new). Include:

1. A description of the vulnerability
2. Steps to reproduce the issue
3. The potential impact
4. Any suggested fixes (optional)

We will acknowledge your report within 48 hours and provide a detailed response within 7 days.

## Supported Versions

| Version | Supported |
|---|---|
| Latest release | Yes |
| Previous minor | Security fixes only |
| Older versions | No |

## Security Measures

Hivemind implements the following security controls:

- **Device Authentication** — Zero-password auth with cryptographically secure one-time codes (`secrets` module, not `random`)
- **Project Isolation** — Agents are sandboxed to their project directory with multi-layer enforcement
- **Rate Limiting** — API endpoints are rate-limited to prevent brute force attacks
- **Content Security Policy** — Strict CSP headers on all dashboard responses
- **Input Validation** — All user inputs are validated and sanitized
- **No Secrets in Code** — All sensitive configuration is loaded from environment variables

## Security Best Practices for Users

### API Keys

- Never commit API keys to version control
- Use environment variables for all sensitive configuration
- Rotate your Claude API key if you suspect it has been compromised

### Device Authentication

- Regularly review approved devices in Settings
- Revoke access for devices you no longer use
- Use the access code rotation feature periodically

### Network Security

- Run Hivemind behind a reverse proxy (nginx/caddy) in production
- Use HTTPS when exposing the dashboard to the internet
- Restrict dashboard access to trusted networks when possible

### File System

- Enable sandbox mode (`SANDBOX_ENABLED=true`) to restrict file access
- Set `CLAUDE_PROJECTS_DIR` to limit which directories agents can access
- Review agent-generated code before deploying to production

## Scope

The following are **in scope** for security reports:

- Authentication bypass
- Unauthorized access to projects or data
- Remote code execution vulnerabilities
- Path traversal attacks
- WebSocket security issues
- API key exposure

The following are **out of scope**:

- Vulnerabilities in third-party dependencies (report to the upstream project)
- Issues that require physical access to the machine
- Social engineering attacks
- Denial of service attacks against local installations

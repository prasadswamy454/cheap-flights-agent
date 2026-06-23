# Security

## Secrets

- Keep API keys, database passwords, and connection strings in `.env`.
- `.env`, generated databases, virtual environments, and caches are excluded
  from Git.
- Do not use the placeholder values from `.env.example` in a public deployment.
- Rotate a credential immediately if it is ever committed, even if the commit
  is later removed.

## Reporting

Please report suspected vulnerabilities privately through GitHub's
**Security** tab using a private vulnerability report. Do not open a public
issue containing credentials or exploit details.


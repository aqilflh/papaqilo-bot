# Security

This bot handles personal data (notes, schedules, chat history) and runs with
API keys. If you find a vulnerability, please do not open a public issue with
exploit details.

## Reporting

Email the maintainer with a description, reproduction steps, and impact. You
will get an acknowledgment within a few days.

## Scope notes

- The LLM is intentionally given no shell, file, or arbitrary-network tools.
- All tool output (RSS, weather, web text) is treated as untrusted data.
- Multi-user isolation is enforced in the data layer; please report any path
  that bypasses `user_id` filtering.
- Secrets must live in `.env` only. Never commit `.env` or anything under
  `data/`.

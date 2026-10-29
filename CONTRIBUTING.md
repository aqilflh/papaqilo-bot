# Contributing

Thanks for your interest. This project started as a personal assistant, so
please keep changes focused and well-tested.

## Ground rules

- Python 3.12+, type hints everywhere.
- `ruff check .` and `pytest` must pass before a PR is considered done.
- Never commit `.env`, `data/`, or any file containing tokens/keys.
- Treat all external text (RSS, weather, web pages, job postings) as untrusted
  data, never as instructions.
- The LLM gets no shell, file, or arbitrary-network tools. Do not add any.
- Multi-user isolation: every data-access function takes `user_id` first and
  filters by it. Add a test proving isolation when you touch data access.
- Each pipeline stage must stay idempotent and runnable on its own.

## Development

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env             # fill in your own keys
pytest -q
```

## Pull requests

- One concern per PR; small diffs are easier to review.
- Add or update tests for behavior changes.
- Update `README.md` / `docs/` when user-facing behavior changes.
- Do not include real tokens, chat IDs, or personal data in commits, tests,
  or fixtures.

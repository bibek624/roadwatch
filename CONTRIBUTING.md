# Contributing

This is a hackathon submission, not a maintained product. The repo is public
under MIT for transparency and judging — not because there's an active
roadmap.

That said: PRs that fix bugs, improve the README, or share corridor results
are welcome. If you build something interesting on top of it, I'd love to
hear about it — open an issue.

## Local dev quick reference

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # add MAPILLARY_TOKEN + ANTHROPIC_API_KEY
uvicorn app.main:app --reload
```

## Style

- Black-formatted Python where it isn't actively annoying.
- No tests yet — this is a 5-day hackathon build. The validation strategy
  was end-to-end runs against real corridors, captured in PROGRESS.md.

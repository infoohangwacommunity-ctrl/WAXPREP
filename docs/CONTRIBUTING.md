# Contributing to Wax Prep

## Before changing anything

1. Inspect the whole repository.
2. Check whether the requested capability already exists.
3. Check the request against the philosophy and architecture.
4. State the plan, affected areas, risks, and tests.
5. Discuss the plan before implementation.

## During implementation

- Keep changes small and understandable.
- Keep the quality gate green.
- Add tests for changed behaviour.
- Use fake data in tests.
- Never commit real student data or secrets.
- Never make paid API calls from the normal quality gate.
- Record significant decisions.
- Avoid dependencies that solve no current requirement.
- Respect the third-party licence policy.

## Quality gate

Run:

```bash
make check
```

It checks:

- formatting
- linting
- types
- tests

GitHub Actions runs the same quality gate for pushes to main and pull requests targeting main.

## Paid tests

Paid provider tests must use pytest's `live` marker.

They must require explicit opt-in.

They must never be part of `make check`.

## Completion

A feature is not complete because the code looks correct.

The relevant tests must pass.

GitHub Actions must provide the CI evidence.

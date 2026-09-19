# sydes-examples/sydes-action

Central integration point for public Sydes demo repositories under the
[sydes-examples](https://github.com/sydes-examples) organization.

This repository hosts, in one place, the pieces every example repo would
otherwise have to copy:

- [`.github/workflows/verify.yml`](.github/workflows/verify.yml) —
  the reusable GitHub Actions workflow that runs `sydes verify-change` on a
  PR, renders the result, upserts a persistent PR comment, writes the job
  summary, and uploads the artifact.
- [`scripts/render_sydes_pr.py`](scripts/render_sydes_pr.py) — the
  deterministic, generic renderer that turns a `sydes-result.json` into the
  Markdown used for both the PR comment and the job summary.
- [`CONTRACT.md`](CONTRACT.md) — the frozen v1 artifact/presentation
  contract this workflow and renderer implement.

## Using this from an example repository

```yaml
name: Sydes

on:
  pull_request:
    branches: [ "master" ]   # or your default branch

permissions:
  contents: read
  pull-requests: write

jobs:
  sydes:
    uses: sydes-examples/sydes-action/.github/workflows/verify.yml@main
    with:
      repo_alias: app
    secrets:
      OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}
```

That's the entire integration a new example repository needs.

## Why this lives in its own repository

This integration previously lived in the `sydes-examples/.github` organization
repository. That worked, but had two problems as a public product entrypoint:
the resulting `uses:` reference had an awkward double `.github`
(`sydes-examples/.github/.github/workflows/sydes-verify.yml@main`), and an
organization `.github` repo is conventionally reserved for org-wide community
health files, not treated as a product's public entrypoint. `sydes-action` is
a dedicated home with a clean, product-shaped reference:
`sydes-examples/sydes-action/.github/workflows/verify.yml@main`.

This is a location/packaging move only — the workflow steps, renderer
semantics, and `CONTRACT.md` are unchanged from what `sydes-examples/.github`
shipped.

## Why centralize here

Before this repository existed, every example repo carried its own full copy
of the workflow steps and the renderer script. That meant a bug fix or
presentation change had to be repeated, by hand, in every repository — which
does not scale past one demo. Centralizing here means:

- one place to fix or improve the renderer,
- one place to version the presentation contract (`CONTRACT.md`),
- and a new example repo needs only a ~15-line caller workflow, not a copy of
  the implementation.

## Versioning

For now, example repos call this workflow at `@main`. That is intentionally
simple for v1 — every consumer currently tracks the latest version. Pinning
consumers to a tag or SHA once this needs to stabilize independently of
in-flight changes is a natural next step, not done yet.

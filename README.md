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
    uses: sydes-examples/sydes-action/.github/workflows/verify.yml@v2
    with:
      repo_alias: app
    secrets:
      OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}
```

That's the entire integration a new example repository needs. `@v1` keeps working
unchanged; v2 adds only opt-in inputs.

## Sydes Runtime Evidence — Beta (v2)

Add one input to also run the repository's own tests against the change and report what they
actually executed:

```yaml
    with:
      repo_alias: app
      runtime_evidence: auto
```

With `auto`, Sydes (0.3.0+, with [DiffGenome](https://pypi.org/project/diffgenome/) 0.1.8+)
detects the Python project, prepares a test environment from its declared dependencies,
selects the relevant tests (the PR's changed tests, then tests calling the changed functions,
then one level of callers), and runs them sandboxed. The comment gains a **Runtime evidence**
section; the job summary records what was detected and the provenance (Sydes and DiffGenome
versions, sandbox backend).

Supported: Python with pytest (including unittest and pytest-django suites), on Linux
(bubblewrap, which this workflow installs and enables) and macOS (Seatbelt) runners. It is
not universal zero-config. Harder repositories describe what cannot be inferred in a
`.sydes.yml` at the repository root, for example service settings:

```yaml
runtime:
  env: {DATABASE_HOST: 127.0.0.1}
  allow_loopback: true
```

Known limitations:

- on Linux the sandbox's loopback cannot reach services on the host (a database started by
  the job); tests that start their own localhost servers work;
- projects with a custom test-runner setup may need `.sydes.yml`;
- projects that cannot run on Python 3.12+ are not supported;
- AI recovery (`ai_recovery`, on by default as in v1) is separate and experimental, not part
  of Runtime Evidence; `ai_recovery: false` turns it off.

Other v2 inputs, all optional: `runner` (default `ubuntu-24.04`, as in v1),
`timeout_minutes` (default 45), `ai_recovery`, and, for advanced or non-Python use,
`runtime_evidence_args` and `runtime_setup`. `sydes_git_ref` installs an unreleased Sydes from
git, for validation only.

## Why this lives in its own repository

This integration previously lived in the `sydes-examples/.github` organization
repository. That worked, but had two problems as a public product entrypoint:
the resulting `uses:` reference had an awkward double `.github`
(`sydes-examples/.github/.github/workflows/sydes-verify.yml@main`), and an
organization `.github` repo is conventionally reserved for org-wide community
health files, not treated as a product's public entrypoint. `sydes-action` is
a dedicated home with a clean, product-shaped reference:
`sydes-examples/sydes-action/.github/workflows/verify.yml@v1`.

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

Example repos call this workflow at a version tag (`@v1`, `@v2`), each pinned to a
known-good commit rather than tracking `main`, with the renderer pinned to the same tag. This means an in-flight change to this
repository never silently changes behavior for existing consumers; a new
tag (`v2`, `v3`, etc.) is cut, and consumers move to it deliberately,
the same way any other reusable-action reference is versioned.

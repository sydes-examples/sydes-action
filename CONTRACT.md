# Sydes GitHub Actions integration — v1 contract

This is the frozen v1 presentation/artifact contract for the reusable
[`verify.yml`](.github/workflows/verify.yml) workflow and
[`render_sydes_pr.py`](scripts/render_sydes_pr.py). A repository consuming
this workflow, or a tool consuming its output, can rely on the following
without inspecting the implementation.

Changing any of this is a breaking change to every repository that calls the
reusable workflow — bump a major version marker (tag/ref) before changing it.

## Check name

The visible GitHub check is **`Sydes verify-change`**.

## Semantics

- **The check status** means "Sydes executed successfully" — nothing about
  the analysis result.
- **The Sydes verdict** (`VERIFIED` / `VERIFICATION INCOMPLETE` /
  `ACTION REQUIRED`) is the analysis conclusion, reported inside the comment
  and job summary, never as the check's pass/fail state.
- A green check with a `VERIFICATION INCOMPLETE` verdict is the expected,
  common case, not a contradiction. The rendered comment says this explicitly.

## PR comment

- Exactly one persistent comment per PR, upserted by a hidden marker:
  `<!-- sydes-verification-comment -->`
- Re-running the workflow updates that comment; it never creates a duplicate.
- The comment excludes internal diagnostics (CBM timings, graph-slice
  counts, route-graph internals, prompt sizes, guide counters, transport
  details). Those live only in the log and in `sydes-diagnostics.json`.

## Job summary

The same rendered Markdown is also written to `$GITHUB_STEP_SUMMARY`, so the
Actions run page is readable without opening raw logs or the PR.

## Artifact

One artifact named `sydes-result`, containing exactly:

| File | Contents |
| --- | --- |
| `sydes-result.json` | The full, unmodified `ChangeVerificationResult` Sydes wrote via `--json`. Includes `diagnostics`. |
| `sydes-comment.md` | The rendered Markdown posted as the PR comment / job summary. |
| `sydes-diagnostics.json` | `diagnostics`, `notes`, and `analysis_notes` extracted from the result, for debugging without reading the full JSON. |

`sydes-diagnostics.json` is a presentation-layer split, not a schema change —
`sydes-result.json` still carries `diagnostics` itself. True schema
separation (if ever done) belongs in Sydes, not in this rendering layer.

## Permissions

The reusable workflow requires, and the calling workflow must grant, exactly:

```yaml
permissions:
  contents: read
  pull-requests: write
```

No broader permission (`issues: write`, `contents: write`, `actions: write`,
`packages: write`) is used or required.

## External-fork PRs

This v1 contract assumes same-repository PRs (`pull_request` from a branch in
the same repo, as used by every current example). A `pull_request` from an
external fork receives a **read-only** token, so the comment-posting step
will fail there — that is expected under this contract, not a bug.

Do not casually switch the trigger to `pull_request_target` to "fix" this.
`pull_request_target` runs with the base repository's permissions against
attacker-controlled fork code, and needs a deliberate secure design (e.g.
running the untrusted analysis in a separate, permission-less job and only
posting the comment from a trusted job that never executes fork code) before
it is safe to adopt. That redesign is out of scope for v1.

## Renderer input assumptions

`render_sydes_pr.py` reads only the fields Sydes' `ChangeVerificationResult`
JSON schema already defines (`summary`, `change`, `pr_semantic_analysis`,
`code_findings`, `accepted_impacts`, `verification_gaps`, `analysis_notes`,
`runtime_dependencies`, `diagnostics`, `notes`). It tolerates any of them
being absent, malformed, or an unrecognized verdict string, and always
produces a renderable comment — including a `NOT PRODUCED` fallback body when
the result file itself is missing or unreadable.

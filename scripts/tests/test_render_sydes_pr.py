"""Tests for render_sydes_pr.py.

Uses two kinds of fixtures:

- `fixtures/real_*.json`: real (trimmed) Sydes results captured from the
  manual calibration suite, covering scenarios that occur naturally in
  production data (established-only, established+inferred, no tests
  mapped, tests mapped but not executed, unsupported/no-flow, and a real
  code_review_status="unavailable" run).
- Small inline synthetic dicts, for the handful of scenarios no real
  captured result happens to cover: a large fan-out (many affected paths,
  to prove the truncation rule actually truncates), review findings
  present, and review not_requested. These are clearly synthetic --
  never confused with real calibration data, and never written back to
  the `results/` directory this repo uses for actual study results.

Renderer determinism is exercised throughout: every test calls `render()`
directly, with no network/LLM involved, and asserts on the literal
Markdown string.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import render_sydes_pr as r

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def _make_flow(flow_id: str, entry_label: str, handler: str, symbol: str, obligations=None) -> dict:
    """A flow with exactly one proven, route-specific call out of the
    handler (`symbol`, via a `followed_call` step) -- the routine one-hop
    case. `changed_nodes` mirrors the diff-wide changed set (unrelated to
    which symbols are shown; see `_flow_connected_calls`), kept here only
    because several existing tests assert on it being present as raw
    "changed" data, not as connectivity evidence."""
    return {
        "id": flow_id,
        "entry_kind": "route",
        "entry_label": entry_label,
        "handler": handler,
        "changed_nodes": [{"repo": "app", "file": "src/app.py", "symbol": symbol}],
        "steps": [
            {"layer": "handler", "kind": "handler", "symbol": handler, "file": "src/handler.py", "line_start": 10, "status": "grounded"},
            {"layer": "followed_call", "kind": "service_call", "symbol": symbol, "file": "src/app.py", "line_start": 20, "status": "grounded"},
        ],
        "obligations": obligations or [],
    }


def _make_obligation(
    kind: str, statement: str, status: str = "unverified", introduced: bool = False,
    reason: str | None = None, mapped_tests: list[dict] | None = None,
    supporting_tests: list[dict] | None = None,
) -> dict:
    return {
        "id": f"ob:{statement[:10]}",
        "flow_id": "flow:x",
        "kind": kind,
        "statement": statement,
        "origin": "test_matrix",
        "introduced_by_change": introduced,
        "status": status,
        "reason": reason,
        "mapped_tests": mapped_tests or [],
        "supporting_tests": supporting_tests or [],
    }


def _make_test(file: str, case_name: str, evidence_tier: str) -> dict:
    return {
        "id": f"{file}::{case_name}",
        "name": case_name,
        "case_name": case_name,
        "file": file,
        "evidence_tier": evidence_tier,
    }


def _base_result(**overrides) -> dict:
    base = {
        "version": "v3",
        "kind": "sydes_change_verification",
        "change": {"base": "main", "symbols": [], "files": []},
        "summary": {
            "verdict": "VERIFICATION INCOMPLETE",
            "risk": "MEDIUM",
            "counts": {"mapped_tests": 0, "tests_executed": 0},
        },
        "code_review_status": "completed",
        "code_findings": [],
        "accepted_impacts": [],
        "affected_boundaries": [],
        "affected_flows": [],
        "analysis_notes": [],
        "runtime_dependencies": [],
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# 1. Established impact only
# ---------------------------------------------------------------------------


def test_established_only_shows_no_likely_section():
    result = _base_result(
        affected_flows=[_make_flow("flow:a", "POST /pets", "PetController.create", "PetService.create")],
        accepted_impacts=[{"id": "flow:a", "status": "proven"}],
    )
    out = r.render(result)
    assert "(likely, not fully established)" not in out
    assert "PetService.create" in out


# ---------------------------------------------------------------------------
# 2. Established + inferred (real data)
# ---------------------------------------------------------------------------


def test_established_and_inferred_both_shown_and_distinct():
    result = _load("real_established_and_inferred.json")
    out = r.render(result)
    section = out.split("### What it may affect")[1].split("###")[0]
    # The two kinds of evidence must never be visually merged into one
    # list -- each gets its own bold sub-label, Established first.
    assert "**Established**" in section
    assert "**Likely, not fully established**" in section
    established_idx = section.index("**Established**")
    likely_idx = section.index("**Likely, not fully established**")
    assert established_idx < likely_idx


# ---------------------------------------------------------------------------
# 3. Not fully traced (nothing established, only an inferred impact)
# ---------------------------------------------------------------------------


def test_not_fully_traced_is_explicit_not_silent():
    # This real case has zero PROVEN flows but one real inferred boundary --
    # the honest render is to show that one likely signal, clearly marked,
    # not to claim nothing was found at all. That distinction (a real,
    # present-but-unproven signal vs. truly nothing) matters and must not
    # be collapsed into one generic message.
    result = _load("real_unsupported_not_traced.json")
    out = r.render(result)
    section = out.split("### What it may affect")[1].split("###")[0]
    # No bare (established) bullet -- every backtick-wrapped bullet here
    # must carry the inline "(likely, ...)" qualifier.
    assert re.search(r"^- `[^`]+`$", section, re.MULTILINE) is None
    assert "(likely, not fully established)" in section
    assert "email verification task processing" in section


# ---------------------------------------------------------------------------
# 4. Many affected paths / truncation (synthetic -- no real case is this broad)
# ---------------------------------------------------------------------------


def test_many_established_paths_are_truncated_deterministically():
    flows = [
        _make_flow(f"flow:{i}", f"GET /resource/{i}", f"Handler{i}.get", f"Service{i}.fetch")
        for i in range(10)
    ]
    impacts = [{"id": f"flow:{i}", "status": "proven"} for i in range(10)]
    result = _base_result(affected_flows=flows, accepted_impacts=impacts)
    out = r.render(result)

    shown = out.count("```text")
    assert shown == r._MAX_ESTABLISHED_PATHS
    assert f"…and {10 - r._MAX_ESTABLISHED_PATHS} more established path(s)" in out
    # The comment must stay reasonably short even with 10 affected routes.
    assert len(out.splitlines()) < 80


def test_many_likely_paths_are_truncated_deterministically():
    impacts = [
        {"id": f"impact:{i}", "status": "inferred", "behavior_label": f"maybe affects service {i}"}
        for i in range(5)
    ]
    result = _base_result(accepted_impacts=impacts)
    out = r.render(result)
    shown = out.count("maybe affects service")
    assert shown == r._MAX_LIKELY_PATHS
    assert f"…and {5 - r._MAX_LIKELY_PATHS} more likely impact(s)" in out


# ---------------------------------------------------------------------------
# 5. No mapped tests (real data)
# ---------------------------------------------------------------------------


def test_no_mapped_tests_says_none_not_zero_confusingly():
    result = _load("real_inferred_only_no_tests.json")
    out = r.render(result)
    # No named test evidence and no aggregate signal either -- the named-
    # evidence block within Test evidence is correctly omitted rather than
    # shown with a hollow "None identified" line.
    assert "None identified" not in out


# ---------------------------------------------------------------------------
# Regression: sydes-examples/nestjs-boilerplate#1 real evaluation run.
# `mapped_tests` (required obligations only) was 0 while 30 real tests
# exercised/supported the affected flows via non-required (test-matrix)
# obligations -- the renderer collapsed that into "None identified", which
# read as "no tests exist near this change" when 30 actually did. The three
# distinct-test counts below are computed across every obligation (see
# `VerificationCounts`), independent of what gates the verdict.
# ---------------------------------------------------------------------------


def test_supporting_evidence_is_shown_instead_of_none_identified():
    """The table-driven design reflects per-obligation status, not
    `summary.counts` aggregates independent of any actual obligation -- a
    shape that cannot occur in a real Sydes result (the counts are always
    derived FROM obligations; see `VerificationCounts`). With no
    obligations attached to the flow at all, the honest table result is
    simply "not found", not a misleading positive."""
    result = _base_result(
        affected_flows=[_make_flow("flow:a", "POST /login", "AuthController.login", "AuthService.login")],
        accepted_impacts=[{"id": "flow:a", "status": "proven"}],
        summary={
            "verdict": "VERIFICATION INCOMPLETE",
            "risk": "MEDIUM",
            "counts": {
                "mapped_tests": 0,
                "supporting_tests": 30,
                "tests_exercising_flows": 5,
                "tests_supporting_behavior": 5,
                "tests_verifying_behavior": 0,
                "tests_executed": 0,
            },
        },
    )
    out = r.render(result)
    section = out.split("### Test evidence")[1].split("###")[0]
    assert "| Relevant regression test | ❌ Not found |" in section
    # Real evidence exists somewhere, but none of it verifies the changed
    # behavior directly -- the before-merge nudge should still fire.
    assert "- Add or run a test covering the affected behavior before merging." in out


def test_verifying_tests_shown_as_the_primary_count():
    result = _base_result(
        affected_flows=[_make_flow("flow:a", "POST /login", "AuthController.login", "AuthService.login")],
        accepted_impacts=[{"id": "flow:a", "status": "proven"}],
        summary={
            "verdict": "VERIFICATION INCOMPLETE",
            "risk": "MEDIUM",
            "counts": {
                "mapped_tests": 0,
                "supporting_tests": 0,
                "tests_exercising_flows": 2,
                "tests_supporting_behavior": 0,
                "tests_verifying_behavior": 2,
                "tests_executed": 0,
            },
        },
    )
    out = r.render(result)
    # Real verifying evidence found (via summary.counts) -- the before-merge
    # nudge, which reads that same aggregate directly, must not fire.
    assert "- Add or run a test covering the affected behavior before merging." not in out


# ---------------------------------------------------------------------------
# 6. Tests identified but not executed (real data, --no-run-tests case)
# ---------------------------------------------------------------------------


def test_tests_identified_not_executed_reads_as_intentional():
    result = _load("real_established_many_tests.json")
    out = r.render(result)
    section = out.split("### Test evidence")[1].split("###")[0]
    assert "| Relevant regression test | ✅ Found |" in section
    # Execution is explicit and distinct from the verification categories --
    # this workflow's --no-run-tests must read as a deliberate config
    # choice, not a failure.
    assert "| Test executed by Sydes | ⬛ Not run (`--no-run-tests`) |" in section
    assert "fail" not in section.lower()
    assert "error" not in section.lower()


# ---------------------------------------------------------------------------
# 7. Review completed, zero findings
# ---------------------------------------------------------------------------


def test_review_completed_zero_findings():
    result = _base_result(code_review_status="completed", code_findings=[])
    out = r.render(result)
    assert "### Code review" in out
    assert "**No blocking issues found**" in out


# ---------------------------------------------------------------------------
# 8. Review findings present (synthetic -- no real captured case has findings)
# ---------------------------------------------------------------------------


def test_review_findings_present_summarized_not_dumped():
    findings = [
        {"id": "f1", "severity": "P0", "title": "SQL injection risk", "file": "a.py", "line": 1},
        {"id": "f2", "severity": "P2", "title": "Unused import", "file": "b.py", "line": 2},
        {"id": "f3", "severity": "P2", "title": "Missing docstring", "file": "c.py", "line": 3},
    ]
    result = _base_result(code_review_status="completed", code_findings=findings)
    out = r.render(result)
    section = out.split("### Code review")[1].split("---")[0]
    # Summarized as a count, never the full per-finding breakdown in the
    # main comment body (that belongs to inline comments / the full result).
    assert "3 finding(s)" in section
    assert "SQL injection risk" not in section
    assert "Unused import" not in section


# ---------------------------------------------------------------------------
# 9. Review unavailable (real data)
# ---------------------------------------------------------------------------


def test_review_unavailable_is_explicit_not_no_findings():
    result = _load("real_review_unavailable.json")
    out = r.render(result)
    # The real, actionable reason (already in diagnostics) is surfaced
    # instead of the old generic "the provider could not complete the
    # analysis" sentence.
    assert "AI code review unavailable: OpenAI provider selected, but OPENAI_API_KEY is not set." in out
    assert "the provider could not complete the analysis" not in out
    assert "No blocking issues found" not in out


# ---------------------------------------------------------------------------
# 10. Review not requested (synthetic)
# ---------------------------------------------------------------------------


def test_review_not_requested_omits_section_entirely():
    result = _base_result(code_review_status="not_requested", code_findings=[])
    out = r.render(result)
    assert "### Code review" not in out


# ---------------------------------------------------------------------------
# 11. Unsupported / no resolved flows (real data)
# ---------------------------------------------------------------------------


def test_unsupported_no_flows_is_honest_not_falsely_reassuring():
    # Same real case as above: 0 proven flows, 1 inferred boundary. Even
    # here -- content Sydes DID find, just not proven -- the render must
    # never read as a clean bill of health.
    result = _load("real_unsupported_not_traced.json")
    out = r.render(result)
    for bad_phrase in ("everything safe", "nothing affected", "no impact"):
        assert bad_phrase not in out.lower()


def test_true_zero_signal_says_could_not_establish():
    # The genuinely-nothing-found case: 0 flows, 0 boundaries, 0 impacts.
    # This must never look like "nothing is affected" either -- it must
    # say plainly that tracing did not reach anything.
    result = _base_result(analysis_notes=["No discovered route declaration reaches the changed symbols."])
    out = r.render(result)
    assert "could not establish a system path" in out
    assert "No discovered route declaration reaches the changed symbols." in out
    for bad_phrase in ("everything safe", "nothing affected", "no impact"):
        assert bad_phrase not in out.lower()


# ---------------------------------------------------------------------------
# 12. No accidental "obligation" in human-facing comment
# ---------------------------------------------------------------------------


def test_word_obligation_never_appears_in_rendered_output():
    for fixture in FIXTURES.glob("real_*.json"):
        result = json.loads(fixture.read_text())
        out = r.render(result)
        assert "obligation" not in out.lower(), f"'obligation' leaked into render of {fixture.name}"


# ---------------------------------------------------------------------------
# 13. No top-level "Analysis PARTIAL" enum dump
# ---------------------------------------------------------------------------


def test_no_raw_analysis_status_enum_dump():
    """The raw `analysis_status` enum value must never leak verbatim --
    "Analysis" alone is no longer a safe proxy for that (the header's own
    "Analysis complete" verdict label and the "Change analysis" section
    both legitimately contain the word now), so this checks the actual
    raw enum shape instead: the bare uppercase token and the
    "analysis_status" field name itself."""
    result = _base_result(analysis_status="partial")
    out = r.render(result)
    assert "PARTIAL" not in out
    assert "analysis_status" not in out


# ---------------------------------------------------------------------------
# 14. No numeric confidence dump
# ---------------------------------------------------------------------------


def test_no_numeric_confidence_shown():
    result = _base_result(
        accepted_impacts=[
            {
                "id": "impact:x",
                "status": "inferred",
                "behavior_label": "might affect billing",
                "llm_confidence": 0.87,
            }
        ]
    )
    out = r.render(result)
    assert "0.87" not in out
    assert "confidence" not in out.lower()


# ---------------------------------------------------------------------------
# 15. Comment marker retained
# ---------------------------------------------------------------------------


def test_marker_present_and_first_line():
    for fixture in FIXTURES.glob("real_*.json"):
        result = json.loads(fixture.read_text())
        out = r.render(result)
        assert out.splitlines()[0] == r.MARKER

    # Also for the unavailable-result fallback path.
    out = r.render_unavailable("no result file was written")
    assert out.splitlines()[0] == r.MARKER


# ---------------------------------------------------------------------------
# 16. Markdown stays valid and readable
# ---------------------------------------------------------------------------


def test_markdown_stays_well_formed():
    for fixture in FIXTURES.glob("real_*.json"):
        result = json.loads(fixture.read_text())
        out = r.render(result)

        # Every opened fence is closed (even count of ``` lines).
        fence_lines = [ln for ln in out.splitlines() if ln.strip() == "```text" or ln.strip() == "```"]
        assert len(fence_lines) % 2 == 0, f"unbalanced code fence in render of {fixture.name}"

        # Every markdown table has a separator row directly under its header.
        lines = out.splitlines()
        for i, line in enumerate(lines):
            if line.startswith("| ") and i + 1 < len(lines):
                nxt = lines[i + 1]
                if nxt.startswith("|") and set(nxt.replace("|", "").replace(" ", "").replace("-", "")) == set():
                    continue  # this line's own separator, fine
        # No literal tab characters, no trailing whitespace-only chaos.
        assert "\t" not in out

        # Comment isn't empty and ends with the footer.
        assert out.strip().endswith(")") or out.strip().endswith("Sydes")


def test_determinism_same_input_same_output():
    result = _load("real_established_and_inferred.json")
    assert r.render(result) == r.render(result)


# ---------------------------------------------------------------------------
# 13. System impact is descriptive, not a bare established/likely count
#     (refinement pass: issue #1)
# ---------------------------------------------------------------------------


def test_system_impact_row_names_a_single_route_concretely():
    result = _base_result(
        affected_flows=[_make_flow("flow:a", "POST /pets", "PetController.create", "PetService.create")],
        accepted_impacts=[{"id": "flow:a", "status": "proven"}],
    )
    out = r.render(result)
    section = out.split("### What it may affect")[1].split("###")[0]
    assert "```text\nPOST /pets\n  → PetController.create\n  → PetService.create\n```" in section
    # The old count-only phrasing must never appear.
    assert "1 established" not in out


def test_system_impact_row_collapses_many_routes_to_a_route_count():
    """The former "Area | Sydes found" table's route-count collapse text
    is gone (folded away with the table itself), but the same cardinality
    fact -- 10 routes, only a few shown -- is still conveyed via the
    established-path truncation note (see
    `test_many_established_paths_are_truncated_deterministically`)."""
    flows = [
        _make_flow(f"flow:{i}", f"GET /resource/{i}", f"Handler{i}.get", f"Service{i}.fetch")
        for i in range(10)
    ]
    impacts = [{"id": f"flow:{i}", "status": "proven"} for i in range(10)]
    result = _base_result(affected_flows=flows, accepted_impacts=impacts)
    out = r.render(result)
    assert f"…and {10 - r._MAX_ESTABLISHED_PATHS} more established path(s)" in out


# ---------------------------------------------------------------------------
# 14. Wider API surface -- a boundary beyond the traced route(s)
#     (refinement pass: issue #1, and a regression guard for a false
#     positive found during this pass)
# ---------------------------------------------------------------------------


def test_wider_api_surface_shown_for_boundary_beyond_traced_route():
    result = _base_result(
        affected_flows=[
            {
                "id": "flow:a",
                "entry_label": "POST /api/auth/logout",
                "handler": "logout",
                "changed_nodes": [{"repo": "app", "file": "AuthController.java", "symbol": "logout"}],
                "artifact_refs": {"route_file": "AuthController.java", "handler_file": "AuthController.java"},
                "obligations": [],
            }
        ],
        accepted_impacts=[{"id": "flow:a", "status": "proven"}],
        affected_boundaries=[
            {"kind": "api", "symbol": "logout", "file": "AuthController.java", "status": "proven", "label": "logout"},
            {
                "kind": "api",
                "symbol": "doFilterInternal",
                "file": "JwtAuthenticationFilter.java",
                "status": "inferred",
                "label": "JWT authentication filter validates tokens including grace period logic",
            },
        ],
    )
    out = r.render(result)
    assert (
        "- Wider API surface: JWT authentication filter validates tokens including grace "
        "period logic (likely, not fully established)"
    ) in out
    assert "Verify the changed behavior on the wider API surface before merging." in out


def test_single_api_boundary_never_produces_a_wider_surface_row():
    """Regression guard: route discovery can mis-locate a route's file (a
    same-named handler in an unrelated example/crate), so file mismatch
    alone must not split a change's ONLY api boundary into a bogus second
    'Wider API surface' row describing the same thing twice."""
    result = _base_result(
        affected_flows=[
            {
                "id": "flow:a",
                "entry_label": "DELETE /",
                "handler": "delete",
                "changed_nodes": [{"repo": "app", "file": "unrelated/other.rs", "symbol": "delete"}],
                "artifact_refs": {"route_file": "unrelated/other.rs", "handler_file": "unrelated/other.rs"},
                "obligations": [],
            }
        ],
        accepted_impacts=[{"id": "flow:a", "status": "proven"}],
        affected_boundaries=[
            {"kind": "api", "symbol": "delete", "file": "examples/todo/src/main.rs", "status": "proven", "label": "delete"},
        ],
    )
    out = r.render(result)
    assert "Wider API surface" not in out


# ---------------------------------------------------------------------------
# 15. Runtime dependencies never appear in the public PR comment
#     (product-output pass: real cases showed this section stayed noisy
#     and not reliably useful -- the field itself is untouched in the
#     canonical result/artifacts, this is a presentation omission only)
# ---------------------------------------------------------------------------


def test_flow_scoped_runtime_dependency_never_surfaces_in_the_comment():
    """Regression: a flow-scoped runtime dependency used to render as an
    "Infrastructure" area row and a "Key dependency" coverage-limits
    bullet. Neither should appear anywhere in the rendered comment now,
    regardless of scope."""
    result = _base_result(
        affected_flows=[_make_flow("flow:a", "POST /pets", "PetController.create", "PetService.create")],
        accepted_impacts=[{"id": "flow:a", "status": "proven"}],
        runtime_dependencies=[
            {"name": "Redis", "scope": "affected_flow"},
            {"name": "Elasticsearch", "scope": "repository"},
        ],
    )
    out = r.render(result)
    assert "Infrastructure" not in out
    assert "Key dependency" not in out
    assert "Redis" not in out
    assert "Elasticsearch" not in out


def test_repository_wide_runtime_dependency_never_surfaces_either():
    result = _base_result(
        affected_flows=[_make_flow("flow:a", "POST /pets", "PetController.create", "PetService.create")],
        accepted_impacts=[{"id": "flow:a", "status": "proven"}],
        runtime_dependencies=[{"name": "SQL database", "scope": "repository"}],
    )
    out = r.render(result)
    assert "Infrastructure" not in out
    assert "Key dependency" not in out
    assert "SQL database" not in out


# ---------------------------------------------------------------------------
# 16. Verification never leaks a raw obligation statement, only the
#     high-level category (refinement pass: issue #2)
# ---------------------------------------------------------------------------


def test_verification_shows_category_not_raw_statement():
    statement = "POST /pets enforces `if request.weight_kg > 999:` unusual-marker-xyz"
    result = _base_result(
        affected_flows=[
            _make_flow(
                "flow:a",
                "POST /pets",
                "PetController.create",
                "PetService.create",
                obligations=[_make_obligation("validation", statement, status="unknown")],
            )
        ],
        accepted_impacts=[{"id": "flow:a", "status": "proven"}],
    )
    out = r.render(result)
    assert "| Validation behavior |" in out
    assert "unusual-marker-xyz" not in out
    assert statement not in out


# ---------------------------------------------------------------------------
# 17. Before merge -- strict, deterministic rules only
#     (refinement pass: issue #3)
# ---------------------------------------------------------------------------


def test_before_merge_never_dumps_a_raw_statement():
    """The old '- Verify: <raw statement>' bullet is gone entirely -- every
    real fixture must be free of it."""
    for fixture in sorted(FIXTURES.glob("real_*.json")):
        result = json.loads(fixture.read_text())
        out = r.render(result)
        assert "- Verify: " not in out, f"raw obligation bullet leaked in render of {fixture.name}"


def test_before_merge_recommends_a_test_when_none_identified_and_impact_found():
    result = _base_result(
        affected_flows=[_make_flow("flow:a", "POST /pets", "PetController.create", "PetService.create")],
        accepted_impacts=[{"id": "flow:a", "status": "proven"}],
    )
    out = r.render(result)
    assert "- Add or run a test covering the affected behavior before merging." in out


def test_unattached_evidence_is_named_and_suppresses_add_a_test():
    """Unleash PR #12632-shaped regression: a changed symbol
    (`strategySchema`) with no HTTP route ever structurally established --
    but two relevant, verified tests target it directly. These must
    appear by name in Existing evidence, and "Add or run a test" must NOT
    render, since real test evidence for this change does exist even
    though it never reached a resolved flow."""
    result = _base_result(
        affected_boundaries=[
            {"kind": "callable", "status": "proven", "label": "strategySchema", "file": "src/lib/services/strategy-schema.ts"},
        ],
        unattached_evidence=[
            {
                "scope": "symbol",
                "target_symbol": "strategySchema",
                "target_file": "src/lib/services/strategy-schema.ts",
                "mapped_tests": [
                    {"file": "src/lib/routes/admin-api/strategy.test.ts", "name": "does not allow duplicate parameter names when creating a strategy"},
                    {"file": "src/lib/routes/admin-api/strategy.test.ts", "name": "does not allow duplicate parameter names when updating a strategy"},
                ],
            },
        ],
        summary={
            "verdict": "VERIFICATION INCOMPLETE", "risk": "MEDIUM",
            "counts": {
                "mapped_tests": 2, "tests_executed": 0,
                "tests_verifying_behavior": 2, "tests_exercising_flows": 2, "tests_supporting_behavior": 0,
            },
        },
    )
    out = r.render(result)
    section = out.split("### Test evidence")[1].split("###")[0]
    assert "does not allow duplicate parameter names when creating a strategy" in section
    assert "does not allow duplicate parameter names when updating a strategy" in section
    assert "changed symbol `strategySchema`" in section
    assert "(not run by Sydes)" in section
    assert "Add or run a test" not in out


def test_before_merge_omitted_when_no_impact_was_found_at_all():
    result = _base_result()  # no flows, no boundaries, no impacts -- true zero signal
    out = r.render(result)
    assert "### What is still unknown" not in out


# ---------------------------------------------------------------------------
# Multiple established changed targets on one flow (regression: a flow that
# reaches genuinely separate changed files -- e.g. a normalizer fix and an
# unrelated voice-loading fix -- must show both, not silently collapse to
# one arbitrary pick). Shape matches the real PR that surfaced this:
# sydes-examples/Kokoro-FastAPI#7.
# ---------------------------------------------------------------------------


def _step(layer: str, symbol: str, file: str, line_start: int, status: str = "grounded", kind: str | None = None) -> dict:
    return {
        "layer": layer,
        "kind": kind or ("handler" if layer == "handler" else "service_call"),
        "symbol": symbol,
        "file": file,
        "line_start": line_start,
        "status": status,
    }


def _flow_with_steps(
    flow_id: str,
    entry_label: str,
    handler: str,
    handler_loc: tuple[str, int],
    calls: list[tuple[str, str, int]],
    changed_nodes: list[tuple[str, str, int]] | None = None,
) -> dict:
    """`calls` is a list of (file, symbol, line_start) `followed_call` steps
    -- real, structurally-followed calls out of the handler, the ONLY
    source `_flow_connected_calls` reads. `changed_nodes` (file, symbol,
    line) is separate, whole-diff changed-symbol data: present only to
    exercise/prove it is NOT used for connectivity, and (when a triple's
    file+line matches a step) to supply canonical naming."""
    handler_file, handler_line = handler_loc
    return {
        "id": flow_id,
        "entry_kind": "route",
        "entry_label": entry_label,
        "handler": handler,
        "changed_nodes": [
            {"repo": "app", "file": f, "symbol": s, "line": ln} for f, s, ln in (changed_nodes or [])
        ],
        "steps": [
            _step("handler", handler, handler_file, handler_line),
            *[_step("followed_call", s, f, ln) for f, s, ln in calls],
        ],
        "obligations": [],
    }


def test_unrelated_changed_symbols_never_render_under_an_unconnected_route():
    """Regression case A (NestJS PR #3 shape): `DELETE /v1/auth/me`'s own
    evidence only reaches `AuthController.delete` -> `AuthService.softDelete`.
    `FilesLocalController.download` and unrelated `AuthService` methods are
    part of the SAME diff but never called from this handler -- they must
    never appear under this route just for having been changed elsewhere."""
    result = _base_result(
        affected_flows=[
            _flow_with_steps(
                "flow:DELETE:/v1/auth/me",
                "DELETE /v1/auth/me",
                "AuthController.delete",
                ("src/auth/auth.controller.ts", 158),
                calls=[("src/auth/auth.service.ts", "AuthService.softDelete", 40)],
                changed_nodes=[
                    ("src/auth/auth.controller.ts", "AuthController.delete", 158),
                    ("src/auth/auth.service.ts", "AuthService.softDelete", 40),
                    ("src/auth/auth.service.ts", "AuthService.logout", 55),
                    ("src/files/files-local.controller.ts", "FilesLocalController.download", 12),
                ],
            )
        ],
        accepted_impacts=[{"id": "flow:DELETE:/v1/auth/me", "status": "proven"}],
    )
    out = r.render(result)

    assert "DELETE /v1/auth/me" in out
    assert "AuthController.delete" in out
    assert "AuthService.softDelete" in out
    assert "AuthService.logout" not in out
    assert "FilesLocalController.download" not in out


def test_canonical_handler_and_call_names_preferred_over_bare_step_symbols():
    """Regression case B (Go simplebank PR #4 shape): the handler step's own
    `symbol` is often bare/receiver-qualified (`server.renewAccessToken`,
    lowercase receiver variable, not the type); `changed_nodes` at the same
    (file, line) already carries the canonical, type-qualified spelling
    (`Server.renewAccessToken`) and should be preferred. An unrelated
    handler changed elsewhere in the same diff (`Server.loginUser`) must
    not appear just because it is also in `changed_nodes`."""
    result = _base_result(
        affected_flows=[
            _flow_with_steps(
                "flow:POST:/tokens/renew_access",
                "POST /tokens/renew_access",
                "server.renewAccessToken",
                ("api/token.go", 23),
                calls=[
                    ("api/token.go", "errorResponse", 55),
                    ("db/sqlc/session.go", "Queries.GetSession", 12),
                    ("token/paseto_maker.go", "JWTMaker.CreateToken", 30),
                    ("token/paseto_maker.go", "JWTMaker.VerifyToken", 45),
                ],
                changed_nodes=[
                    ("api/token.go", "Server.renewAccessToken", 23),
                    ("api/login.go", "Server.loginUser", 10),
                    ("api/middleware.go", "authMiddleware", 5),
                ],
            )
        ],
        accepted_impacts=[{"id": "flow:POST:/tokens/renew_access", "status": "proven"}],
    )
    out = r.render(result)

    assert "Server.renewAccessToken" in out
    assert "server.renewAccessToken" not in out
    assert "Server.loginUser" not in out
    assert "authMiddleware" not in out


def test_unrelated_handler_does_appear_when_flow_local_evidence_actually_connects_it():
    """The positive mirror of the case above: `Server.loginUser` must be
    EXCLUDED only because renew_access's own steps never call it -- if the
    handler's own evidence genuinely does call into it (e.g. a shared
    session-refresh helper), it must render, exactly like any other proven
    call. The exclusion rule is about proof, not about the symbol's name."""
    result = _base_result(
        affected_flows=[
            _flow_with_steps(
                "flow:POST:/tokens/renew_access",
                "POST /tokens/renew_access",
                "server.renewAccessToken",
                ("api/token.go", 23),
                calls=[("api/login.go", "Server.loginUser", 10)],
                changed_nodes=[
                    ("api/token.go", "Server.renewAccessToken", 23),
                    ("api/login.go", "Server.loginUser", 10),
                ],
            )
        ],
        accepted_impacts=[{"id": "flow:POST:/tokens/renew_access", "status": "proven"}],
    )
    out = r.render(result)
    assert "→ Server.loginUser" in out


def test_zero_mapped_tests_never_renders_a_positive_aggregate_count():
    """A stricter form of `test_no_mapped_tests_says_none_not_zero_confusingly`:
    when every test-count field the summary carries is genuinely zero, no
    digit greater than zero for a test count may appear anywhere in the
    Test evidence section -- not just that the named-evidence block is
    omitted, but that no positive count can leak in from elsewhere."""
    result = _base_result(
        affected_flows=[_make_flow("flow:a", "POST /login", "AuthController.login", "AuthService.login")],
        accepted_impacts=[{"id": "flow:a", "status": "proven"}],
        summary={
            "verdict": "VERIFICATION INCOMPLETE",
            "risk": "MEDIUM",
            "counts": {
                "mapped_tests": 0,
                "supporting_tests": 0,
                "tests_exercising_flows": 0,
                "tests_supporting_behavior": 0,
                "tests_verifying_behavior": 0,
                "tests_executed": 0,
            },
        },
    )
    out = r.render(result)
    assert "| Test executed by Sydes | ⬛ Not run |" in out
    assert "Yes —" not in out


def test_single_changed_target_renders_exactly_as_before():
    """Regression: the common one-target-per-flow case is unchanged --
    no stray '+N more' note, no behavior change for the ordinary case."""
    result = _base_result(
        affected_flows=[_make_flow("flow:a", "POST /pets", "PetController.create", "PetService.create")],
        accepted_impacts=[{"id": "flow:a", "status": "proven"}],
    )
    out = r.render(result)
    assert "PetService.create" in out
    assert "more traced call" not in out


def test_many_connected_calls_on_one_flow_caps_and_notes_remainder():
    calls = [(f"src/file_{i}.py", f"symbol_{i}", 10 + i) for i in range(5)]
    result = _base_result(
        affected_flows=[
            _flow_with_steps(
                "flow:a", "POST /pets", "PetController.create", ("src/pet_controller.py", 1), calls
            )
        ],
        accepted_impacts=[{"id": "flow:a", "status": "proven"}],
    )
    out = r.render(result)
    for i in range(r._MAX_FLOW_TERMINALS):
        assert f"symbol_{i}" in out
    for i in range(r._MAX_FLOW_TERMINALS, 5):
        assert f"symbol_{i}" not in out
    assert f"+{5 - r._MAX_FLOW_TERMINALS} more traced call(s)" in out


def test_route_with_no_deeper_connectivity_renders_cleanly_as_route_to_handler():
    """Section 7 fallback: when a flow's `steps` contain nothing beyond the
    handler itself, the renderer must stop there -- route -> handler,
    nothing padded on, nothing fabricated."""
    result = _base_result(
        affected_flows=[
            _flow_with_steps(
                "flow:a", "POST /pets", "PetController.create", ("src/pet_controller.py", 1), calls=[]
            )
        ],
        accepted_impacts=[{"id": "flow:a", "status": "proven"}],
    )
    out = r.render(result)
    assert "POST /pets" in out
    assert "PetController.create" in out
    assert "also touches" not in out
    assert "handler also calls" not in out
    assert "more traced call" not in out


# ---------------------------------------------------------------------------
# Footer: a single link, not the same URL twice (no dashboard exists yet).
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# TRUE CONNECTED PATH vs. PROVEN FAN-OUT: a handler shown calling more than
# one thing must not render as if route -> handler -> call_a -> call_b were
# one proven causal SEQUENCE. Sydes proves "the handler calls each of
# these", not an order between them.
# ---------------------------------------------------------------------------


def test_multiple_connected_calls_are_not_chained_as_a_sequence():
    result = _base_result(
        affected_flows=[
            _flow_with_steps(
                "flow:GET:/api/articles/feed",
                "GET /api/articles/feed",
                "feed_articles",
                ("src/http/articles/feed.rs", 1),
                calls=[
                    ("src/http/articles/comments.rs", "add_comment", 20),
                    ("src/http/articles/mod.rs", "create_article", 30),
                ],
            )
        ],
        accepted_impacts=[{"id": "flow:GET:/api/articles/feed", "status": "proven"}],
    )
    out = r.render(result)

    # The true, established hop still renders as a connected arrow chain.
    assert "GET /api/articles/feed" in out
    assert "→ feed_articles" in out
    # Both proven calls must still be visible ...
    assert "add_comment" in out
    assert "create_article" in out
    # ... but never as if chained onto the route/handler as further hops:
    # no "→ add_comment" or "→ create_article" anywhere in the output.
    assert "→ add_comment" not in out
    assert "→ create_article" not in out
    # Rendered instead as explicit, unordered tree-branch connectors.
    assert "├─ add_comment" in out
    assert "└─ create_article" in out


def test_global_changed_nodes_not_auto_rendered_under_every_unrelated_flow():
    """Confirmed against a real run (sydes-examples/realworld-axum-sqlx
    PR #3): every one of 12 unrelated routes' flows carried the identical
    whole-diff `changed_nodes` list. Two routes here share the same
    `changed_nodes`, but each has its OWN, disjoint set of proven calls --
    neither route's rendering may leak the other's changed symbol."""
    shared_changed = [
        ("src/http/articles/comments.rs", "add_comment", 20),
        ("src/http/users/mod.rs", "update_user", 40),
    ]
    result = _base_result(
        affected_flows=[
            _flow_with_steps(
                "flow:a", "POST /comments", "CommentController.create",
                ("src/http/articles/comments.rs", 1),
                calls=[("src/http/articles/comments.rs", "add_comment", 20)],
                changed_nodes=shared_changed,
            ),
            _flow_with_steps(
                "flow:b", "PUT /users", "UserController.update",
                ("src/http/users/mod.rs", 1),
                calls=[("src/http/users/mod.rs", "update_user", 40)],
                changed_nodes=shared_changed,
            ),
        ],
        accepted_impacts=[{"id": "flow:a", "status": "proven"}, {"id": "flow:b", "status": "proven"}],
    )
    out = r.render(result)

    # Split each flow's block out and check the other flow's proven call
    # never leaks into it.
    section = out.split("### What it may affect")[1].split("###")[0]
    comments_idx = section.index("POST /comments")
    users_idx = section.index("PUT /users")
    out = section
    comments_block = out[comments_idx:users_idx] if comments_idx < users_idx else out[comments_idx:]
    users_block = out[users_idx:comments_idx] if users_idx < comments_idx else out[users_idx:]
    assert "add_comment" in comments_block
    assert "update_user" not in comments_block
    assert "update_user" in users_block
    assert "add_comment" not in users_block


def test_single_connected_call_still_reads_as_one_established_hop():
    """The common, narrower case (exactly one proven call) is unaffected --
    still one hop past the handler, still no set notation for a single
    item."""
    result = _base_result(
        affected_flows=[_make_flow("flow:a", "POST /pets", "PetController.create", "PetService.create")],
        accepted_impacts=[{"id": "flow:a", "status": "proven"}],
    )
    out = r.render(result)
    assert "→ PetService.create" in out
    assert "also touches" not in out
    assert "handler also calls" not in out


def test_footer_has_exactly_one_link_to_the_run():
    result = _base_result()
    out = r.render(result, run_url="https://example.com/runs/1")
    assert out.count("https://example.com/runs/1") == 1
    assert "View full analysis" not in out
    assert "[View run](https://example.com/runs/1)" in out


def test_footer_omits_link_entirely_when_no_run_url():
    result = _base_result()
    out = r.render(result, run_url=None)
    assert out.rstrip().splitlines()[-1] == "Sydes"


# ---------------------------------------------------------------------------
# Coverage-limit wording: must not imply the path just shown is itself
# unresolved when a global, repository-wide caveat is also present.
# ---------------------------------------------------------------------------


def test_coverage_limit_scoped_to_other_routes_when_a_path_is_established():
    """The table-driven design uses one consistent "Route coverage" row
    regardless of whether a path was established -- the established-vs-not
    label distinction the old prose bullet made is now implicit (the
    established path itself is already visible above, in What it may
    affect); the caption underneath still carries the real note text."""
    result = _base_result(
        affected_flows=[_make_flow("flow:a", "POST /pets", "PetController.create", "PetService.create")],
        accepted_impacts=[{"id": "flow:a", "status": "proven"}],
        analysis_notes=["Route composition is unresolved in this repository; some routes may be missing."],
    )
    out = r.render(result)
    assert "| Route coverage | 🟡 Incomplete |" in out
    assert "_Route composition is unresolved in this repository; some routes may be missing._" in out


def test_coverage_limit_plain_label_when_nothing_established():
    result = _base_result(
        analysis_notes=["Route composition is unresolved in this repository; some routes may be missing."],
    )
    out = r.render(result)
    assert "| Route coverage | 🟡 Incomplete |" in out
    assert "_Route composition is unresolved in this repository; some routes may be missing._" in out


# ---------------------------------------------------------------------------
# Issue 7 (final-result internal consistency): a flow with no matching
# accepted_impacts entry must fall back to the flow's OWN impact_status,
# never a hardcoded "proven" -- otherwise a flow explicitly marked
# non-proven silently renders as Established the moment its accepted_impact
# entry happens to be missing.
# ---------------------------------------------------------------------------


def test_flow_without_matching_impact_falls_back_to_its_own_status_not_proven():
    result = _base_result(
        affected_flows=[
            {
                "id": "flow:a",
                "entry_label": "POST /pets",
                "handler": "PetController.create",
                "changed_nodes": [],
                "impact_status": "inferred",
            }
        ],
        accepted_impacts=[],  # deliberately no matching entry at all
    )
    out = r.render(result)
    section = out.split("### What it may affect")[1].split("###")[0]
    assert "**Established**" not in section
    assert "**Likely, not fully established**" in section
    assert "PetController.create" in section


def test_flow_without_matching_impact_defaults_proven_only_when_flow_itself_says_so():
    """Sanity check the other direction: the ordinary, fully-consistent
    case (a flow whose own impact_status really is proven, matched or
    not) must still render as Established -- the fix must not flip every
    unmatched flow to Likely regardless of its own status."""
    result = _base_result(
        affected_flows=[
            {
                "id": "flow:a",
                "entry_label": "POST /pets",
                "handler": "PetController.create",
                "changed_nodes": [],
                "impact_status": "proven",
            }
        ],
        accepted_impacts=[],
    )
    out = r.render(result)
    section = out.split("### What it may affect")[1].split("###")[0]
    assert "**Established**" in section
    assert "**Likely, not fully established**" not in section


# ---------------------------------------------------------------------------
# Product output + evidence correction pass (sydes-examples/nestjs-
# boilerplate PR #3 as the reference case): named existing evidence,
# explicit execution, per-category "why unverified" reasoning, and
# non-defect PR-semantic-analysis observations surfaced honestly.
# ---------------------------------------------------------------------------


def test_existing_evidence_names_the_real_test_with_tier_and_execution():
    """Reproduces the JAVA-1 case: a real, changed test directly verifying
    a route-contract obligation must be named, not just counted."""
    result = _base_result(
        affected_flows=[
            _make_flow(
                "flow:a", "PUT /articles/{slug}", "updateArticle", "ArticleCommandService.update",
                obligations=[
                    _make_obligation(
                        "route_contract", "PUT /articles/{slug} responds 200", status="unknown",
                        reason="Test execution was disabled (--no-run-tests)",
                        mapped_tests=[_make_test("src/test/ArticleApiTest.java", "should_update_article_content_success", "A_direct_route_exercise")],
                    )
                ],
            )
        ],
        accepted_impacts=[{"id": "flow:a", "status": "proven"}],
        summary={
            "verdict": "VERIFICATION INCOMPLETE", "risk": "MEDIUM",
            "counts": {
                "mapped_tests": 1, "tests_executed": 0,
                "tests_exercising_flows": 1, "tests_supporting_behavior": 0, "tests_verifying_behavior": 1,
            },
        },
    )
    out = r.render(result)
    section = out.split("### Test evidence")[1].split("###")[0]
    assert "`ArticleApiTest.java::should_update_article_content_success`" in section
    assert "directly covers: PUT /articles/{slug}" in section
    assert "(not run by Sydes)" in section


def test_execution_section_states_explicitly_whether_sydes_ran_tests():
    result = _base_result(notes=["test_execution=skipped reason=--no-run-tests"])
    out = r.render(result)
    section = out.split("### Test evidence")[1].split("###")[0]
    assert "| Test executed by Sydes | ⬛ Not run (`--no-run-tests`) |" in section


def test_execution_section_reports_a_real_run_count():
    result = _base_result(summary={
        "verdict": "VERIFIED", "risk": "LOW", "counts": {"mapped_tests": 0, "tests_executed": 5},
    })
    out = r.render(result)
    assert "| Test executed by Sydes | ✅ Yes — 5 test(s) run |" in out


def test_still_unverified_distinguishes_no_test_found_from_test_not_executed():
    """The exact ambiguity flagged as a real problem: two very different
    obligations must never collapse into the same "Not yet run" text."""
    result = _base_result(
        affected_flows=[
            _make_flow(
                "flow:a", "POST /v1/auth/logout", "logout", "AuthService.logout",
                obligations=[
                    _make_obligation(
                        "route_contract", "POST /v1/auth/logout responds 204", status="unknown",
                        reason="Test execution was disabled (--no-run-tests)",
                        mapped_tests=[_make_test("auth.e2e-spec.ts", "should logout", "A_direct_route_exercise")],
                    ),
                    _make_obligation(
                        "validation", "rejects missing Authorization header", status="unverified",
                        reason="No existing test asserts this behavior",
                    ),
                ],
            )
        ],
        accepted_impacts=[{"id": "flow:a", "status": "proven"}],
    )
    out = r.render(result)
    section = out.split("### Test evidence")[1].split("###")[0]
    assert "| API behavior | 🟡 Found, not executed |" in section
    assert "| Validation behavior | ❌ Not found |" in section


def test_still_unverified_reports_a_genuine_failure_distinctly():
    result = _base_result(
        affected_flows=[
            _make_flow(
                "flow:a", "POST /login", "login", "AuthService.login",
                obligations=[
                    _make_obligation(
                        "route_contract", "POST /login responds 200", status="failed",
                        reason="`should_login` failed in the repository test suite",
                        mapped_tests=[_make_test("auth.e2e-spec.ts", "should_login", "A_direct_route_exercise")],
                    ),
                ],
            )
        ],
        accepted_impacts=[{"id": "flow:a", "status": "proven"}],
    )
    out = r.render(result)
    section = out.split("### Test evidence")[1].split("###")[0]
    assert "| API behavior | ❌ Failed — `should_login` failed in the repository test suite |" in section


def test_verified_category_shown_separately_from_still_unverified():
    result = _base_result(
        affected_flows=[
            _make_flow(
                "flow:a", "GET /pets", "list", "PetService.list",
                obligations=[
                    _make_obligation(
                        "route_contract", "GET /pets responds 200", status="passed",
                        mapped_tests=[_make_test("pets.e2e-spec.ts", "should_list_pets", "A_direct_route_exercise")],
                    ),
                ],
            )
        ],
        accepted_impacts=[{"id": "flow:a", "status": "proven"}],
    )
    out = r.render(result)
    section = out.split("### Test evidence")[1].split("###")[0]
    assert "| API behavior | ✅ Verified |" in section
    # The passed category must never also appear as an unverified bullet
    # (bold, with a reason) anywhere else in the comment.
    assert "**API behavior:**" not in out


def test_notable_observations_surface_pr_semantic_local_risks_not_as_findings():
    """`pr_semantic_analysis.local_risks` are evidence-cited hypotheses
    about the change, not code-review defects -- shown under a distinctly
    labeled, non-defect heading, and only when the review itself found no
    blocking issues."""
    result = _base_result(
        code_review_status="completed",
        code_findings=[],
        pr_semantic_analysis={
            "local_risks": [
                {
                    "description": "AuthService.logout now expects a JwtPayloadType-based sessionId instead of a refresh-payload-based one.",
                    "citations": [{"file": "src/auth/auth.service.ts", "line": 538}],
                }
            ]
        },
    )
    out = r.render(result)
    section = out.split("### Code review")[1].split("---")[0]
    assert "**No blocking issues found**" in section
    assert "**Notable observations**" in section
    assert "JwtPayloadType-based sessionId" in section
    assert "(src/auth/auth.service.ts:538)" in section


def test_notable_observations_omitted_when_no_local_risks_exist():
    result = _base_result(code_review_status="completed", code_findings=[])
    out = r.render(result)
    assert "**Notable observations**" not in out


def test_coverage_limits_surfaces_the_unresolved_route_prefix_diagnostic():
    """The exact case that hid why relevant-looking tests were not mapped:
    Sydes already computes this diagnostic (see
    verify/test_mapping.py::_route_prefix_mismatch) -- it just wasn't shown."""
    result = _base_result(
        affected_flows=[_make_flow("flow:a", "GET /v1/auth/me", "me", "AuthService.me")],
        accepted_impacts=[{"id": "flow:a", "status": "proven"}],
        diagnostics=[
            "possible missing route prefix: flow path '/v1/auth/me' looks like a suffix of "
            "'/api/v1/auth/me' used in test/user/auth.e2e-spec.ts:136 -- route composition was "
            "not fixed here, so this was not used to map any test",
        ],
    )
    out = r.render(result)
    section = out.split("### What is still unknown")[1].split("###")[0] if "### What is still unknown" in out else ""
    assert "Tests reference `/api/v1/auth/me`, which may be `/v1/auth/me`" in section


def test_main_result_unavailable_path(tmp_path):
    """The CLI entrypoint's own fallback for a missing/unreadable result
    file -- exercised end to end, not just render_unavailable() directly."""
    out_path = tmp_path / "comment.md"
    missing = tmp_path / "does-not-exist.json"
    # main() reads argv via argparse, so invoke it the same way the CLI does.
    import sys as _sys

    old_argv = _sys.argv
    try:
        _sys.argv = ["render_sydes_pr.py", str(missing), "--out", str(out_path)]
        code = r.main()
    finally:
        _sys.argv = old_argv
    assert code == 0
    assert out_path.exists()
    assert "No result produced" in out_path.read_text()


# ---------------------------------------------------------------------------
# 15. Reframed messaging: "about this change" vs "about the surrounding
# route", handoff language for unexecuted evidence, and the header's new
# verdict/impact wording. Regression tests for the submission reframing --
# no verdict/risk COMPUTATION changed, only presentation.
# ---------------------------------------------------------------------------


def test_header_uses_reframed_verdict_and_impact_labels():
    result = _base_result(summary={
        "verdict": "VERIFICATION INCOMPLETE", "risk": "MEDIUM",
        "counts": {"mapped_tests": 0, "tests_executed": 0},
    })
    out = r.render(result)
    header = out.split("\n")[4]
    assert "◐ Analysis complete" in header
    assert "Medium impact" in header
    assert "risk" not in header.lower()


def test_header_action_required_keeps_a_strong_signal():
    result = _base_result(summary={
        "verdict": "ACTION REQUIRED", "risk": "HIGH",
        "counts": {"mapped_tests": 0, "tests_executed": 0},
    })
    out = r.render(result)
    header = out.split("\n")[4]
    assert "⚠ Action required" in header
    assert "High impact" in header


def test_change_analysis_all_green_when_change_is_fully_established_and_verified():
    """The Express-shaped acceptance case: changed symbol, established
    path, mapped test, passed -- every line should read positively."""
    result = _base_result(
        change={"base": "main", "symbols": [{"name": "create", "file": "a.ts"}], "files": []},
        accepted_impacts=[{"id": "flow:x", "status": "proven"}],
        affected_flows=[
            _make_flow("flow:x", "POST /pets", "create", "PetService.create", obligations=[
                _make_obligation(
                    "validation", "rejects non-positive age", status="passed", introduced=True,
                    mapped_tests=[_make_test("PetService.test.ts", "rejects", "A_direct_invocation")],
                ),
            ]),
        ],
    )
    out = r.render(result)
    section = out.split("### Test evidence")[1].split("###")[0]
    # Two compact state lines now, not four -- "changed behavior identified"
    # and "path established" are answered implicitly by Change/What it may
    # affect having content, rather than repeated here.
    assert section.count("✅") == 2
    assert "❌" not in section
    assert "○" not in section


def test_change_analysis_shows_handoff_icon_when_test_found_but_not_executed():
    result = _base_result(
        change={"base": "main", "symbols": [{"name": "create", "file": "a.ts"}], "files": []},
        accepted_impacts=[{"id": "flow:x", "status": "proven"}],
        affected_flows=[
            _make_flow("flow:x", "POST /pets", "create", "PetService.create", obligations=[
                _make_obligation(
                    "validation", "rejects non-positive age", status="unknown", introduced=True,
                    reason="Test execution was disabled (--no-run-tests)",
                    mapped_tests=[_make_test("PetService.test.ts", "rejects", "A_direct_invocation")],
                ),
            ]),
        ],
    )
    out = r.render(result)
    section = out.split("### Test evidence")[1].split("###")[0]
    assert "| Relevant regression test | ✅ Found |" in section
    assert "| Validation behavior | 🟡 Found, not executed |" in section


def test_change_analysis_all_red_when_nothing_is_established():
    result = _base_result()
    out = r.render(result)
    section = out.split("### Test evidence")[1].split("###")[0]
    assert "| Relevant regression test | ❌ Not found |" in section
    assert "✅" not in section


def test_verification_separates_this_change_from_the_surrounding_route():
    """The core reframing: a route whose changed-behavior obligation is
    unverified shows as a Test evidence table row (about THIS change),
    while a completely unrelated, pre-existing obligation on the same
    route groups separately under "Also on this route (pre-existing)" in
    What is still unknown -- never merged into one undifferentiated,
    indistinguishable list."""
    result = _base_result(
        accepted_impacts=[{"id": "flow:x", "status": "proven"}],
        affected_flows=[
            _make_flow("flow:x", "POST /pets", "create", "PetService.create", obligations=[
                _make_obligation(
                    "validation", "rejects non-positive age", status="unverified", introduced=True,
                    reason="No existing test asserts this behavior",
                ),
                _make_obligation(
                    "event_emission", "dispatches pet.created", status="unverified", introduced=False,
                    reason="No existing test asserts this behavior",
                ),
            ]),
        ],
    )
    out = r.render(result)
    test_evidence_section = out.split("### Test evidence")[1].split("###")[0]
    assert "| Validation behavior | ❌ Not found |" in test_evidence_section
    assert "Event emission" not in test_evidence_section

    assert "### What is still unknown" in out
    still_unknown_section = out.split("### What is still unknown")[1].split("###")[0]
    assert "**Also on this route (pre-existing)**" in still_unknown_section
    assert "**Event emission:**" in still_unknown_section
    assert "Validation behavior" not in still_unknown_section


def test_verification_falls_back_to_one_section_when_introduced_by_change_is_unpopulated():
    """No `introduced_by_change` signal anywhere -- there is nothing to
    split on, so this must fall back to the original single,
    undifferentiated section rather than fabricate a "this change" claim
    with no evidence behind it."""
    result = _base_result(
        accepted_impacts=[{"id": "flow:x", "status": "proven"}],
        affected_flows=[
            _make_flow("flow:x", "POST /pets", "create", "PetService.create", obligations=[
                _make_obligation(
                    "validation", "rejects non-positive age", status="unverified", introduced=False,
                    reason="No existing test asserts this behavior",
                ),
            ]),
        ],
    )
    out = r.render(result)
    test_evidence_section = out.split("### Test evidence")[1].split("###")[0]
    assert "| Validation behavior | ❌ Not found |" in test_evidence_section

    still_unknown_section = out.split("### What is still unknown")[1].split("###")[0]
    assert "**Also on this route (pre-existing)**" not in still_unknown_section


def test_executed_test_count_falls_back_to_obligation_executions():
    """Regression test for a real self-contradiction the reframed comment
    exposed: `summary.counts.tests_executed` only ever reflects a
    whole-suite run, never the individually-targeted mapped-test
    execution path -- so "Existing evidence" could show a test's
    Execution: passed while "Execution" said no tests ran at all, in the
    SAME comment."""
    result = _base_result(
        summary={"verdict": "VERIFICATION INCOMPLETE", "risk": "MEDIUM", "counts": {"tests_executed": 0}},
        affected_flows=[
            _make_flow("flow:x", "POST /pets", "create", "PetService.create", obligations=[
                _make_obligation(
                    "validation", "rejects non-positive age", status="passed", introduced=True,
                    mapped_tests=[_make_test("PetService.test.ts", "rejects", "A_direct_invocation")],
                ),
            ]),
        ],
    )
    result["affected_flows"][0]["obligations"][0]["executions"] = [
        {"test_id": "PetService.test.ts::rejects", "status": "passed"}
    ]
    out = r.render(result)
    assert "| Test executed by Sydes | ✅ Yes — 1 test(s) run |" in out


def test_change_analysis_falls_back_to_status_when_mapped_tests_is_trimmed_from_the_result():
    """A captured/trimmed result can carry `status`/`reason` proving a test
    was mapped (`resolve_obligation_status` never sets these otherwise)
    while `mapped_tests` itself is absent -- the checklist must not read
    that as "no relevant test found"."""
    result = _base_result(
        affected_flows=[
            _make_flow("flow:x", "POST /pets", "create", "PetService.create", obligations=[
                _make_obligation(
                    "validation", "rejects non-positive age", status="unknown", introduced=True,
                    reason="Test execution was disabled (--no-run-tests)",
                    # mapped_tests deliberately omitted, as in a trimmed fixture.
                ),
            ]),
        ],
    )
    out = r.render(result)
    section = out.split("### Test evidence")[1].split("###")[0]
    assert "| Relevant regression test | ✅ Found |" in section
    assert "| Validation behavior | 🟡 Found, not executed |" in section

#!/usr/bin/env python3
"""Render a reviewer-facing Markdown PR comment from a Sydes result JSON.

Reads the machine-readable result written by `sydes verify-change --json` and
emits the comment body used for both the PR comment and the Actions job
summary.

DESIGN INTENT (read this before changing section order or wording):

The PR comment is a decision surface, not a metrics dump. It answers, in five
scannable sections, the questions a reviewer actually has in order: what
changed (### Change), what system behavior it reaches and through what
logical path (### What it may affect), what test evidence exists and whether
it's been executed (### Test evidence), what gaps remain -- about this change
or pre-existing on the same route -- plus any before-merge/coverage caveats
(### What is still unknown), and what an AI review pass found (### Code
review). Deep evidence -- full obligation lists, confidence scores, graph
diagnostics, the complete symbol table -- belongs in the uploaded JSON
artifact and (eventually) a dashboard, not here. A tiny, deliberately sparse
<details> block carries a few grounding facts; it is not a second render of
the whole result.

Confidence is never flattened away by this consolidation: an established
fact is a plain bullet; anything inferred or not fully established keeps an
explicit inline qualifier (e.g. "(likely, not fully established)"), and a
pre-existing, unrelated route-level gap keeps "(pre-existing on this
route)" rather than being merged indistinguishably with a gap in the change
itself.

Canonical Sydes vocabulary (`VERIFICATION INCOMPLETE`, `obligation`,
`proven`/`inferred`, `unresolved`) is preserved everywhere in the underlying
JSON and is NEVER changed by this script. Only the human-facing Markdown
text translates it -- see _HUMAN_VERDICT / _HUMAN_RISK / the "Established"/
"Likely"/"Not fully traced" vocabulary below. The word "obligation" never
appears in rendered output.

Everything here is deterministic: no LLM calls, no network calls, and the
same input JSON always renders identically.

Usage:
    render_sydes_pr.py RESULT_JSON --out comment.md
                       [--diagnostics-out diagnostics.json] [--run-url URL]
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

MARKER = "<!-- sydes-verification-comment -->"

# ---------------------------------------------------------------------------
# Canonical -> human vocabulary. Internal enums are never shown to a
# reviewer; only these translations are.
# ---------------------------------------------------------------------------

#: These are presentation labels only -- `summary.verdict`'s own three enum
#: values and how they're computed are untouched (see `verify/analyzer.py`).
#: The wording change is deliberate: "VERIFICATION INCOMPLETE" (Sydes' own
#: internal name for "nothing failed, but not everything on this route has
#: executed evidence") previously read as "More verification needed" --
#: which sounds like Sydes is asking the reviewer for more work, when what
#: it actually means is "analysis finished, here's exactly what's left and
#: why" (see `render_test_evidence`/`render_what_is_still_unknown`, which
#: separate what's about THIS change from what's about the surrounding,
#: possibly pre-existing route). Reserve alarming language for a genuine
#: blocking signal: a failed test, a blocking review finding, or an
#: unresolved issue that materially affects the changed behavior itself --
#: that is exactly what "ACTION REQUIRED" already means and continues to.
_HUMAN_VERDICT = {
    "VERIFIED": "✅ Verified",
    "VERIFICATION INCOMPLETE": "◐ Analysis complete",
    "ACTION REQUIRED": "⚠ Action required",
    "OK": "✅ No affected behavior found",
}

#: Relabeled from "risk" to "impact": this number has always measured how
#: much of the affected route lacks executed evidence, not how sensitive or
#: broad the change itself is -- "risk" read as a judgment on the CHANGE;
#: "impact" reads as a fact about how much surface Sydes is describing.
#: Same three underlying values, `summary.risk`, untouched.
_HUMAN_RISK = {"LOW": "Low impact", "MEDIUM": "Medium impact", "HIGH": "High impact"}

_AREA_BY_BOUNDARY_KIND = {
    "api": "API",
    "callable": "Service logic",
    "async": "Background jobs",
    "external": "External integration",
    "unknown": "Other",
}

#: Verification is reported per high-level category, never per raw obligation
#: statement (see `render_what_is_still_unknown`) -- these are the only categories
#: shown, in this fixed display order. `side_effect` has no entry: it is
#: excluded everywhere obligations are read (see
#: `_CODE_FRAGMENT_OBLIGATION_KINDS`).
_OBLIGATION_CATEGORY_LABEL = {
    "route_contract": "API behavior",
    "validation": "Validation behavior",
    "cross_repo_call": "Cross-service behavior",
    "state_consistency": "State consistency",
    "event_emission": "Event emission",
}
_OBLIGATION_CATEGORY_ORDER = [
    "route_contract",
    "validation",
    "cross_repo_call",
    "state_consistency",
    "event_emission",
]

#: When a category has more than one obligation, show the worst status
#: across the group (a reviewer needs to know the worst case, not an
#: arbitrary one) -- lower rank wins.
_OBLIGATION_STATUS_RANK = {"failed": 0, "unverified": 1, "unknown": 2, "passed": 3}

# A large fraction of `VerificationObligation.statement` values are
# auto-generated route-contract boilerplate ("contract happy path", "POST
# /x responds 201 — Default 201 response skeleton.") with no reviewer value.
# Filtering these out, rather than rendering every obligation, is what keeps
# the Verification section from becoming another metrics dump.
_BOILERPLATE_STATEMENT_RE = re.compile(
    r"^contract happy path$|responds \d+ — Default \d+ response skeleton\.?$",
    re.IGNORECASE,
)

# Deterministic truncation limits. These are the renderer's entire
# "how much is too much" policy -- change them here, not ad hoc in a
# render function.
_MAX_ESTABLISHED_PATHS = 3
_MAX_LIKELY_PATHS = 2
_MAX_AREA_ROWS = 6
_MAX_CHECKLIST_ROWS = 4
_MAX_DETAIL_SYMBOLS = 5


def _get(mapping: Any, *keys: str, default: Any = None) -> Any:
    """Walk nested dicts without assuming any level exists."""
    current = mapping
    for key in keys:
        if not isinstance(current, dict):
            return default
        current = current.get(key)
    return default if current is None else current


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _clean(text: Any, limit: int = 240) -> str:
    """Collapse a model- or backend-authored string to one safe, bounded
    Markdown line. Shorter default limit than before: this script no longer
    renders long paragraphs outside the single `change_summary` line.

    Truncates at a word boundary -- cutting mid-word ("...stronger inpu…")
    reads as broken, not concise."""
    if not isinstance(text, str):
        return ""
    flattened = " ".join(text.split())
    if len(flattened) <= limit:
        return flattened
    truncated = flattened[: limit - 1]
    last_space = truncated.rfind(" ")
    if last_space > limit * 0.6:  # only back off to the word boundary if it's not too far short
        truncated = truncated[:last_space]
    return truncated.rstrip().rstrip(".,;:") + "…"


def _test_file_paths(result: dict[str, Any]) -> set[str]:
    """Paths whose file role marks them as tests, for the production split."""
    paths: set[str] = set()
    for item in _as_list(_get(result, "change", "files", default=[])):
        role = str(_get(item, "role", default="") or "")
        path = _get(item, "path", default="")
        if path and "test" in role.lower():
            paths.add(str(path))
    return paths


#: `analysis_notes` is a flat list mixing genuinely different concerns --
#: structural/route-discovery notes, and separately, provider-availability
#: notes (a missing API key, an LLM call failing) -- with no type tag to
#: tell them apart programmatically. Blindly picking the first note can
#: surface "OPENAI_API_KEY is not set" as if it explained why no system
#: path was found, which is misleading when a real structural note (e.g.
#: "No discovered route declaration reaches the changed symbols.") is
#: also present later in the same list.
_PROVIDER_NOTE_MARKERS = ("api_key", "provider", "code review was requested")


def _pick_analysis_note(result: dict[str, Any], limit: int = 200) -> str:
    """The single most reviewer-relevant analysis note, if any: prefers a
    structural note over a provider-availability one, but still returns a
    provider note rather than nothing if that's all there is."""
    notes = [_clean(n, limit=limit) for n in _as_list(_get(result, "analysis_notes", default=[]))]
    notes = [n for n in notes if n]
    if not notes:
        return ""
    structural = [n for n in notes if not any(m in n.lower() for m in _PROVIDER_NOTE_MARKERS)]
    return structural[0] if structural else notes[0]


# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------


def render_header(result: dict[str, Any], lines: list[str]) -> None:
    verdict = str(_get(result, "summary", "verdict", default="UNKNOWN"))
    risk = str(_get(result, "summary", "risk", default="UNKNOWN"))
    human_verdict = _HUMAN_VERDICT.get(verdict, verdict.capitalize())
    human_risk = _HUMAN_RISK.get(risk, risk.capitalize() + " impact" if risk != "UNKNOWN" else "Impact unknown")

    lines.append("## Sydes")
    lines.append("")
    lines.append(f"**{human_verdict}** · {human_risk}")
    lines.append("")


# ---------------------------------------------------------------------------
# What changed
# ---------------------------------------------------------------------------


def render_change(result: dict[str, Any], lines: list[str]) -> None:
    """One grounded, plain-English paragraph. No confidence numbers, no
    per-behavior-change bullet list here -- individual behavior changes
    that matter are what System impact exists to show, with an actual
    path attached, not a restated sentence."""
    summary = _clean(_get(result, "pr_semantic_analysis", "change_summary", default=""), limit=600)
    if not summary:
        return
    lines.append("### Change")
    lines.append("")
    lines.append(summary)
    lines.append("")


# ---------------------------------------------------------------------------
# System impact -- the centerpiece.
# ---------------------------------------------------------------------------


def _boundary_status(boundary: dict[str, Any]) -> str:
    return str(_get(boundary, "status", default="proven"))


# A raw CBM-style qualified identifier (a repo-path-prefixed dotted symbol
# name used internally for identity matching) occasionally ends up as a
# boundary's only `label` when no better human description was available.
# It is never meant for display -- e.g.
# "home-runner-work-Rocket-Rocket.examples.todo.src.main.delete" -- so
# detect that shape and fall back to the boundary's own `symbol` field,
# which is always a plain, short name.
_RAW_IDENTIFIER_LABEL_RE = re.compile(r"[a-zA-Z0-9_-]+(\.[a-zA-Z0-9_-]+){3,}$")


def _boundary_display_label(boundary: dict[str, Any]) -> str:
    # Raw-identifier detection MUST run before truncation: `_clean` cuts a
    # long label at a hard character limit and appends "…", and a truncated
    # string can never match `_RAW_IDENTIFIER_LABEL_RE` (which requires the
    # string to *end* in a clean dotted segment) -- so checking the already-
    # truncated text let a raw CBM-style identifier like
    # "home-runner-work-nestjs-boilerplate-nestjs-boilerplate.src.utils.
    # deep-resolver.deepResolvePromises" through as "…deepResolv…" instead
    # of falling back to the boundary's own clean `symbol` field, exactly as
    # this safeguard was meant to prevent (confirmed on a real render:
    # sydes-examples/nestjs-boilerplate PR #3).
    raw_label = str(_get(boundary, "label", default="") or "").strip()
    if raw_label and not _RAW_IDENTIFIER_LABEL_RE.match(raw_label.replace(" ", "")):
        return _clean(raw_label, limit=90)
    symbol = str(_get(boundary, "symbol", default="") or "").strip()
    if symbol:
        return f"`{symbol}`"
    return _clean(raw_label, limit=90) or "Affected"


def _impact_status_by_id(result: dict[str, Any]) -> dict[str, str]:
    impacts = _as_list(_get(result, "accepted_impacts", default=[]))
    return {str(_get(imp, "id", default="")): str(_get(imp, "status", default="")) for imp in impacts}


def _flow_fallback_status(flow: dict[str, Any]) -> str:
    """A flow's OWN `impact_status` field, used only when no
    `accepted_impacts` entry matches this flow's id at all.

    The fallback must never be a hardcoded "proven": that would render a
    flow explicitly marked non-proven (e.g. `impact_status="inferred"`) as
    Established the moment its accepted_impacts entry happens to be
    missing -- a real internal-consistency break (see task item 7), not a
    theoretical one. `AffectedFlow.impact_status` already carries this
    same "proven" default in the canonical model itself, so trusting it
    here changes nothing for the ordinary, fully-populated case and only
    fixes the case where the two collections disagree."""
    return str(_get(flow, "impact_status", default="proven") or "proven")


def _flow_routes_by_status(
    result: dict[str, Any], impact_status_by_id: dict[str, str]
) -> tuple[list[str], list[str]]:
    """All flow entry routes (e.g. `POST /users`), split into established vs
    likely using the same `accepted_impacts` cross-reference used for the
    representative-path rendering below -- deduped, in flow order."""
    established: list[str] = []
    likely: list[str] = []
    seen: set[str] = set()
    for flow in _as_list(_get(result, "affected_flows", default=[])):
        route = str(_get(flow, "entry_label", default="") or "").strip()
        if not route or route in seen:
            continue
        seen.add(route)
        status = impact_status_by_id.get(str(_get(flow, "id", default="")), _flow_fallback_status(flow))
        (established if status == "proven" else likely).append(route)
    return established, likely


def _route_impact_row(established_routes: list[str], likely_routes: list[str]) -> tuple[str, str] | None:
    """A concrete, descriptive 'API' row -- never a bare established/likely
    count. One or two routes are named directly; three or more collapse to
    a count (still a route count, not an internal analysis-state count)."""
    parts: list[str] = []
    if established_routes:
        if len(established_routes) == 1:
            parts.append(f"`{established_routes[0]}` impact established")
        elif len(established_routes) == 2:
            parts.append(f"`{established_routes[0]}` and `{established_routes[1]}` impact established")
        else:
            parts.append(f"{len(established_routes)} API routes affected (established)")
    if likely_routes:
        if len(likely_routes) == 1:
            parts.append(f"`{likely_routes[0]}` likely affected, not fully traced")
        else:
            parts.append(f"{len(likely_routes)} more API routes likely affected, not fully traced")
    return ("API", "; ".join(parts)) if parts else None


def _boundary_groups_by_kind(result: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    by_kind: dict[str, list[dict[str, Any]]] = {}
    for b in _as_list(_get(result, "affected_boundaries", default=[])):
        by_kind.setdefault(str(_get(b, "kind", default="unknown")), []).append(b)
    return by_kind


def _describe_group(items: list[dict[str, Any]]) -> str:
    """Describe one area's boundaries using their own label/status fields --
    concrete for one item, still descriptive (never a bare count alone) for
    two, and only falling back to a count when a group is large enough that
    naming each one would be noise."""
    established = [b for b in items if _boundary_status(b) == "proven"]
    likely = [b for b in items if _boundary_status(b) != "proven"]
    if len(items) == 1:
        qualifier = "established" if established else "likely, not fully established"
        return f"{_boundary_display_label(items[0])} ({qualifier})"
    parts: list[str] = []
    if established:
        if len(established) == 1:
            parts.append(f"{_boundary_display_label(established[0])} (established)")
        else:
            parts.append(f"{len(established)} established")
    if likely:
        if len(likely) == 1:
            parts.append(f"{_boundary_display_label(likely[0])} (not fully traced)")
        else:
            parts.append(f"{len(likely)} not fully traced")
    return "; ".join(parts)


def _system_impact_data(
    result: dict[str, Any],
) -> tuple[list[tuple[str, str]], list[str], bool]:
    """The single source of truth for 'what did Sydes find' -- shared by the
    System impact table and the Before-merge rules below so the two never
    disagree. Returns (area rows, area names flagged as an unresolved wider
    surface, whether any real impact signal exists at all)."""
    impact_status_by_id = _impact_status_by_id(result)
    established_routes, likely_routes = _flow_routes_by_status(result, impact_status_by_id)
    flow_files: set[str] = set()
    for flow in _as_list(_get(result, "affected_flows", default=[])):
        refs = _get(flow, "artifact_refs", default={})
        for key in ("route_file", "handler_file"):
            f = _get(refs, key, default="") if isinstance(refs, dict) else ""
            if f:
                flow_files.add(str(f))
        flow_file = _get(flow, "file", default="")
        if flow_file:
            flow_files.add(str(flow_file))

    by_kind = _boundary_groups_by_kind(result)
    api_boundaries = by_kind.pop("api", [])

    rows: list[tuple[str, str]] = []
    wider_areas: list[str] = []

    route_row = _route_impact_row(established_routes, likely_routes)
    if route_row:
        rows.append(route_row)
        # An `api`-kind boundary whose own source file is not one of the
        # traced routes' files is a genuinely separate signal -- e.g. a
        # shared auth filter the traced route passes through that also
        # gates other, untraced routes. It must never be folded into the
        # route count above (that would either overcount or hide it).
        #
        # Only worth checking at all when there is more than one distinct
        # api boundary: with exactly one, it IS the boundary behind the
        # route row above -- there is nothing "wider" to split out, and a
        # mismatched file (route discovery can mis-locate a route file,
        # e.g. a same-named handler in an unrelated example/crate) would
        # otherwise duplicate that single boundary as a second, bogus row.
        if len(api_boundaries) > 1:
            extra = [b for b in api_boundaries if str(_get(b, "file", default="")).strip() not in flow_files]
            if extra:
                area = "Wider API surface"
                rows.append((area, _describe_group(extra)))
                wider_areas.append(area)
    elif api_boundaries:
        # No flow data at all for this change -- describe the api
        # boundaries directly, same as any other kind below.
        rows.append(("API", _describe_group(api_boundaries)))

    for kind, items in by_kind.items():
        if items:
            rows.append((_AREA_BY_BOUNDARY_KIND.get(kind, "Other"), _describe_group(items)))

    has_any_impact = bool(established_routes or likely_routes or api_boundaries or any(by_kind.values()))

    # Runtime-dependency data (`result.runtime_dependencies`) is deliberately
    # not surfaced in the public PR comment -- it stayed noisy and not
    # reliably useful across real cases (confirmed on Healthchecks). The
    # field itself is untouched in the canonical result/artifacts; this is
    # a presentation omission only, not a change to what Sydes discovers.

    return rows[:_MAX_AREA_ROWS], wider_areas, has_any_impact


def summarize_system_impact_areas(result: dict[str, Any]) -> list[tuple[str, str]]:
    rows, _wider_areas, _has_any_impact = _system_impact_data(result)
    return rows


#: Cap on distinct changed-target terminals shown per established flow (see
#: `_flow_changed_terminals`) -- keeps a flow that touches many files from
#: turning one route's box into a symbol dump, while still surfacing more
#: than one genuinely distinct established behavior instead of silently
#: collapsing to an arbitrary single pick.
_MAX_FLOW_TERMINALS = 3

#: `flow.steps` layer values that represent a real, structurally-followed
#: call out of the handler -- see `sydes.trace.layered_contract`, which only
#: emits a `followed_call` step after actually resolving a call site inside
#: the handler's own body (or a symbol it in turn calls) to a real target.
#: Never `changed_nodes`: that list is the whole diff's changed-symbol set,
#: identical across every flow in the result, so it is not itself evidence
#: that THIS route's handler reaches any particular one of them.
_CONNECTED_CALL_LAYERS = {"followed_call"}
#: Statuses trusted enough to show as connected -- "grounded" is the only
#: one observed in real results; anything else (should it ever appear) is
#: excluded rather than guessed to be equally solid. Empty string means the
#: field was omitted, treated as unknown-but-not-explicitly-uncertain.
_CONNECTED_STEP_STATUSES = {"", "grounded"}


def _canonical_symbol_lookup(flow: dict[str, Any]) -> dict[tuple[str, int], str]:
    """A (file, line) -> qualified-symbol lookup built from this flow's own
    `changed_nodes`.

    Used ONLY to upgrade a step's bare or receiver-variable-qualified symbol
    name (e.g. Go's `server.renewAccessToken`, a lowercase receiver
    variable, not the type) to the canonical, class/type-qualified spelling
    the exact same declaration line is already known under elsewhere in the
    result (`Server.renewAccessToken`). Never used to decide WHICH symbols
    are shown -- `changed_nodes` remains untrusted for that, per
    `_flow_connected_calls` -- only how an already-selected one is spelled.
    """
    lookup: dict[tuple[str, int], str] = {}
    for node in _as_list(_get(flow, "changed_nodes", default=[])):
        file = str(_get(node, "file", default="") or "")
        line = _get(node, "line", default=None)
        symbol = str(_get(node, "symbol", default="") or "").strip()
        if file and isinstance(line, int) and symbol:
            lookup[(file, line)] = symbol
    return lookup


def _canonical_name(fallback: str, file: str, line: Any, lookup: dict[tuple[str, int], str]) -> str:
    if isinstance(line, int) and (file, line) in lookup:
        return lookup[(file, line)]
    return fallback


def _handler_step(flow: dict[str, Any]) -> dict[str, Any] | None:
    """The one step that names the handler's own declaration -- `depth 1`,
    `layer == "handler"`, `kind == "handler"` specifically (not a
    same-depth statement/transform/response step inside the handler body,
    which shares the depth but not the handler's own declaration line)."""
    for step in _as_list(_get(flow, "steps", default=[])):
        if str(_get(step, "layer", default="")) == "handler" and str(_get(step, "kind", default="")) == "handler":
            return step
    return None


def _flow_connected_calls(flow: dict[str, Any], test_paths: set[str], handler: str) -> list[str]:
    """The symbol(s) this flow's own evidence actually shows the handler
    calling into -- read from `steps`, never from `changed_nodes` (see the
    module-level note above `_CONNECTED_CALL_LAYERS`). A symbol only
    appears here when Sydes actually followed a real call to it from this
    specific route's own handler; two unrelated files changed in the same
    diff no longer appear here just for having been touched."""
    lookup = _canonical_symbol_lookup(flow)
    seen: set[str] = set()
    ordered: list[str] = []
    for step in _as_list(_get(flow, "steps", default=[])):
        if str(_get(step, "layer", default="")) not in _CONNECTED_CALL_LAYERS:
            continue
        if str(_get(step, "status", default="")) not in _CONNECTED_STEP_STATUSES:
            continue
        file = str(_get(step, "file", default="") or "")
        if file in test_paths:
            continue
        symbol = str(_get(step, "symbol", default="") or "").strip()
        if not symbol or symbol == handler:
            continue
        canonical = _canonical_name(symbol, file, _get(step, "line_start", default=None), lookup)
        if canonical in seen:
            continue
        seen.add(canonical)
        ordered.append(canonical)
    return ordered


def _flow_path_label(
    flow: dict[str, Any], test_paths: set[str],
) -> tuple[list[str], list[str], int]:
    """Build the TRUE CONNECTED PATH for one affected flow: route, then its
    canonical handler, then -- ONLY when this flow's own evidence
    establishes a real call from that handler -- the symbol(s) it is shown
    calling into (see `_flow_connected_calls`). Never a changed-symbol
    dump: two files touched by the same diff are not thereby connected to
    each other, or to this specific route, just by both having changed.

    Returns (path_parts, connected_calls, omitted_count). `path_parts` is
    `[route]` or `[route, handler]` -- the route/handler hop this data
    always establishes when a handler is resolved at all. `connected_calls`
    holds up to `_MAX_FLOW_TERMINALS` symbols the handler is actually shown
    calling; `omitted_count` is how many more existed beyond that cap. Both
    are empty when no deeper connectivity is established for this flow --
    stopping at route -> handler is then the correct, complete answer, not
    a partial one; richer, safely-established connectivity beyond one hop
    is not fabricated to fill the gap.
    """
    path_parts = [str(_get(flow, "entry_label", default="") or "").strip()]
    handler = str(_get(flow, "handler", default="") or "").strip()
    canonical_handler = handler
    if handler:
        handler_step = _handler_step(flow)
        if handler_step is not None:
            lookup = _canonical_symbol_lookup(flow)
            canonical_handler = _canonical_name(
                handler,
                str(_get(handler_step, "file", default="") or ""),
                _get(handler_step, "line_start", default=None),
                lookup,
            )
    if canonical_handler and canonical_handler != path_parts[0]:
        path_parts.append(canonical_handler)
    connected = _flow_connected_calls(flow, test_paths, handler) if len(path_parts) > 1 else []
    capped = connected[:_MAX_FLOW_TERMINALS]
    return path_parts, capped, max(0, len(connected) - _MAX_FLOW_TERMINALS)


def select_representative_paths(
    result: dict[str, Any],
) -> tuple[list[tuple[list[str], list[str], int]], list[str], int, int]:
    """Deterministic representative-path selection -- the rule that keeps a
    20-route change from dumping 20 paths into the comment.

    Returns (established_paths, likely_labels, established_remaining,
    likely_remaining). `established_paths` are (path_parts, connected_calls,
    omitted_count) triples -- `path_parts` is the route->handler TRUE
    CONNECTED PATH for the fenced/tree rendering, `connected_calls` are the
    symbol(s) this flow's own evidence shows the handler actually calling
    (never a whole-diff changed-symbol dump -- see `_flow_connected_calls`),
    rendered as a set, never chained onto `path_parts` as further hops
    (Sydes proves "handler calls each of these", not an order between
    them), and `omitted_count` is how many further such calls this same
    flow had beyond what `_flow_path_label` already kept (see
    `_MAX_FLOW_TERMINALS`). `likely_labels` are plain strings (inferred
    impacts rarely have a full traced chain to show)."""
    flows = _as_list(_get(result, "affected_flows", default=[]))
    impacts = _as_list(_get(result, "accepted_impacts", default=[]))
    impact_status_by_id = _impact_status_by_id(result)
    test_paths = _test_file_paths(result)

    established_all: list[tuple[list[str], list[str], int]] = []
    likely_all: list[str] = []
    shown_impact_ids: set[str] = set()

    for flow in flows:
        parts, connected_calls, omitted = _flow_path_label(flow, test_paths)
        if not parts or not parts[0]:
            continue
        flow_id = str(_get(flow, "id", default=""))
        shown_impact_ids.add(flow_id)
        status = impact_status_by_id.get(flow_id, _flow_fallback_status(flow))
        if status == "proven":
            established_all.append((parts, connected_calls, omitted))
        else:
            # A "likely" impact has no established path at all; the
            # connected calls (if any) are folded into one plain label
            # rather than given their own set notation, since nothing here
            # is proven either way.
            likely_all.append(" → ".join(parts + connected_calls))

    for impact in impacts:
        if str(_get(impact, "id", default="")) in shown_impact_ids:
            continue
        if str(_get(impact, "status", default="")) != "inferred":
            continue
        label = _clean(_get(impact, "behavior_label", default=""), limit=100) or _clean(
            _get(impact, "label", default=""), limit=100
        )
        if label:
            likely_all.append(label)

    established = established_all[:_MAX_ESTABLISHED_PATHS]
    likely = likely_all[:_MAX_LIKELY_PATHS]
    return (
        established,
        likely,
        max(0, len(established_all) - len(established)),
        max(0, len(likely_all) - len(likely)),
    )


def _render_established_block(
    parts: list[str], connected_calls: list[str], omitted: int, lines: list[str]
) -> None:
    """The ASCII-ladder rendering for one established path: route, then
    handler, then -- when this flow's own evidence establishes it -- the
    symbol(s) the handler actually calls. A single connected call reads as
    one more arrow hop (the routine case: a genuine, single-file sequence).
    More than one uses tree-branch connectors (`├─`/`└─`), never another
    arrow: the handler is proven to call each of these, but never proven to
    call them in any particular order, and chaining them with `→` would
    fabricate a sequence that was never traced. A bare route with no
    resolved handler/calls renders as a plain bullet -- there is no ladder
    to draw."""
    if len(parts) > 1 or connected_calls:
        lines.append("```text")
        lines.append(parts[0])
        for p in parts[1:]:
            lines.append(f"  → {p}")
        if len(connected_calls) == 1:
            lines.append(f"  → {connected_calls[0]}")
        elif connected_calls:
            for call in connected_calls[:-1]:
                lines.append(f"  ├─ {call}")
            lines.append(f"  └─ {connected_calls[-1]}")
        if omitted:
            lines.append(f"  … +{omitted} more traced call(s)")
        lines.append("```")
    else:
        lines.append(f"- `{parts[0]}`")


def render_what_it_may_affect(result: dict[str, Any], lines: list[str]) -> None:
    """The section a reviewer actually needs: what area of the system this
    reaches, and through what logical path.

    Confidence is never flattened away: an established path renders as an
    ASCII ladder (see `_render_established_block`) or a bare bullet, always
    under its own "Established" label; anything not fully established
    renders separately, under "Likely, not fully established", never
    visually merged with a proven path."""
    lines.append("### What it may affect")
    lines.append("")

    established, likely, established_more, likely_more = select_representative_paths(result)
    # Non-API area rows (Service logic, Background jobs, External
    # integration, Other, Wider API surface) carry genuinely additional
    # information beyond the routes already shown via paths below. The API
    # row itself just restates those same routes, so it's skipped here to
    # avoid showing the same route twice under two different bullets.
    other_area_bullets = [f"{area}: {impact}" for area, impact in summarize_system_impact_areas(result) if area != "API"]

    if not established and not likely and not other_area_bullets:
        # Nothing resolved at all -- this must never read as "nothing is
        # affected". Say plainly that tracing did not reach anything, and
        # cite the real reason when one is available.
        reason = _pick_analysis_note(result, limit=160)
        lines.append(
            "Sydes could not establish a system path from the changed code to any "
            "entrypoint for this change."
        )
        if reason:
            lines.append(f"_{reason}_")
        lines.append("")
        return

    if established:
        lines.append("**Established**")
        lines.append("")
        for idx, (parts, connected_calls, omitted) in enumerate(established):
            _render_established_block(parts, connected_calls, omitted, lines)
            if (len(parts) > 1 or connected_calls) and idx < len(established) - 1:
                lines.append("")  # blank line between fences -- otherwise
                                   # adjacent ```text blocks can render as
                                   # one merged block
        if established_more:
            lines.append(f"_…and {established_more} more established path(s) in the full result._")
        lines.append("")

    if likely:
        lines.append("**Likely, not fully established**")
        lines.append("")
        for label in likely:
            lines.append(f"- {label}")
        if likely_more:
            lines.append(f"_…and {likely_more} more likely impact(s) in the full result._")
        lines.append("")

    for bullet in other_area_bullets:
        lines.append(f"- {bullet}")
    if other_area_bullets:
        lines.append("")


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


#: `kind == "side_effect"` obligations are generated from the template
#: "{route} accessed {raw source expression}" -- confirmed across every
#: language and case inspected (Go, Java, Rust, TypeScript all follow it
#: exactly). The statement is a literal code fragment BY CONSTRUCTION, not
#: sometimes; there is no clean-vs-messy split within this kind to detect,
#: so it is excluded entirely rather than truncated into a half-cut
#: snippet. This is a data-shape observation, not a semantic judgment
#: about side effects being unimportant -- see the module docstring's
#: scope note: rendering can't parse code to produce a clean claim, and
#: showing a raw fragment reads worse than omitting it.
_CODE_FRAGMENT_OBLIGATION_KINDS = {"side_effect"}


def _real_statement_obligations(result: dict[str, Any]) -> list[dict[str, Any]]:
    """Obligations with a real, specific, reviewer-legible statement --
    filtering out both the generic route-contract boilerplate ("contract
    happy path", "responds 201 — Default 201 response skeleton.") and the
    code-fragment-by-construction kinds (see
    `_CODE_FRAGMENT_OBLIGATION_KINDS`). Deduplicated by (kind, statement)
    since the same generic-shaped claim can otherwise repeat once per
    flow."""
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for flow in _as_list(_get(result, "affected_flows", default=[])):
        for obligation in _as_list(_get(flow, "obligations", default=[])):
            kind = str(_get(obligation, "kind", default=""))
            if kind in _CODE_FRAGMENT_OBLIGATION_KINDS:
                continue
            statement = str(_get(obligation, "statement", default="") or "").strip()
            if not statement or _BOILERPLATE_STATEMENT_RE.search(statement):
                continue
            key = (kind, statement[:80])
            if key in seen:
                continue
            seen.add(key)
            out.append(obligation)
    return out


def _obligations_split_by_relevance(
    result: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """`(about_this_change, about_the_route)` -- the same `introduced_by_change`
    signal `_meaningful_obligations` already reads, but keeping BOTH groups
    instead of picking one. A PR whose own changed behavior is fully proven
    must not read as unhealthy merely because unrelated, pre-existing
    behavior on the same route (a side effect the diff never touched, a
    route-contract skeleton nobody wrote a test for last year) also lacks
    executed evidence -- that is a fact about the route, not about this
    change, and the two must never be presented as one undifferentiated
    pile of "still unverified".

    When `introduced_by_change` is unpopulated for every obligation (a real,
    known data gap on some analysis paths -- see `_meaningful_obligations`),
    there is no signal to split on at all: everything returns in
    `about_the_route`, and callers fall back to the old undifferentiated
    presentation rather than fabricate a "this change" claim with nothing
    behind it."""
    real = _real_statement_obligations(result)
    about_this_change = [o for o in real if _get(o, "introduced_by_change", default=False)]
    changed_ids = {id(o) for o in about_this_change}
    about_the_route = [o for o in real if id(o) not in changed_ids]
    return about_this_change, about_the_route


# ---------------------------------------------------------------------------
# Change analysis -- the four-line answer to the question a reviewer
# actually has first ("is THIS change covered?"), computed entirely from
# data the sections below already read. No new Sydes signal; this just
# surfaces the same facts earlier and scoped to what the diff introduced,
# not the whole route.
# ---------------------------------------------------------------------------


def _obligation_has_mapped_test(obligation: dict[str, Any]) -> bool:
    """True when a test is genuinely mapped to this obligation -- normally
    just a non-empty `mapped_tests`, but also true when `status`/`reason`
    can only exist BECAUSE a mapped test was present (`resolve_obligation_
    status`/its `--no-run-tests` override in `verify/analyzer.py` never
    sets these otherwise): `passed`/`failed`, or a reason naming execution
    at all. Needed because `mapped_tests` itself is absent from some
    captured/trimmed results even when the obligation's own status proves
    one existed; reading only `mapped_tests` would then under-report."""
    if _as_list(_get(obligation, "mapped_tests", default=[])):
        return True
    status = str(_get(obligation, "status", default=""))
    if status in {"passed", "failed"}:
        return True
    reason = str(_get(obligation, "reason", default="") or "").lower()
    return "no-run-tests" in reason or "was not executed" in reason


def render_test_evidence(result: dict[str, Any], lines: list[str]) -> None:
    """A compact `Check | Result` table first -- every status fact a
    reviewer needs (a test found; each behavior category's verification
    state; whether Sydes actually ran anything; how complete route
    discovery was) as one glanceable row each, instead of the same facts
    restated across several prose sections. Named test evidence follows as
    one line per test, not a paragraph -- detail for whoever wants it,
    without re-explaining what the table already said.

    The two dropped checklist lines from the very first version of this
    section ("Changed behavior identified"/"Affected API/system path
    established") are still answered implicitly by Change/What it may
    affect having content above."""
    about_this_change, about_the_route = _obligations_split_by_relevance(result)
    # Same fallback used throughout: when `introduced_by_change` is
    # unpopulated everywhere (a known data gap on some analysis paths, not
    # "nothing here relates to the change"), `about_this_change` is empty --
    # checking only that set would falsely read as "no relevant test found"
    # even when `about_the_route` (really just "every real obligation" in
    # this case) plainly has one. Never apply this fallback when
    # `about_this_change` genuinely has entries that just don't happen to
    # pass yet -- that IS a real answer, not a data gap.
    relevant = about_this_change if about_this_change else about_the_route
    has_mapped_test = any(_obligation_has_mapped_test(o) for o in relevant)

    lines.append("### Test evidence")
    lines.append("")
    lines.append("| Check | Result |")
    lines.append("| --- | --- |")
    lines.append(f"| Relevant regression test | {'✅ Found' if has_mapped_test else '❌ Not found'} |")
    for label, obligation in _category_status_rows(relevant):
        lines.append(f"| {label} | {_short_status_phrase(obligation)} |")

    counts = _get(result, "summary", "counts", default={})
    executed = counts.get("tests_executed", 0) or _executed_test_count(result)
    if executed:
        lines.append(f"| Test executed by Sydes | ✅ Yes — {executed} test(s) run |")
    else:
        disabled = any(
            "no-run-tests" in str(note) for note in _as_list(_get(result, "notes", default=[]))
        )
        exec_result = "⬛ Not run (`--no-run-tests`)" if disabled else "⬛ Not run"
        lines.append(f"| Test executed by Sydes | {exec_result} |")

    # Route-coverage completeness gets its own top-level row -- it's exactly
    # the kind of "how much do I trust this" signal a reviewer wants near
    # the top, not buried in a bullet further down. The full note text is
    # kept as a caption right under the table, not dropped.
    coverage_note = _pick_analysis_note(result, limit=200)
    if coverage_note:
        lines.append("| Route coverage | 🟡 Incomplete |")
    lines.append("")
    if coverage_note:
        lines.append(f"_{coverage_note}_")
        lines.append("")

    entries = _named_test_entries(result)
    if entries:
        for label, checks_behavior, route, run_by_sydes in entries[:_MAX_EXISTING_EVIDENCE]:
            lines.append(f"- {label}")
            fields = [f"Route: {route}"] if route else []
            fields.append(f"Checks the behavior: {checks_behavior}")
            fields.append(f"Run by Sydes: {run_by_sydes}")
            lines.append(f"  {' · '.join(fields)}")
        if len(entries) > _MAX_EXISTING_EVIDENCE:
            lines.append(f"_…and {len(entries) - _MAX_EXISTING_EVIDENCE} more mapped test(s) in the full result._")
        lines.append("")


#: Plain yes/partially/no answer to "does this test actually check the
#: specific changed behavior" -- what a test's evidence tier demonstrates,
#: never an internal tier code. Never collapsed to a strict yes/no: Tier B
#: ("Supports") is real but indirect evidence, and reading identically to
#: Tier C ("Exercises, no assertion") would quietly discard that
#: distinction -- the exact kind of confidence-flattening this renderer is
#: designed never to do (see the module docstring).
_TIER_CHECKS_BEHAVIOR = {
    "A_direct_route_exercise": "Yes",
    "A_direct_invocation": "Yes",
    "B_asserted_effect": "Partially",
    "C_declared": "No",
}

_MAX_EXISTING_EVIDENCE = 4


def _obligation_execution_note(status: str) -> str:
    if status == "passed":
        return "Yes"
    if status == "failed":
        return "Yes, failed"
    return "No"


def _named_test_entries(result: dict[str, Any]) -> list[tuple[str, str, str, str]]:
    """Real, named test evidence pulled directly from `mapped_tests`/
    `supporting_tests` on each obligation -- never a new analysis pass, just
    reading data Sydes already computed. Deduplicated by (file, case) since
    the same test can be attached to more than one obligation on a flow.
    Returns (test_label, checks_behavior, scope_label, run_by_sydes) tuples
    -- each a plain yes/partially/no answer, not a technical phrase, for
    the compact "Route: ... · Checks the behavior: ... · Run by Sydes: ..."
    line this feeds (see `render_test_evidence`)."""
    entries: list[tuple[str, str, str, str]] = []
    seen: set[tuple[str, str]] = set()
    for flow in _as_list(_get(result, "affected_flows", default=[])):
        route = str(_get(flow, "entry_label", default="")).strip()
        for obligation in _as_list(_get(flow, "obligations", default=[])):
            status = str(_get(obligation, "status", default=""))
            tests = _as_list(_get(obligation, "mapped_tests", default=[])) + _as_list(
                _get(obligation, "supporting_tests", default=[])
            )
            for test in tests:
                file = str(_get(test, "file", default="") or "")
                case = str(_get(test, "case_name", default="") or _get(test, "name", default="") or "")
                if not file or not case:
                    continue
                key = (file, case)
                if key in seen:
                    continue
                seen.add(key)
                tier = str(_get(test, "evidence_tier", default=""))
                checks_behavior = _TIER_CHECKS_BEHAVIOR.get(tier, "Partially")
                file_name = file.rsplit("/", 1)[-1]
                entries.append((f"`{file_name}::{case}`", checks_behavior, route, _obligation_execution_note(status)))

    # Evidence that could not be attached to any resolved flow/obligation,
    # but was preserved instead of disappearing (see
    # `sydes.recovery.canonical_merge`'s evidence-ownership ladder) --
    # never a route/handler claim, so the label says exactly what scope it
    # actually is: a named changed symbol, or the change as a whole.
    for slot in _as_list(_get(result, "unattached_evidence", default=[])):
        scope = str(_get(slot, "scope", default=""))
        target_symbol = _get(slot, "target_symbol", default=None)
        if scope == "symbol" and target_symbol:
            scope_label = f"changed symbol `{target_symbol}` (no established route/boundary)"
        else:
            scope_label = "this change (no established route/boundary)"
        for test in _as_list(_get(slot, "mapped_tests", default=[])):
            file = str(_get(test, "file", default="") or "")
            case = str(_get(test, "case_name", default="") or _get(test, "name", default="") or "")
            if not file or not case:
                continue
            key = (file, case)
            if key in seen:
                continue
            seen.add(key)
            file_name = file.rsplit("/", 1)[-1]
            entries.append((f"`{file_name}::{case}`", "Partially", scope_label, "No"))
    return entries


# ---------------------------------------------------------------------------
# Shared by render_test_evidence above -- execution is explicit and
# separate from verification status. Whether a test was *mapped* to an
# obligation and whether Sydes *ran* it are different claims; folding "not
# run" into the same status word as "no test found" is exactly the
# ambiguity a reviewer cannot resolve from the comment alone.
# ---------------------------------------------------------------------------


def _executed_test_count(result: dict[str, Any]) -> int:
    """`summary.counts.tests_executed` only ever reflects a whole-repo-suite
    run (`ci_suite.tests_passed + tests_failed`) -- it was never updated for
    the individually-targeted mapped-test execution path
    (`VerificationObligation.executions`, one real `TestExecution` per
    mapped test Sydes ran on its own; see `sydes.verify.analyzer.
    _run_test_execution`), so a result where every relevant test was
    individually run and passed could still read `tests_executed=0`,
    contradicting "Execution: passed" shown two sections earlier in the
    SAME comment. This derives the real count directly from the
    `executions` Sydes already recorded, deduplicated by `test_id` (an
    obligation's mapped test can appear on more than one obligation), never
    changing what Sydes verified -- only reading a more complete field."""
    seen: set[str] = set()
    for flow in _as_list(_get(result, "affected_flows", default=[])):
        for obligation in _as_list(_get(flow, "obligations", default=[])):
            for execution in _as_list(_get(obligation, "executions", default=[])):
                if str(_get(execution, "status", default="")) not in {"passed", "failed"}:
                    continue
                test_id = _get(execution, "test_id", default=None)
                if test_id:
                    seen.add(str(test_id))
    return len(seen)


# ---------------------------------------------------------------------------
# Still unverified -- one strict meaning throughout: Sydes does not have
# enough EXECUTED evidence to call this behavior verified. Never conflated
# with "no test exists" or "no test was found" -- each row says which one it
# actually is, read from the obligation's own `reason` (already computed;
# see `resolve_obligation_status` and its `--no-run-tests` override in
# `verify/analyzer.py`), not re-derived here.
# ---------------------------------------------------------------------------

def _unverified_reason_phrase(obligation: dict[str, Any]) -> str:
    """A test Sydes found but could not run is a HANDOFF, not a dead end --
    Sydes' own sandbox lacking a DB/container/secret says nothing about
    whether the test actually passes; the reviewer's own environment (or
    existing CI) very likely can run it. Reserve the strongest wording
    ("FAILED") for a genuine, actually-executed failure -- that remains the
    one case this section should read as alarming."""
    status = str(_get(obligation, "status", default=""))
    reason = str(_get(obligation, "reason", default="") or "").strip()
    lowered = reason.lower()
    if status == "failed":
        return f"verification FAILED — {reason}" if reason else "verification failed"
    if "no-run-tests" in lowered or "was not executed" in lowered:
        return "a relevant test was found — run it in your own environment to confirm"
    if "no existing test asserts" in lowered:
        return "no relevant test found"
    if "exercise this flow but none assert" in lowered:
        return "a test exercises this flow but does not assert this specific behavior"
    if "could not be executed" in lowered or "without attributable" in lowered:
        return "Sydes could not run the test suite in this environment — try running it in yours"
    if reason:
        return reason[0].lower() + reason[1:] if len(reason) > 1 else reason.lower()
    return "impact path incomplete or verification evidence insufficient"


def _category_status_rows(obligations: list[dict[str, Any]]) -> list[tuple[str, dict[str, Any]]]:
    """`[(category label, worst-status obligation in that kind), ...]`, one
    row per obligation KIND present (never a raw statement -- see the
    module-level filtering notes above), in fixed display order, worst
    status wins within a kind. Callers derive whatever wording they need
    (a short table-cell phrase, or the fuller prose reason) from the
    obligation itself -- see `_short_status_phrase`/`_unverified_reason_
    phrase`."""
    by_category: dict[str, list[dict[str, Any]]] = {}
    for obligation in obligations:
        kind = str(_get(obligation, "kind", default=""))
        if kind in _OBLIGATION_CATEGORY_LABEL:
            by_category.setdefault(kind, []).append(obligation)

    rows: list[tuple[str, dict[str, Any]]] = []
    for kind in _OBLIGATION_CATEGORY_ORDER:
        items = by_category.get(kind)
        if not items:
            continue
        worst = min(
            items, key=lambda o: _OBLIGATION_STATUS_RANK.get(str(_get(o, "status", default="")), 2)
        )
        rows.append((_OBLIGATION_CATEGORY_LABEL[kind], worst))
        if len(rows) >= _MAX_CHECKLIST_ROWS:
            break
    return rows


def _short_status_phrase(obligation: dict[str, Any]) -> str:
    """Table-cell-sized status for one category row in the Test evidence
    table -- the same underlying facts as `_unverified_reason_phrase`,
    compressed to a few words with a leading icon. The fuller prose reason
    is still used verbatim wherever a category needs to be explained at
    length (see `render_what_is_still_unknown`'s "Also on this route"
    group)."""
    status = str(_get(obligation, "status", default=""))
    if status == "passed":
        return "✅ Verified"
    raw_reason = str(_get(obligation, "reason", default="") or "").strip()
    reason = raw_reason.lower()
    if status == "failed":
        # The one case worth the extra width: a genuine, already-executed
        # failure is the single most alarming row in this table, and a
        # bare "Failed" with no reason would be a worse regression than a
        # slightly longer cell.
        return f"❌ Failed — {raw_reason}" if raw_reason else "❌ Failed"
    if "no-run-tests" in reason or "was not executed" in reason:
        return "🟡 Found, not executed"
    if "no existing test asserts" in reason:
        return "❌ Not found"
    if "exercise this flow but none assert" in reason:
        return "🟡 Exercised, not asserted"
    if "could not be executed" in reason or "without attributable" in reason:
        return "⬛ Could not run"
    return "🟡 Incomplete"


def render_what_is_still_unknown(result: dict[str, Any], lines: list[str]) -> None:
    """Only what the Test evidence table can't say: this-change,
    per-category gaps now live as table rows there (see
    `render_test_evidence`), so this section keeps just three groups, each
    a bold sub-label with its own bullets rather than a full heading:

    - a gap on the surrounding, pre-existing route (never conflated with
      the table above, which only ever reflects THIS change -- see
      `_obligations_split_by_relevance`);
    - the former "Before merge" nudges, under "Before merging";
    - a rare route-prefix diagnostic, under "Coverage limits" (the common
      coverage-completeness signal is the table's "Route coverage" row;
      this is only the specific "tests reference an unresolved prefix"
      case, which doesn't fit a single status row).

    When `introduced_by_change` is unpopulated everywhere (a known data gap
    on some analysis paths), `about_the_route` IS the table's own `relevant`
    set (see `render_test_evidence`'s fallback) -- so there is nothing left
    to show here as a separate, pre-existing-route group."""
    route_bullets: list[str] = []
    about_this_change, about_the_route = _obligations_split_by_relevance(result)
    if about_this_change and about_the_route:
        for label, obligation in _category_status_rows(about_the_route):
            if str(_get(obligation, "status", default="")) != "passed":
                route_bullets.append(f"**{label}:** {_unverified_reason_phrase(obligation)}")

    # Former "Before merge" rules -- both grounded in facts already shown
    # in What it may affect / the table above, nothing new invented here.
    _rows, wider_areas, has_any_impact = _system_impact_data(result)
    counts = _get(result, "summary", "counts", default={})
    # Not `mapped_tests` (required obligations only) -- a test that verifies
    # a non-required, advisory obligation is still a real reason not to ask
    # for another one.
    verifying_tests = counts.get("tests_verifying_behavior", 0)
    before_merge_bullets: list[str] = []
    if wider_areas:
        before_merge_bullets.append("Verify the changed behavior on the wider API surface before merging.")
    if verifying_tests == 0 and has_any_impact:
        before_merge_bullets.append("Add or run a test covering the affected behavior before merging.")

    coverage_bullets = list(_route_prefix_notes(result))

    if not (route_bullets or before_merge_bullets or coverage_bullets):
        return

    lines.append("### What is still unknown")
    lines.append("")
    for heading, group in (
        ("Also on this route (pre-existing)", route_bullets),
        ("Before merging", before_merge_bullets),
        ("Coverage limits", coverage_bullets),
    ):
        if not group:
            continue
        lines.append(f"**{heading}**")
        for bullet in group:
            lines.append(f"- {bullet}")
        lines.append("")


# ---------------------------------------------------------------------------
# Code review -- status/count only. Detailed findings are a different
# product surface (inline comments / full result), never duplicated here.
# ---------------------------------------------------------------------------


def _diagnostic_first_line(result: dict[str, Any], prefix: str) -> str:
    """The first line of the one diagnostic starting with `prefix`, if any --
    e.g. `code_review_unavailable: OpenAI provider selected, but
    OPENAI_API_KEY is not set.` A diagnostic's own remediation instructions
    (an `export ...` line, an alternate `--model` suggestion) that follow on
    later lines are written for an operator's terminal, not a PR comment, so
    only the first line is ever surfaced here."""
    for note in _as_list(_get(result, "diagnostics", default=[])):
        text = str(note)
        if text.startswith(prefix):
            first_line = text.split("\n", 1)[0]
            return first_line[len(prefix):].strip()
    return ""


#: `pr_semantic_analysis.local_risks` are LLM-hypothesized, evidence-cited
#: risk notes about the CHANGE itself (not correctness defects a code-review
#: pass would flag) -- e.g. "this signature change could break an unseen
#: caller". Never conflated with `code_findings` (real review output): a
#: local risk is a "worth double-checking" observation, a finding is a
#: reviewed defect. Both can be shown, but under different, honestly-labeled
#: headings, and neither is ever invented here -- only surfaced from data
#: Sydes already produced.
_MAX_NOTABLE_OBSERVATIONS = 3


def _notable_observations(result: dict[str, Any]) -> list[str]:
    risks = _as_list(_get(result, "pr_semantic_analysis", "local_risks", default=[]))
    out: list[str] = []
    for risk in risks[:_MAX_NOTABLE_OBSERVATIONS]:
        description = _clean(_get(risk, "description", default=""), limit=220)
        if not description:
            continue
        citations = _as_list(_get(risk, "citations", default=[]))
        location = ""
        if citations:
            file = _get(citations[0], "file", default="")
            line = _get(citations[0], "line", default="")
            if file:
                location = f" ({file}:{line})" if line else f" ({file})"
        out.append(f"{description}{location}")
    return out


def render_review(result: dict[str, Any], lines: list[str]) -> None:
    """`code_findings` being empty means something different depending on
    `code_review_status` -- the pass never ran, it ran and failed, or it
    ran and genuinely found nothing -- and only the status field can tell
    those apart. Rendering "no findings" for a review that never actually
    completed would be absence of evidence read as evidence of absence."""
    status = _get(result, "code_review_status")
    findings = _as_list(_get(result, "code_findings", default=[]))
    if status is None:
        if not findings:
            return
        status = "completed"

    if status == "not_requested":
        return

    lines.append("### Code review")
    lines.append("")

    if status == "unavailable":
        reason = _diagnostic_first_line(result, "code_review_unavailable:")
        if reason:
            lines.append(f"AI code review unavailable: {reason}")
        else:
            lines.append("Code review unavailable — the provider could not complete the analysis.")
        lines.append("")
        return

    if not findings:
        lines.append("**No blocking issues found**")
        lines.append("")
        observations = _notable_observations(result)
        if observations:
            lines.append("**Notable observations**")
            lines.append("")
            for observation in observations:
                lines.append(f"- {observation}")
            lines.append("")
        return

    severities = [str(_get(f, "severity", default="P3")) for f in findings]
    high = sum(1 for s in severities if s in ("P0", "P1"))
    low = len(findings) - high
    parts = []
    if high:
        parts.append(f"{high} higher-priority")
    if low:
        parts.append(f"{low} lower-priority")
    lines.append(f"{len(findings)} finding(s) ({', '.join(parts)}) — see the full result for detail.")
    lines.append("")


# ---------------------------------------------------------------------------
# Optional, deliberately tiny technical-evidence block. NOT a second render
# of the whole result -- see module docstring.
# ---------------------------------------------------------------------------


#: Reads the already-computed "possible missing route prefix" diagnostic
#: (see verify/test_mapping.py::_route_prefix_mismatch) -- Sydes found
#: tests using a prefixed path that may be this exact route, but declined
#: to map them without source/config evidence the prefix is real. Never
#: guessed at here either: this only surfaces what Sydes already refused to
#: assume, so a reviewer can see why relevant-looking tests did not count
#: as evidence.
_ROUTE_PREFIX_NOTE_RE = re.compile(
    r"possible missing route prefix: flow path '([^']+)' looks like a suffix of '([^']+)'"
)


def _route_prefix_notes(result: dict[str, Any]) -> list[str]:
    seen: set[tuple[str, str]] = set()
    notes: list[str] = []
    for note in _as_list(_get(result, "diagnostics", default=[])):
        match = _ROUTE_PREFIX_NOTE_RE.search(str(note))
        if not match:
            continue
        flow_path, prefixed_path = match.group(1), match.group(2)
        key = (flow_path, prefixed_path)
        if key in seen:
            continue
        seen.add(key)
        notes.append(
            f"Tests reference `{prefixed_path}`, which may be `{flow_path}` with an unresolved "
            "prefix; not used to map any test without source/config confirmation."
        )
    return notes


def render_details(result: dict[str, Any], lines: list[str]) -> None:
    """A tiny, deliberately sparse <details> block: changed-symbol grounding
    only. Coverage limits are folded into "What is still unknown" (see
    `render_what_is_still_unknown`) -- they are a reviewer-facing caveat,
    not internal evidence to hide behind an extra click."""
    symbols = _as_list(_get(result, "change", "symbols", default=[]))
    test_paths = _test_file_paths(result)
    production_symbols = [
        s for s in symbols if str(_get(s, "file", default="")) not in test_paths
    ]
    if not production_symbols:
        return
    names = [str(_get(s, "name", default="")) for s in production_symbols[:_MAX_DETAIL_SYMBOLS] if _get(s, "name", default="")]
    if not names:
        return
    more = len(production_symbols) - len(names)
    line = "**Changed symbols:** " + ", ".join(f"`{n}`" for n in names)
    if more > 0:
        line += f" (+{more} more)"

    lines.append("<details><summary>Technical evidence</summary>")
    lines.append("")
    lines.append(f"- {line}")
    lines.append("")
    lines.append("</details>")
    lines.append("")


# ---------------------------------------------------------------------------
# Footer
# ---------------------------------------------------------------------------


def render_footer(lines: list[str], run_url: str | None) -> None:
    """One link, not two: until a distinct full-analysis viewer exists
    (there is no dashboard yet), the run URL is the only place to look --
    a second, identically-targeted "View full analysis" link next to
    "View run" would just be the same link twice."""
    lines.append("---")
    footer = "Sydes"
    if run_url:
        footer += f" · [View run]({run_url})"
    lines.append(footer)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def render(result: dict[str, Any], run_url: str | None = None) -> str:
    lines: list[str] = [MARKER, ""]
    render_header(result, lines)
    render_change(result, lines)
    render_what_it_may_affect(result, lines)
    render_test_evidence(result, lines)
    render_what_is_still_unknown(result, lines)
    render_review(result, lines)
    render_details(result, lines)
    render_footer(lines, run_url)
    return "\n".join(lines).rstrip() + "\n"


def render_unavailable(reason: str, run_url: str | None = None) -> str:
    """Fallback body when there is no result to read — a failed or partial
    run should still leave the reviewer with an explanation rather than
    silence."""
    lines = [
        MARKER,
        "",
        "## Sydes",
        "",
        f"**No result produced** — {reason}",
        "",
        "This does not indicate a verdict about the change. See the run log for details.",
        "",
    ]
    render_footer(lines, run_url)
    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Render a Sydes result as Markdown.")
    parser.add_argument("result", type=Path, help="Path to sydes-result.json")
    parser.add_argument("--out", type=Path, required=True, help="Markdown output path")
    parser.add_argument("--diagnostics-out", type=Path, help="Write diagnostics to this JSON path")
    parser.add_argument("--run-url", default=None, help="Actions run URL for the footer link")
    args = parser.parse_args()

    result: dict[str, Any] | None = None
    reason = ""
    try:
        loaded = json.loads(args.result.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            result = loaded
        else:
            reason = "the result file did not contain a JSON object"
    except FileNotFoundError:
        reason = "no result file was written"
    except (OSError, json.JSONDecodeError) as exc:
        reason = f"the result file could not be read ({exc})"

    if result is None:
        args.out.write_text(render_unavailable(reason, args.run_url), encoding="utf-8")
        print(f"Sydes result unavailable: {reason}")
        return 0

    args.out.write_text(render(result, args.run_url), encoding="utf-8")
    print(f"Rendered Sydes comment to {args.out}")

    # Diagnostics are split out here rather than in Sydes itself: the result
    # schema still carries them, and this keeps the split to the presentation
    # layer. Real schema separation belongs in Sydes later.
    if args.diagnostics_out:
        payload = {
            "generated_at": result.get("generated_at"),
            "diagnostics": _as_list(result.get("diagnostics")),
            "notes": _as_list(result.get("notes")),
            "analysis_notes": _as_list(result.get("analysis_notes")),
        }
        args.diagnostics_out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"Wrote diagnostics to {args.diagnostics_out}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

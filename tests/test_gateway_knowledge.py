"""Knowledge loading: the shipped sample data, and how a miss behaves.

Two properties matter here. A malformed knowledge base must fail at load, so a
broken deployment is caught when the gateway starts rather than by an agent
getting "not found" for three weeks. A *valid* knowledge base that simply does
not hold the answer must return a miss, because an agent asking about a service
that does not exist is normal traffic.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_platform.gateway.knowledge import (
    Assertion,
    KnowledgeError,
    KnowledgeSource,
    ServiceContext,
    YamlKnowledgeSource,
    default_knowledge_dir,
    load_sample_knowledge,
)

SAMPLE_SERVICES = """\
services:
  - name: checkout-api
    aliases: [checkout]
    owner_team: payments-platform
    owner_contact: "#payments-platform"
    tier: tier-1
    criticality: critical
    repo: github.com/acme-internal/checkout-api
    data_classification: pci
    description: Payment authorisation.
    dependencies: [ledger-service, postgres-checkout]
"""

SAMPLE_CONTROLS = """\
change_classes:
  - change_class: schema-migration
    aliases: [db-migration]
    description: DDL against a live store.
    controls:
      - control_id: CTL-DB-001
        title: Backward-compatible migration plan
        standard: ENG-STD-004
        requirement: Expand then contract.
        evidence: Migration plan in the ticket.
      - control_id: CTL-DB-007
        title: Tier-1 change window
        standard: OPS-STD-002
        requirement: Run inside an approved window.
"""

SAMPLE_ASSERTIONS = """\
assertions:
  - subject: checkout-api
    statement: Latency budget is 250 ms p99.
    source: ADR-0114
    recorded_at: 2026-06-02
    confidence: high
  - subject: checkout-api
    statement: Consent lookups are cached.
    source: INC-2291 postmortem
    recorded_at: 2026-05-14
"""


def write_knowledge(
    directory: Path,
    *,
    services: str | None = SAMPLE_SERVICES,
    controls: str | None = SAMPLE_CONTROLS,
    assertions: str | None = SAMPLE_ASSERTIONS,
) -> Path:
    """Materialise a knowledge directory; pass None to omit a file."""
    directory.mkdir(parents=True, exist_ok=True)
    for filename, content in (
        ("services.yaml", services),
        ("controls.yaml", controls),
        ("assertions.yaml", assertions),
    ):
        if content is not None:
            (directory / filename).write_text(content, encoding="utf-8")
    return directory


@pytest.fixture
def source(tmp_path: Path) -> YamlKnowledgeSource:
    return YamlKnowledgeSource.from_directory(write_knowledge(tmp_path / "knowledge"))


def test_yaml_source_satisfies_the_knowledge_source_protocol(
    source: YamlKnowledgeSource,
) -> None:
    assert isinstance(source, KnowledgeSource)


def test_service_context_loads_every_field(source: YamlKnowledgeSource) -> None:
    context = source.service_context("checkout-api")
    assert context == ServiceContext(
        name="checkout-api",
        owner_team="payments-platform",
        owner_contact="#payments-platform",
        criticality="critical",
        dependencies=("ledger-service", "postgres-checkout"),
        tier="tier-1",
        repo="github.com/acme-internal/checkout-api",
        data_classification="pci",
        description="Payment authorisation.",
        aliases=("checkout",),
    )


def test_lookups_tolerate_model_phrasing(source: YamlKnowledgeSource) -> None:
    """Case, surrounding whitespace and aliases all resolve to the same entry."""
    canonical = source.service_context("checkout-api")
    for spelling in ("Checkout-API", "  checkout-api  ", "checkout", "CHECKOUT"):
        assert source.service_context(spelling) is canonical


def test_recall_accepts_the_same_aliases_as_a_service_lookup(
    source: YamlKnowledgeSource,
) -> None:
    """The two read tools must agree on what a name means.

    `lookup_service_context("checkout")` resolves to `checkout-api`. If `recall`
    does not, an agent that used the alias successfully once is told we hold no
    assertions about a service we hold three about — a false negative it cannot
    tell apart from "nothing recorded".
    """
    canonical = source.assertions("checkout-api")
    assert canonical, "fixture must hold assertions for the canonical name"
    for spelling in ("checkout", "Checkout", "  checkout  "):
        assert source.service_context(spelling) is not None
        assert source.assertions(spelling) == canonical


def test_recall_on_a_non_service_subject_still_works(tmp_path: Path) -> None:
    """Alias resolution must not narrow `recall` to services only."""
    directory = write_knowledge(
        tmp_path / "knowledge",
        assertions="assertions:\n"
        "  - subject: evidence-log\n"
        "    statement: Rows are written at the point of action.\n"
        "    source: PLAN.md Section 6\n",
    )
    loaded = YamlKnowledgeSource.from_directory(directory)
    assert loaded.service_context("evidence-log") is None
    assert len(loaded.assertions("evidence-log")) == 1


def test_unknown_subjects_are_misses_not_exceptions(source: YamlKnowledgeSource) -> None:
    assert source.service_context("no-such-service") is None
    assert source.control_requirements("no-such-change-class") is None
    assert source.assertions("no-such-subject") == ()


def test_control_requirements_expose_distinct_standards(
    source: YamlKnowledgeSource,
) -> None:
    requirements = source.control_requirements("db-migration")
    assert requirements is not None
    assert requirements.change_class == "schema-migration"
    assert [c.control_id for c in requirements.controls] == ["CTL-DB-001", "CTL-DB-007"]
    assert requirements.standards == ("ENG-STD-004", "OPS-STD-002")
    assert requirements.controls[1].evidence is None


def test_assertions_come_back_newest_first(source: YamlKnowledgeSource) -> None:
    """An agent that reads only the first assertion should read the latest one."""
    found = source.assertions("checkout-api")
    assert [a.recorded_at for a in found] == ["2026-06-02", "2026-05-14"]
    assert found[1] == Assertion(
        subject="checkout-api",
        statement="Consent lookups are cached.",
        source="INC-2291 postmortem",
        recorded_at="2026-05-14",
        confidence="unknown",
    )


def test_yaml_dates_are_stored_as_text(source: YamlKnowledgeSource) -> None:
    """PyYAML parses a bare date into `datetime.date`; evidence stays JSON-safe."""
    recorded_at = source.assertions("checkout-api")[0].recorded_at
    assert isinstance(recorded_at, str)
    assert recorded_at == "2026-06-02"


def test_missing_directory_fails_at_load(tmp_path: Path) -> None:
    with pytest.raises(KnowledgeError, match="knowledge directory not found"):
        YamlKnowledgeSource.from_directory(tmp_path / "absent")


def test_missing_file_fails_at_load(tmp_path: Path) -> None:
    directory = write_knowledge(tmp_path / "knowledge", assertions=None)
    with pytest.raises(KnowledgeError, match="assertions.yaml"):
        YamlKnowledgeSource.from_directory(directory)


def test_malformed_yaml_fails_at_load(tmp_path: Path) -> None:
    directory = write_knowledge(tmp_path / "knowledge", services="services: [oops")
    with pytest.raises(KnowledgeError, match="invalid YAML"):
        YamlKnowledgeSource.from_directory(directory)


def test_wrong_shape_fails_at_load(tmp_path: Path) -> None:
    directory = write_knowledge(tmp_path / "knowledge", services="services: not-a-list")
    with pytest.raises(KnowledgeError, match="must be a list"):
        YamlKnowledgeSource.from_directory(directory)


def test_missing_required_field_names_the_entry(tmp_path: Path) -> None:
    directory = write_knowledge(
        tmp_path / "knowledge",
        services="services:\n  - name: orphan\n",
    )
    with pytest.raises(KnowledgeError, match=r"services\[0\]: missing required field 'owner_team'"):
        YamlKnowledgeSource.from_directory(directory)


def test_duplicate_alias_fails_at_load(tmp_path: Path) -> None:
    """Two services claiming one name is ambiguity we refuse to resolve silently."""
    directory = write_knowledge(
        tmp_path / "knowledge",
        services=SAMPLE_SERVICES
        + """\
  - name: checkout-api-v2
    aliases: [Checkout]
    owner_team: payments-platform
    owner_contact: "#payments-platform"
    criticality: high
""",
    )
    with pytest.raises(KnowledgeError, match="duplicate service key"):
        YamlKnowledgeSource.from_directory(directory)


def test_empty_section_loads_as_empty(tmp_path: Path) -> None:
    directory = write_knowledge(tmp_path / "knowledge", assertions="assertions:\n")
    loaded = YamlKnowledgeSource.from_directory(directory)
    assert loaded.assertions("checkout-api") == ()
    assert loaded.service_context("checkout-api") is not None


# -- The dataset that actually ships ------------------------------------------


def test_sample_dataset_is_present_and_loads() -> None:
    """`data/knowledge` must load without configuration, network or credentials."""
    sample = load_sample_knowledge()
    assert default_knowledge_dir().is_dir()
    assert "checkout-api" in sample.known_services()
    assert "schema-migration" in sample.known_change_classes()


#: Dependencies in `services.yaml` that legitimately have no service entry:
#: infrastructure and platform components, not services with an owning team.
#: Listing them by name is what makes the test below able to fail — a typo in a
#: real service name is neither a known service nor on this list.
NON_SERVICE_DEPENDENCIES = frozenset(
    {
        "postgres-checkout",
        "postgres-ledger",
        "postgres-profile",
        "kafka-settlement",
        "bigquery-reporting",
        "vertex-endpoint-fraud-v4",
        "feature-store",
        "consent-registry",
    }
)


def _sample_dependencies() -> set[str]:
    sample = load_sample_knowledge()
    dependencies: set[str] = set()
    for name in sample.known_services():
        context = sample.service_context(name)
        assert context is not None, f"known service {name!r} does not resolve"
        dependencies.update(context.dependencies)
    return dependencies


def test_every_sample_dependency_is_a_known_service_or_declared_infrastructure() -> None:
    """No dependency is a typo.

    The earlier form of this test asked whether `service_context(dep).name` was
    in `known_services()`, which is true by construction for anything the lookup
    returns and false-negative-proof for everything else — it could not fail.
    Comparing against an explicit list of non-service dependencies can.
    """
    known = set(load_sample_knowledge().known_services())
    unaccounted = {
        dependency
        for dependency in _sample_dependencies()
        if dependency not in known and dependency not in NON_SERVICE_DEPENDENCIES
    }
    assert unaccounted == set(), f"dependencies naming nothing we hold: {sorted(unaccounted)}"


def test_the_infrastructure_list_cannot_be_padded() -> None:
    """The allowlist above is only honest if every entry is actually referenced.

    Otherwise the cheap way to fix a broken dependency is to add the typo to the
    list, and the test stops meaning anything again.
    """
    unused = NON_SERVICE_DEPENDENCIES - _sample_dependencies()
    assert unused == set(), f"declared but unreferenced: {sorted(unused)}"


def test_sample_assertions_all_cite_a_source() -> None:
    """§3.3's recall exists to retrieve prior assertions, not unattributed claims."""
    sample = load_sample_knowledge()
    for assertions in sample.subject_assertions.values():
        for assertion in assertions:
            assert assertion.source.strip()


def test_environment_variable_overrides_the_sample_dataset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Phase 3 repoints the gateway at real data without a code change."""
    directory = write_knowledge(tmp_path / "elsewhere")
    monkeypatch.setenv("AGENT_PLATFORM_KNOWLEDGE_DIR", str(directory))
    assert default_knowledge_dir() == directory
    assert load_sample_knowledge().known_services() == ("checkout-api",)

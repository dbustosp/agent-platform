"""The knowledge behind the gateway's read tools.

Section 3.3 gives the gateway three read tools. This module is what they read.
The interface is deliberately **three methods wide** — one per tool, nothing
speculative. Phase 3 replaces these YAML files with a real knowledge graph; if
that swap requires editing `tools.py`, the interface was too wide and we leaked
storage concerns into the tool surface (P1, and P5's "can we swap the gateway's
storage without touching the agent?").

Local-first by construction: files on disk, no client, no credentials, no
network. The plan's Phase 1 runs on an engineer's machine, and §3.4 bans any
hosted service in the runtime critical path — including ours.

A lookup that finds nothing returns a miss, never an exception. Agents ask about
services that do not exist; that is a normal answer, not a fault, and the
gateway must be able to log it as one.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import yaml

#: Filenames the file-backed source expects inside a knowledge directory.
SERVICES_FILE = "services.yaml"
CONTROLS_FILE = "controls.yaml"
ASSERTIONS_FILE = "assertions.yaml"

#: Overrides the shipped sample dataset. Set by the CLI or a deployment.
KNOWLEDGE_DIR_ENV = "AGENT_PLATFORM_KNOWLEDGE_DIR"


class KnowledgeError(RuntimeError):
    """The knowledge base is missing or malformed.

    Raised at load time, never at lookup time. A broken deployment should fail
    when the gateway starts, not silently answer "not found" to every question
    an agent asks for the next three weeks.
    """


@dataclass(frozen=True, slots=True)
class ServiceContext:
    """Ownership, dependencies and criticality for one service or repo."""

    name: str
    owner_team: str
    owner_contact: str
    criticality: str
    dependencies: tuple[str, ...] = ()
    tier: str | None = None
    repo: str | None = None
    data_classification: str | None = None
    description: str = ""
    aliases: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe projection, grouped the way Section 3.3 words the tool."""
        return {
            "service": self.name,
            "ownership": {
                "team": self.owner_team,
                "contact": self.owner_contact,
            },
            "dependencies": list(self.dependencies),
            "criticality": self.criticality,
            "tier": self.tier,
            "repo": self.repo,
            "data_classification": self.data_classification,
            "description": self.description,
        }


@dataclass(frozen=True, slots=True)
class Control:
    """One applicable control, traceable to the standard that demands it."""

    control_id: str
    title: str
    standard: str
    requirement: str
    evidence: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "control_id": self.control_id,
            "title": self.title,
            "standard": self.standard,
            "requirement": self.requirement,
            "evidence": self.evidence,
        }


@dataclass(frozen=True, slots=True)
class ControlRequirements:
    """Every control that applies to a class of change."""

    change_class: str
    description: str
    controls: tuple[Control, ...] = ()

    @property
    def standards(self) -> tuple[str, ...]:
        """Distinct standards referenced, in first-seen order."""
        seen: dict[str, None] = {}
        for control in self.controls:
            seen.setdefault(control.standard, None)
        return tuple(seen)

    def to_dict(self) -> dict[str, Any]:
        return {
            "change_class": self.change_class,
            "description": self.description,
            "controls": [c.to_dict() for c in self.controls],
            "standards": list(self.standards),
        }


@dataclass(frozen=True, slots=True)
class Assertion:
    """A prior statement about a subject, with where it came from.

    `source` is not decoration. An assertion an agent cannot trace back to an
    incident, a review or a decision record is a rumour, and the gateway should
    not launder rumours into an agent's context.
    """

    subject: str
    statement: str
    source: str
    recorded_at: str = ""
    confidence: str = "unknown"

    def to_dict(self) -> dict[str, Any]:
        return {
            "subject": self.subject,
            "statement": self.statement,
            "source": self.source,
            "recorded_at": self.recorded_at,
            "confidence": self.confidence,
        }


@runtime_checkable
class KnowledgeSource(Protocol):
    """Everything the three read tools are allowed to ask of storage.

    Three methods, all read-only, all returning plain dataclasses. Anything a
    future knowledge graph offers beyond this (traversal, ranking, embeddings)
    stays behind the same three questions until a tool actually needs it.
    """

    def service_context(self, service: str) -> ServiceContext | None:
        """Return context for `service`, or None if we hold none."""
        ...

    def control_requirements(self, change_class: str) -> ControlRequirements | None:
        """Return the controls applicable to `change_class`, or None."""
        ...

    def assertions(self, subject: str) -> tuple[Assertion, ...]:
        """Return prior assertions about `subject`; empty tuple for a miss."""
        ...


def _normalise(value: Any) -> str:
    """Fold a lookup key so a model's phrasing matches our filing.

    Models write `Checkout API`, `checkout-api ` and `CHECKOUT-API` for the
    same thing. Case and whitespace differences are not interesting enough to
    charge an agent a failed lookup for.
    """
    return " ".join(str(value).split()).strip().lower()


def _as_text(value: Any) -> str:
    """YAML turns bare dates into `datetime.date`; evidence stays text."""
    if value is None:
        return ""
    return str(value)


def _as_tuple(value: Any, *, field_name: str, where: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str) or not isinstance(value, Iterable):
        msg = f"{where}: {field_name!r} must be a list, got {type(value).__name__}"
        raise KnowledgeError(msg)
    return tuple(str(item) for item in value)


def _require(entry: Mapping[str, Any], key: str, where: str) -> Any:
    if key not in entry or entry[key] in (None, ""):
        msg = f"{where}: missing required field {key!r}"
        raise KnowledgeError(msg)
    return entry[key]


def _load_section(path: Path, top_key: str) -> list[Mapping[str, Any]]:
    """Read one YAML file and return the list under `top_key`."""
    if not path.is_file():
        msg = f"knowledge file not found: {path}"
        raise KnowledgeError(msg)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        msg = f"{path}: invalid YAML ({exc})"
        raise KnowledgeError(msg) from exc
    if raw is None:
        return []
    if not isinstance(raw, Mapping):
        msg = f"{path}: expected a mapping at the top level, got {type(raw).__name__}"
        raise KnowledgeError(msg)
    entries = raw.get(top_key)
    if entries is None:
        return []
    if not isinstance(entries, list):
        msg = f"{path}: {top_key!r} must be a list, got {type(entries).__name__}"
        raise KnowledgeError(msg)
    for index, entry in enumerate(entries):
        if not isinstance(entry, Mapping):
            msg = f"{path}: {top_key}[{index}] must be a mapping, got {type(entry).__name__}"
            raise KnowledgeError(msg)
    return entries


@dataclass
class YamlKnowledgeSource:
    """A `KnowledgeSource` backed by three YAML files in one directory.

    Loaded eagerly and held in memory. The pilot dataset is a few kilobytes, and
    an in-memory dict means a denied tool call costs nothing and a permitted one
    cannot fail on a filesystem hiccup halfway through a run.

    Git is the system of record for these files, the same way §3.3 puts skills
    in Git: reviewable, diffable, and portable to whatever reads them next.
    """

    services: dict[str, ServiceContext] = field(default_factory=dict)
    controls: dict[str, ControlRequirements] = field(default_factory=dict)
    subject_assertions: dict[str, tuple[Assertion, ...]] = field(default_factory=dict)

    @classmethod
    def from_directory(cls, directory: str | Path) -> YamlKnowledgeSource:
        """Load `services.yaml`, `controls.yaml` and `assertions.yaml`."""
        root = Path(directory)
        if not root.is_dir():
            msg = f"knowledge directory not found: {root}"
            raise KnowledgeError(msg)
        return cls(
            services=_parse_services(_load_section(root / SERVICES_FILE, "services"), root),
            controls=_parse_controls(_load_section(root / CONTROLS_FILE, "change_classes"), root),
            subject_assertions=_parse_assertions(
                _load_section(root / ASSERTIONS_FILE, "assertions"), root
            ),
        )

    # -- KnowledgeSource -------------------------------------------------

    def service_context(self, service: str) -> ServiceContext | None:
        return self.services.get(_normalise(service))

    def control_requirements(self, change_class: str) -> ControlRequirements | None:
        return self.controls.get(_normalise(change_class))

    def assertions(self, subject: str) -> tuple[Assertion, ...]:
        key = _normalise(subject)
        found = self.subject_assertions.get(key)
        if found is not None:
            return found
        # A subject that names a service by one of its aliases is the same
        # subject. `lookup_service_context` already answers for "checkout" when
        # the entry is filed under "checkout-api"; a `recall` that does not is a
        # false negative about knowledge we hold, and a model has no way to tell
        # that apart from "nothing has been recorded".
        service = self.services.get(key)
        if service is not None:
            return self.subject_assertions.get(_normalise(service.name), ())
        return ()

    # -- Operator conveniences -------------------------------------------
    # Not on the protocol: a real knowledge graph cannot cheaply enumerate
    # itself, and no tool may come to depend on being able to.

    def known_services(self) -> tuple[str, ...]:
        """Canonical service names, sorted. For the CLI and for tests."""
        return tuple(sorted({ctx.name for ctx in self.services.values()}))

    def known_change_classes(self) -> tuple[str, ...]:
        return tuple(sorted({req.change_class for req in self.controls.values()}))


def _parse_services(entries: list[Mapping[str, Any]], root: Path) -> dict[str, ServiceContext]:
    index: dict[str, ServiceContext] = {}
    for position, entry in enumerate(entries):
        where = f"{root / SERVICES_FILE}: services[{position}]"
        name = str(_require(entry, "name", where))
        aliases = _as_tuple(entry.get("aliases"), field_name="aliases", where=where)
        context = ServiceContext(
            name=name,
            owner_team=str(_require(entry, "owner_team", where)),
            owner_contact=str(_require(entry, "owner_contact", where)),
            criticality=str(_require(entry, "criticality", where)),
            dependencies=_as_tuple(
                entry.get("dependencies"), field_name="dependencies", where=where
            ),
            tier=_as_text(entry.get("tier")) or None,
            repo=_as_text(entry.get("repo")) or None,
            data_classification=_as_text(entry.get("data_classification")) or None,
            description=_as_text(entry.get("description")),
            aliases=aliases,
        )
        for key in (name, *aliases):
            normalised = _normalise(key)
            if normalised in index:
                msg = f"{where}: duplicate service key {key!r}"
                raise KnowledgeError(msg)
            index[normalised] = context
    return index


def _parse_controls(entries: list[Mapping[str, Any]], root: Path) -> dict[str, ControlRequirements]:
    index: dict[str, ControlRequirements] = {}
    for position, entry in enumerate(entries):
        where = f"{root / CONTROLS_FILE}: change_classes[{position}]"
        change_class = str(_require(entry, "change_class", where))
        raw_controls = entry.get("controls") or []
        if not isinstance(raw_controls, list):
            msg = f"{where}: 'controls' must be a list, got {type(raw_controls).__name__}"
            raise KnowledgeError(msg)
        controls: list[Control] = []
        for control_position, raw in enumerate(raw_controls):
            control_where = f"{where}.controls[{control_position}]"
            if not isinstance(raw, Mapping):
                msg = f"{control_where}: must be a mapping, got {type(raw).__name__}"
                raise KnowledgeError(msg)
            controls.append(
                Control(
                    control_id=str(_require(raw, "control_id", control_where)),
                    title=str(_require(raw, "title", control_where)),
                    standard=str(_require(raw, "standard", control_where)),
                    requirement=str(_require(raw, "requirement", control_where)),
                    evidence=_as_text(raw.get("evidence")) or None,
                )
            )
        requirements = ControlRequirements(
            change_class=change_class,
            description=_as_text(entry.get("description")),
            controls=tuple(controls),
        )
        aliases = _as_tuple(entry.get("aliases"), field_name="aliases", where=where)
        for key in (change_class, *aliases):
            normalised = _normalise(key)
            if normalised in index:
                msg = f"{where}: duplicate change class key {key!r}"
                raise KnowledgeError(msg)
            index[normalised] = requirements
    return index


def _parse_assertions(
    entries: list[Mapping[str, Any]], root: Path
) -> dict[str, tuple[Assertion, ...]]:
    grouped: dict[str, list[Assertion]] = {}
    for position, entry in enumerate(entries):
        where = f"{root / ASSERTIONS_FILE}: assertions[{position}]"
        subject = str(_require(entry, "subject", where))
        assertion = Assertion(
            subject=subject,
            statement=str(_require(entry, "statement", where)),
            source=str(_require(entry, "source", where)),
            recorded_at=_as_text(entry.get("recorded_at")),
            confidence=_as_text(entry.get("confidence")) or "unknown",
        )
        grouped.setdefault(_normalise(subject), []).append(assertion)
    # Newest first: an agent that reads only the first assertion should read the
    # most recent one. Blank dates sort last rather than crashing the load.
    return {
        subject: tuple(sorted(items, key=lambda a: a.recorded_at, reverse=True))
        for subject, items in grouped.items()
    }


def default_knowledge_dir() -> Path:
    """Where the shipped sample dataset lives.

    `AGENT_PLATFORM_KNOWLEDGE_DIR` wins if set, which is how Phase 3 points the
    gateway at a real dataset without a code change. Otherwise fall back to the
    `data/knowledge` directory in this checkout — the repository root is four
    levels above this file (`src/agent_platform/gateway/knowledge.py`).
    """
    override = os.environ.get(KNOWLEDGE_DIR_ENV)
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[3] / "data" / "knowledge"


def load_sample_knowledge(directory: str | Path | None = None) -> YamlKnowledgeSource:
    """Load the sample dataset the pilot starts from."""
    return YamlKnowledgeSource.from_directory(directory or default_knowledge_dir())

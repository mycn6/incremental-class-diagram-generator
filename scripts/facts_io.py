#!/usr/bin/env python3
"""Own the structured facts inventory: schema, serialization, merge, validation.

The inventory is this skill's primary artifact and its single source of truth -
every reviewable fact about one Git range, each carrying where it came from. The
.drawio diagram is a pure function of it.

This module does not scan source code and does not measure text. It only decides
what a well-formed inventory is, and how two of them combine.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

SCHEMA = 1

CLASS_KINDS = ("class", "interface", "abstract", "enum", "struct")
CHANGE_STATES = ("added", "modified", "removed", "unchanged")
RELATION_KINDS = (
    "inheritance",
    "implementation",
    "association",
    "aggregation",
    "composition",
    "dependency",
)
ORIGINS = ("scan", "agent")

CLASS_KEYS = ("id", "name", "kind", "change", "source", "origin", "fields", "methods")
RELATION_KEYS = (
    "from", "to", "to_declared", "kind", "evidence", "origin",
    "topic", "topic_label",
)
TOPIC_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
CJK_PATTERN = re.compile(r"[\u3400-\u9fff]")


class FactsError(Exception):
    """The inventory cannot be read or cannot be used as it stands."""


@dataclass
class ClassFact:
    id: str
    name: str
    kind: str
    change: str
    source: str
    origin: str = "scan"
    fields: list[str] = field(default_factory=list)
    methods: list[str] = field(default_factory=list)
    # Unknown keys survive a load/dump round trip untouched, so a newer writer
    # can add fields without this module having to learn about them.
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class RelationFact:
    source_id: str
    # None means the declared target is not on this page. That is the documented
    # default for framework and standard-library bases, not a defect.
    target_id: str | None
    target_declared: str
    kind: str
    evidence: str
    origin: str = "scan"
    topic: str = ""
    topic_label: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class Facts:
    scope: dict[str, Any]
    classes: list[ClassFact] = field(default_factory=list)
    relations: list[RelationFact] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def derive_id(name: str, taken: set[str]) -> str:
    """A key that names exactly one class within the file.

    Two files may each declare a User. The first keeps the declared name and the
    rest get a suffix, because a relation that cannot name one of them cannot be
    drawn without guessing between them.
    """
    if name not in taken:
        return name
    number = 2
    while f"{name}#{number}" in taken:
        number += 1
    return f"{name}#{number}"


def _class_to_payload(fact: ClassFact) -> dict[str, Any]:
    payload = {
        "id": fact.id,
        "name": fact.name,
        "kind": fact.kind,
        "change": fact.change,
        "source": fact.source,
        "origin": fact.origin,
        "fields": list(fact.fields),
        "methods": list(fact.methods),
    }
    payload.update(fact.extra)
    return payload


def _class_from_payload(payload: dict[str, Any], where: str) -> ClassFact:
    if not isinstance(payload, dict):
        raise FactsError(f"{where}: a class entry must be an object")
    extra = {key: value for key, value in payload.items() if key not in CLASS_KEYS}
    return ClassFact(
        id=str(payload.get("id", "")),
        name=str(payload.get("name", "")),
        kind=str(payload.get("kind", "")),
        change=str(payload.get("change", "")),
        source=str(payload.get("source", "")),
        origin=str(payload.get("origin", "scan")),
        fields=[str(value) for value in payload.get("fields") or []],
        methods=[str(value) for value in payload.get("methods") or []],
        extra=extra,
    )


def _relation_to_payload(fact: RelationFact) -> dict[str, Any]:
    payload = {
        "from": fact.source_id,
        "to": fact.target_id,
        "to_declared": fact.target_declared,
        "kind": fact.kind,
        "evidence": fact.evidence,
        "origin": fact.origin,
    }
    if fact.topic:
        payload["topic"] = fact.topic
        payload["topic_label"] = fact.topic_label
    payload.update(fact.extra)
    return payload


def _relation_from_payload(payload: dict[str, Any], where: str) -> RelationFact:
    if not isinstance(payload, dict):
        raise FactsError(f"{where}: a relation entry must be an object")
    target = payload.get("to")
    extra = {key: value for key, value in payload.items() if key not in RELATION_KEYS}
    return RelationFact(
        source_id=str(payload.get("from", "")),
        target_id=None if target is None else str(target),
        target_declared=str(payload.get("to_declared", "")),
        kind=str(payload.get("kind", "")),
        evidence=str(payload.get("evidence", "")),
        origin=str(payload.get("origin", "scan")),
        topic=str(payload.get("topic", "")),
        topic_label=str(payload.get("topic_label", "")),
        extra=extra,
    )


def to_payload(facts: Facts) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "scope": dict(facts.scope),
        "classes": [_class_to_payload(fact) for fact in facts.classes],
        "relations": [_relation_to_payload(fact) for fact in facts.relations],
        "files": list(facts.files),
        "warnings": list(facts.warnings),
    }


def from_payload(payload: Any, where: str = "inventory") -> Facts:
    if not isinstance(payload, dict):
        raise FactsError(f"{where}: the inventory must be a JSON object")
    version = payload.get("schema")
    if version != SCHEMA:
        raise FactsError(f"{where}: unsupported schema {version!r}, expected {SCHEMA}")
    scope = payload.get("scope")
    if not isinstance(scope, dict):
        raise FactsError(f"{where}: scope must be an object")
    return Facts(
        scope=dict(scope),
        classes=[
            _class_from_payload(entry, f"{where}: classes[{number}]")
            for number, entry in enumerate(payload.get("classes") or [])
        ],
        relations=[
            _relation_from_payload(entry, f"{where}: relations[{number}]")
            for number, entry in enumerate(payload.get("relations") or [])
        ],
        files=[str(value) for value in payload.get("files") or []],
        warnings=[str(value) for value in payload.get("warnings") or []],
    )


def loads(text: str, where: str = "inventory") -> Facts:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise FactsError(f"{where}: not valid JSON: {exc}") from exc
    return from_payload(payload, where)


def dumps(facts: Facts) -> str:
    """Serialize in a fixed shape so two equal inventories produce equal bytes."""
    return json.dumps(to_payload(facts), ensure_ascii=False, indent=2) + "\n"


def load_facts(path: Path) -> Facts:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise FactsError(f"inventory not found: {path}") from exc
    except UnicodeDecodeError as exc:
        raise FactsError(f"inventory is not valid UTF-8: {path} ({exc})") from exc
    return loads(text, str(path))


def dump_facts(facts: Facts, path: Path) -> None:
    """Write strictly-UTF-8 JSON. Chinese evidence strings live here too."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dumps(facts), encoding="utf-8", newline="\n")


def scope_matches(existing: dict[str, Any], current: dict[str, Any]) -> bool:
    """Whether two inventories describe the same Git range.

    Only the range decides this. generated_at always differs and is not part of
    the question; merging two different ranges would silently mix unrelated
    facts into one file, which no later step could detect.

    "window" carries the day for scopes that are not named by two revisions.
    "--days 7" means different commits today and tomorrow, so the day is what
    keeps those two runs from folding into one file.
    """
    return all(existing.get(key) == current.get(key) for key in ("base", "head", "window"))


def _bad_source(value: str) -> str | None:
    if not value:
        return "source is empty"
    if value.startswith(("/", "\\")) or (len(value) > 1 and value[1] == ":"):
        return f"source must be repository-relative, got {value!r}"
    if ".." in Path(value).parts:
        return f"source must stay inside the repository, got {value!r}"
    return None


def validate_facts(facts: Facts) -> tuple[list[str], list[str]]:
    """Return (errors, warnings).

    Errors are structural: an inventory that has them cannot describe anything,
    so no reader could act on it. Everything whose legality depends on judgement
    is a warning instead - an over-strict validator would push the author back to
    hand-editing the exported XML, which is the failure this inventory exists to
    remove.
    """
    errors: list[str] = []
    warnings: list[str] = []

    seen: set[str] = set()
    for fact in facts.classes:
        if not fact.id:
            errors.append(f"class {fact.name!r} has no id")
            continue
        if fact.id in seen:
            errors.append(f"duplicate class id {fact.id!r}")
        seen.add(fact.id)
        if fact.kind not in CLASS_KINDS:
            errors.append(f"{fact.id}: unsupported kind {fact.kind!r}")
        if fact.change not in CHANGE_STATES:
            errors.append(f"{fact.id}: unsupported change {fact.change!r}")
        if fact.origin not in ORIGINS:
            errors.append(f"{fact.id}: unsupported origin {fact.origin!r}")
        problem = _bad_source(fact.source)
        if problem:
            errors.append(f"{fact.id}: {problem}")

    topic_labels: dict[str, str] = {}
    for number, fact in enumerate(facts.relations, start=1):
        where = f"relation {number} ({fact.source_id} -> {fact.target_declared or fact.target_id})"
        if fact.kind not in RELATION_KINDS:
            errors.append(f"{where}: unsupported kind {fact.kind!r}")
        if fact.origin not in ORIGINS:
            errors.append(f"{where}: unsupported origin {fact.origin!r}")
        if not fact.evidence.strip():
            errors.append(f"{where}: evidence must not be empty")
        if fact.source_id not in seen:
            errors.append(f"{where}: source {fact.source_id!r} is not a class in this inventory")
        if fact.target_id is not None and fact.target_id not in seen:
            errors.append(f"{where}: target {fact.target_id!r} is not a class in this inventory")
        if fact.target_id is None and not fact.target_declared.strip():
            errors.append(f"{where}: an unresolved relation must still name the declared target")
        if bool(fact.topic) != bool(fact.topic_label):
            errors.append(f"{where}: topic and topic_label must appear together")
        if fact.topic:
            if TOPIC_PATTERN.fullmatch(fact.topic) is None:
                errors.append(f"{where}: topic must be a lowercase kebab-case key")
            if CJK_PATTERN.search(fact.topic_label) is None:
                errors.append(f"{where}: topic_label must contain a Chinese business description")
            previous = topic_labels.setdefault(fact.topic, fact.topic_label)
            if previous != fact.topic_label:
                errors.append(
                    f"{where}: topic {fact.topic!r} uses both {previous!r} and {fact.topic_label!r}"
                )
        short = fact.target_declared.rsplit(".", 1)[-1]
        if short and short not in fact.evidence:
            warnings.append(
                f"{where}: evidence does not mention {short!r}, "
                "so a reviewer cannot tell which class the edge points at"
            )

    return errors, warnings


def merge_facts(existing: Facts, fresh: Facts) -> Facts:
    """Fold a re-scan into the inventory it already produced.

    The scan owns what it can read out of the source, so those entries are
    replaced wholesale on every run. The agent owns the judgements it added
    during review, and those cannot be re-derived - dropping them would make
    re-scanning destroy work, which is the failure this contract exists to
    prevent.

    Merge notices are folded into the result's own warnings, so a caller that
    reports the inventory's warnings has already reported these.
    """
    warnings: list[str] = []

    fresh_class_ids = {fact.id for fact in fresh.classes}
    kept_classes: list[ClassFact] = []
    for fact in existing.classes:
        if fact.origin != "agent":
            continue
        if fact.id in fresh_class_ids:
            # The class is now part of the change set, so the scan's own entry
            # is the accurate one and the id can only belong to one of them.
            warnings.append(
                f"{fact.id} is now in the change set; the scanned entry replaces the "
                "reviewed one and its added members are dropped"
            )
            continue
        # No notice for a source outside the change set: reaching one hop out to
        # an unchanged type is exactly what a reviewed entry is for, so warning
        # about it would fire every time and mean nothing.
        kept_classes.append(fact)

    known_ids = fresh_class_ids | {fact.id for fact in kept_classes}
    existing_topics = {
        (fact.source_id, fact.target_id, fact.kind): (fact.topic, fact.topic_label)
        for fact in existing.relations
        if fact.topic and fact.topic_label
    }
    for fact in fresh.relations:
        topic = existing_topics.get((fact.source_id, fact.target_id, fact.kind))
        if topic is not None and not fact.topic:
            fact.topic, fact.topic_label = topic

    fresh_edges = {(fact.source_id, fact.target_id, fact.kind) for fact in fresh.relations}
    kept_relations: list[RelationFact] = []
    for fact in existing.relations:
        if fact.origin != "agent":
            continue
        if (fact.source_id, fact.target_id, fact.kind) in fresh_edges:
            warnings.append(
                f"the scanner now finds {fact.kind} {fact.source_id} -> "
                f"{fact.target_declared}; the scanned entry replaces the reviewed one"
            )
            continue
        if fact.source_id not in known_ids:
            warnings.append(f"reviewed relation from {fact.source_id} has lost that class")
        if fact.target_id is not None and fact.target_id not in known_ids:
            warnings.append(f"reviewed relation to {fact.target_id} has lost that class")
        kept_relations.append(fact)

    return Facts(
        scope=fresh.scope,
        classes=list(fresh.classes) + kept_classes,
        relations=list(fresh.relations) + kept_relations,
        files=list(fresh.files),
        warnings=list(fresh.warnings) + warnings,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", type=Path, metavar="PATH", required=True, help="Inventory JSON to validate")
    args = parser.parse_args(argv)

    try:
        facts = load_facts(args.check)
    except FactsError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    errors, warnings = validate_facts(facts)
    for warning in warnings:
        print(f"WARN: {warning}", file=sys.stderr)
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1

    print(
        f"PASS: {len(facts.classes)} class(es) and {len(facts.relations)} relation(s) "
        "satisfy the inventory contract."
    )
    print("Source truth, member completeness, and evidence quality still require separate review.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

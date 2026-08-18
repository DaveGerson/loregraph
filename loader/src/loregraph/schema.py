from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


class SchemaRegistry:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.data = json.loads(self.path.read_text(encoding="utf-8"))
        self.node_types: dict[str, dict[str, Any]] = self.data["node_types"]
        self.predicates: dict[str, dict[str, Any]] = self.data["predicates"]
        self.profiles: dict[str, dict[str, list[str]]] = self.data["profiles"]
        self.validate()

    def validate(self) -> None:
        identifier = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
        for value in [*self.node_types, *self.predicates]:
            if not identifier.fullmatch(value):
                raise ValueError(f"Unsafe schema identifier: {value!r}")
        for name, profile in self.profiles.items():
            unknown_nodes = set(profile["node_types"]) - set(self.node_types)
            unknown_predicates = set(profile["predicates"]) - set(self.predicates)
            if unknown_nodes or unknown_predicates:
                raise ValueError(
                    f"Profile {name} references unknown nodes={unknown_nodes}, "
                    f"predicates={unknown_predicates}"
                )
        for predicate, rule in self.predicates.items():
            unknown = (set(rule["domain"]) | set(rule["range"])) - set(self.node_types)
            if unknown:
                raise ValueError(f"Predicate {predicate} references unknown node types: {unknown}")

    def allowed_node_types(self, profile: str) -> set[str]:
        return set(self.profiles[profile]["node_types"])

    def allowed_predicates(self, profile: str) -> set[str]:
        return set(self.profiles[profile]["predicates"])

    def pattern_allowed(
        self,
        profile: str,
        predicate: str,
        subject_type: str,
        object_type: str,
    ) -> bool:
        if predicate not in self.allowed_predicates(profile):
            return False
        rule = self.predicates[predicate]
        return subject_type in rule["domain"] and object_type in rule["range"]

    def compact_prompt_schema(self, profile: str) -> str:
        nodes = []
        for label in self.profiles[profile]["node_types"]:
            node = self.node_types[label]
            subtypes = ", ".join(node.get("subtypes", [])) or "open"
            nodes.append(f"- {label}: {node['description']} Subtypes: {subtypes}.")
        predicates = []
        for predicate in self.profiles[profile]["predicates"]:
            rule = self.predicates[predicate]
            predicates.append(
                f"- {predicate}: {'|'.join(rule['domain'])} -> {'|'.join(rule['range'])}"
            )
        return "NODE TYPES\n" + "\n".join(nodes) + "\n\nPREDICATES\n" + "\n".join(predicates)

    def neo4j_simple_schema(self, profile: str) -> dict[str, Any]:
        node_types = []
        for label in self.profiles[profile]["node_types"]:
            node_types.append(
                {
                    "label": label,
                    "description": self.node_types[label]["description"],
                    "properties": [
                        {"name": "name", "type": "STRING"},
                        {"name": "subtype", "type": "STRING"},
                    ],
                    "additional_properties": False,
                }
            )
        relationship_types = [
            {
                "label": predicate,
                "properties": [
                    {"name": "source_quote", "type": "STRING"},
                    {"name": "confidence", "type": "FLOAT"},
                ],
                "additional_properties": False,
            }
            for predicate in self.profiles[profile]["predicates"]
        ]
        patterns = [
            (source, predicate, target)
            for predicate in self.profiles[profile]["predicates"]
            for source in self.predicates[predicate]["domain"]
            for target in self.predicates[predicate]["range"]
            if source in self.profiles[profile]["node_types"]
            and target in self.profiles[profile]["node_types"]
        ]
        return {
            "node_types": node_types,
            "relationship_types": relationship_types,
            "patterns": patterns,
            "additional_node_types": False,
            "additional_relationship_types": False,
        }

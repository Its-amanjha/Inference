"""Pure, conservative transformation of a workflow for another instance."""

from copy import deepcopy
import hashlib
import json
from typing import Any


def fingerprint(value: Any) -> str:
    """Hash canonical JSON, rejecting non-finite numbers and non-JSON values."""
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _literal(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip()) and not value.startswith("=") and "{{" not in value


def _mapping(value: Any) -> None:
    if not isinstance(value, dict) or any(not _literal(k) or not _literal(v) for k, v in value.items()):
        raise ValueError("Mappings must contain literal nonempty string identifiers")


def _references(value: Any, path: str = "parameters") -> list[str]:
    found = []
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}"
            if key == "workflowId":
                found.append(child_path)
            found.extend(_references(child, child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found.extend(_references(child, f"{path}[{index}]"))
    return found


def prepare(workflow: dict, credential_map: dict[str, str], subworkflow_map: dict[str, str]) -> dict:
    """Return a detached writable workflow and blocking mapping diagnostics.

    Structural errors raise ValueError. Missing references and identity ambiguity
    are returned as diagnostics; callers must reject deployment when either list
    is nonempty. Only explicit credential and executeWorkflow fields are rewritten.
    """
    _mapping(credential_map)
    _mapping(subworkflow_map)
    if not isinstance(workflow, dict):
        raise ValueError("Workflow must be an object")
    try:
        fingerprint(workflow)
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise ValueError("Workflow must contain valid finite JSON") from exc
    if not isinstance(workflow.get("name"), str) or not workflow["name"].strip():
        raise ValueError("Workflow needs a nonempty name")
    if not isinstance(workflow.get("nodes"), list) or not isinstance(workflow.get("connections"), dict):
        raise ValueError("Workflow needs nodes and connections")
    if "settings" in workflow and not isinstance(workflow["settings"], dict):
        raise ValueError("Workflow settings must be an object")
    result = {"workflow": deepcopy({k: workflow[k] for k in ("name", "nodes", "connections", "settings") if k in workflow}),
              "required_credentials": [], "mapped": [], "missing": [], "collisions": [], "warnings": []}
    writable = result["workflow"]
    writable.setdefault("settings", {})
    error_workflow = writable["settings"].get("errorWorkflow", "")
    if error_workflow != "":
        location = {"kind": "workflow", "path": "settings.errorWorkflow"}
        if not _literal(error_workflow):
            result["missing"].append({**location, "code": "unsupported_workflow_reference"})
        elif error_workflow not in subworkflow_map:
            result["missing"].append({**location, "id": error_workflow, "code": "workflow_mapping_required"})
        else:
            target_id = subworkflow_map[error_workflow]
            writable["settings"]["errorWorkflow"] = target_id
            result["mapped"].append({**location, "id": error_workflow, "target_id": target_id})
    names: set[str] = set()
    ids: set[str] = set()
    credentials: dict[str, str] = {}
    targets: dict[str, str] = {}
    required: set[tuple[str, str]] = set()
    for node in writable["nodes"]:
        if not isinstance(node, dict) or not isinstance(node.get("name"), str) or not node["name"].strip() or not isinstance(node.get("type"), str) or not node["type"]:
            raise ValueError("Each node needs a name and type")
        name = node["name"]
        if name in names:
            result["collisions"].append({"code": "duplicate_node_name", "node": name})
        names.add(name)
        if "id" in node:
            if not _literal(node["id"]):
                raise ValueError("Node identifiers must be literal strings")
            if node["id"] in ids:
                result["collisions"].append({"code": "duplicate_node_id", "node": name})
            ids.add(node["id"])
        parameters = node.get("parameters", {})
        bindings = node.get("credentials", {})
        if not isinstance(parameters, dict) or not isinstance(bindings, dict):
            raise ValueError("Node parameters and credentials must be objects")
        for kind, binding in bindings.items():
            if not _literal(kind) or not isinstance(binding, dict) or not _literal(binding.get("id")) or not isinstance(binding.get("name", ""), str):
                raise ValueError("Credential bindings need a literal ID, type and optional string name")
            source_id = binding["id"]
            entry = {"id": source_id, "type": kind, "name": binding.get("name", "")}
            if source_id in credentials and credentials[source_id] != kind:
                result["collisions"].append({**entry, "code": "credential_type_conflict", "node": name})
            credentials[source_id] = kind
            if (source_id, kind) not in required:
                required.add((source_id, kind))
                result["required_credentials"].append(entry)
            target_id = credential_map.get(source_id)
            if target_id is None:
                result["missing"].append({**entry, "code": "credential_mapping_required", "kind": "credential", "node": name})
            else:
                if target_id in targets and targets[target_id] != kind:
                    result["collisions"].append({**entry, "target_id": target_id, "code": "target_credential_type_conflict", "node": name})
                targets[target_id] = kind
                binding["id"] = target_id
                result["mapped"].append({**entry, "target_id": target_id, "kind": "credential", "node": name})
        known = node["type"] == "n8n-nodes-base.executeWorkflow"
        for path in _references(parameters):
            if not known or path != "parameters.workflowId":
                result["missing"].append({"code": "unsupported_workflow_reference", "kind": "workflow", "node": name, "path": path})
        if known:
            reference = parameters.get("workflowId")
            source_id = reference.get("value") if isinstance(reference, dict) else reference
            locator_ok = not isinstance(reference, dict) or (reference.get("__rl") is True and reference.get("mode") in ("id", "list"))
            if parameters.get("source", "database") != "database" or not locator_ok or not _literal(source_id):
                result["missing"].append({"code": "unsupported_workflow_reference", "kind": "workflow", "node": name})
            elif source_id not in subworkflow_map:
                result["missing"].append({"code": "workflow_mapping_required", "kind": "workflow", "node": name, "id": source_id})
            else:
                target_id = subworkflow_map[source_id]
                if isinstance(reference, dict):
                    reference["value"] = target_id
                    reference.pop("cachedResultName", None)
                    reference.pop("cachedResultUrl", None)
                else:
                    parameters["workflowId"] = target_id
                result["mapped"].append({"kind": "workflow", "node": name, "id": source_id, "target_id": target_id})
    for source, channels in writable["connections"].items():
        if source not in names or not isinstance(channels, dict):
            raise ValueError("Connections must reference existing nodes")
        for channel, outputs in channels.items():
            if not isinstance(channel, str) or not isinstance(outputs, list):
                raise ValueError("Invalid connection channels")
            for edges in outputs:
                if not isinstance(edges, list):
                    raise ValueError("Invalid connection outputs")
                for edge in edges:
                    if not isinstance(edge, dict) or not isinstance(edge.get("node"), str) or edge["node"] not in names or not isinstance(edge.get("type"), str) or type(edge.get("index")) is not int or edge["index"] < 0:
                        raise ValueError("Invalid connection edge")
    return result

from copy import deepcopy

import pytest

from backend.modules.n8n_promote.transform import fingerprint, prepare


def workflow():
    return {
        "id": "source", "name": "Promotion", "active": True, "versionId": "old",
        "tags": ["private"], "staticData": {"last": 42}, "pinData": {"secret": "data"},
        "nodes": [
            {"id": "a", "name": "Fetch", "type": "n8n-nodes-base.httpRequest",
             "position": [0, 20], "parameters": {"body": {"id": "cred", "literal": ["child", "cred"]}},
             "credentials": {"httpBasicAuth": {"id": "cred", "name": "Account"}}},
            {"id": "b", "name": "Child", "type": "n8n-nodes-base.executeWorkflow",
             "parameters": {"workflowId": "child"}, "disabled": True},
        ],
        "connections": {"Fetch": {"main": [[{"node": "Child", "type": "main", "index": 0}]]}},
        "settings": {"executionOrder": "v1"},
    }


def test_rewrites_known_fields_only_preserves_graph_and_does_not_mutate_source():
    source = workflow()
    original = deepcopy(source)
    result = prepare(source, {"cred": "target-cred"}, {"child": "target-child"})
    output = result["workflow"]
    assert source == original
    assert set(output) == {"name", "nodes", "connections", "settings"}
    assert output["connections"] == source["connections"]
    assert output["nodes"][0]["credentials"]["httpBasicAuth"]["id"] == "target-cred"
    assert output["nodes"][0]["parameters"] == source["nodes"][0]["parameters"]
    assert output["nodes"][1]["parameters"]["workflowId"] == "target-child"
    assert output["nodes"][1]["disabled"] is True
    assert not result["missing"] and not result["collisions"]
    assert result["required_credentials"] == [{"id": "cred", "type": "httpBasicAuth", "name": "Account"}]
    output["connections"]["Fetch"]["main"].clear()
    assert source == original


@pytest.mark.parametrize("mode", ["id", "list"])
def test_resource_locator_is_mapped_and_source_cached_links_are_removed(mode):
    source = workflow()
    source["nodes"][1]["parameters"]["workflowId"] = {
        "__rl": True, "mode": mode, "value": "child", "cachedResultName": "Old", "cachedResultUrl": "/workflow/child",
    }
    result = prepare(source, {"cred": "new"}, {"child": "next"})
    assert result["workflow"]["nodes"][1]["parameters"]["workflowId"] == {"__rl": True, "mode": mode, "value": "next"}
    assert not result["missing"]


def test_unmapped_references_block_even_when_names_are_available():
    result = prepare(workflow(), {}, {})
    assert {item["code"] for item in result["missing"]} == {"credential_mapping_required", "workflow_mapping_required"}


@pytest.mark.parametrize("reference", ["={{ $json.id }}", "{{expression}}", 12, None,
    {"__rl": True, "mode": "url", "value": "child"}, {"value": "child"},
    {"__rl": True, "mode": "list", "value": "={{ $json.child }}"}])
def test_dynamic_and_unknown_workflow_references_block_without_echoing_expression(reference):
    source = workflow()
    source["nodes"][1]["parameters"]["workflowId"] = reference
    result = prepare(source, {"cred": "new"}, {"child": "next"})
    assert result["missing"] == [{"code": "unsupported_workflow_reference", "kind": "workflow", "node": "Child"}]


def test_unknown_node_and_nested_workflow_reference_block_without_rewriting():
    source = workflow()
    source["nodes"][0]["parameters"]["workflowId"] = "child"
    source["nodes"][1]["parameters"]["nested"] = {"workflowId": "child"}
    result = prepare(source, {"cred": "new"}, {"child": "next"})
    assert len(result["missing"]) == 2
    assert all(item["code"] == "unsupported_workflow_reference" for item in result["missing"])
    assert result["workflow"]["nodes"][0]["parameters"]["workflowId"] == "child"
    assert result["workflow"]["nodes"][1]["parameters"]["nested"]["workflowId"] == "child"


def test_execute_workflow_non_database_source_is_not_guessed():
    source = workflow()
    source["nodes"][1]["parameters"]["source"] = "localFile"
    result = prepare(source, {"cred": "new"}, {"child": "next"})
    assert result["missing"][0]["code"] == "unsupported_workflow_reference"


def test_same_source_id_with_conflicting_credential_types_blocks():
    source = workflow()
    source["nodes"][1]["credentials"] = {"otherType": {"id": "cred", "name": "Account"}}
    result = prepare(source, {"cred": "new"}, {"child": "next"})
    assert {entry["code"] for entry in result["collisions"]} == {"credential_type_conflict", "target_credential_type_conflict"}


def test_different_credential_types_cannot_share_a_target():
    source = workflow()
    source["nodes"][1]["credentials"] = {"otherType": {"id": "other", "name": "Account"}}
    result = prepare(source, {"cred": "new", "other": "new"}, {"child": "next"})
    assert result["collisions"][0]["code"] == "target_credential_type_conflict"


def test_repeated_binding_is_deduplicated_but_same_names_do_not_establish_identity():
    source = workflow()
    source["nodes"][1]["credentials"] = deepcopy(source["nodes"][0]["credentials"])
    result = prepare(source, {"cred": "new"}, {"child": "next"})
    assert len(result["required_credentials"]) == 1
    source["nodes"][1]["credentials"]["httpBasicAuth"]["id"] = "another"
    result = prepare(source, {"cred": "new"}, {"child": "next"})
    assert result["missing"][0]["id"] == "another"


@pytest.mark.parametrize("mutate", [
    lambda w: w.update(nodes={}), lambda w: w.update(settings=[]), lambda w: w.update(name=""),
    lambda w: w["nodes"][0].update(parameters=[]), lambda w: w["nodes"][0].update(credentials=[]),
    lambda w: w["nodes"][0]["credentials"].update(httpBasicAuth={"name": "Account"}),
    lambda w: w["nodes"][0]["credentials"]["httpBasicAuth"].update(id=12),
    lambda w: w["connections"]["Fetch"]["main"][0][0].update(node="missing"),
    lambda w: w["connections"]["Fetch"]["main"][0][0].update(node=[]),
    lambda w: w["connections"]["Fetch"]["main"][0][0].update(index=True),
    lambda w: w["settings"].update(invalid=float("nan")),
])
def test_malformed_workflow_rejected(mutate):
    source = workflow()
    mutate(source)
    with pytest.raises(ValueError):
        prepare(source, {}, {})


@pytest.mark.parametrize("mapping", [{"cred": ""}, {"cred": 12}, {12: "target"}, {"cred": "={{ $json.id }}"}, []])
def test_invalid_mapping_rejected(mapping):
    with pytest.raises(ValueError):
        prepare(workflow(), mapping, {})


def test_duplicate_node_identity_blocks():
    source = workflow()
    source["nodes"].append(deepcopy(source["nodes"][0]))
    result = prepare(source, {}, {})
    assert {entry["code"] for entry in result["collisions"]} == {"duplicate_node_name", "duplicate_node_id"}


def test_fingerprint_is_order_independent_exact_and_rejects_nan():
    assert fingerprint({"a": 1, "b": "é"}) == fingerprint({"b": "é", "a": 1})
    assert fingerprint({"a": [1, 2]}) != fingerprint({"a": [2, 1]})
    assert fingerprint({"a": "1"}) != fingerprint({"a": 1})
    with pytest.raises(ValueError):
        fingerprint({"number": float("nan")})


def test_error_workflow_setting_requires_explicit_mapping_and_preserves_source():
    source = workflow()
    source["settings"]["errorWorkflow"] = "errors-source"
    original = deepcopy(source)
    result = prepare(source, {"cred": "new"}, {"child": "next", "errors-source": "errors-target"})
    assert not result["missing"]
    assert result["workflow"]["settings"]["errorWorkflow"] == "errors-target"
    assert {"kind": "workflow", "path": "settings.errorWorkflow", "id": "errors-source", "target_id": "errors-target"} in result["mapped"]
    assert source == original
    missing = prepare(source, {"cred": "new"}, {"child": "next"})
    assert missing["missing"] == [{"kind": "workflow", "path": "settings.errorWorkflow", "id": "errors-source", "code": "workflow_mapping_required"}]


@pytest.mark.parametrize("reference", [None, 12, {}, "={{ $json.errorWorkflow }}", "{{ expression }}"])
def test_dynamic_or_unknown_error_workflow_setting_blocks(reference):
    source = workflow()
    source["settings"]["errorWorkflow"] = reference
    result = prepare(source, {"cred": "new"}, {"child": "next"})
    assert result["missing"] == [{"kind": "workflow", "path": "settings.errorWorkflow", "code": "unsupported_workflow_reference"}]


def test_empty_error_workflow_setting_is_allowed():
    source = workflow()
    source["settings"]["errorWorkflow"] = ""
    result = prepare(source, {"cred": "new"}, {"child": "next"})
    assert not result["missing"]
    assert result["workflow"]["settings"]["errorWorkflow"] == ""

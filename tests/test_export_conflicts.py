import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "export_solved_issues",
    Path(__file__).parents[1] / "scripts" / "export_solved_issues.py",
)
export = importlib.util.module_from_spec(spec)
spec.loader.exec_module(export)


def block(sid, title, fix="same"):
    return f"## {sid}: {title}\n**Fix:** {fix}\n"


def test_spacing_and_trailing_rule_are_not_conflicts():
    file = {"SI-001": block("SI-001", "Port in use") + "\n---\n"}
    db = {"SI-001": block("SI-001", "Port in use")}
    assert export.find_conflicts(file, db) == ([], [])


def test_changed_body_is_a_conflict_without_title_warning():
    file = {"SI-002": block("SI-002", "Port in use", "old")}
    db = {"SI-002": block("SI-002", "Port in use", "corrected")}
    assert export.find_conflicts(file, db) == (["SI-002"], [])


def test_different_issue_under_same_id_is_flagged():
    file = {"SI-091": block("SI-091", "JSON parsing fails on control characters")}
    db = {"SI-091": block("SI-091", "Webcam hard-fails above 640x480")}
    assert export.find_conflicts(file, db) == (["SI-091"], ["SI-091"])


def test_ids_on_one_side_only_are_ignored():
    assert export.find_conflicts({"SI-1": "a"}, {"SI-2": "b"}) == ([], [])

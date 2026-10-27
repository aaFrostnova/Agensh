import json

from peer_influence.common import canonical_json, sha256_bytes, tree_sha256, write_json_atomic


def test_tree_sha256_detects_content_rename_and_addition(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "a/x.txt").write_text("one")
    (tmp_path / "y.txt").write_text("two")
    base = tree_sha256(tmp_path)
    assert base == tree_sha256(tmp_path)

    (tmp_path / "y.txt").write_text("TWO")
    changed = tree_sha256(tmp_path)
    assert changed != base

    (tmp_path / "y.txt").write_text("two")
    (tmp_path / "y.txt").rename(tmp_path / "z.txt")
    assert tree_sha256(tmp_path) != base

    (tmp_path / "z.txt").rename(tmp_path / "y.txt")
    assert tree_sha256(tmp_path) == base
    (tmp_path / "extra").write_text("")
    assert tree_sha256(tmp_path) != base


def test_canonical_json_is_key_order_independent():
    assert canonical_json({"b": 1, "a": [1, 2]}) == canonical_json({"a": [1, 2], "b": 1})
    assert sha256_bytes(canonical_json({"a": 1}).encode()) == sha256_bytes(b'{"a":1}')


def test_write_json_atomic_replaces_file_without_leaving_temp(tmp_path):
    target = tmp_path / "state.json"
    write_json_atomic(target, {"n": 1})
    write_json_atomic(target, {"n": 2})
    assert json.loads(target.read_text()) == {"n": 2}
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]

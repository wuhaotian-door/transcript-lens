import json
import os
from core.runtime_cache import MARKER, clean_stale


def test_bundle_cleanup_requires_marker_age_and_both_processes_gone(tmp_path):
    root = tmp_path / "SGMS_RunCache"
    root.mkdir()
    current = root / "_MEI0000002a9"
    current.mkdir()

    def bundle(name, parent, child, age):
        folder = root / name
        folder.mkdir()
        if parent is not None:
            marker = folder / MARKER
            marker.write_text(json.dumps({"parent": parent, "child": child}), encoding="utf-8")
            os.utime(marker, (200_000 - age, 200_000 - age))
        return folder

    removed = bundle("_MEI0000004f2", 79, 150, 90_000)
    running = bundle("_MEI000000692", 105, 456, 90_000)
    fresh = bundle("_MEI000000842", 132, 155, 3)
    unrelated = bundle("_MEI000000f51", None, None, 100_000)
    wrong_marker = bundle("_MEI000000441", 777, 150, 100_000)
    assert clean_stale(root, current, process_gone=lambda pid: pid != 456, now=200_000) == [removed.name]
    assert all(path.exists() for path in (current, running, fresh, unrelated, wrong_marker))

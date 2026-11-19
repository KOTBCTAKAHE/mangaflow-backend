from pathlib import Path

from app.services.images import _writable_dir


def test_unwritable_cache_dir_falls_back_to_tmp(tmp_path: Path):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")  # a file where the cache dir should be: mkdir fails
    d = _writable_dir(str(blocker / "cache"))
    assert d != blocker / "cache"
    assert d.name == "mangaflow-images" and d.is_dir()


def test_writable_cache_dir_is_kept(tmp_path: Path):
    assert _writable_dir(str(tmp_path / "c")) == tmp_path / "c"

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


def test_resize_is_cheap_and_still_right():
    import io

    from PIL import Image

    from app.services.images import _resize

    buf = io.BytesIO()
    Image.new("RGB", (2400, 3600), (200, 40, 40)).save(buf, "JPEG", quality=90)
    data, ctype = _resize(buf.getvalue(), 300, "image/jpeg")
    assert ctype == "image/webp"
    with Image.open(io.BytesIO(data)) as im:
        assert im.size == (300, 450)
        r, g, b = im.convert("RGB").getpixel((150, 225))
        assert abs(r - 200) < 12 and g < 60


def test_prune_drops_the_oldest_files(tmp_path):
    import os

    from app.services.images import _prune

    for i in range(10):
        p = tmp_path / "ab" / f"f{i}"
        p.parent.mkdir(exist_ok=True)
        p.write_bytes(b"x" * 1000)
        p.with_suffix(".ct").write_text("image/jpeg")
        os.utime(p, (1000 + i, 1000 + i))
    _prune(tmp_path, 5000)
    left = sorted(p.name for p in (tmp_path / "ab").iterdir() if p.suffix != ".ct")
    assert left == ["f6", "f7", "f8", "f9"]

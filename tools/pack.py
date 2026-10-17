"""Build the extension and legacy add-on zips from qclick2/.

Usage: python tools/pack.py [OUT_DIR]   (default: dist/)

The legacy zip is the same source under a qclick2/ prefix, with
packaging/legacy/__init__.py (which carries bl_info) in place of
__init__.py and without blender_manifest.toml.
"""
import pathlib, py_compile, sys, tempfile, tomllib, zipfile

root = pathlib.Path(__file__).resolve().parent.parent
src = root / "qclick2"
legacy_init = root / "packaging" / "legacy" / "__init__.py"
out = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else root / "dist"
version = tomllib.loads((src / "blender_manifest.toml").read_text("utf-8"))["version"]

with tempfile.TemporaryDirectory() as tmp:
    for f in [*src.glob("*.py"), legacy_init]:
        py_compile.compile(str(f), doraise=True, cfile=str(pathlib.Path(tmp) / "_c.pyc"))

def files(legacy):
    for f in sorted(src.iterdir()):
        if not f.is_file() or f.suffix in (".pyc", ".tmp", ".bak"):
            continue
        if legacy and f.name == "blender_manifest.toml":
            continue
        yield (legacy_init if legacy and f.name == "__init__.py" else f), f.name

def pack(dest, prefix, legacy):
    if dest.exists():
        raise SystemExit(f"{dest} exists, not overwriting")
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
        for path, name in files(legacy):
            z.write(path, prefix + name)
    print(dest, len(zipfile.ZipFile(dest).namelist()))

out.mkdir(parents=True, exist_ok=True)
pack(out / f"QClick-{version}-legacy.zip", "qclick2/", True)
pack(out / f"QClick-{version}-extension.zip", "", False)

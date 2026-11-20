import ast
import subprocess
import sys
from pathlib import Path

import polari_app.storage
from dav_helpers import PASSWORD, ROOT_URL, USERNAME
from polari_app.storage import NextcloudStorage, ObjectStorage

STORAGE_DIR = Path(polari_app.storage.__file__).parent
FORBIDDEN_TOP_LEVEL = {"PySide6", "PyQt6", "PyQt5", "shiboken6", "pyqtgraph"}


def test_import_does_not_load_gui_or_experiment_modules() -> None:
    # A fresh interpreter, so modules imported by other tests do not count.
    code = (
        "import sys, polari_app.storage\n"
        f"bad = sorted(m for m in sys.modules if m.split('.')[0] in "
        f"{sorted(FORBIDDEN_TOP_LEVEL)!r} or m.startswith('polari_app.experiment'))\n"
        "print(','.join(bad))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == ""


def test_source_has_no_forbidden_imports() -> None:
    for source in STORAGE_DIR.glob("*.py"):
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                assert name.split(".")[0] not in FORBIDDEN_TOP_LEVEL, source
                if name.startswith("polari_app"):
                    assert name.startswith("polari_app.storage"), source


def test_module_docstrings_only_in_init() -> None:
    # Team convention from the PR #2 review.
    for source in STORAGE_DIR.glob("*.py"):
        tree = ast.parse(source.read_text(encoding="utf-8"))
        has_docstring = ast.get_docstring(tree) is not None
        assert has_docstring == (source.name == "__init__.py"), source


def test_nextcloud_storage_satisfies_protocol() -> None:
    # Checked statically by ty and pyrefly; at runtime we check the names.
    storage: ObjectStorage = NextcloudStorage(ROOT_URL, USERNAME, PASSWORD)
    for name in (
        "list_objects",
        "head_object",
        "get_object",
        "put_object",
        "delete_object",
    ):
        assert callable(getattr(storage, name))


def test_no_public_client_factory_or_constants() -> None:
    public = set(polari_app.storage.__all__)
    assert "create_http_client" not in public
    assert not any(name.startswith("DEFAULT_") for name in public)


def test_module_constants_are_private() -> None:
    # Team convention from the PR #2 review: implementation constants are
    # private. PROPFIND_BODY is the propfind module's request-body interface.
    allowed = {"PROPFIND_BODY"}
    for source in STORAGE_DIR.glob("*.py"):
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for node in tree.body:
            targets = node.targets if isinstance(node, ast.Assign) else []
            if isinstance(node, ast.AnnAssign):
                targets = [node.target]
            for target in targets:
                if isinstance(target, ast.Name) and target.id.isupper():
                    name = target.id
                    assert name.startswith("_") or name in allowed, (source, name)

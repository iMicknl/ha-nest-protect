"""Generate protobuf files for pynest.

pip install grpcio-tools mypy-protobuf
python scripts/generate_protos.py
# To delete untracked files:
git clean -fdi

The generated files keep protoc's `ValidateProtobufRuntimeVersion` guard, so a
gencode/runtime mismatch fails loudly at import instead of behaving arbitrarily.
That means the `protobuf` lower bound in `custom_components/nest_protect/
manifest.json` must be at least the "Protobuf Python Version" that protoc
stamps into the generated files. If you need to support an older runtime,
regenerate with an older protoc rather than removing the guard.
"""

import logging
import os
import re
import subprocess
import sys
from functools import partial
from pathlib import Path

logging.basicConfig(level=logging.INFO)
_LOGGER = logging.getLogger(__name__)


def generate_protos() -> None:
    """Run the protoc command to generate Python files from .proto definitions."""
    # Define paths (this script lives in scripts/, protos live at the repo root)
    base_dir = Path(__file__).parent.parent
    proto_dir = base_dir / "protobuf"
    output_dir = (
        base_dir / "custom_components" / "nest_protect" / "pynest" / "protobuf_gen"
    )

    # Ensure output directory exists
    output_dir.mkdir(parents=True, exist_ok=True)

    # List of proto files to generate. google/* are source-only — the runtime
    # uses googleapis-common-protos from PyPI for those.
    proto_files = [
        str(p.relative_to(proto_dir))
        for p in proto_dir.rglob("*.proto")
        if p.relative_to(proto_dir).parts[:1] != ("google",)
    ]

    # Construct the protoc command
    cmd = [
        sys.executable,
        "-m",
        "grpc_tools.protoc",
        f"-I{proto_dir}",
        f"--python_out={output_dir}",
        f"--mypy_out={output_dir}",
    ]

    # Add all proto files to the command
    cmd.extend(str(proto_dir / proto_file) for proto_file in proto_files)

    _LOGGER.info("Running command: %s", " ".join(cmd))

    try:
        subprocess.check_call(cmd)
        _LOGGER.info("Protobuf generation successful")
    except subprocess.CalledProcessError:
        _LOGGER.exception("Protobuf generation failed")
        sys.exit(1)

    # Create __init__.py files in the generated directories to make them packages
    for root, dirs, _files in os.walk(output_dir):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        init_file = Path(root) / "__init__.py"
        if not init_file.exists():
            init_file.touch()

    # Automatically transform absolute imports to relative imports
    fix_imports(output_dir)


def _get_from_replacement(match: re.Match, dots: str, output_dir: Path) -> str:
    """Process 'from X import Y' replacements."""
    pkg = match.group(1)
    mod = match.group(2)

    # Exclude already relative imports and the 'google' namespace
    if pkg.startswith((".", "google.")) or pkg == "google":
        return match.group(0)

    # Case 1: `from nest.trait import selftest_pb2`
    expected_file_1 = output_dir / pkg.replace(".", "/") / f"{mod}.py"
    if expected_file_1.exists():
        return f"from {dots}{pkg} import {mod}"

    # Case 2: `from nest.trait.selftest_pb2 import MyClass`
    expected_file_2 = output_dir / f"{pkg.replace('.', '/')}.py"
    if expected_file_2.exists():
        return f"from {dots}{pkg} import {mod}"

    return match.group(0)


def _get_import_replacement(match: re.Match, dots: str, output_dir: Path) -> str:
    """Process 'import X' replacements."""
    mod = match.group(1)

    # Exclude already relative imports and the 'google' namespace
    if mod.startswith((".", "google.")) or mod == "google":
        return match.group(0)

    # Case: `import wdl_event_importance_pb2`
    expected_file = output_dir / f"{mod.replace('.', '/')}.py"
    if expected_file.exists():
        if "." in mod:  # e.g., import a.b.c_pb2
            parts = mod.rsplit(".", 1)
            return f"from {dots}{parts[0]} import {parts[1]}"

        return f"from {dots} import {mod}"

    return match.group(0)


def fix_imports(output_dir: Path) -> None:
    """Convert absolute protoc imports to relative imports recursively."""
    # Regex patterns for matching standard protoc imports
    import_from_re = re.compile(
        r"^from\s+([a-zA-Z0-9_.]+)\s+import\s+([a-zA-Z0-9_]+)", re.MULTILINE
    )
    import_re = re.compile(r"^import\s+([a-zA-Z0-9_.]+)", re.MULTILINE)

    for root, dirs, files in os.walk(output_dir):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        root_path = Path(root)
        try:
            # Calculate how deep we are to determine the number of relative dots
            rel_path = root_path.relative_to(output_dir)
            depth = len(rel_path.parts)
        except ValueError:
            continue

        dots = "." * (depth + 1)

        for file in files:
            if file.endswith((".py", ".pyi")):
                file_path = root_path / file

                with file_path.open(encoding="utf-8") as f:
                    content = f.read()

                from_func = partial(
                    _get_from_replacement, dots=dots, output_dir=output_dir
                )
                new_content = import_from_re.sub(from_func, content)
                import_func = partial(
                    _get_import_replacement, dots=dots, output_dir=output_dir
                )
                new_content = import_re.sub(import_func, new_content)

                # Write back if alterations were made, forcing UNIX newlines
                if new_content != content:
                    new_content = new_content.replace("\r\n", "\n")
                    with file_path.open("w", encoding="utf-8", newline="\n") as f:
                        f.write(new_content)


if __name__ == "__main__":
    generate_protos()

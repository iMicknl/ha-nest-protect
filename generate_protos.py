"""Generate protobuf files for pynest.

pip install grpcio-tools mypy-protobuf
python generate_protos.py
# To delete untracked files:
git clean -fdi
"""

from functools import partial
import logging
import os
from pathlib import Path
import re
import subprocess
import sys

logging.basicConfig(level=logging.INFO)
_LOGGER = logging.getLogger(__name__)


def generate_protos() -> None:
    """Run the protoc command to generate Python files from .proto definitions."""
    # Define paths
    base_dir = Path(__file__).parent
    proto_dir = base_dir / "protobuf"
    output_dir = (
        base_dir / "custom_components" / "nest_legacy" / "pynest" / "protobuf_gen"
    )

    # Ensure output directory exists
    output_dir.mkdir(parents=True, exist_ok=True)

    # List of proto files to generate
    # Find all .proto files in the protobuf directory
    proto_files = [str(p.relative_to(proto_dir)) for p in proto_dir.rglob("*.proto")]

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
    except subprocess.CalledProcessError as e:
        _LOGGER.error("Protobuf generation failed with error: %s", e)
        sys.exit(1)

    # Create __init__.py files in the generated directories to make them packages
    for root, _dirs, _files in os.walk(output_dir):
        init_file = Path(root) / "__init__.py"
        if not init_file.exists():
            init_file.touch()

    # Automatically transform absolute imports to relative imports
    fix_imports(output_dir)

    # Remove runtime version validation for compatibility with older protobuf versions
    fix_runtime_version(output_dir)


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

    for root, _dirs, files in os.walk(output_dir):
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

                with open(file_path, encoding="utf-8") as f:
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
                    with open(file_path, "w", encoding="utf-8", newline="\n") as f:
                        f.write(new_content)


def fix_runtime_version(output_dir: Path) -> None:
    """Remove protobuf runtime version validation from generated files."""
    import_re = re.compile(
        r"^from google\.protobuf import runtime_version as _runtime_version\n",
        re.MULTILINE,
    )
    validate_re = re.compile(
        r"_runtime_version\.ValidateProtobufRuntimeVersion\(\s*[^)]*\)\n",
        re.DOTALL,
    )

    for file_path in output_dir.rglob("*_pb2.py"):
        content = file_path.read_text(encoding="utf-8")

        if "runtime_version" not in content:
            continue

        new_content = import_re.sub("", content)
        new_content = validate_re.sub("", new_content)

        if new_content != content:
            file_path.write_text(new_content, encoding="utf-8")
            _LOGGER.info("Removed runtime version validation from %s", file_path.name)


if __name__ == "__main__":
    generate_protos()

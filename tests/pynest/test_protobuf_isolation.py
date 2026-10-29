"""Tests that the generated protobuf modules stay out of the default pool."""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).parents[2]


def _run_isolated(code: str) -> subprocess.CompletedProcess[str]:
    """Run `code` in a fresh interpreter, since the default pool is process-global."""
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_import_survives_conflicting_weave_protos_in_default_pool() -> None:
    """Another integration defining weave.common must not break our import (#693)."""
    # Mirrors nest_yale_lock, which registers the same package under a
    # different file name.
    result = _run_isolated(
        """
        from google.protobuf import descriptor_pb2, descriptor_pool

        fdp = descriptor_pb2.FileDescriptorProto(
            name="proto/weave/common.proto", package="weave.common", syntax="proto3"
        )
        enum = fdp.enum_type.add(name="DayOfWeek")
        enum.value.add(name="DAY_OF_WEEK_UNSPECIFIED", number=0)
        descriptor_pool.Default().AddSerializedFile(fdp.SerializeToString())

        import custom_components.nest_protect.pynest.grpc_client
        """
    )

    assert result.returncode == 0, result.stderr


def test_generated_protos_do_not_register_in_default_pool() -> None:
    """Our protos must not break other integrations that load after us."""
    result = _run_isolated(
        """
        from google.protobuf import descriptor_pool

        import custom_components.nest_protect.pynest.grpc_client

        registered = []
        for name in (
            "weave/common.proto",
            "weave/trait/security.proto",
            "nestlabs/gateway/v2.proto",
            "google/rpc/streambody.proto",
            "wdl-event-importance.proto",
        ):
            try:
                descriptor_pool.Default().FindFileByName(name)
            except KeyError:
                continue
            registered.append(name)
        assert not registered, registered
        """
    )

    assert result.returncode == 0, result.stderr

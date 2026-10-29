"""Private descriptor pool for the generated protobuf modules.

Other custom integrations bundle their own copies of the reverse-engineered
Nest protos (e.g. nest_yale_lock registers `weave.common` under a different
file name). The default pool is shared by the whole Home Assistant process and
rejects a second definition of any symbol, so whichever integration loaded
second failed to import (#693). The generated modules register here instead.
"""

from __future__ import annotations

from google.protobuf import descriptor_pb2, descriptor_pool
from google.protobuf.descriptor import FileDescriptor

POOL = descriptor_pool.DescriptorPool()


def _copy_from_default(name: str) -> None:
    """Copy an external file (and its imports) from the default pool into POOL.

    Only files we don't generate end up here (google/protobuf/*, and
    google/rpc/status.proto from googleapis-common-protos). The generated
    module has already imported their `_pb2` modules, so they're in the default
    pool by the time we look.
    """
    try:
        POOL.FindFileByName(name)
    except KeyError:
        pass
    else:
        return
    file: FileDescriptor = descriptor_pool.Default().FindFileByName(name)
    for dependency in file.dependencies:
        _copy_from_default(dependency.name)
    POOL.AddSerializedFile(file.serialized_pb)


def add_serialized_file(serialized_pb: bytes) -> FileDescriptor:
    """Add a generated file to POOL, pulling in any external imports first."""
    file_proto = descriptor_pb2.FileDescriptorProto.FromString(serialized_pb)
    for dependency in file_proto.dependency:
        _copy_from_default(dependency)
    return POOL.AddSerializedFile(serialized_pb)

import struct

class RBXMInstance:
    __slots__ = ("id", "class_name", "name", "parent_id")

    def __init__(self, id, class_name, parent_id):
        self.id = id
        self.class_name = class_name
        self.parent_id = parent_id
        self.name = class_name  # default, overridden if Name property found


def _read_string(f):
    length = struct.unpack("<I", f.read(4))[0]
    return f.read(length).decode("utf-8", errors="replace")


def parse_rbxm_hierarchy(path):
    """
    Returns a list of:
      (id, name, class_name, parent_id)
    """

    instances = {}
    names = {}

    with open(path, "rb") as f:
        header = f.read(8)
        if not header.startswith(b"<roblox"):
            raise ValueError("Not a valid RBXM file")

        while True:
            chunk_header = f.read(8)
            if len(chunk_header) < 8:
                break

            chunk_type = chunk_header[:4]
            chunk_size = struct.unpack("<I", chunk_header[4:])[0]
            chunk_data = f.read(chunk_size)

            # INST chunk = instance table
            if chunk_type == b"INST":
                offset = 0
                class_id, = struct.unpack_from("<I", chunk_data, offset)
                offset += 4

                class_name_len, = struct.unpack_from("<I", chunk_data, offset)
                offset += 4
                class_name = chunk_data[offset:offset+class_name_len].decode("utf-8")
                offset += class_name_len

                instance_count, = struct.unpack_from("<I", chunk_data, offset)
                offset += 4

                for _ in range(instance_count):
                    inst_id, parent_id = struct.unpack_from("<ii", chunk_data, offset)
                    offset += 8
                    instances[inst_id] = RBXMInstance(inst_id, class_name, parent_id)

            # PROP chunk = properties (we only care about Name)
            elif chunk_type == b"PROP":
                offset = 0
                class_id, = struct.unpack_from("<I", chunk_data, offset)
                offset += 4

                prop_name_len, = struct.unpack_from("<I", chunk_data, offset)
                offset += 4
                prop_name = chunk_data[offset:offset+prop_name_len].decode("utf-8")
                offset += prop_name_len

                if prop_name != "Name":
                    continue

                value_type = chunk_data[offset]
                offset += 1

                if value_type != 0x02:  # string
                    continue

                value_count, = struct.unpack_from("<I", chunk_data, offset)
                offset += 4

                for _ in range(value_count):
                    inst_id, = struct.unpack_from("<i", chunk_data, offset)
                    offset += 4
                    name_len, = struct.unpack_from("<I", chunk_data, offset)
                    offset += 4
                    name = chunk_data[offset:offset+name_len].decode("utf-8")
                    offset += name_len
                    names[inst_id] = name

    # apply names
    result = []
    for inst_id, inst in instances.items():
        inst.name = names.get(inst_id, inst.class_name)
        result.append((inst.id, inst.name, inst.class_name, inst.parent_id))

    return result

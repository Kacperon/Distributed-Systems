import sys

import grpc

from google.protobuf import descriptor_pb2, descriptor_pool
from google.protobuf.message_factory import GetMessageClass
from grpc_reflection.v1alpha import reflection_pb2, reflection_pb2_grpc


DEFAULT_TARGET = "localhost:50061"


def list_services(stub):
    req = reflection_pb2.ServerReflectionRequest(list_services="")
    resp = list(stub.ServerReflectionInfo(iter([req])))[0]
    return [s.name for s in resp.list_services_response.service]


def parse_file_descriptors(responses):
    out = []
    for resp in responses:
        if resp.HasField("error_response"):
            raise RuntimeError(f"reflection error: {resp.error_response.error_message}")
        if resp.HasField("file_descriptor_response"):
            for raw in resp.file_descriptor_response.file_descriptor_proto:
                fdp = descriptor_pb2.FileDescriptorProto()
                fdp.ParseFromString(raw)
                out.append(fdp)
    return out


def fetch_by_symbol(stub, symbol):
    req = reflection_pb2.ServerReflectionRequest(file_containing_symbol=symbol)
    return parse_file_descriptors(stub.ServerReflectionInfo(iter([req])))


def fetch_by_filename(stub, filename):
    req = reflection_pb2.ServerReflectionRequest(file_by_filename=filename)
    return parse_file_descriptors(stub.ServerReflectionInfo(iter([req])))


def load_pool(stub, services):
    pool = descriptor_pool.DescriptorPool()
    loaded = set()

    def add(fdp):
        if fdp.name in loaded:
            return
        for dep in fdp.dependency:
            if dep not in loaded:
                for sub in fetch_by_filename(stub, dep):
                    add(sub)
        pool.Add(fdp)
        loaded.add(fdp.name)

    for svc in services:
        for fdp in fetch_by_symbol(stub, svc):
            add(fdp)
    return pool


class Method:
    def __init__(self, service_full_name, method_desc):
        self.service = service_full_name
        self.name = method_desc.name
        self.path = f"/{service_full_name}/{method_desc.name}"
        self.input_desc = method_desc.input_type
        self.output_desc = method_desc.output_type
        self.input_class = GetMessageClass(method_desc.input_type)
        self.output_class = GetMessageClass(method_desc.output_type)
        self.client_streaming = method_desc.client_streaming
        self.server_streaming = method_desc.server_streaming


def discover_methods(pool, services):
    out = {}
    for svc_name in services:
        svc = pool.FindServiceByName(svc_name)
        for m in svc.methods:
            out[(svc_name, m.name)] = Method(svc_name, m)
    return out


def call_unary(channel, method, req):
    rpc = channel.unary_unary(
        method.path,
        request_serializer=method.input_class.SerializeToString,
        response_deserializer=method.output_class.FromString,
    )
    return rpc(req)


def call_server_stream(channel, method, req):
    rpc = channel.unary_stream(
        method.path,
        request_serializer=method.input_class.SerializeToString,
        response_deserializer=method.output_class.FromString,
    )
    return rpc(req)


def format_value(v, indent=0):
    spaces = "  " * indent
    if hasattr(v, "DESCRIPTOR"):
        return format_message(v, indent)
    if isinstance(v, list):
        if not v:
            return "[]"
        return f"[\n{spaces}  " + f",\n{spaces}  ".join(format_value(x, indent + 1) for x in v) + f"\n{spaces}]"
    if isinstance(v, dict):
        if not v:
            return "{}"
        items = [f"{k}: {format_value(val, indent + 1)}" for k, val in v.items()]
        return "{\n" + spaces + "  " + f",\n{spaces}  ".join(items) + f"\n{spaces}}}"
    return repr(v)


def format_message(msg, indent=0):
    spaces = "  " * indent
    lines = []
    for field in msg.DESCRIPTOR.fields:
        v = getattr(msg, field.name)
        if field.message_type and field.message_type.full_name == "google.protobuf.Empty":
            continue
        if not v and (field.label != field.LABEL_REPEATED or isinstance(v, (list, dict))):
            continue
        lines.append(f"{field.name}: {format_value(v, indent + 1)}")
    return "{\n" + spaces + "  " + f",\n{spaces}  ".join(lines) + f"\n{spaces}}}"


def read_field(field_desc, indent=0):
    name = field_desc.name
    is_repeated = field_desc.label == field_desc.LABEL_REPEATED

    spaces = "  " * indent
    if field_desc.message_type and field_desc.message_type.full_name == "google.protobuf.Empty":
        return None

    if is_repeated:
        val_str = input(f"{spaces}{name} (comma-separated, optional): ").strip()
        if not val_str:
            return []
        if field_desc.type == field_desc.TYPE_INT32 or field_desc.type == field_desc.TYPE_INT64:
            try:
                return [int(x.strip()) for x in val_str.split(",")]
            except ValueError:
                print(f"{spaces}  invalid int list")
                return []
        return [x.strip() for x in val_str.split(",")]

    if field_desc.type == field_desc.TYPE_INT32 or field_desc.type == field_desc.TYPE_INT64:
        val_str = input(f"{spaces}{name} (int, optional): ").strip()
        return int(val_str) if val_str else 0

    if field_desc.type == field_desc.TYPE_STRING:
        return input(f"{spaces}{name}: ").strip()

    if field_desc.message_type:
        print(f"{spaces}{name} ({field_desc.message_type.name}):")
        return read_message(field_desc.message_type, indent + 1)

    return None


def read_message(msg_desc, indent=0):
    kwargs = {}
    for field in msg_desc.fields:
        val = read_field(field, indent)
        if val is not None:
            kwargs[field.name] = val
    msg_class = GetMessageClass(msg_desc)
    return msg_class(**kwargs)


def format_field(field, value, indent=0):
    spaces = "  " * indent
    if field.message_type and field.message_type.full_name == "google.protobuf.Empty":
        return None
    if not value and (field.label != field.LABEL_REPEATED):
        return None

    if field.message_type and field.message_type.has_options and field.message_type.GetOptions().map_entry:
        if not value:
            return None
        items = [f"{k}: {repr(v)}" for k, v in value.items()]
        return f"{field.name}: {{{', '.join(items)}}}"

    if field.label == field.LABEL_REPEATED:
        if not value:
            return None
        if field.message_type and not (field.message_type.has_options and field.message_type.GetOptions().map_entry):
            items = [format_message(v, indent + 1) for v in value]
        else:
            items = [repr(v) for v in value]
        return f"{field.name}: [\n{spaces}  " + f",\n{spaces}  ".join(items) + f"\n{spaces}]"

    if field.message_type:
        return f"{field.name}: {format_message(value, indent + 1)}"

    return f"{field.name}: {repr(value)}"


def print_response(resp, indent=0):
    spaces = "  " * indent
    if not hasattr(resp, "DESCRIPTOR"):
        print(f"{spaces}{repr(resp)}")
        return
    for field in resp.DESCRIPTOR.fields:
        formatted = format_field(field, getattr(resp, field.name), indent)
        if formatted:
            print(f"{spaces}{formatted}")


def menu_loop(channel, methods):
    sorted_methods = sorted(methods.items())

    while True:
        print("\navailable methods:")
        for i, ((svc, name), m) in enumerate(sorted_methods, 1):
            flags = []
            if m.client_streaming:
                flags.append("client-stream")
            if m.server_streaming:
                flags.append("server-stream")
            extra = f" [{','.join(flags)}]" if flags else ""
            print(f"{i}) {svc}/{name}{extra}")
        print("q) quit")

        try:
            choice = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("")
            break

        if choice in ("q", "quit", "exit"):
            break

        try:
            idx = int(choice) - 1
            if idx < 0 or idx >= len(sorted_methods):
                print("invalid choice")
                continue

            (svc, name), method = sorted_methods[idx]

            print(f"calling {svc}/{name}")
            print(f"input type: {method.input_desc.full_name}")
            req = read_message(method.input_desc)
            print("sending request:")
            print_response(req)

            if method.server_streaming:
                print("response (streaming):")
                got = 0
                for resp in call_server_stream(channel, method, req):
                    got += 1
                    print(f"[{got}]")
                    print_response(resp, 1)
                print(f"received {got} messages")
            else:
                print("response:")
                resp = call_unary(channel, method, req)
                print_response(resp, 1)
        except grpc.RpcError as e:
            print(f"rpc error: code={e.code()} details={e.details()}")
        except ValueError as e:
            print(f"input error: {e}")
        except Exception as e:
            print(f"error: {e}")


def main():
    target = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_TARGET
    print(f"connecting to {target}")
    channel = grpc.insecure_channel(target)
    refl_stub = reflection_pb2_grpc.ServerReflectionStub(channel)

    try:
        services = [s for s in list_services(refl_stub) if not s.startswith("grpc.reflection.")]
    except grpc.RpcError as e:
        print(f"could not reach server at {target}: code={e.code()} details={e.details()}")
        channel.close()
        sys.exit(1)

    print(f"discovered services: {', '.join(services)}")
    pool = load_pool(refl_stub, services)
    methods = discover_methods(pool, services)

    if not methods:
        print("no methods available")
        channel.close()
        sys.exit(1)

    try:
        menu_loop(channel, methods)
    finally:
        channel.close()


if __name__ == "__main__":
    main()

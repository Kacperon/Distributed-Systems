import sys

import grpc

from google.protobuf import descriptor_pb2, descriptor_pool
from google.protobuf.message_factory import GetMessageClass
from grpc_reflection.v1alpha import reflection_pb2, reflection_pb2_grpc


DEFAULT_TARGET = "localhost:50061"
SERVICE_NAME = "catalog.Catalog"


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


def print_book(b):
    print(f"  id={b.id} title='{b.title}' author='{b.author}' year={b.year} tags={list(b.tags)}")


def menu_loop(channel, services, methods):
    add_m = methods[(SERVICE_NAME, "AddBook")]
    find_m = methods[(SERVICE_NAME, "FindByAuthor")]
    summary_m = methods[(SERVICE_NAME, "Summary")]
    remove_m = methods[(SERVICE_NAME, "RemoveBook")]

    while True:
        print("")
        print("1) AddBook")
        print("2) FindByAuthor (server streaming)")
        print("3) Summary")
        print("4) RemoveBook")
        print("5) list services and methods")
        print("q) quit")
        try:
            choice = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("")
            break

        try:
            if choice == "1":
                title = input("title: ").strip()
                author = input("author: ").strip()
                year_raw = input("year: ").strip()
                year = int(year_raw) if year_raw else 0
                tags_raw = input("tags (comma-separated, optional): ").strip()
                tags = [t.strip() for t in tags_raw.split(",") if t.strip()]
                req = add_m.input_class(title=title, author=author, year=year, tags=tags)
                resp = call_unary(channel, add_m, req)
                print(f"added id={resp.id}")
            elif choice == "2":
                author = input("author substring: ").strip()
                limit_raw = input("limit (0 for no limit): ").strip()
                limit = int(limit_raw) if limit_raw else 0
                req = find_m.input_class(author=author, limit=limit)
                got = 0
                for book in call_server_stream(channel, find_m, req):
                    got += 1
                    print_book(book)
                print(f"received {got} books")
            elif choice == "3":
                resp = call_unary(channel, summary_m, summary_m.input_class())
                print(f"total={resp.total}")
                print(f"by_author={dict(resp.by_author)}")
                print("recent:")
                for b in resp.recent:
                    print_book(b)
            elif choice == "4":
                bid = int(input("id: ").strip())
                req = remove_m.input_class(id=bid)
                call_unary(channel, remove_m, req)
                print(f"removed id={bid}")
            elif choice == "5":
                print("services:")
                for svc in services:
                    print(f"  {svc}")
                print("methods:")
                for (svc, name), m in methods.items():
                    flags = []
                    if m.client_streaming:
                        flags.append("client-stream")
                    if m.server_streaming:
                        flags.append("server-stream")
                    extra = f" [{','.join(flags)}]" if flags else ""
                    print(f"  {svc}/{name}  in={m.input_desc.full_name}  out={m.output_desc.full_name}{extra}")
            elif choice in ("q", "quit", "exit"):
                break
            else:
                print("unknown choice")
        except grpc.RpcError as e:
            print(f"rpc error: code={e.code()} details={e.details()}")
        except ValueError as e:
            print(f"input error: {e}")


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

    if (SERVICE_NAME, "AddBook") not in methods:
        print(f"server does not expose {SERVICE_NAME} (got: {', '.join(services)})")
        channel.close()
        sys.exit(1)

    try:
        menu_loop(channel, services, methods)
    finally:
        channel.close()


if __name__ == "__main__":
    main()

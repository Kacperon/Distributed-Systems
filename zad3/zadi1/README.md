# Zadanie I1 - Wywołanie dynamiczne (gRPC)

## TL;DR — co tu jest

Klient w **Pythonie** wywołuje serwer w **Scali** przez gRPC. **Cały sens zadania**: klient nie ma w sobie żadnego kodu wygenerowanego z `catalog.proto` — typy wiadomości i sygnatury metod **odkrywa w czasie wykonania** przez gRPC reflection. To samo zrobiłby `grpcurl`/Postman — i działają one z naszym serwerem identycznie.

Łańcuch w 5 zdaniach:
1. Serwer Scala: `sbt` woła `scalapb` na `catalog.proto` → generuje case classy + skeleton serwisu w `target/.../src_managed/`. Reszta kodu serwera implementuje 4 RPC w trzech operacjach unary i jednej server-streaming, oraz włącza `ProtoReflectionService`.
2. Serwer trzyma binarny `FileDescriptorProto` (czyli "skompilowaną" reprezentację `catalog.proto`) i serwuje go każdemu klientowi przez reflection.
3. Klient Python startuje, otwiera kanał gRPC, woła `ServerReflectionInfo` → dostaje listę usług, potem deskryptor `catalog.proto`, potem deskryptor importowanego `google/protobuf/empty.proto`. Wrzuca to wszystko w `DescriptorPool`.
4. Z deskryptorów buduje **w runtime** klasy Pythona dla każdej wiadomości (`MessageFactory.GetMessageClass`) — interfejs identyczny ze statycznymi klasami z `_pb2.py`.
5. Mając klasy + ścieżki RPC w postaci `/<pkg>.<Service>/<Method>`, woła generic `channel.unary_unary` / `unary_stream` — to są te same prymityki, których używa "normalny" stub statyczny pod maską.

---

## Spis

1. [Layout projektu](#layout-projektu)
2. [Jak uruchomić](#jak-uruchomić)
3. [Główne technologie](#główne-technologie)
   1. [Protocol Buffers (protobuf)](#1-protocol-buffers-protobuf)
   2. [gRPC nad HTTP/2](#2-grpc-nad-http2)
   3. [gRPC server reflection](#3-grpc-server-reflection)
   4. [scalapb + sbt-protoc](#4-scalapb--sbt-protoc-codegen-po-stronie-serwera)
   5. [DescriptorPool + MessageFactory + generic channel](#5-descriptorpool--messagefactory--generic-channel-runtime-po-stronie-klienta)
   6. [Server streaming jako wyróżniona forma RPC](#6-server-streaming-jako-wyróżniona-forma-rpc)
4. [Życie wywołania end-to-end](#życie-wywołania-end-to-end)
5. [IDL](#idl)
6. [Compliance vs treść zadania](#compliance-vs-treść-zadania)
7. [Q&A na obronę](#qa-na-obronę)

---

## Layout projektu

```
zad4/zadi1/
  server/                              -- Scala, sbt + scalapb
    build.sbt                          -- konfiguracja kompilacji + codegen
    project/
      build.properties                 -- sbt.version
      plugins.sbt                      -- enable sbt-protoc + scalapb
    src/main/proto/catalog.proto       -- IDL (jedyne wspolne zrodlo prawdy)
    src/main/scala/CatalogServer.scala  -- bootstrap: ServerBuilder + reflection
    src/main/scala/CatalogImpl.scala    -- implementacja 4 RPC + walidacja
    target/scala-2.13/src_managed/...   -- (generated) stuby scalapb (case classy + service trait + FileDescriptorProto)
  client/                              -- Python, dynamic
    main.py                            -- klient: discovery + invokery + menu
    tests.py                           -- 36 testow end-to-end
    requirements.txt                   -- grpcio, grpcio-reflection, protobuf
    .venv/                             -- (generated) zaleznosci
```

Pliki generowane (stuby scalapb, klasy `.class`, `.venv`) leżą w innych katalogach niż kod źródłowy — wymóg z briefu. Po stronie klienta **nie ma żadnego katalogu `generated/`, żadnego `_pb2.py`, żadnego importu z `catalog.*`** — to jest twardy warunek "wywołania dynamicznego" z briefu.

---

## Jak uruchomić

### Serwer

```
cd zad4/zadi1/server
sbt run            # default port 50061; sbt 'run 50051' aby zmienic
```

Pierwsze uruchomienie pobiera zależności (~1-2 min — sbt ściąga scalac, scalapb, grpc-netty, protobuf-java do `~/.cache/coursier`). Spodziewany log:
```
[catalog] server listening on 50061 (reflection enabled)
```

### Klient interaktywny

```
cd zad4/zadi1/client
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python main.py                      # default localhost:50061
.venv/bin/python main.py 192.168.1.10:50061   # zdalny adres / port
```

### Testy end-to-end (36 asercji)

```
cd zad4/zadi1/client && .venv/bin/python tests.py
```

Każde uruchomienie używa unikalnego sufiksu opartego o timestamp, więc testy są **idempotentne** — dwa kolejne uruchomienia na tym samym serwerze dają 36/36 PASS.

### grpcurl (równorzędne narzędzie)

```
# instalacja
curl -sSL https://github.com/fullstorydev/grpcurl/releases/download/v1.9.1/grpcurl_1.9.1_linux_x86_64.tar.gz \
  | sudo tar -xz -C /usr/local/bin grpcurl

# discovery przez reflection
grpcurl -plaintext localhost:50061 list
grpcurl -plaintext localhost:50061 describe catalog.Catalog

# RPC
grpcurl -plaintext -d '{"title":"Dune","author":"Herbert","year":1965,"tags":["sci-fi"]}' \
  localhost:50061 catalog.Catalog/AddBook
grpcurl -plaintext -d '{"author":"Herbert","limit":0}' \
  localhost:50061 catalog.Catalog/FindByAuthor
grpcurl -plaintext localhost:50061 catalog.Catalog/Summary
grpcurl -plaintext -d '{"id":1}' localhost:50061 catalog.Catalog/RemoveBook
```

### Postman

New → gRPC → server URL `localhost:50061` → "Use server reflection: Enable" → wybrać metodę → wkleić JSON → Invoke.

---

## Główne technologie

Każda sekcja: najpierw **co to jest ogólnie**, potem **co konkretnie robi w naszym projekcie** (z numerami linii).

---

### 1. Protocol Buffers (protobuf)

#### Ogólnie

Protobuf to Google'owy format serializacji **opisu i danych**. Dwie części:

**Opis (IDL)** — plik `.proto` opisuje schemat:
```protobuf
message Book {
  int32 id = 1;
  string title = 2;
  repeated string tags = 5;
}
```
Numery `= 1`, `= 2`, `= 5` to **field tagi**. Imiona pól nie istnieją w bajtach na drucie — tylko numery. Stąd:
- nieznane pole nie psuje deserializacji (forward/backward compatibility),
- przy dodaniu nowego pola wystarczy nadać mu nieużyty numer,
- przeniesienie pola wymaga zachowania numeru, nie nazwy.

**Wire format** — każde pole zakodowane jako:
```
[tag<<3 | wire_type] [opcjonalny length-prefix] [bajty wartości]
```
Najczęstsze wire types:
- `0` (varint) — `int32`, `int64`, `bool`, enumy. 1-10 bajtów w zależności od wartości.
- `1` (fixed64) — `double`, `fixed64`.
- `2` (length-delimited) — `string`, `bytes`, zagnieżdżone `message`, `repeated` typy złożone.
- `5` (fixed32) — `float`, `fixed32`.

`Book{id=1, title="Dune"}` w bajtach:
```
08 01                  # field 1 (id), wire 0 (varint), value 1
12 04 44 75 6e 65      # field 2 (title), wire 2 (length-delimited), len 4, "Dune"
```

**Self-describing przez deskryptory** — protobuf opisuje sam siebie. Plik `descriptor.proto` definiuje wiadomości takie jak `FileDescriptorProto`, `DescriptorProto`, `FieldDescriptorProto`, `ServiceDescriptorProto`. Zbiór tych wiadomości stanowi **kompletny opis dowolnego pliku `.proto`**. Skompilowany plik `.proto` daje binarny `FileDescriptorProto` — który można przesłać przez sieć, sparsować jak normalną wiadomość protobuf i z niego zbudować klasy serializujące. **Bez tej własności gRPC reflection nie istniałby.**

#### U nas

- **IDL**: [server/src/main/proto/catalog.proto](server/src/main/proto/catalog.proto) — 5 wiadomości (`Book`, `BookId`, `AddBookRequest`, `AuthorQuery`, `CatalogStats`), 1 service (`Catalog`), import `google/protobuf/empty.proto`.
- **Po stronie serwera**: scalapb wkompilowuje binarny `FileDescriptorProto` jako stałą Scali w pliku `target/scala-2.13/src_managed/main/scalapb/catalog/CatalogProto.scala` (singleton z polem `javaDescriptor: FileDescriptor`). To właśnie ten obiekt zostaje serwowany klientowi przez reflection.
- **Po stronie klienta** ([main.py:33-46](client/main.py#L33-L46)): `parse_file_descriptors(...)` parsuje surowe bajty z reflection do instancji `FileDescriptorProto` (klasa pochodząca z meta-protokołu protobufa, nie z naszego IDL). To jest moment gdzie "binarny opis IDL leci po sieci jako normalna wiadomość protobuf".

---

### 2. gRPC nad HTTP/2

#### Ogólnie

gRPC to framework **RPC** (zdalnego wywołania procedur) nad **HTTP/2** z **protobufem** jako serializacja domyślną. Gdy mówimy "klient wywołuje metodę serwera", pod spodem dzieje się:

1. Klient otwiera kanał (`Channel`) — to jest faktyczne TCP/TLS + HTTP/2 connection.
2. Każde wywołanie RPC = **jeden HTTP/2 stream**. Multiple RPC = multiple streamy nad jednym TCP. To eliminuje head-of-line blocking — wolny RPC nie blokuje szybkiego.
3. Request body to ramki HTTP/2 typu DATA. Format jednej "wiadomości gRPC":
   ```
   [1B compressed flag] [4B big-endian len] [N bytes protobuf payload]
   ```
   Dla unary: jedna taka wiadomość w request, jedna w response. Dla server-streaming: jedna w request, wiele w response (każda jako kolejna ramka DATA).
4. Status RPC leci w **HTTP/2 trailers** (HEADERS frame z flagą END_STREAM) jako `grpc-status: 0` (OK) lub np. `grpc-status: 5, grpc-message: "book id=999 not found"` (NOT_FOUND).

Cztery typy RPC odpowiadają temu jak ramki DATA są dystrybuowane:
- **unary**: 1 request, 1 response.
- **server streaming**: 1 request, N responses w jednym streamie.
- **client streaming**: N requests, 1 response.
- **bidirectional**: N+M wymieszane.

Charakterystyczne nagłówki gRPC:
```
:method: POST
:path: /<package>.<Service>/<Method>          # np. /catalog.Catalog/AddBook
content-type: application/grpc+proto
te: trailers
grpc-encoding: identity                        # albo gzip, snappy itd.
```

#### U nas

- **Serwer**: [CatalogServer.scala:15-19](server/src/main/scala/CatalogServer.scala#L15-L19) używa `io.grpc.ServerBuilder.forPort(port)` z `grpc-netty-shaded` — to backend HTTP/2 oparty o Netty. `addService(CatalogGrpc.bindService(...))` rejestruje nasz handler, `addService(ProtoReflectionService.newInstance())` dorzuca reflection.
- **Klient**: [main.py:186](client/main.py#L186) `grpc.insecure_channel(target)` otwiera kanał. Brak TLS dla demo. Każde `call_unary`/`call_server_stream` ([main.py:85-100](client/main.py#L85-L100)) tworzy nowy HTTP/2 stream.
- **Mapowanie ścieżek**: [main.py:67](client/main.py#L67) `self.path = f"/{service_full_name}/{method_desc.name}"` daje np. `/catalog.Catalog/AddBook`. Identyczna konwencja co statyczny klient.
- **Błędy → grpc-status**: [CatalogImpl.scala:17-19](server/src/main/scala/CatalogImpl.scala#L17-L19) — `Status.INVALID_ARGUMENT.withDescription(...).asRuntimeException()`. gRPC-Java tłumaczy to na trailery HTTP/2 z `grpc-status: 3` + `grpc-message`. Po stronie klienta to wraca jako `grpc.RpcError` z `e.code()` = `StatusCode.INVALID_ARGUMENT`.

---

### 3. gRPC server reflection

#### Ogólnie

Reflection to **gRPC service** zdefiniowany w `grpc/reflection/v1alpha/reflection.proto`. Serwer rejestruje go obok własnych serwisów. Klient, znając tylko adres, może odkryć:
- jakie usługi są na serwerze,
- jakie metody, jakie typy wejścia/wyjścia,
- pełne deskryptory plików `.proto` (a więc całą strukturę wiadomości).

Sygnatura (uproszczona):
```protobuf
service ServerReflection {
  rpc ServerReflectionInfo(stream ServerReflectionRequest)
        returns (stream ServerReflectionResponse);
}

message ServerReflectionRequest {
  oneof message_request {
    string list_services = 3;
    string file_containing_symbol = 4;     # np. "catalog.Catalog" lub "catalog.Book"
    string file_by_filename = 5;           # np. "google/protobuf/empty.proto"
    ...
  }
}

message ServerReflectionResponse {
  oneof message_response {
    ListServiceResponse list_services_response = 6;
    FileDescriptorResponse file_descriptor_response = 4;
    ErrorResponse error_response = 7;
    ...
  }
}

message FileDescriptorResponse {
  repeated bytes file_descriptor_proto = 1;   # serializowany FileDescriptorProto, mozna byc kilka plikow
}
```

Reflection to **bidirectional stream** — można pchać na nim wiele zapytań, ale w praktyce klient wysyła jedno i czyta jedno.

Reflection na publicznych endpointach jest typowo wyłączana (ujawnia całe API), na wewnętrznych włączana — bezcenna do debug/tooling.

#### U nas

- **Serwer włącza reflection** jednym wpisem: [CatalogServer.scala:18](server/src/main/scala/CatalogServer.scala#L18) — `addService(ProtoReflectionService.newInstance())`. To wszystko, transparentne dla naszej logiki.
- **Klient woła reflection** w trzech krokach przy starcie:
  - [main.py:14-17](client/main.py#L14-L17) `list_services(stub)` — pyta `list_services=""`, dostaje listę nazw, filtruje `grpc.reflection.*`.
  - [main.py:33-35](client/main.py#L33-L35) `fetch_by_symbol(stub, symbol)` — pyta `file_containing_symbol="catalog.Catalog"`, dostaje binarny `FileDescriptorProto` dla `catalog.proto`.
  - [main.py:38-40](client/main.py#L38-L40) `fetch_by_filename(stub, filename)` — pyta `file_by_filename="google/protobuf/empty.proto"` (bo `catalog.proto` to importuje), dostaje binarny `FileDescriptorProto` dla `empty.proto`.
- **Składanie w pulę**: [main.py:43-60](client/main.py#L43-L60) `load_pool` — post-order DFS po `dependency` polu każdego deskryptora, dodaje liście do `DescriptorPool` najpierw, potem rodziców. To jest wymagane bo `pool.Add(fdp)` rzuca jeśli któryś `dependency` nie jest jeszcze w puli.

---

### 4. scalapb + sbt-protoc (codegen po stronie serwera)

#### Ogólnie

`protoc` to oryginalny kompilator protobufa (C++) — domyślnie generuje C++/Java/Python. Inne języki (Scala, Go, Rust, ...) realizowane są jako **pluginy** do `protoc`.

**scalapb** to plugin protoc generujący kod Scala. **sbt-protoc** to plugin sbt który:
1. Pobiera `protoc` (binary) i scalapb plugin do `~/.cache/coursier`.
2. Skanuje wskazane katalogi za plikami `.proto`.
3. Woła `protoc --scala_out=... pliki.proto` przed `compile`.
4. Wynik kładzie w `target/scala-X.Y/src_managed/main/scalapb/...` — osobno od `src/`, więc IDE/git ich nie indeksuje.

**Co generuje scalapb dla każdej wiadomości protobuf**: case class z accessorami pól, metodami `toByteArray`/`parseFrom`/`mergeFrom`, builderami `withFoo(value)`, `defaultInstance`. **Dla każdego service**: `Xxx.scala` z trait'em (`abstract class` z metodami) i metodą `bindService(impl, ec)` produkującą `ServerServiceDefinition` rejestrowalny w `ServerBuilder`.

**Dla całego pliku** scalapb generuje też singleton (`CatalogProto`) zawierający **`FileDescriptorProto`** jako stałą — to jest ten sam binarny opis, który serwer reflection udostępnia klientowi. Bez niego serwer nie miałby co odpowiadać.

#### U nas

- **Plugin** [server/project/plugins.sbt](server/project/plugins.sbt):
  ```
  addSbtPlugin("com.thesamet" % "sbt-protoc" % "1.0.7")
  libraryDependencies += "com.thesamet.scalapb" %% "compilerplugin" % "0.11.17"
  ```
- **Hook do codegen** [server/build.sbt:13-16](server/build.sbt#L13-L16):
  ```scala
  Compile / PB.protoSources := Seq((Compile / sourceDirectory).value / "proto"),
  Compile / PB.targets := Seq(
    scalapb.gen(flatPackage = true) -> (Compile / sourceManaged).value / "scalapb"
  ),
  ```
  - `protoSources` mówi gdzie szukać plików (default to `src/main/protobuf`, my mamy `src/main/proto`)
  - `flatPackage = true` zapobiega zagnieżdżaniu nazwy pliku jako pod-pakietu (bez tego mielibyśmy `catalog.catalog.Book` zamiast `catalog.Book`)
- **Co powstaje** w `server/target/scala-2.13/src_managed/main/scalapb/catalog/`:
  - `Book.scala`, `BookId.scala`, `AddBookRequest.scala`, `AuthorQuery.scala`, `CatalogStats.scala` — case classy
  - `CatalogProto.scala` — singleton z `FileDescriptorProto` (to jest serwowane przez reflection)
  - `CatalogGrpc.scala` — `trait Catalog` (rozszerzany przez `CatalogImpl`) + `bindService`
- **Implementacja** [CatalogImpl.scala:11](server/src/main/scala/CatalogImpl.scala#L11) `class CatalogImpl extends CatalogGrpc.Catalog` — łączymy się z wygenerowanym kodem przez dziedziczenie.

---

### 5. DescriptorPool + MessageFactory + generic channel (runtime po stronie klienta)

#### Ogólnie

To samo co scalapb robi **w czasie kompilacji** (genereruje klasy z `FileDescriptorProto`), Pythonowy `protobuf` potrafi **w runtime**.

**`DescriptorPool`** (`google.protobuf.descriptor_pool`) to rejestr typów. Kontener trzymający `FileDescriptor` / `Descriptor` (dla wiadomości) / `MethodDescriptor` / `ServiceDescriptor`. Kluczowe operacje:
- `pool.Add(fdp)` — wrzuca `FileDescriptorProto` (binarny opis pliku). **Rzuca**, jeśli któryś `fdp.dependency` nie jest jeszcze dodany. Stąd post-order DFS w naszym `load_pool`.
- `pool.FindMessageTypeByName("catalog.Book")` — zwraca `Descriptor`.
- `pool.FindServiceByName("catalog.Catalog")` — zwraca `ServiceDescriptor` z listą metod.

**`message_factory.GetMessageClass(descriptor)`** — bierze `Descriptor` i produkuje klasę Pythona implementującą tę wiadomość. Klasa ma metody `SerializeToString()`, `FromString(bytes)`, konstruktor `__init__(**fields)`. **Interfejs identyczny ze statycznymi klasami z `_pb2.py`** — tyle że tworzony w runtime poprzez metaklasę. To jest sedno "dynamic invocation" w gRPC z Pythona.

**Generic invocation przez `Channel.unary_unary` / `unary_stream` / `stream_unary` / `stream_stream`** — `Channel` ma cztery fabryki callable, po jednej na każdy typ RPC. Każda przyjmuje:
- ścieżkę metody (`"/<package>.<Service>/<Method>"`),
- `request_serializer` (funkcja `Message → bytes`),
- `response_deserializer` (funkcja `bytes → Message`),

i zwraca callable `(request) → response` (lub iterator dla streamów). **To są te same funkcje, których statyczny stub używa pod maską** — my wywołujemy je ręcznie.

#### U nas

Cały rdzeń dynamic invocation żyje w 3 miejscach `main.py`:

**1. [main.py:43-60](client/main.py#L43-L60) `load_pool(stub, services)`** — post-order DFS:
```python
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
```

**2. [main.py:63-73](client/main.py#L63-L73) `class Method`** — kapsułkuje wynik discovery dla pojedynczej metody RPC:
```python
class Method:
    def __init__(self, service_full_name, method_desc):
        self.path = f"/{service_full_name}/{method_desc.name}"   # np. /catalog.Catalog/AddBook
        self.input_class = GetMessageClass(method_desc.input_type)   # KLASA stworzona w runtime
        self.output_class = GetMessageClass(method_desc.output_type)
        self.server_streaming = method_desc.server_streaming         # flag z proto
```

**3. [main.py:85-100](client/main.py#L85-L100) `call_unary` / `call_server_stream`** — generic invokery:
```python
def call_unary(channel, method, req):
    rpc = channel.unary_unary(
        method.path,
        request_serializer=method.input_class.SerializeToString,
        response_deserializer=method.output_class.FromString,
    )
    return rpc(req)
```

To jest cała magia. Reszta `main.py` to UI menu, error handling, parsowanie wejścia.

---

### 6. Server streaming jako wyróżniona forma RPC

#### Ogólnie

W "klasycznym" RPC odpowiedź jest jedna. Server streaming to **długi pojedynczy HTTP/2 stream**, na którym serwer pcha wiele wiadomości (każda jako osobna ramka DATA), klient czyta je po kolei.

Po stronie generatora kodu:
- W IDL: `rpc Foo(Req) returns (stream Resp);`
- W Javie/Scali: `def foo(request: Req, observer: StreamObserver[Resp]): Unit` — implementacja woła `observer.onNext(resp)` ile razy chce, na końcu `observer.onCompleted()`.
- W Pythonie (klient): `for resp in stub.Foo(req):` — iterator, **leniwy**, pobiera każdą wiadomość gdy klient ją konsumuje. HTTP/2 daje flow-control okno na strumień — back-pressure działa "za darmo".

#### U nas

- **Deklaracja IDL** [catalog.proto](server/src/main/proto/catalog.proto): `rpc FindByAuthor(AuthorQuery) returns (stream Book);`
- **Implementacja serwera** [CatalogImpl.scala:47-67](server/src/main/scala/CatalogImpl.scala#L47-L67):
  ```scala
  override def findByAuthor(request: AuthorQuery, responseObserver: StreamObserver[Book]): Unit = {
    if (request.author.trim.isEmpty) {
      responseObserver.onError(Status.INVALID_ARGUMENT....)
    } else {
      // filtrowanie + sortowanie + opcjonalny limit
      limited.foreach { b =>
        responseObserver.onNext(b)
      }
      responseObserver.onCompleted()
    }
  }
  ```
- **Wywołanie u klienta** [main.py:94-100](client/main.py#L94-L100) — `channel.unary_stream(...)` zamiast `unary_unary`. Zwraca iterator. W menu [main.py:144-147](client/main.py#L144-L147):
  ```python
  for book in call_server_stream(channel, find_m, req):
      got += 1
      print_book(book)
  ```

  Reflection przekazuje informację `server_streaming=true` w `MethodDescriptor` ([main.py:73](client/main.py#L73)) — dlatego klient wie, że trzeba użyć `unary_stream` zamiast `unary_unary`. **To informacja pochodząca z deskryptora — bez niej klient by się pomylił.**

---

## Życie wywołania end-to-end

### Discovery startowe (jednorazowe, przy `main()`)

```
[main.py:186] grpc.insecure_channel("localhost:50061")
              -> TCP + HTTP/2 preface + SETTINGS frames (negocjacja okna)

[main.py:190] list_services(refl_stub)
   reflection request: list_services=""
   wire: HTTP/2 stream id=1, DATA z protobufem {3: ""}
   <- list_services_response: {service: [{name:"catalog.Catalog"},{name:"grpc.reflection.v1alpha.ServerReflection"}]}
   filter "grpc.reflection.*"  ->  ["catalog.Catalog"]

[main.py:197] load_pool(stub, ["catalog.Catalog"])
   fetch_by_symbol(stub, "catalog.Catalog")
     reflection request: file_containing_symbol="catalog.Catalog"
     <- file_descriptor_response: {file_descriptor_proto: [<bytes catalog.proto>]}
     parse -> FileDescriptorProto{name:"catalog.proto", dependency:["google/protobuf/empty.proto"], ...}
   add(catalog_fdp):
     dependency "google/protobuf/empty.proto" not loaded
     fetch_by_filename(stub, "google/protobuf/empty.proto")
       <- FileDescriptorProto{name:"google/protobuf/empty.proto", message_type:[Empty]}
     add(empty_fdp): no deps -> pool.Add(empty_fdp); loaded={"google/protobuf/empty.proto"}
     pool.Add(catalog_fdp); loaded += "catalog.proto"

[main.py:198] discover_methods(pool, services)
   pool.FindServiceByName("catalog.Catalog") -> ServiceDescriptor z 4 metodami
   for each method:
     Method() trzyma:
       path = "/catalog.Catalog/<MethodName>"
       input_class  = GetMessageClass(input_descriptor)   # klasa Pythona zbudowana TUTAJ, w runtime
       output_class = GetMessageClass(output_descriptor)
       server_streaming = true|false z proto

# klient ma teraz wszystko co statyczny klient z _pb2 - tylko zdobyte runtime'owo
```

### Pojedyncze wywołanie unary AddBook

```
[main.py:135] req = add_m.input_class(title="Dune", author="Herbert", year=1965, tags=["sci-fi"])
              -> instancja runtime'owej klasy AddBookRequest

[main.py:136] call_unary(channel, add_m, req)
              channel.unary_unary("/catalog.Catalog/AddBook",
                                  request_serializer=AddBookRequest.SerializeToString,
                                  response_deserializer=BookId.FromString)(req)
              ->
                serialize: req.SerializeToString() = N bajtow protobuf wire format
                wire: HTTP/2 POST /catalog.Catalog/AddBook, headers + DATA frame [0x00, len_be32, bytes]
                czeka na DATA + trailery

[serwer]      grpc-netty otrzymuje stream
              dispatcher widzi /catalog.Catalog/AddBook -> CatalogGrpc bindService -> CatalogImpl.addBook
              deserialize: AddBookRequest.parseFrom(bytes)  # scalapb-generated case class
              [CatalogImpl.scala:15] addBook(request) Future {
                walidacja -> ewent. throw Status.INVALID_ARGUMENT.asRuntimeException
                if duplicate -> throw Status.ALREADY_EXISTS.asRuntimeException
                store.put(id, Book(...))
                println(...)  # log na konsole
                BookId(id = id)
              }.onComplete:
                Success(bookId) -> serialize bookId; HTTP/2 DATA + trailery {grpc-status: 0}
                Failure(StatusRuntimeException) -> trailery {grpc-status: <code>, grpc-message}

[klient]      otrzymuje DATA frame
              deserialize: BookId.FromString(bytes)
              return BookId{ id: 298 }

              jesli grpc-status != 0:
                rzut grpc.RpcError z e.code() + e.details()
                lapane w menu_loop except grpc.RpcError -> print friendly message
```

### Pojedyncze wywołanie server-streaming FindByAuthor

```
[klient]      call_server_stream(...) zwraca iterator
              for book in iterator:
                # leniwe pobieranie - kazdy element to jedna ramka DATA od serwera
                # grpc-python pulluje z HTTP/2 buffera w miarę konsumpcji (back-pressure HTTP/2 flow-control)

[serwer]      [CatalogImpl.scala:47] findByAuthor(request, observer):
                walidacja
                for each match:
                  println(...)
                  observer.onNext(book)   # serializacja + DATA frame
                observer.onCompleted()    # trailery {grpc-status: 0}, koniec streama
              jesli onError(...) zamiast onCompleted -> trailery z error code
```

---

## IDL

[catalog.proto](server/src/main/proto/catalog.proto):

```protobuf
syntax = "proto3";
package catalog;

import "google/protobuf/empty.proto";

service Catalog {
  rpc AddBook(AddBookRequest) returns (BookId);
  rpc FindByAuthor(AuthorQuery) returns (stream Book);
  rpc Summary(google.protobuf.Empty) returns (CatalogStats);
  rpc RemoveBook(BookId) returns (google.protobuf.Empty);
}

message Book          { int32 id; string title; string author; int32 year; repeated string tags; }
message AddBookRequest{ string title; string author; int32 year; repeated string tags; }
message BookId        { int32 id; }
message AuthorQuery   { string author; int32 limit; }
message CatalogStats  { int32 total; map<string,int32> by_author; repeated Book recent; }
```

| RPC | input | output | rodzaj | błędy |
|-----|-------|--------|--------|-------|
| AddBook | AddBookRequest | BookId | unary | `INVALID_ARGUMENT` (puste title/author, year<0), `ALREADY_EXISTS` (duplikat case-insensitive) |
| FindByAuthor | AuthorQuery | stream Book | server-streaming | `INVALID_ARGUMENT` (puste author) |
| Summary | google.protobuf.Empty | CatalogStats | unary | - |
| RemoveBook | BookId | google.protobuf.Empty | unary | `NOT_FOUND` |

`AuthorQuery.limit = 0` traktowane jako "no limit" (proto3 default int32 to 0; sentinel jest standardową konwencją w gRPC API).

Brak dziedziczenia interfejsów — proto3 nie wspiera dziedziczenia ani usług, ani wiadomości; brief mówi "tam gdzie możliwe i uzasadnione".

---

## Compliance vs treść zadania

- [x] **Wywołanie dynamiczne** — klient nie zna IDL przy kompilacji, opiera się o reflection
- [x] **Brak skompilowanych stubów IDL u klienta** — żadnego `_pb2` z `catalog.proto`. Importowane `descriptor_pb2` i `reflection_pb2` to **meta-protokoły** gRPC (analogia: TCP stack vs. protokół aplikacyjny)
- [x] **>= 3 operacje** — mamy 4
- [x] **Nietrywialne struktury** — `repeated string tags`, `map<string,int32> by_author`, `repeated Book recent`
- [x] **Komunikacja strumieniowa** — `FindByAuthor returns (stream Book)`
- [x] **Hardcoded calls + parametry z konsoli** — menu czyta input
- [x] **gRPC + reflection** — `ProtoReflectionService` w serwerze, klient używa reflection v1alpha
- [x] **grpcurl/Postman** — kompatybilne (instrukcja powyżej)
- [x] **Dwa różne języki** — Python (klient) vs Scala (serwer)
- [x] **Dojrzały IDL** — odpowiednie typy (int32, string, repeated, map), gniazdowanie struktur, błędy przez Status, Empty z well-known
- [x] **Logi konsolowe** — serwer i klient logują każde wywołanie
- [x] **Stuby w osobnym katalogu** — `server/target/scala-2.13/src_managed/main/scalapb/`
- [x] **Klient tekstowy, interaktywny** — menu

---

## Q&A na obronę

### Co to jest "wywołanie dynamiczne" w naszym kontekście?

Wywołanie zdalnej procedury, gdzie klient **nie zna kontraktu (IDL) w czasie kompilacji**. Klient wie tylko jak działa protokół (gRPC = HTTP/2 + protobuf wire format) i odkrywa kontrakt w runtime — u nas przez gRPC reflection. Statyczny odpowiednik: klient ma `catalog_pb2.py` skompilowane z `catalog.proto`, kompilator/IDE zna typy, błąd wykrywany przy kompilacji. Tu nic z tego nie ma.

### Czy `descriptor_pb2`/`reflection_pb2` to nie są stuby? Klient ich używa.

Są, ale **stuby meta-protokołu**, nie stuby `catalog.proto`. `grpcurl` napisany w Go też ma skompilowane stuby `descriptor.proto` i `reflection.proto`, a wciąż jest "dynamic". Brief zabrania stubów IDL **aplikacyjnego** — ten warunek spełniamy (zero importów z `catalog.*`).

### Kiedy reflection jest niedostępny — jak działać?

1. **`FileDescriptorSet` dystrybuowany osobno**: `protoc --descriptor_set_out=catalog.desc catalog.proto`. Klient wczyta `FileDescriptorSet` (definicja w `descriptor.proto`) i użyje tego samego mechanizmu `DescriptorPool.Add(...)` co u nas.
2. **Centralny rejestr** (Buf Schema Registry, Confluent Schema Registry).
3. **Statyczne stuby** — wracamy do podejścia kompilowanego.

### Wady wywołania dynamicznego

- brak type safety przy kompilacji,
- brak wsparcia IDE,
- discovery przy starcie (1 RTT × n plików),
- reflection ujawnia całe API (security/exposure),
- "blast radius" — refaktor pól nie wybucha kompilacją u klienta.

### Kiedy go używać?

Narzędzia ops/dev (grpcurl, Postman), API gateways, generic clients pokrywające wiele serwisów, eksploracja legacy bez dostępu do `.proto`, frameworki testowe wywołujące "wszystkie metody". **Nie** dla zwykłej aplikacyjnej komunikacji — straty produktywności większe niż zyski.

### Jak to się ma do ICE Dynamic Invocation?

ICE: `Communicator.stringToProxy` + `ice_invoke(operationName, mode, inParams, outParams)` — bajtowy stream zaszyty ręcznie, klient musi znać sygnatury operacji. Niektóre struktury złożone wymagają stubów po stronie klienta (link "streaming interfaces" w briefie). gRPC dynamic invocation jest "łatwiejsze": protobuf to self-describing format (każde pole ma tag i wire type, więc bez znajomości typu da się przeskoczyć), a reflection dostarcza pełen schemat za darmo.

### Backward/forward compatibility?

Protobuf zachowuje nieznane pola (proto3) lub odrzuca (proto2 strict). Nasz dynamic klient po pobraniu deskryptora automatycznie nadąża za zmianami serwera bez rekompilacji. Statyczny klient z starym `_pb2.py` ignoruje nowe pola, ale wciąż działa (forward compat).

### Bezpieczeństwo reflection w produkcji?

Reflection ujawnia całe API. Na publicznych endpointach typowo wyłączamy lub gateway'em filtrujemy. Wewnątrz klastra (mTLS, sieć prywatna) zazwyczaj włączona — narzędzia ops/dev są bezcenne.

### TOCTOU w sprawdzaniu duplikatów na serwerze?

W [`addBook`](server/src/main/scala/CatalogImpl.scala#L26-L33) sekwencja `store.values.exists(...)` → `store.put(...)` nie jest atomowa. Przy bardzo szybkich równoczesnych Add tej samej książki możliwy duplikat. Dla demo niegroźne; produkcyjnie `synchronized` na sekcji "check + put", albo struktura z deterministycznym kluczem (title+author lower) i `putIfAbsent`.

### Co się stanie po `Ctrl+C` w kliencie / serwerze?

Klient: `input()` rzuca `KeyboardInterrupt`, łapane w menu, czysty exit. Serwer: `sys.addShutdownHook` woła `server.shutdown()` → graceful drain w gRPC-Java (nowe RPC odrzucane, in-flight kończą się). Brak `awaitTermination` po shutdownie => proces może wyjść tuż po, zanim długie streamy się skończą; przy długich streamach dodać `server.awaitTermination(5, SECONDS)` w hooku.

### Co jeśli klient wyceluje w zły adres / port / serwer bez reflection?

Złapane w `main()` jako `grpc.RpcError`, drukujemy `code=...` + `details=...`, exit 1. Zweryfikowane:
- wrong port → `UNAVAILABLE: Connection refused`
- istniejący serwer bez reflection (np. zada2 na 50051) → `UNIMPLEMENTED: Method not found: grpc.reflection.v1alpha.ServerReflection/ServerReflectionInfo`
- nieistniejący host → `UNAVAILABLE: DNS lookup failed`

### Najszybsza ścieżka demo

1. `tree zad4/zadi1` — pokazać brak `_pb2`/`catalog/` w `client/`
2. `cd server && sbt run` (port 50061, log "listening on")
3. Drugi terminal: `cd client && .venv/bin/python tests.py` → 36/36 PASS
4. `.venv/bin/python main.py`, opcja **5** (lista usług+metod **z reflection**, nie z lokalnego źródła)
5. AddBook, FindByAuthor (zwrócić uwagę na "stream"), Summary (`by_author` map + `recent` list), RemoveBook nieistniejącego id → NOT_FOUND
6. Trzeci terminal: `grpcurl -plaintext localhost:50061 list` / `describe` → ten sam wynik co Python
7. Postman z reflection → ten sam wynik
8. Pokazać w `client/main.py`:
   - linie 43-60 (`load_pool`) — post-order DFS po deps z reflection
   - linie 63-73 (`Method`) — kapsuje runtime-built klasy + ścieżkę RPC
   - linie 85-100 (`call_unary`/`call_server_stream`) — generic `channel.unary_unary` / `unary_stream`
9. Podkreślić: te 60 linii to cały dynamic invocation. Reszta to UI menu, error handling, parsowanie inputu.

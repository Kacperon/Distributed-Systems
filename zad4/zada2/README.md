# A2 - Subskrypcja na zdarzenia (gRPC)

Aplikacja klient-serwer w gRPC: serwer Java emituje strumień losowych ramek CAN (electric vehicle CAN bus), klient w Pythonie z TUI subskrybuje wybrane kategorie/nazwy i widzi tylko swoje. Połączenie jest odporne na padnięcia (auto-reconnect z bufrowaniem po stronie serwera) i NAT-friendly (HTTP/2 PING co 20s).

Tu się streszczę po użytych technologiach: po co istnieją, jak działają w teorii, i jak my je faktycznie wykorzystujemy. Ma być jasne co i dlaczego robi każdy kawałek kodu.

## Stos technologiczny - co i po co

```
+-----------------------------+         +-----------------------------+
|        KLIENT (Python)      |         |        SERWER (Java)        |
|                             |         |                             |
|  Textual (TUI)              |         |  Netty (event loop, IO)     |
|       |                     |         |       ^                     |
|  asyncio.Queue              |         |  grpc-java (handlery RPC)   |
|       |                     |         |       ^                     |
|  CanClient (grpc.aio)       |  HTTP/2 |  Sessions + Catalog         |
|       |    --------------- TCP  -->   |       ^                     |
|  CanServiceStub (generated) |         |  CanServiceImpl             |
+-----------------------------+         +-----------------------------+
            ^                                       ^
            |                                       |
            +----------- proto/can.proto -----------+
                       (wspolny IDL)
```

### gRPC - co to jest

Framework RPC od Google, otwarte standardy. Definiujesz interfejs w pliku `.proto` (Interface Definition Language), generator robi z tego stuby klienta i serwera w wybranym języku, a w wire idzie HTTP/2 z payloadami w formacie Protocol Buffers.

Kluczowe rzeczy w gRPC:
- **4 rodzaje RPC**: unary (1 request -> 1 response), server-streaming (1 -> N), client-streaming (N -> 1), bidirectional streaming (N <-> N).
- **Status codes**: każde wywołanie kończy się statusem (`OK`, `INVALID_ARGUMENT`, `NOT_FOUND`, `UNAVAILABLE`, `CANCELLED`, ...). Standardowe, niezależne od języka.
- **Metadata**: pary klucz-wartość w nagłówkach HTTP/2 (jak `Authorization` w HTTP). Przesyłane przy każdym RPC, dostępne dla interceptorów. My używamy `session-id` w metadanych.
- **Channel/Stub**: po stronie klienta `Channel` to fizyczne połączenie HTTP/2 do serwera, `Stub` to typowane proxy generowane z proto. Otwierasz channel raz, na nim odpalasz wiele RPC.

W naszym przypadku:
- **3 RPC**: `Subscribe` (bidi), `ListMessages` (unary), `GetStats` (unary).
- **Bidi dla Subscribe**, bo klient chce zmieniać filtr w trakcie streamu i unsubscribe-ować bez zamykania połączenia. Server-streaming nie pozwoli na requesty od klienta po starcie.
- **Status codes**: `INVALID_ARGUMENT` (brak `session-id`, puste `message_name`), `NOT_FOUND` (nieznana nazwa w `GetStats`), `ABORTED` (sesja przejęta przez nowy attach), `CANCELLED` (klient zerwał).

### Protocol Buffers (protobuf) - format wymiany danych

Binarny, schemowany format. W pliku `.proto` definiujesz typy, każde pole ma numer (tag) używany w wire format. Tagi są stabilne - możesz dodawać pola, stare pola są zachowywane (forward/backward compatible).

Wire format:
- Każde pole = `(tag << 3) | wire_type` + wartość.
- Liczby są kodowane jako varint (dla niskich liczb 1 bajt, dla większych więcej; ZigZag dla signed).
- Stringi i bytes mają prefiks długości.
- Repeated to po prostu kilka pól z tym samym tagiem.

W naszym `can.proto`:
- enum `MessageCategory` z 10 wariantami (BMS=0, ENGINE=1, ..., NODE_STATUS=9). proto3 wymaga aby pierwszy enum miał wartość 0.
- `Signal { string name; double value; string unit }` - typowy message.
- `CanUpdate { string message_name; MessageCategory category; uint64 timestamp_ms; repeated Signal signals }` - **`repeated`** na liście sygnałów.
- `oneof payload { Cache snapshot; CanUpdate update; SessionInfo session_info }` w `SubscribeResponse` - **polimorfizm**: jedna ramka response może być jednym z trzech typów. Klient sprawdza `WhichOneof("payload")` żeby wiedzieć co dostał.
- Generator (`protoc`) tworzy klasy w Javie i Pythonie. W Javie: `SubscribeRequest.newBuilder().setUnsubscribe(true).build()`. W Pythonie: `can_pb2.SubscribeRequest(unsubscribe=True)`.

Pliki generowane:
- Server: `target/generated-sources/protobuf/java/edu/sub/proto/*.java` + `grpc-java/edu/sub/proto/CanServiceGrpc.java` (stuby).
- Client: `client/generated/can_pb2.py` + `can_pb2_grpc.py`.

### HTTP/2 - transport gRPC

HTTP/2 to ewolucja HTTP/1.1: jedno połączenie TCP, wiele strumieni równolegle (multiplexing), nagłówki kompresowane (HPACK), binarny format ramek. gRPC nadbudowuje na HTTP/2 - każdy RPC to osobny "stream" w obrębie tego samego TCP. Bidi RPC = stream w dwie strony.

Ramki HTTP/2 które zobaczysz w Wiresharku:
- **HEADERS** - nagłówki HTTP/2 (m.in. nasze metadata `session-id`).
- **DATA** - payload protobuf (request/response messages).
- **PING** - puste ramki keepalive. Strona inicjująca wysyła PING, druga odpowiada PING ACK.
- **RST_STREAM** - cancel/error na konkretnym streamie.
- **GOAWAY** - serwer zamyka całe połączenie.
- **WINDOW_UPDATE** - flow control, każdy stream ma okno (domyślnie 64KB). Wysyłający musi czekać aż odbiorca prześle WINDOW_UPDATE jak okno się zapełni.

W naszym przypadku:
- **Keepalive 20s** (klient i serwer): co 20s niezależnie wysyłają PING. Jeśli druga strona nie odpowie w 10s (`keepAliveTimeout`), połączenie jest uznane za martwe i zamykane. Cel: utrzymanie NAT mapping (NAT zazwyczaj wygasza nieaktywne TCP po 30s-2min) oraz szybsze wykrywanie zerwania.
- **`permitKeepAliveWithoutCalls(true)` na serwerze**: pozwala klientom robić PING nawet gdy nie ma aktywnych RPC. Default w grpc-java to `false` (i zwraca `ENHANCE_YOUR_CALM`).
- **TCP keepalive 30s** ustawione obok HTTP/2 keepalive - dwie warstwy ochrony.

### grpc-java + Netty (serwer)

`grpc-netty-shaded` to runtime gRPC dla Javy w wariancie z wbudowanym Netty (asynchroniczna biblioteka IO). Architektura serwera:

1. **NettyServerBuilder** buduje `Server`, używa N wątków event-loop Netty (default = 2*CPU). Każde przychodzące połączenie TCP jest przypisane do jednego event-loopa.
2. **Service implementation** to klasa rozszerzająca `CanServiceGrpc.CanServiceImplBase` (wygenerowana z proto). Każda metoda RPC to override.
3. **StreamObserver**: gRPC abstrakcja na strumień ramek. Server dostaje `responseObserver` (zapisuje doń responsy) i zwraca `requestObserver` (odbiera requesty). Bidi: oba aktywne równolegle. Unary: jeden request, jeden response, automatycznie.
4. **ServerInterceptor**: middleware - wycina headers, można dodać auth, logging, etc. My używamy `SessionIdInterceptor` żeby wyłuskać metadata `session-id` i wsadzić do `Context` (thread-local-ish, ale działa cross-threadowo dzięki `Contexts.interceptCall`).

Wątki w naszym serwerze:
- **N event-loopów Netty** - obsługują IO i wywołują nasze metody RPC. Nie blokuj ich.
- **1 wątek "generator"** - co `tick_seconds` woła `Catalog.generateRandom()` i `Sessions.dispatch(update)`.
- **1 wątek "purger"** - co 5s woła `Sessions.purgeExpired()`.

Synchronizacja: jeden monitor `synchronized (this)` w klasie `Sessions`. Chroni `HashMap<sessionId, State>`. `dispatch` najpierw zbiera lokalnie listę aktywnych observerów (pod lockiem), potem woła `tx.onNext()` poza lockiem - dzięki temu wolny klient nie blokuje generatora.

### grpc.aio (klient Python)

`grpc.aio` to async wariant `grpcio` zbudowany na asyncio. API:
- `grpc.aio.insecure_channel(target, options=...)` - asynchroniczny channel.
- `stub = can_pb2_grpc.CanServiceStub(channel)` - typowany stub.
- Unary: `await stub.ListMessages(request)`.
- Bidi: `call = stub.Subscribe(async_iterator, metadata=...)`. `call` jest async iteratorem responsów. `async_iterator` (request_iter) yielduje requesty.
- **Cancellation**: `call.cancel()` lub asyncio CancelledError propaguje do serwera (server widzi `CANCELLED`).

W naszym przypadku:
- **`run_subscribe_loop`** to event loop reconnectu. Wokół `_one_session()` jest try/except. Na fail: backoff (1, 2, 4, 8, 16, 30, 30s), ile razy nie da rady, aż przekroczy `RECONNECT_TOTAL_S=60s` -> `EXPIRED`.
- **`_one_session`** otwiera kanał, wysyła metadata `session-id`, definiuje `request_iter` (async generator który yielduje z `outgoing` queue), i odpala `stub.Subscribe(request_iter(), metadata=...)`. Iteruje po responsach, każdy ląduje w `incoming` queue.
- **`current_filter`** trzymany na obiekcie klienta: po reconnect przy nowym `_one_session` request_iter najpierw yielduje go ponownie, żeby serwer wiedział co znów subskrybujemy.

asyncio model: cały klient siedzi na jednym wątku, jednym event loopie. Brak synchronizacji bo single-threaded. Współbieżność robią `asyncio.create_task()` (nasze: worker + reader + UI).

### Textual (TUI w terminalu)

Textual to nowoczesna biblioteka TUI w Pythonie - wygląda jak terminalowa apka, ale CSS, asyncio, eventy widgety jak w przeglądarce. Kluczowe:
- **App** - główny obiekt. Ma `compose()` zwracający widgety, `on_mount()` callback gdy app wystartuje.
- **Widgety** - `Header`, `Footer`, `Input`, `Button`, `RichLog`, `SelectionList`, `RadioSet`, `Static`. Layoutowane przez kontenery `Horizontal`, `Vertical`, `Grid`.
- **CSS** - tak, normalny CSS dla terminala. Selektor `#id`, `.class`, regular `Widget`.
- **Eventy/Akcje** - handlery typu `async def on_button_pressed(self, event)`. Bindowanie skrótów: `BINDINGS = [("ctrl+q", "clean_exit", "...")]`.
- **Integracja z asyncio** - Textual prowadzi event loop, możesz `asyncio.create_task()` z dowolnego handlera.

W naszym przypadku:
- Layout: `Horizontal(left | right)`. Lewy panel: panele Subscription i Unary calls. Prawy: log strumienia.
- 2 taski w tle (`on_mount`): `run_subscribe_loop` (gRPC worker) + `_consume_incoming` (renderuje queue do RichLog).
- Komunikacja UI <-> worker przez `asyncio.Queue` w obie strony. UI nie woła gRPC bezpośrednio (poza unary), tylko wrzuca request do `outgoing`.

## Architektura naszego rozwiązania

### IDL ([can.proto](server/src/main/proto/can.proto))

```proto
service CanService {
  rpc Subscribe(stream SubscribeRequest) returns (stream SubscribeResponse);
  rpc ListMessages(ListMessagesRequest) returns (ListMessagesResponse);
  rpc GetStats(StatsRequest) returns (MessageStats);
}

enum MessageCategory { BMS=0; ENGINE=1; ...; NODE_STATUS=9; }

message Signal { string name=1; double value=2; string unit=3; }

message CanUpdate {
  string message_name = 1;
  MessageCategory category = 2;
  uint64 timestamp_ms = 3;
  repeated Signal signals = 4;       # 'repeated' wymagane przez tresc zadania
}

message Cache { repeated CanUpdate entries = 1; }
message SessionInfo { bool resumed=1; uint32 dropped_count=2; }

message SubscribeRequest {
  repeated MessageCategory categories = 1;
  repeated string message_names = 2;
  bool unsubscribe = 3;
}

message SubscribeResponse {
  oneof payload {                    # polimorficzny response
    Cache snapshot = 1;
    CanUpdate update = 2;
    SessionInfo session_info = 3;
  }
}

message MessageInfo { string name=1; MessageCategory category=2; }
message ListMessagesResponse { repeated MessageInfo messages = 1; }
message StatsRequest { string message_name = 1; }
message MessageStats { uint64 last_seen_ms=1; uint64 total_count=2; }
```

Spełnia wymóg zadania "pola liczbowe + enum + string + message + oneof + co najmniej jeden repeated".

### Serwer (Java)

5 plików w `src/main/java/edu/sub/`:

#### Main.java - bootstrap
- Wczytuje `config.properties` (lub plik podany jako `args[0]`).
- Tworzy `Sessions` (manager sesji), `CanServiceImpl` (implementacja RPC).
- Startuje 2 wątki daemon: generator i purger.
- Buduje `NettyServerBuilder` z keepalive HTTP/2 i TCP, dodaje serwis owinięty interceptorem.
- `server.start()` + `awaitTermination()`. Shutdown hook na SIGINT.

#### Catalog.java - dane do generowania
- `static List<Msg>` z 24 hardcoded wiadomościami CAN (po 2-3 na każdą z 10 kategorii). Każda ma listę `Sig` (nazwa, jednostka, zakres min-max).
- `generateRandom()` losuje wiadomość z listy + losowe wartości w zakresie. Zwraca gotowy `CanUpdate`.
- `listAll()` zwraca metadane (nazwa + kategoria) wszystkich. Używane przez `ListMessages`.
- `exists(name)` sprawdza czy nazwa jest w katalogu. Używane przez `GetStats` do zwrotu `NOT_FOUND`.

#### Sessions.java - manager sesji (mózg systemu)
Kluczowe dane:
```java
class State {
    Set<MessageCategory> cats;          // filter osi 1
    Set<String> names;                  // filter osi 2
    boolean active;                     // czy filter aktywny (false po unsubscribe lub przed Apply)
    Deque<CanUpdate> buffer;            // gdy klient rozlaczony
    int dropped;                        // licznik dropniętych z buforu
    StreamObserver<SubscribeResponse> tx; // observer do zywego klienta lub null
    long disconnectAt;                  // timestamp rozlaczenia, do TTL
}
HashMap<String, State> sessions;        // key = session-id (UUID string)
HashMap<String, long[]> stats;          // key = message_name, value = [last_seen_ms, count]
```

Metody (wszystko `synchronized (this)`):
- `attach(id, tx)`: jak sesja istnieje - zwraca `(resumed=true, dropped, bufferSnapshot)` i czyści bufor. Jak istnieje + ma żywego `tx` (race - dwóch klientów z tym samym id) - wysyła stary observer `onError(ABORTED)` zanim podstawi nowy. Tworzy sesję jeśli nie ma.
- `detach(id, tx)`: ustawia `tx=null` i `disconnectAt=now()`. **Ważne**: porównuje observer-equality - jeśli `s.tx != tx` (czyli tx został podmieniony przez nowsze attach), nie zeruje, bo nowy attach dostarczył tymczasem.
- `setFilter(id, cats, names)` / `clearFilter(id)`: aktualizuje filter, ustawia/zeruje `active`.
- `dispatch(update)`: dla każdej sesji - sprawdza filter, jeśli pasuje: gdy `tx` jest nie-null dodaje do listy do wysłania, gdy null - addLast do buffera (z capem - jak full, pollFirst najstarszą i `dropped++`). Send do listy `tx` jest robiony POZA lockiem, by slow client nie blokował generatora.
- `purgeExpired()`: usuwa sesje z `disconnectAt > 0 && now-disconnectAt > ttl*1000`.
- `recordStats(update)` / `getStats(name)`: per-message stats.

Algorytm filtra (`dispatch`): `update` jest wysyłany jeśli `(cats.isEmpty() || cats.contains(u.category)) && (names.isEmpty() || names.contains(u.name))`. Pusta lista = wildcard tej osi. Obie zaznaczone = AND/intersection.

#### SessionIdInterceptor.java
```java
public class SessionIdInterceptor implements ServerInterceptor {
    static Context.Key<String> SESSION_ID = Context.key("session-id");
    static Metadata.Key<String> META_KEY = Metadata.Key.of("session-id", ASCII_MARSHALLER);

    @Override
    public <ReqT, RespT> Listener<ReqT> interceptCall(ServerCall<ReqT, RespT> call,
                                                       Metadata headers,
                                                       ServerCallHandler<ReqT, RespT> next) {
        String id = headers.get(META_KEY);
        Context ctx = Context.current().withValue(SESSION_ID, id);
        return Contexts.interceptCall(ctx, call, headers, next);
    }
}
```
Wyłuskuje header `session-id`, pakuje do `Context`. W `CanServiceImpl.subscribe()` woła się `SessionIdInterceptor.SESSION_ID.get()` zamiast manualnie czytać metadata. Bonus: można byłoby tu też walidować/odrzucać requesty bez nagłówka, ale my dla prostoty robimy walidację w `subscribe()` żeby zachować prostotę logiki.

#### CanServiceImpl.java - handlery RPC

`subscribe(respObs)`:
1. Wyłuskuje `session-id` z Context. Brak -> `respObs.onError(INVALID_ARGUMENT)` i return pustego observera.
2. `sessions.attach(id, respObs)` - dostaje `(resumed, dropped, snapshot)`.
3. Jak `resumed`: wysyła `SessionInfo(resumed=true, dropped_count=N)` i (jeśli buffer niepusty) `Cache snapshot`.
4. Zwraca anonymous `StreamObserver<SubscribeRequest>`:
   - `onNext(req)`: `unsubscribe=true` -> `clearFilter`, inaczej `setFilter`.
   - `onError(t)` lub `onCompleted()`: `sessions.detach(id, respObs)`. Loguje przyczynę.

`listMessages(req, obs)`: `obs.onNext(ListMessagesResponse z Catalog.listAll())`, `onCompleted()`.

`getStats(req, obs)`:
- pusta nazwa -> `INVALID_ARGUMENT`.
- nieznana nazwa (`!Catalog.exists(name)`) -> `NOT_FOUND`.
- inaczej -> zwraca stats (`MessageStats(last_seen_ms, total_count)`).

### Klient (Python)

#### main.py
Parsuje `--name` (default `default`), tworzy `Session` (load/create UUID z pliku `.session_<name>`), uruchamia Textual app.

#### src/session.py
Plikowa persystencja UUID. Generuje go raz, zapisuje, czyta przy każdym kolejnym starcie. `clear()` usuwa plik (na clean exit).

#### src/grpc_client.py - worker
`CanClient`:
- `_channel_options`: HTTP/2 keepalive ustawienia (czas, timeout, permit_without_calls).
- `list_messages()` / `get_stats(name)`: unary, otwiera fresh channel per call. Proste, ale każde wywołanie ma kosztowny handshake. OK dla rzadkich wywołań UI.
- `run_subscribe_loop()`: pętla reconnectu.
  - while True: try `_one_session()` except E:
    - `first_fail_at = now`, `idx = 0` na początku.
    - jeśli `now - first_fail_at > RECONNECT_TOTAL_S` -> emit `EXPIRED` i return.
    - inaczej emit `RECONNECTING`, sleep `BACKOFF[idx]`, `idx++`.
- `_one_session()`:
  - otwiera channel z metadata `session-id`.
  - request_iter (async gen): jak `current_filter` istnieje - yielduje go pierwszego (resume po reconnect). Potem pobiera z `outgoing` queue do None (signal kończenia).
  - `call = stub.Subscribe(request_iter(), metadata=...)`.
  - emit `CONNECTED`, reset `first_fail_at` i `idx`.
  - `async for resp in call`: każdy response przekazuje do `incoming` queue.

#### src/tui.py - Textual app

Layout (CSS + compose):
```
Header [c1 [a1b2c3d4] connected]
+-------------------------------+----------------------+
| Subscription                  | Stream               |
|  [SelectionList: 10 kategorii]|  [RichLog]           |
|  [Input: nazwy]               |   - timestamp + log  |
|  [Apply] [Unsubscribe]        |   - SNAPSHOT (N)     |
|                               |   - RESUMED dropped  |
| Unary calls                   |   - update msg+sigs  |
|  [RadioSet: List|Stats]       |  [Clear]             |
|  [Input: stat name]           |                      |
|  [Call] [Clear]               |                      |
|  [RichLog wynikow]            |                      |
+-------------------------------+----------------------+
Footer [ctrl+q clean_exit]
```

`__init__`: tworzy `outgoing` i `incoming` queues, `CanClient`. `on_mount`: `create_task(run_subscribe_loop)` i `create_task(_consume_incoming)`.

`_consume_incoming` (czytnik queue):
- `STATUS_CONNECTED` -> tytuł `connected`, log "CONNECTED".
- `STATUS_RECONNECTING` -> tytuł `reconnecting`, log "RECONNECTING (...)".
- `STATUS_EXPIRED` -> tytuł `expired`.
- `KIND_RESPONSE` -> renderuje wg `WhichOneof`:
  - `update`: `[HH:MM:SS] message_name: sig1=v1unit, sig2=v2unit, ...`
  - `snapshot`: `[HH:MM:SS] SNAPSHOT (N entries)` + linie z każdą.
  - `session_info`: `[HH:MM:SS] RESUMED dropped=N`.

`on_button_pressed` (handler Apply/Unsub/Call/Clear):
- Apply: czyta `SelectionList.selected` + `Input.value`, buduje `SubscribeRequest`, `outgoing.put(req)`.
- Unsub: `outgoing.put(SubscribeRequest(unsubscribe=True))`.
- Call: dispatch unary RPC po wybraniu radio. Handluje `AioRpcError` i renderuje status code.
- Clear: czyści RichLog.

`action_clean_exit` (Ctrl+Q): wysyła unsub, None do outgoing (zamknij stream), czeka 2s na worker, usuwa plik sesji, exit.

## Przepływy

### Standardowy: subskrypcja po kategorii
1. UI: zaznacz BMS, ENGINE w SelectionList, wpisz nic w names. Apply.
2. UI -> outgoing queue `SubscribeRequest(categories=[BMS, ENGINE], names=[], unsubscribe=False)`.
3. Worker -> serwer (przez HTTP/2 DATA frame na bidi stream).
4. Server: `CanServiceImpl.subscribe.requestObserver.onNext(req)` -> `Sessions.setFilter(id, [BMS,ENGINE], [])`.
5. Generator co 1s woła `Sessions.dispatch(update)`. Dla naszej sesji:
   - Filter aktywny? Tak.
   - Cats puste albo zawiera u.cat? `[BMS,ENGINE].contains(BMS) -> tak`.
   - Names puste? Tak (wildcard) -> match.
   - tx nie null -> dodaj do listy live tx.
6. Dispatch puszcza `tx.onNext(SubscribeResponse(update=u))`.
7. Klient: `async for resp in call` dostaje to, do incoming queue.
8. UI: czytnik renderuje "[HH:MM:SS] BMSMasterStatus: pack_voltage=380.5V, soc=72.3%, ...".

### Reconnect z buforem (najważniejszy demo path)
1. Klient ma aktywną subskrypcję BMS.
2. Klient `Ctrl+C`. Asyncio cancel propaguje, kanał zamknięty.
3. Server wykrywa zerwanie (HTTP/2 RST lub keepalive timeout). `requestObserver.onError(t)` -> `sessions.detach(id, respObs)`. `s.tx=null`, `disconnectAt=now`.
4. Generator co 1s emituje. Dla naszej sesji `tx=null`, więc do `s.buffer.addLast(u)`. Buffer rośnie.
5. Klient ponownie startuje (`python main.py --name c1`). Czyta ten sam UUID z pliku.
6. Worker łączy się, otwiera bidi z `metadata=session-id`. `_one_session` emituje CONNECTED.
7. Server: `attach` znajduje sesję, `resumed=true`. Wysyła `SessionInfo(resumed=true, dropped_count=N)` i `Cache snapshot` z buffer (i czyści buffer).
8. Worker: `request_iter` yielduje `current_filter` (BMS) jeśli klient go pamięta. Server `setFilter`.
9. Strumień live wraca.

### Race: dwóch klientów z tym samym session-id
1. Klient A połączony, sesja żyje, `tx=A_observer`.
2. Klient B (inna instancja, ten sam plik `.session_default`) próbuje connect.
3. `attach(id, B_observer)` widzi `s.tx != null`. Wysyła do A `onError(ABORTED "session replaced")`. Klient A widzi błąd, wpada w reconnect loop.
4. `s.tx = B_observer`. Resumed=true. Wysyła SessionInfo + snapshot.
5. Klient A wraca w pętli reconnect. `attach` znowu, kicka B. Tak długo aż jeden zwycięży lub user zamknie jednego.

To rozwiązuje też "klient ubiegły umarł, serwer jeszcze nie wykrył (keepalive 30s), klient wraca w 2s". Bez tej obrony nowy klient by się nigdy nie wgryzł.

### Unsubscribe bez disconnect
1. Klient klikał Apply (filter aktywny, updaty lecą).
2. Klient klika Unsubscribe. UI -> outgoing `SubscribeRequest(unsubscribe=True)`.
3. Worker przekazuje. Server `onNext(req)` -> `clearFilter` (`s.active=false`).
4. Stream żywy (request_iter dalej czeka na outgoing). Generator `dispatch` widzi `!s.active` -> pomija.
5. UI status nadal `connected`. Header keepalive PING idzie co 20s, połączenie nie wygaśnie.
6. Klient klika Apply z innym filtrem - `setFilter(active=true)` - i znów lecą updaty. Bez nowego streamu, bez nowej sesji.

## Mikro-konfig

Serwer ([server/config.properties](server/config.properties)):
```
port=50051
tick_seconds=1.0           # co tyle sekund generator emituje 1 wiadomosc
session_ttl_seconds=60     # po tylu sekundach rozlaczona sesja jest usuwana
buffer_cap=1000            # max liczba zbuforowanych wiadomosci na sesje (DoS cap)
keepalive_seconds=20       # HTTP/2 PING co tyle sekund (NAT-friendly)
```

Można uruchomić z innym configiem: `mvn exec:java -Dexec.mainClass=edu.sub.Main -Dexec.args=config-test.properties`.

Klient ([client/config.py](client/config.py)): `HOST`, `PORT`, `RECONNECT_BACKOFF` (lista sekund), `RECONNECT_TOTAL_S`, `KEEPALIVE_MS`, `KEEPALIVE_TIMEOUT_MS`, `CATEGORIES` (lista nazw enum do UI).

## Layout repo

```
zad4/zada2/
  README.md                              # ten plik
  run_all_tests.sh                       # orkiestrator: 3 fazy testow
  server/
    pom.xml                              # Maven, dependencies, protoc plugin
    config.properties                    # mikro-konfig (TTL=60s)
    config-test.properties               # konfig testowy (TTL=8s)
    src/main/proto/can.proto             # IDL (wspolny z klientem)
    src/main/java/edu/sub/
      Main.java                          # bootstrap, watki, server build
      Catalog.java                       # 24 hardcoded wiadomosci CAN
      Sessions.java                      # manager sesji + filter + buffer
      SessionIdInterceptor.java          # metadata -> Context
      CanServiceImpl.java                # impl RPC (Subscribe + 2 unary)
    target/generated-sources/protobuf/   # generowane stuby (po mvn compile)
    target/classes/                      # .class (po mvn compile)
  client/
    requirements.txt                     # grpcio, grpcio-tools, textual
    config.py                            # mikro-konfig
    proto/can.proto                      # kopia IDL (do regeneracji)
    gen_proto.sh                         # protoc -> generated/
    main.py                              # entrypoint, parsuje --name
    src/
      __init__.py
      session.py                         # plikowa persystencja UUID
      grpc_client.py                     # worker (run_subscribe_loop, reconnect)
      tui.py                             # Textual app, layout, handlery
    generated/                           # generowane stuby (po gen_proto.sh)
    test_e2e.py                          # 12 testow e2e + main_ttl
    test_tui_smoke.py                    # smoke test TUI (Pilot)
```

Pliki generowane (`target/generated-sources/`, `client/generated/`) są w odrębnych katalogach od kodu źródłowego, pliki kompilacji (`target/classes/`, `__pycache__/`, `.venv/`) też.

## Uruchomienie

### Serwer (Java + Maven)
```bash
cd zad4/zada2/server
mvn exec:java -Dexec.mainClass=edu.sub.Main
```
Wypisze: `[main] loaded config from config.properties` + `[main] listening on port 50051 tick=1000ms ttl=60s bufCap=1000 keepAlive=20s`.

### Klient (Python + venv)
```bash
cd zad4/zada2/client
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
./gen_proto.sh                              # generuje generated/can_pb2{,_grpc}.py
.venv/bin/python main.py --name c1
```
`--name` to dowolny tag. Pozwala odpalić kilka instancji z tego samego folderu, każda ma osobny plik sesji `.session_<name>`.

### Testy automatyczne (orkiestrator)
```bash
cd zad4/zada2
./run_all_tests.sh
```
3 fazy:
1. **Phase 1**: tests 1-11 z domyślnym serwerem (TTL=60s).
2. **Phase 2**: test 12 (TTL expiration) z `config-test.properties` (TTL=8s).
3. **Phase 3**: test TUI smoke (Pilot - test layout + button handlery, bez serwera).

Pokrycie testów:
- test 1: ListMessages (24, 10 kategorii), GetStats (success/NOT_FOUND/INVALID_ARGUMENT).
- test 2: Subscribe bez `session-id` -> INVALID_ARGUMENT.
- test 3: filtr po kategorii (BMS+ENGINE only).
- test 4: filtr po dokładnej nazwie (BMSMasterStatus only).
- test 5: zmiana filtra w locie (BMS -> CHARGER), bez restartu streamu.
- test 6: unsubscribe bez disconnect, brak update przez 3s, resubscribe w tym samym streamie.
- test 7: 2 klientow równolegle z różnymi filtrami.
- test 8: abrupt disconnect, server buforuje, reconnect z `SessionInfo(resumed=true)` + `Cache snapshot`.
- test 9: 3 klientów, ten sam generator, izolacja filtrów.
- test 10: race condition - rapid reconnect z tym samym `session-id`, stary observer dostaje ABORTED.
- test 11: combined filter (categories AND names) - intersection.
- test 12: TTL expiration - klient dropowany >TTL, reconnect z tym samym `session-id` = nowa sesja.
- TUI smoke: layout się komponuje, Apply/Unsubscribe button handlery działają.

Pełny bieg trwa ~5 min (limitujące: oczekiwanie na losowe trafienia z 24-elementowego katalogu przy 1Hz tick).

Wynik ostatniego biegu: `ALL PHASES PASSED RC=0`.

## Spełnienie wymagań zadania

| Wymaganie | Realizacja |
|---|---|
| gRPC | grpc-java + grpcio.aio. Bidi `Subscribe` + 2 unary. |
| Wielu odbiorcow na 1 zdarzenie | `Sessions.dispatch` iteruje wszystkie sesje. Test 9. |
| Wiele niezależnych subskrypcji | Każda sesja ma własny `State` z filtrem. Test 7, 9. |
| Streaming, brak pollingu | bidi stream, klient czeka, serwer push'uje. |
| Interwał sekundowy | `tick_seconds=1.0`. |
| Pola: liczbowe + enum + string + message + oneof + repeated | proto: `uint64`, `double`, `MessageCategory`, `string`, `Signal`, `oneof payload`, `repeated Signal/MessageCategory/CanUpdate`. |
| Filtr precyzyjny | `categories` i `message_names` po stronie klienta, server filtruje w `dispatch`. Test 3, 4, 11. |
| Unsubscribe bez disconnect | `unsubscribe=true` zostawia stream, gasi `active`. Test 6. |
| Disconnect = end of subscription | `onError`/`onCompleted` -> `detach`, purger po TTL. Test 12. |
| Generator zdarzen | `Catalog.generateRandom()`. |
| Reconnect bez restartu | client backoff + retry z tym samym `session-id`. Test 8. |
| Buforowanie po stronie serwera | `Deque<CanUpdate>` w `State`, cap=1000, drop-oldest, `dropped_count` w SessionInfo. Test 8. |
| DoS resistance | bufor cap, TTL, dispatch O(n) w jednym wątku, send poza lockiem. |
| NAT-friendly | HTTP/2 PING co 20s + TCP keepalive 30s. Wireshark `tcp.port==50051` widzi PING ramki. |
| 2 jezyki | Java (server) + Python (client). |
| Logowanie konsola | `[main]`, `[subscribe] attach/set_filter/unsubscribe/error/completed`, `[purger]`. |
| Generowane pliki w osobnym katalogu | `target/generated-sources/`, `client/generated/`. |
| Pliki kompilacji osobno | `target/classes/`, `__pycache__/`, `.venv/`. |
| Klient interaktywny tekstowy | Textual TUI, 4 panele + buttony, RichLog live. |

## Trudne pytania (przygotowanie do odbioru)

### Q: Dlaczego bidi stream a nie server-stream?
Bo klient musi zmieniać filtr w trakcie i robić unsubscribe bez rozłączenia. Server-stream wymagałby zamykania i otwierania nowego streamu na każdą zmianę intencji - tracąc atomowość i dobrze widoczne pojęcie "sesji".

### Q: Czemu `session-id` w metadanych a nie w pierwszym SubscribeRequest?
Bo to jest cecha połączenia, nie operacji. W gRPC metadane to standardowe miejsce na korelację (jak cookies/auth headers w HTTP). Daje też możliwość weryfikacji w interceptorze przed wejściem do logiki. Ułatwia ListMessages/GetStats bez dorzucania session-id w request.

### Q: Co jeśli ktoś wymyśli session-id i podszyje się pod inną sesję?
Nie ma autentykacji - to demo. W prodzie session-id musiałaby być tokenem podpisanym (JWT) albo związana z TLS client cert. Wystarczy zmienić `SessionIdInterceptor` na walidację podpisu.

### Q: Czemu HTTP/2 keepalive 20s i nie coś krótszego?
NAT-y zazwyczaj trzymają TCP mapping 5min-2h zależnie od typu (UDP ~30s ale my mamy TCP). 20s to bezpieczny margines poniżej najszybszych NAT-ów i wystarczy na dotechnięcie do większości deployów. Krótszy zwiększyłby trafic dla niczego (PING ramki to overhead 9 bajtów ale zawsze).

### Q: TCP keepalive vs HTTP/2 keepalive?
TCP keepalive (30s) trzyma sam socket alive na warstwie OS - widzi go każdy NAT, ale NIE widzą go HTTP/2 proxy które terminują TCP (envoy, NGINX). HTTP/2 PING (20s) to ramka aplikacyjna - przejdzie przez proxy, dotrze do prawdziwego peer. Razem pokrywają oba scenariusze. `permitKeepAliveWithoutCalls(true)` na serwerze dopuszcza PING gdy nie ma aktywnych RPC (default w grpc-java odbija ENHANCE_YOUR_CALM).

### Q: Skąd biorą się 60 sekund TTL sesji?
Wystarczająca na typowy reconnect po WiFi switch (kilka sekund) lub krótkim padzie serwera (do minuty). Klient ma `RECONNECT_TOTAL_S=60` żeby pasowało (po 60s idzie w EXPIRED). Konfigurowalne.

### Q: Co robi serwer jak bufor sesji się zapełni?
`Sessions.dispatch` sprawdza `s.buffer.size() >= bufCap` przed addLast. Jak tak - `pollFirst()` (wyrzuca najstarszą) i `s.dropped++`. Klient po reconnect dostaje `SessionInfo.dropped_count` żeby wiedział że stracił dane. Drop-oldest wybrałem żeby najnowsze stany dominowały (dla CAN bus to sens - latest snapshot ważniejszy niż stara historia).

### Q: Jak to jest z DoS?
1. Bufor cap (1000) - złośliwy klient który rozłącza się i nie wraca, generator pompuje na bufor, ale max 1000. Po 60s TTL cała sesja znika.
2. Generator emituje 1 wiadomość/sek niezależnie od liczby klientów. Nie da się go zalać.
3. dispatch wykonuje się w jednym wątku, deterministyczne O(n). Indywidualny slow consumer nie blokuje generatora bo `onNext` poza lockiem (a przy zapchaniu HTTP/2 window grpc-java buforuje na poziomie Netty - my nie czekamy).

### Q: Co jak klient odpalił `Subscribe` ale nigdy nie wysłał `SubscribeRequest`?
W `attach` sesja jest tworzona, ale `s.active=false` (default). `dispatch` ją pomija. Buffer pusty. Sesja zajmuje slot w mapie. Jak rozłączenie - TTL ją usunie. Czyste.

### Q: Race condition - dwóch klientów z tym samym session-id?
W `attach` jak istniejący `s.tx != null`, wołam `onError(ABORTED "session replaced")` na starym observerze. Wymusza zamknięcie streamu, nowy bierze sesję. W `detach` porównujemy observer-equality - stary onError (jak nadejdzie późno) nie zeruje nowego tx. Test 10 weryfikuje.

### Q: Co się stanie jak Ctrl+C na kliencie w trakcie odbierania snapshot?
Asyncio cancel propaguje, kanał zamknięty. Server dostaje `onError(CANCELLED)`. `detach` -> `disconnectAt=now()`. Plik `.session_<name>` zostaje (clean exit przez Ctrl+Q usuwa go). Po restarcie klient wraca z tym samym session-id, attach widzi że sesja żyje, robi resume z aktualnym buforem.

### Q: Czemu protobuf a nie JSON/Avro/CBOR?
gRPC z definicji używa protobuf, to przedmiot zadania. protobuf ma stable schema, kompaktowy binary wire format (varint, ZigZag), generowane stuby dla 10+ języków. Dla naszego CAN-like ruchu (małe message, dużo identycznych pól) protobuf wygrywa znacznie z JSON-em rozmiarowo.

### Q: Czemu enum 0=BMS i nie zarezerwowane?
Proto3 wymaga aby pierwszy enum miał wartość 0 (default value). Można dodać `UNKNOWN=0` jako sentinel, ale dla naszego case-a 10 kategorii bez niespodzianki - 0=BMS jest OK. UI używa `MessageCategory.Name(category)` żeby renderować nazwę zamiast liczbę.

### Q: Czemu nie TLS?
Demo na localhost. W prodzie `NettyServerBuilder.useTransportSecurity(certFile, keyFile)` + `grpc.aio.secure_channel()` po stronie klienta. Logika sesji niezależna od warstwy szyfrowania.

### Q: Wątki w serwerze - ile, jak synchronizowane?
- 1 wątek "generator".
- 1 wątek "purger".
- N wątków Netty event-loop (default = 2*CPU). Każde wywołanie subscribe leci na losowym z nich.
- 1 monitor `synchronized (this)` w `Sessions` chroni mapę i stats. Krytyczne sekcje krótkie. `dispatch` zbiera tx pod lockiem, send poza lockiem.

### Q: Asyncio w kliencie - pojedynczy event loop?
Tak, Textual prowadzi event loop. `run_subscribe_loop` to coroutine na `asyncio.create_task` w `on_mount`. `_consume_incoming` to drugi task. UI events (kliki) są async handlerami Textuala. Wszystko na jednym wątku, bez problemów GIL.

### Q: A jak klient śle request szybciej niż serwer dispatchuje? Backpressure?
`outgoing` queue jest unbounded ale UI generuje tylko kliknięcia (rzadko). gRPC bidi siedzi na HTTP/2 flow control - jak serwer wolno odbiera, send jest zablokowany na poziomie Netty (HTTP/2 WINDOW_UPDATE). U nas <1 request/sek od klienta - non-issue.

Po stronie serwera: `respObs.onNext` może blokować jak HTTP/2 window pełne. Zatrzymałoby wątek dispatch. Dla 1Hz nie problem. W prodzie warto rozważyć `OnReadyHandler` z `ServerCallStreamObserver`.

### Q: Co jak generator emituje co 1s ale klient subskrybuje 0 wiadomości (puste filtry)?
`active=true` z pustymi listami = wildcard po obu osiach = wszystko pasuje. Klient dostanie wszystkie 1Hz updaty. Z `unsubscribe=true` = `active=false` = nic. Dla naszego demo to OK ale w prodzie warto byłoby zabronić "subskrybuj wszystko" przez walidację min. jednej kategorii.

### Q: Czemu `unsubscribe` to bool a nie osobny RPC?
Bo bidi stream wszystko leci tym samym kanałem. Osobny RPC otwierałby drugi stream/sesję. `bool unsubscribe` na `SubscribeRequest` to kompromis - prosty, w jednym streamie, bez osobnego state machine.

### Q: Czemu `oneof` w SubscribeResponse zamiast 3 osobne RPC?
Żeby klient miał jeden async iterator do obsługi. Snapshot, update, session_info to różne typy zdarzeń w czasie życia subskrypcji. Łatwiej multiplexować w `oneof` niż mieć 3 równoległe streamy z synchronizacją.

### Q: Jakie dziedziczenie interfejsów IDL?
proto3 nie ma dziedziczenia message (jak Avro czy starsze IDL-e). `oneof` jest najbliżej polimorfizmu (CanUpdate/Cache/SessionInfo - jeden response, 3 alternatywne typy). `repeated Signal` w `CanUpdate` to kompozycja. Dziedziczenie service po stronie API: można `service Foo extends Bar` w niektórych dialektach proto, ale to rzadko.

### Q: Jak Wireshark ujawnia, że rozwiązanie jest NAT-friendly?
Filtr `tcp.port == 50051`. W idle (klient po Apply, ale generator nic nie emituje pasującego, np. unsubscribe) co ~20s widać ramki:
- `HTTP2 80 Magic, PING[0]` - PING z klienta.
- `HTTP2 80 PING[1]` - PING ACK z serwera.
- Co ~30s: `tcp.flags.ack==1 && tcp.len==0` - TCP keepalive probes.

NAT mapping nie wygasa, bo TCP widzi ruch. Proxy HTTP/2 widzi ramki PING. Demo solid.

## Co świadomie pominięte

- **TLS** - demo na localhost. Wpięcie to ~5 LOC.
- **Autentykacja** - session-id nie podpisana. JWT/mTLS przez interceptor.
- **Persystencja sesji między restartami serwera** - po restarcie czysty stan. Klient po wykryciu (no SessionInfo) wyśle nowy filter.
- **Skalowanie horyzontalne** - sesje w pamięci jednego procesu. Wieloma instancjami: shared state w Redis/etcd.
- **Smart batching dispatch** - jeden update na raz przez `onNext`. OK przy 1Hz.
- **Telemetria** - tylko stdout logi. W prodzie OpenTelemetry / Prometheus.

## Wersje bibliotek

- grpc-java 1.80.0, protobuf-java 4.34.1
- grpcio (>=1.60), textual (>=0.50)
- JDK 17, Python 3.12
- protobuf-maven-plugin 0.6.1, os-maven-plugin 1.7.0

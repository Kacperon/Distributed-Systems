# Notatka do nauki - zada2 (Subskrypcja przez gRPC)

CAN bus subskrypcja: serwer Java (grpc-java + Netty) emituje strumien
ramek CAN, klient Python (grpc.aio + Textual TUI) subskrybuje wybrane
kategorie/nazwy. Glowne tematy: bidi streaming, sesje przezywajace
rozlaczenia, bufor po stronie serwera, NAT-friendly keepalive.

## 1. Protobuf - schemat i wire format

### 1.1 Po co istnieje

Problem rozproszenia: dwie strony w roznych jezykach (Java + Python),
musza wymieniac dane jednoznacznie i wstecznie kompatybilnie. JSON jest
schemaless, XML gadatliwy. Protobuf:
- schemat (`.proto`) jako jedyne zrodlo prawdy
- binarny wire format (3-10x mniejszy od JSON)
- codegen produkuje typy w obu jezykach
- numery pol (tagi) sa stabilne -> dodawanie pol nie psuje starych klientow

### 1.2 Wire format

Kazde pole zakodowane jako:
```
[tag<<3 | wire_type] [opcjonalny length-prefix] [bajty wartosci]
```

Wire types kluczowe dla nas:
- `0` varint - int32/uint64/bool/enum, 1-10 bajtow
- `1` fixed64 - double (Signal.value), 8 bajtow
- `2` length-delimited - string, message, repeated message

Forward/backward compatibility: nieznane pole jest pomijane (proto3) lub
trafia do `unknown_fields`. Stary klient czytajacy nowy message nie pada,
nowy klient czytajacy stary dostaje default values (0, "", []).

### 1.3 IDL u nas (can.proto)

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
  repeated Signal signals = 4;
}

message SubscribeRequest {
  repeated MessageCategory categories = 1;
  repeated string message_names = 2;
  bool unsubscribe = 3;
}

message SubscribeResponse {
  oneof payload {
    Cache snapshot = 1;
    CanUpdate update = 2;
    SessionInfo session_info = 3;
  }
}
```

`oneof payload` to **polimorfizm w response**: jedna ramka response moze
byc jednym z 3 typow. Klient sprawdza `WhichOneof("payload")`. Korzysc
rozproszeniowa: jeden async iterator dostaje rozne typy zdarzen w czasie
zycia subskrypcji - bez 3 osobnych streamow z synchronizacja.

`repeated` to kompozycja kolekcji w wire format - po prostu kilka
kolejnych pol z tym samym tagiem.

## 2. gRPC nad HTTP/2

### 2.1 Anatomia jednego wywolania

Klient otwiera Channel = TCP + HTTP/2 connection. Kazdy RPC = osobny
HTTP/2 stream. To eliminuje **head-of-line blocking** z HTTP/1.1: wolny
RPC nie blokuje szybkiego, oba leca rownolegle multiplexowane na jednym TCP.

Format jednej "wiadomosci gRPC" w ramce DATA:
```
[1B compressed flag] [4B big-endian length] [N bytes protobuf payload]
```

Status RPC leci w trailerach (HEADERS frame z END_STREAM):
- `grpc-status: 0` (OK)
- `grpc-status: 5, grpc-message: "..."` (NOT_FOUND)
- `grpc-status: 14` (UNAVAILABLE)

### 2.2 Cztery typy RPC, my uzywamy 2

| typ | u nas | dlaczego |
|---|---|---|
| unary 1->1 | ListMessages, GetStats | klasyczne CRUD |
| bidi N<->N | Subscribe | klient zmienia filter w trakcie + unsubscribe bez zamykania streamu |

**Dlaczego bidi a nie server-stream dla Subscribe?**
Server-stream wymagalby zamykania i otwierania nowego streamu na kazda
zmiane filtra (= reset sesji). Bidi pozwala klientowi wysylac nowe
SubscribeRequest na zywym kanale, serwer zmienia filter, snapshot zachowany.

### 2.3 Status codes - granularna semantyka bledow

Klient moze warunkowo retry'owac w zaleznosci od kodu:
- **UNAVAILABLE** (14) -> retry z backoffem (transient, network down)
- **DEADLINE_EXCEEDED** (4) -> moze retry, ostroznie
- **INVALID_ARGUMENT** (3) -> NIE retry, blad klienta
- **CANCELLED** (1) -> klient sam zerwal, OK
- **ABORTED** (10) -> mozna retry, conflict (u nas: session replaced)

W zada2:
- INVALID_ARGUMENT - brak `session-id`, pusty `message_name`, nieznana nazwa
  do GetStats (faktycznie NOT_FOUND)
- ABORTED - "session replaced by newer attach"
- CANCELLED - klient zerwal kanal

### 2.4 Metadata - korelacja sesji

Metadata to user-defined headers HTTP/2 (klucz->wartosc). Standardowe miejsce
na rzeczy ktore towarzysza wywolaniu, nie sa danymi biznesowymi:
- auth (`Authorization: Bearer xxx`)
- trace context (OpenTelemetry)
- session id (u nas)

`session-id` w metadanych a nie w `SubscribeRequest`, bo to cecha
**polaczenia/sesji**, nie operacji. Interceptor moze waliduwac przed
wejsciem do logiki biznesowej.

## 3. HTTP/2 - co dziala pod gRPC

### 3.1 Multiplexing

HTTP/1.1: kazde zadanie ma swoj socket TCP (lub ogonek z keep-alive bez
pipeliningu). HTTP/2: **jedno TCP**, **wiele streamow**, kazdy ma `stream-id`.

Co rozwiazuje:
- mniej socketow = mniej state w kernelu i firewallach
- backpressure per stream (osobne flow-control okno)

### 3.2 Ramki w Wireshark (filtr `tcp.port == 50051`)

| ramka | znaczenie |
|---|---|
| HEADERS | naglowki HTTP/2 (HPACK), trailery |
| DATA | payload (protobuf) |
| PING | keepalive, druga strona odsyla PING ACK |
| RST_STREAM | abort konkretnego streamu |
| GOAWAY | serwer zamyka cale polaczenie (graceful) |
| WINDOW_UPDATE | flow control - kazdy stream ma okno 64KB default |

### 3.3 Flow control = backpressure za darmo

Kazdy stream ma okno (initial 64KB). Wysylajacy moze wyslac max tyle danych
zanim odbiorca przysle WINDOW_UPDATE z dodanym creditem. Slow consumer nie
przeciaza producent. U nas: serwer nie zalewa wolnego klienta - `tx.onNext`
sie blokuje na poziomie Netty buffer gdy okno pelne.

## 4. NAT-friendly keepalive (kluczowe dla zadania)

### 4.1 Problem

Klient za NAT: tablica mappingu `(internal_ip:port) -> (external_ip:port)`.
Bez ruchu na streamie wygasa - typowe timeouty:
- TCP "established" w consumer NAT: 5min - 2h
- TCP w enterprise/CGN: czasem 30s - 2min
- UDP: 30s

Po wygasnieciu pakiety sa odrzucane przez NAT - oba peers maja zywego
socketa, ale **cisza w obie strony**.

### 4.2 Rozwiazanie dwuwarstwowe

**Warstwa 1: TCP keepalive** (OS):
```
SO_KEEPALIVE on, TCP_KEEPIDLE = 30s
```
Pusty ACK co 30s. Trzyma NAT mapping.
Wada: NIE przechodzi przez proxy HTTP/2 (envoy, NGINX) - po proxy nowe TCP.

**Warstwa 2: HTTP/2 PING ramki** (warstwa aplikacyjna gRPC):
```java
NettyServerBuilder.forPort(port)
    .keepAliveTime(20, SECONDS)               // PING co 20s
    .keepAliveTimeout(30, SECONDS)            // czeka 30s na PING ACK
    .permitKeepAliveTime(5, SECONDS)          // klient moze pingowac min co 5s
    .permitKeepAliveWithoutCalls(true)        // PING dozwolony bez RPC
```
Klient (config.py):
```python
("grpc.keepalive_time_ms", 20000),
("grpc.keepalive_timeout_ms", 10000),
("grpc.keepalive_permit_without_calls", 1),
```
Kazdy gRPC peer (w tym proxy) widzi te ramki. Dotrze do prawdziwego serwera.

### 4.3 Co kazdy parametr robi

- **keepAliveTime** = jak czesto serwer aktywnie testuje peer. 20s pasuje
  do NAT timeout marginu.
- **keepAliveTimeout** = czas oczekiwania na PING ACK. 30s daje rezerwe
  na zatkana siec.
- **permitKeepAliveTime** = MINIMUM odstepu jaki klient moze stosowac.
  Klient pinguje czesciej -> serwer wysyla GOAWAY z error code
  ENHANCE_YOUR_CALM (anti-DoS).
- **permitKeepAliveWithoutCalls(true)** = klient moze pingowac nawet bez
  aktywnych RPC. Default false (chroni przed spamem).

Bez tych 4 parametrow gRPC dziala, ale klient za NAT-em umiera cicho.

## 5. Sesje przezywajace awarie - rdzen zadania

### 5.1 Problem rozproszony

Sieci sa zawodne (CAP, partition tolerance):
- mikro-padki sieci (kilka sek)
- WiFi switch
- restart serwera (do minuty)
- partycja sieciowa

Klient nie powinien tracic stanu po kazdym pad. **Sesja = przezywajaca
tozsamosc** po stronie serwera identyfikowana przez session-id (UUID).

### 5.2 Mechanika

```
klient                                serwer
------                                ------
generuje UUID, zapisuje w pliku
otwiera kanal, metadata session-id    attach(id):
                                        - nowa lub resume
                                        - zwraca (resumed?, dropped, snapshot)

[zywy stream]
... klient pada / NAT cisza ...
serwer wykrywa (RST/keepalive)        detach(id):
                                        - tx=null
                                        - disconnectAt=now
                                        - generator odtad bufuje do Deque
[bufor rosnie do cap, drop-oldest]

klient wraca z tym samym UUID         attach(id):
                                        - resumed=true, dropped=N
                                        - wysyla SessionInfo + Cache snapshot
                                        - czysci bufor
[live znowu leci]

... klient nie wraca > TTL ...        purger usuwa sesje
```

### 5.3 Klasa Sessions (mozg systemu)

```java
class State {
    Set<MessageCategory> cats;          // filter os 1
    Set<String> names;                  // filter os 2
    boolean active;
    Deque<CanUpdate> buffer;            // gdy klient rozlaczony
    int dropped;
    StreamObserver<SubscribeResponse> tx;
    long disconnectAt;
}
HashMap<String, State> sessions;        // session-id -> State
```

Wszystkie metody `synchronized (this)` poza `dispatch` (ktora robi
dwufazowy wzorzec).

### 5.4 dispatch - dwufazowy wzorzec

```java
public void dispatch(CanUpdate u) {
    List<StreamObserver> live = new ArrayList<>();
    synchronized (this) {
        for (State s : sessions.values()) {
            if (filter pasuje) {
                if (s.tx != null) live.add(s.tx);
                else { /* buffer z cap, dropped++ */ }
            }
        }
    }
    for (StreamObserver tx : live) {
        try { tx.onNext(resp); } catch (Exception e) {}
    }
}
```

Dlaczego: `tx.onNext` moze blokowac (HTTP/2 flow control). Trzymanie locka
podczas send blokowaloby `attach`/`detach` z innych watkow. **Slow consumer
zatrzymalby caly system.** Send poza lockiem rozwiazuje to.

### 5.5 TTL i bufor cap - DoS resistance

- **TTL=60s**: bez tego nieaktywne sesje rosna w nieskonczonosc (DoS przez
  zostawione UUID-y).
- **Buffer cap=1000**: bez tego pamiec serwera rosnie linearnie z
  generatorem. Drop-oldest dla CAN-bus snapshot-like data.

Powiazania konfiguracji:
- `RECONNECT_TOTAL_S < session_ttl` z marginesem (60s vs 60s)
- `keepalive_seconds * 1.5 < typowy NAT timeout`

### 5.6 Race - dwoch klientow ten sam UUID

Naturalny scenariusz: klient A "padl" (z perspektywy serwera, NAT cisza),
serwer jeszcze nie wykryl (keepalive ~30s), klient A wraca w 5s.

Rozwiazanie w `attach`:
```java
if (s.tx != null) {
    s.tx.onError(Status.ABORTED.withDescription("session replaced by newer attach"));
}
s.tx = newTx;
```

Stary observer jest closed, nowy bierze sesje. Klient A widzi ABORTED -> idzie
w reconnect loop.

Korner case w `detach`: stary `onError` przychodzi pozno do detach. Bez
ochrony zerowal by `tx` nowego klienta.
```java
if (s.tx != tx) return;   // observer-equality - to nie jest ten sam observer
```

## 6. Reconnect i exponential backoff

### 6.1 Po stronie klienta (grpc.aio)

```python
async def run_subscribe_loop(self):
    while True:
        try:
            await self._one_session()
            return
        except Exception as e:
            now = ...
            if first_fail_at is None: first_fail_at = now
            if now - first_fail_at > RECONNECT_TOTAL_S:
                emit EXPIRED; return
            emit RECONNECTING
            await sleep(BACKOFF[idx])
            idx += 1
```

### 6.2 Backoff = [1, 2, 4, 8, 16, 30, 30]

Exponential zamiast fixed delay - **rozprasza burze retry'ow**. Gdy serwer
pada, 1000 klientow probujacych co 100ms zalewa go gdy tylko wstaje.
Backoff rozcinguje na osi czasu.

### 6.3 request_iter pattern (resume po reconnect)

```python
async def request_iter():
    if self.current_filter is not None:
        yield self.current_filter             # resume - resend filter
    while True:
        req = await self.outgoing.get()
        if req is None: return
        if req.unsubscribe: self.current_filter = None
        else: self.current_filter = req
        yield req
```

Kluczowe: `current_filter` trzymany na obiekcie klienta. Po reconnect
nowy `_one_session` znow yielduje go pierwszy. Serwer dostaje setFilter
przed pierwszym update. Bez tego po reconnect filter byl by pusty
(brak `active=true`) - klient by nic nie dostawal.

## 7. Watki i synchronizacja w serwerze

### 7.1 Model

```
Netty event-loop pool (default 2*CPU watkow)
  - obsluga HTTP/2 framing, dispatcher RPC
  - "DON'T BLOCK" - blokujac event-loop blokujemy inne polaczenia

Generator (1 watek daemon)
  - co 1s wola Catalog.generateRandom() + Sessions.dispatch(update)

Purger (1 watek daemon)
  - co 5s wola Sessions.purgeExpired()
```

### 7.2 Co Netty robi pod spodem

`grpc-netty-shaded`:
- **EventLoopGroup** = pula watkow obslugujacych IO. Kazde TCP polaczenie
  pinned do jednego EventLoop -> ten sam watek czyta i pisze ramki dla
  tego polaczenia (lock-free per-connection state).
- **NIO selector** = epoll na Linuxie. Pojedynczy watek obsluguje tysiace
  socketow.

Implikacja: nie blokuj watku event-loop. Nasz dispatch wola `tx.onNext`
z dedykowanego watku generatora, nie z event-loopa.

### 7.3 ServerInterceptor - middleware

```java
public class SessionIdInterceptor implements ServerInterceptor {
    public Listener<ReqT> interceptCall(call, headers, next) {
        String id = headers.get(META_KEY);
        Context ctx = Context.current().withValue(SESSION_ID, id);
        return Contexts.interceptCall(ctx, call, headers, next);
    }
}
```

Dostep do metadata + nazwa metody + mozliwosc wstrzykniecia wartosci do
`Context` (per-RPC kontener thread-local-ish, dziala cross-threadowo).
W `subscribe` metoda wola `SessionIdInterceptor.SESSION_ID.get()` - bez
manualnego grzebania w metadanych.

To wzorzec do auth/tracing/multi-tenancy w prodzie.

### 7.4 Shutdown

```java
Runtime.getRuntime().addShutdownHook(() -> server.shutdown());
```

`shutdown()` jest **graceful**: serwer wysyla GOAWAY na wszystkie polaczenia,
in-flight RPC dokanczaja sie normalnie, klient widzi GOAWAY -> nowe RPC
ida na nowe polaczenie.

## 8. Klient - asyncio single-threaded concurrency

```python
class CanClient:
    async def run_subscribe_loop(self):
        # reconnect loop
    async def _one_session(self):
        async with grpc.aio.insecure_channel(target, options=...) as ch:
            stub = can_pb2_grpc.CanServiceStub(ch)
            metadata = (("session-id", self.session_id),)
            call = stub.Subscribe(request_iter(), metadata=metadata)
            async for resp in call:
                await self.incoming.put((KIND_RESPONSE, resp))
```

Caly klient na jednym watku (Textual prowadzi event loop). Zadne race
conditions Python-level - w czasie `await` event loop swiadomie oddaje
sterowanie.

Komunikacja UI <-> worker przez `asyncio.Queue` (outgoing/incoming) -
bezpieczna miedzy taskami, gotowa serializacja.

## 9. Pytania prowadzacego (kierunki)

### Sieciowe / HTTP/2

1. **Pokaz rozne typy ramek HTTP/2 w Wireshark.** Filtr `tcp.port == 50051`,
   nazwy: HEADERS, DATA, PING, RST_STREAM, GOAWAY, WINDOW_UPDATE.
2. **TCP keepalive vs HTTP/2 PING - co i kiedy.** TCP nie przechodzi
   przez proxy L7, PING przechodzi.
3. **Co `permitKeepAliveWithoutCalls` robi?** Bez tego serwer wysyla
   ENHANCE_YOUR_CALM klientowi pingujacemu bez RPC.
4. **Co GOAWAY?** Graceful close, in-flight RPC dokanczaja, nowe odrzucane.
5. **Flow control - co sie dzieje gdy okno zerowe?** `onNext` blokuje sie.

### Sesje / awarie

6. **Pokaz scenario reconnect z buforem.** Kill klient -> RECONNECTING ->
   start klient -> CONNECTED + SessionInfo(resumed=true) + Cache snapshot.
7. **Race: dwoch klientow ten sam UUID.** Stary dostaje ABORTED "session
   replaced".
8. **Bufor pelny - co sie dzieje?** Drop-oldest, dropped++, klient widzi
   w SessionInfo.
9. **Klient padnie nie wracajac > TTL.** Purger usuwa sesje. Klient po
   powrocie dostanie nowa sesje (resumed=false).
10. **Backoff - dlaczego exponential?** Rozprasza burze retry'ow przy
    crash recovery serwera.

### Bidi / protokol

11. **Dlaczego bidi a nie server-stream dla Subscribe?** Klient zmienia
    filter w trakcie i robi unsubscribe bez restartu sesji.
12. **Dlaczego oneof w SubscribeResponse?** Polimorfizm - 3 typy zdarzen
    w jednym streamie, jeden async iterator.
13. **Czemu session-id w metadanych a nie w request?** To cecha sesji,
    nie operacji. Standardowe miejsce w gRPC.

### Watki / DoS

14. **Co jak generator wisi na slow consumerze?** Nie wisi - dispatch ma
    dwufazowy wzorzec, send poza lockiem.
15. **Watki w serwerze - ile, kto co robi.** Netty pool + 1 generator + 1
    purger. Synchronized monitor w Sessions.
16. **DoS resistance - jak chronicie?** Bufor cap (1000), TTL (60s), single
    generator (1Hz, niezalezny od liczby klientow), dispatch O(n) w 1 watku.

### Konfig

17. **Powiazania konfiguracji.** `RECONNECT_TOTAL_S <= session_ttl`,
    `keepalive*1.5 < NAT timeout`, `tick * buf_cap = max bufor age`.

## 10. Mapping problemow rozproszenia

| problem | rozwiazanie |
|---|---|
| jezyki/platformy | protobuf + codegen |
| schemat ewolucja | stabilne tagi, default values |
| serializacja efektywna | binarny wire format, varint |
| wiele rownoleglych operacji | HTTP/2 multiplexing |
| slow consumer | HTTP/2 flow control, dispatch dwufazowy |
| transient failures | gRPC status codes, UNAVAILABLE -> retry |
| zerwane polaczenie | session-id + reconnect z buforem |
| NAT mapping wygasa | keepalive PING 20s + TCP keepalive 30s |
| burza retry'ow | exponential backoff |
| sesja zostawiona | TTL + purger |
| DoS unbounded buffer | bufor cap + drop-oldest |
| dwoch klientow ten sam state | linearyzacja monitor + observer kick |
| auth/correlation | metadata HTTP/2 + interceptor |
| stan sesji w trakcie | bidi stream, requesty multiplexed na response stream |

## 11. Co mozna by dodac w prodzie

| brakuje | jak dodac |
|---|---|
| TLS | `useTransportSecurity` + `secure_channel` |
| auth | JWT w `Authorization` metadata + interceptor |
| persystencja sesji miedzy restartami | bufor i state w Redis/etcd |
| skalowanie horyzontalne | shared session store + sticky routing |
| observability | OpenTelemetry interceptor |
| limit liczby sesji | dodatkowy cap w `Sessions.attach` |
| circuit breaker | po stronie klienta gdy serwer ciagle UNAVAILABLE |

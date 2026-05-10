# Notatka do nauki - zad4 (RabbitMQ posrednik agencja-przewoznik)

System posrednictwa miedzy agencjami kosmicznymi (publikuja zlecenia
3 typow uslug) a przewoznikami (kazdy obsluguje 2 z 3 typow). Premium:
administrator dostaje kopie ruchu i moze rozsylac broadcasty.

Glowne tematy: AMQP 0-9-1 i jego model brokerowany, exchange topic vs
direct/fanout, work queue z competing consumers, prefetch i manual ack
dla "pierwszy wolny przewoznik", per-actor queues dla adresowanej
delivery, admin spy queue przez wildcardy w bindingach.

## 1. AMQP 0-9-1 - czym sie rozni od HTTP/gRPC/Ice

### 1.1 Model brokerowany, nie peer-to-peer

gRPC, Ice, HTTP: **klient laczy sie bezposrednio z serwerem** (request-response).
AMQP: zarowno producent jak i konsument lacza sie z **brokerem** (RabbitMQ),
ktory posredniczy. Zalety:

- producent NIE wie kto konsumuje (loose coupling)
- konsument moze byc offline, broker buforuje wiadomosci
- jedno wiele/wiele wielu rozdzial naturalny (nie trzeba multipleksowac
  w aplikacji)
- backpressure, persistence, retry - delegowane do brokera

Wady:
- broker = single point of failure (mitygowane przez clustering, mirror
  queues, quorum queues)
- dodatkowe hopy = wieksze opoznienie
- koszt operacyjny dodatkowego komponentu

### 1.2 Cztery filary AMQP

| pojecie | rola |
|---|---|
| Connection | TCP do brokera (zazwyczaj 5672, TLS na 5671). Heartbeat negocjowany na starcie. |
| Channel | logiczne "podpolaczenie" w ramach connection. Multipleksuje wiele operacji na jednym TCP - analog HTTP/2 streams. |
| Exchange | komponent routujacy. Dostaje wiadomosci od producenta, wedlug typu i bindingow rozdziela do kolejek. |
| Queue | bufor wiadomosci. Konsumenci subskrybuja kolejki, nie exchange'e. |

Kluczowe: producent wysyla **tylko do exchange'a**, nie do kolejki. Konsument
**tylko z kolejki**, nie z exchange'a. Mapowanie exchange->queue robia
**bindings** z kluczami routingu.

### 1.3 Wire format - frames AMQP

Kazda ramka:
```
[type:1B][channel:2B][payload-size:4B][payload bytes][frame-end:1B 0xCE]
```

Typy ramek:
- `1` METHOD - wywolanie metody AMQP (connection.start, channel.open,
  exchange.declare, basic.publish, basic.deliver, basic.ack, ...)
- `2` HEADER - properties wiadomosci (content-type, delivery-mode,
  reply-to, correlation-id, priority, expiration, ...)
- `3` BODY - payload aplikacyjny (nasz JSON)
- `8` HEARTBEAT - keepalive

Wiadomosc aplikacyjna = METHOD frame (basic.publish/deliver) + HEADER
frame + 1..N BODY frames (jesli body wiekszy niz frame-max).

### 1.4 Idempotentnosc declare

`exchange_declare` i `queue_declare` sa **idempotentne** jesli parametry
sie zgadzaja. Konsument moze zadeklarowac swoja kolejke kazde uruchomienie
- jak juz istnieje, no-op. Jak inny uzytkownik wczesniej zadeklarowal z
innymi parametrami (np. `durable=False` vs `durable=True`), to leci
PRECONDITION_FAILED i kanal sie zamyka. Stad waznosc trzymania jednej
prawdy o parametrach kolejki w jednym miejscu.

## 2. Exchange types - co i kiedy

### 2.1 Cztery typy

**direct**: routing key konsumenta == routing key producenta. Klasyczny
"adresowany" routing. Przyklad: `confirm.NASA` -> kolejka NASA.

**fanout**: ignoruje routing key, wysyla do **wszystkich** zwiazanych
kolejek. Klasyczny pub-sub broadcast. Przyklad: powiadomienia do
wszystkich aplikacji.

**topic**: wzorzec na routing key z wildcardami `*` (jeden segment) i
`#` (zero lub wiecej). Producent posyla `order.osoby`, kolejka bound na
`order.*` to dostaje, kolejka bound na `order.satelita` nie dostaje.

**headers**: routing po properties wiadomosci, nie routing key. Rzadko
uzywane, brak zysku w naszym przypadku.

### 2.2 Dlaczego u nas topic

Mozliwe wybory:
- 3 osobne exchange'e direct (`orders`, `confirms`, `broadcasts`) +
  fanouty per grupa
- 1 topic + wszystko na nim

Wybralismy topic bo:
- **uniwersalny routing** - klucz `order.<typ>`, `confirm.<agencja>`,
  `broadcast.<grupa>` - kazdy ma sensowny pattern match
- **admin za darmo** - admin bind na `order.*`, `confirm.*`, `broadcast.*`
  i widzi wszystko, bez modyfikacji producentow/konsumentow
- **mniej entitiy** - jeden exchange zamiast trzech, latwiej diagnozowac
  w panelu :15672

Tradeoff: topic ma O(n) match per message vs O(1) lookup w direct.
Przy naszej skali (5 aktorow) to zaden problem.

### 2.3 Wildcardy topic

```
order.*        # match: order.osoby, order.ladunek         (jeden segment)
order.#        # match: order, order.osoby, order.x.y.z   (zero lub wiecej segmentow)
*.osoby        # match: order.osoby                       (lewy segment dowolny)
broadcast.*    # match: broadcast.agencies, broadcast.all
#              # match: wszystko
```

Admin u nas binduje tylko na 3 prefixy a nie na `#`, bo:
- `#` lapie tez ramki czysto-systemowe ktore moze RabbitMQ wewnetrznie
  generuje (w naszej wersji nie ma takich, ale to defensywny zwyczaj)
- 3 osobne prefixy daja czytelnosc w logu admina (kazda kategoria ma swoj
  prefix w SPY logu)

## 3. Work queue z competing consumers - jak wygenerowac "pierwszy wolny"

### 3.1 Wyzwanie

Brief: "konkretne zlecenie powinno trafic do **pierwszego wolnego**
przewoznika ktory obsluguje ten typ zlecenia".

Naiwne fanout NIE dziala - kazdy przewoznik dostalby kopie zlecenia,
co lamie "dane zlecenie nie moze trafic do wiecej niz jednego".

Naiwne direct z routing key per-przewoznik tez nie - jak agencja ma
zdecydowac kto wolny? To wlasnie chcemy zdelegowac do brokera.

### 3.2 Wzorzec "work queue"

```
[exchange] -- routing-key=order.osoby --> [service.osoby queue]
                                              |
                          +----+--------+-----+--------+----+
                          |    |        |              |    |
                       Carrier1 Carrier2 ...           CarrierN
                       (consumer1) (consumer2)         (consumerN)
```

**Jedna kolejka**, **wielu konsumentow**, broker **rozdziela jedna
wiadomosc do jednego konsumenta**. To jest klasyczny "competing consumers".

Domyslna polityka brokera = round-robin. Z `basic_qos(prefetch_count=1)`
zmienia sie na "fair dispatch": broker daje nowa wiadomosc tylko temu
konsumentowi ktory NIE ma w obrobce wiadomosci nieackowanej.

### 3.3 Prefetch a backpressure

`channel.basic_qos(prefetch_count=N)`:
- broker pcha do tego kanalu max N nieackowanych wiadomosci na raz
- `N=1` = "natural one-at-a-time" (idealne dla naszego briefu, gdzie
  zlecenia obrabiamy kolejno)
- `N=10..100` = throughput-friendly, ale szybki konsument moze zlapac
  kilka jednoczesnie i poczekac

W naszym kodzie:
```python
ch.basic_qos(prefetch_count=1)
```
Plus manual ack. Razem to znaczy: "daj mi jedna wiadomosc, jak skoncze
i zackuje, daj nastepna". Idealne odwzorowanie "pierwszy wolny".

### 3.4 Manual ack vs auto ack

`auto_ack=True`: broker uznaje wiadomosc za dostarczona w momencie
wysylki do konsumenta. Plus: prosto. Minus: jak konsument padnie po
otrzymaniu a przed obrobka, wiadomosc ginie.

`auto_ack=False` (default + nasze ustawienie dla orders): konsument musi
recznie wywolac `basic_ack(delivery_tag)` po obsluzeniu. Jak konsument
padnie bez ack, broker po wykryciu padniecia (zamkniety channel) ponownie
postawi wiadomosc w kolejke i da innemu konsumentowi.

To jest klucz do "zlecenie nie moze sie zgubic". Plus dziala razem z
prefetch=1 (broker wie, kto ma cos w obrobce).

Dla broadcastow uzywamy `auto_ack=True` bo:
- broadcast to `broadcast.<grupa>` z fanout-like semantyka, kazdy w
  grupie ma dostac swoja kopie
- ich utrata nie jest tragiczna (nie ma SLA na admin notification)
- prosciej

### 3.5 Bez prefetch+ack: co bylo by zle

Bez `prefetch=1`: broker rozesle 1000 zlecen do jednego "szybkiego"
konsumenta jak tylko ten otworzy kanal. Drugi konsument staje bezczynnie.
Naruszenie "pierwszy wolny". Auto_ack pogarsza - tracone zlecenia.

Bez manual ack: jakikolwiek crash przewoznika zjada wszystkie wiadomosci
ktore byly w jego prefetch buforze.

## 4. Per-actor queues - kiedy i czemu

### 4.1 Kolejki agencji `agency.<NAZWA>`

Brief: "po wykonaniu uslugi przewoznik wysyla potwierdzenie **do agencji**".

To jest wiadomosc adresowana do konkretnego odbiorcy, nie work queue.
Realizacja:
- przewoznik publikuje `confirm.NASA` do exchange'a
- `agency.NASA` queue jest bound na `confirm.NASA` (i tylko ta kolejka)
- NASA konsumuje z `agency.NASA`, ESA z `agency.ESA`

`exclusive=True` na declare:
- kolejka przezywa tylko jedno polaczenie - po rozlaczeniu znika
- jeden konsument na kolejke (broker nie pozwoli drugiemu sie podpiac)

To pasuje do naszego CLI: agencja zywie tyle co terminal, kolejka razem
z nia. Plus: rozne instancje agencji o tej samej nazwie konkurowalyby o
kolejke `agency.NASA`, `exclusive=True` sprawia ze druga dostanie blad
RESOURCE_LOCKED zamiast cicho zjesc czesc potwierdzen.

### 4.2 Multi-binding do tej samej kolejki

`agency.NASA` ma 3 wiazania:
- `confirm.NASA` (potwierdzenia tylko dla NASA)
- `broadcast.agencies` (admin do wszystkich agencji)
- `broadcast.all` (admin do wszystkich)

Skutek: jedna kolejka odbiera 3 rozne kategorie ruchu, konsument
rozroznia je po `method.routing_key`. Mozna by zrobic 3 osobne kolejki,
ale 3 wiazania -> 1 kolejka jest prostsze (jeden consumer loop, jeden
prefetch budget).

### 4.3 Kolejki przewoznikow `carrier.<ID>` - czemu OSOBNA od work queues

Mozna by pomyslec: "skoro przewoznik C1 czyta `service.osoby` i
`service.ladunek`, dorzuc tam tez `broadcast.carriers`". To NIE dziala:
- `service.osoby` to **work queue** - jak admin wyslalby broadcast.carriers
  i bound by sie do tej kolejki, broadcast trafialby tylko do **jednego**
  przewoznika obslugujacego osoby (competing consumers)
- chcemy fanout do wszystkich przewoznikow

Stad kazdy przewoznik dostaje **osobna** kolejke `carrier.<ID>` z
bindingami `broadcast.carriers`, `broadcast.all`. Kazdy ma wlasna kopie
broadcastu.

### 4.4 Admin spy `admin.<auto>`

Brief: "Administrator dostaje kopie wszystkich wiadomosci".

`queue_declare(queue='', exclusive=True)` - broker generuje unikalna
nazwe (np. `amq.gen-Xxx...`), exclusive na nasze polaczenie. Idealne
dla "tymczasowego konsumenta".

Bindingi na 3 prefixy daja kopie kazdej wiadomosci ktora przeszla
przez exchange. Nawet wlasne broadcasty admina wracaja do niego (bo
admin tez bind na `broadcast.*`) - to jest zamierzone, sluzy jako
self-confirmation.

## 5. Nasze polaczenia, watki, pika BlockingConnection

### 5.1 Pika nie jest thread-safe

`pika.BlockingConnection` to klasyczny synchroniczny klient. **Jeden
kanal = jeden watek**. Probowanie publish'a w watku 1 i consume w
watku 2 na tym samym kanale = race + bledy.

### 5.2 Wzorzec dla Agencja i Administratora

Oboje sa **producentami i konsumentami** jednoczesnie:
- input z stdin -> publikuja zlecenia/broadcasty
- przychodzace potwierdzenia/broadcasty drukuja do stdout

Rozwiazanie: **2 polaczenia**.
- Watek glowny: open conn1 + ch1 do publish, czyta input, publikuje.
- Watek konsumenta (daemon): open conn2 + ch2, declare queue, bind,
  `basic_consume` + `start_consuming` (blokujacy).

Kazdy watek ma swoja `BlockingConnection`. Zero kontencji.

### 5.3 Dlaczego nie SelectConnection / aio_pika

Mozna - `pika.SelectConnection` to async wariant, podobnie `aio_pika`
nad asyncio. Wtedy jeden watek + event loop. Plus: mniej kodu. Minus:
gorsza ergonomia dla synchronicznego CLI z input(). Brief nie wymaga,
zatem dwa watki + dwie connections to wystarczajaco proste.

### 5.4 Przewoznik - jeden watek

Przewoznik nie czyta stdin. Dostaje zlecenie -> obrabia -> publikuje
potwierdzenie. **Wszystko w callbacku** (jeden watek, ktory `start_consuming`
zwroci kiedy connection sie zamknie).

To bezpieczne: callback dziala w tym samym watku co consumer loop, na
tym samym kanale. `ch_.basic_publish(...)` z callbacku idzie tym samym
kanalem co `basic_deliver` ktory wywolal callback. Zero race.

## 6. Co RabbitMQ daje za darmo (a czego nie uzywamy)

### 6.1 Persistent messages + durable queues

```python
ch.queue_declare(queue='X', durable=True)
ch.basic_publish(..., properties=pika.BasicProperties(delivery_mode=2))
```

`durable=True` na queue: przezywa restart brokera (metadane na disk).
`delivery_mode=2` na message: payload zapisany na disk razem z metadanymi.

U nas `durable=False` (default). Restart brokera = strata wszystkich
nieobsluzonych zlecen. Brief tego nie wymaga, w prezentacji obsluga
jest natychmiastowa.

### 6.2 Publisher confirms

```python
ch.confirm_delivery()
ok = ch.basic_publish(...)  # zwroci True/False
```

Bez confirms: `basic_publish` jest fire-and-forget. RabbitMQ moze odrzucic
wiadomosc (np. queue full) i nikt sie nie dowie. Z confirms: broker
wysyla `basic.ack` po przyjeciu, klient ma pewnosc. To jest gRPC analog
unary RPC odpowiedzi.

U nas brak - prezentacja sie nie sypie, w prodzie wlaczyc.

### 6.3 Dead letter exchange (DLX)

Wiadomosci ktore wygasly (`x-message-ttl`), zostaly odrzucone (`basic.reject`)
albo wyleciaja po przepelnieniu kolejki (`x-max-length`) ida do DLX -
specjalnego exchange'a. Tam mozna podpiac kolejke "incydentow" do analizy.

U nas brak - kolejki zlecen sa nielimitowane, brak TTL.

### 6.4 Mirror queues / quorum queues

Replikacja danych queue na N node'ow w klastrze RabbitMQ. Survives node
crash. Brief nie wymaga klastra.

### 6.5 Plugins: shovel, federation, MQTT, STOMP, web-stomp

Shovel: most miedzy klastrami RabbitMQ. Federation: lekka replikacja
exchange-to-exchange miedzy datacenter. MQTT/STOMP/web-stomp: protokol
adapter (np. JS w przegladarce).

## 7. Porownanie z innymi systemami komunikacji

### 7.1 vs gRPC (zad3 zada2)

| | gRPC | RabbitMQ AMQP |
|---|---|---|
| transport | HTTP/2 | dedykowany binarny TCP |
| model | client-server, request-response + streaming | broker + asynchroniczny pub-sub |
| coupling | kazdy klient zna serwer (DNS, IP) | wszyscy znaja brokera, NIE siebie |
| persistence | brak | persistent messages, durable queues |
| backpressure | flow control HTTP/2 per-stream | broker buforuje, prefetch |
| schema | protobuf .proto | brak globalnego (custom JSON, AMQP properties) |
| typowy use case | sync API, RPC | async event flows, fan-out, work queues |

Mozna budowac request-response w AMQP (correlation-id + reply-to queue),
ale to jest nie idiomatyczne. AMQP swieta sie w wielo-do-wielu i async.

### 7.2 vs Ice (zad3 zadi1)

Ice = peer-to-peer + service registry (IceGrid). Bidirectional connections,
servant model. RabbitMQ to inny paradygmat - **nie ma servera w sensie
"zawolam metode"**. Wszystko co wysylasz, wysylasz **brokerowi**.

Ice/gRPC -> "klient ma proxy do serwisu X, woluje X.method()".
AMQP/RabbitMQ -> "klient publikuje na exchange, ktos kiedys zezre".

### 7.3 vs Kafka

Kafka tez broker, ale:
- **append-only log** zamiast queues z ack/delete
- konsument trzyma offset -> moze "rewind", odtwarzac zdarzenia
- partycjonowanie z key-based hash (ordering w obrebie partycji)
- wiekszy throughput, gorsze opoznienie

Use case Kafki: event sourcing, analytics, metrics. RabbitMQ: task queues,
RPC, transactional flows. Nasze "zlecenia z ack" pasuje do RabbitMQ, nie
Kafki.

### 7.4 vs ZMQ

ZMQ = brokerless library (kontrastuje z RabbitMQ jako broker). PUB-SUB,
PUSH-PULL, REQ-REP wzorce w samym socket layer. Plusy: ekstremalnie szybki
(no broker). Minusy: brak persistence, brak retry, kazdy peer musi znac
wszystkich. Dla zad4 byloby okropnie - admin musialby polaczyc sie z
kazdym wezlem osobno.

## 8. Pytania prowadzacego (kierunki)

### O AMQP

1. **Jakich typow exchange'a uzyles i czemu?** Jeden topic - daje
   uniwersalny routing po `order.*`, `confirm.*`, `broadcast.*` w
   jednym miejscu, plus admin moze podsluchiwac wszystko 3 bindingami.

2. **Czemu nie fanout dla broadcastow?** Mozna by - osobny fanout exchange
   dla broadcastow. Ale wtedy 2 exchange'e zamiast 1. Topic robi to samo
   dzieki wildcardom, prosciej.

3. **Czemu nie direct dla potwierdzen?** Mozna by, identycznie. Ale wtedy
   3 exchange'y zamiast 1. Topic obejmuje wszystko z dodatkowym bonusem
   wildcardow dla admina.

4. **Co zrobi RabbitMQ jak publikujesz na exchange ktory nie ma zadnego
   pasujacego bindingu?** Default - wiadomosc zostaje **silently dropped**.
   Jak chcesz wiedziec, ustawiasz `mandatory=True` w basic_publish ->
   broker zwroci `basic.return` jak nikt nie pasuje.

### O work queue / fairness

5. **Jak zapewniasz "pierwszy wolny przewoznik"?** Wspoldzielona kolejka
   `service.<typ>` + `prefetch_count=1` + manual ack. Broker daje wiadomosc
   konsumentowi ktory jest "wolny" (brak nie ackowanych w prefetch).

6. **Co jak dwoch przewoznikow obsluguje ten sam typ?** Subskrybuja te
   sama kolejke (np. obaj subskrybuja `service.ladunek`). Broker dystrybuuje
   round-robin/fair dispatch.

7. **Co jak przewoznik padnie w trakcie obrobki?** Manual ack nie zostal
   wyslany. Broker wykrywa zamkniecie kanalu, wraca wiadomosc do kolejki,
   daje innemu konsumentowi (`requeue=true` default).

8. **Czemu prefetch=1 a nie 10?** Brief mowi "pierwszy wolny" - prefetch=1
   to dosloowne odzwierciedlenie. Z prefetch=10 jeden szybki przewoznik
   moglby zlapac 10 zlecen na raz, drugi siedzi bezczynnie - to nie
   jest "first available".

### O bindings / routing

9. **Co znaczy `*` a co `#` w topic?** `*` = dokladnie jeden segment,
   `#` = zero lub wiecej.

10. **Pokaz na bajtach co leci po sieci dla `basic.publish`.**
    METHOD frame z `basic.publish(exchange, routing-key, mandatory, immediate)` +
    HEADER frame z properties (content-type, etc.) + 1..N BODY frames
    z payloadem (nasze JSON).

11. **Czemu agency.NASA jest exclusive=True?** Kolejka zywie tyle co
    polaczenie agencji NASA. Druga instancja NASA dostanie blad. Przy
    rozlaczeniu kolejka znika - czysto, bez dluby resource leak.

### O broadcastach / admin

12. **Jak admin dostaje kopie wszystkich wiadomosci?** Anonimowa exclusive
    kolejka z 3 bindingami: `order.*`, `confirm.*`, `broadcast.*`. Kazda
    wiadomosc w systemie ma routing key zaczynajacy sie jednym z tych
    prefixow.

13. **Czemu admin tez bind na `broadcast.*` (widzi swoje broadcasty)?**
    Self-confirmation: admin widzi w SPY ze jego broadcast wyszedl.
    Tania pseudo-publisher-confirm.

14. **Czemu admin -> przewoznicy potrzebuje OSOBNEJ kolejki na przewoznika
    (carrier.<ID>) zamiast pchnac na work queues service.*?** Bo work
    queues maja competing consumers - broadcast trafilby tylko do jednego
    przewoznika z grupy. Personal queue daje fanout-like delivery.

15. **Co jak dodac 4 przewoznika? 5 agencji?** Skala liniowa: kazdy nowy
    przewoznik to +1 personal queue + dodatkowe basic_consume na 2 work
    queues. Kazda nowa agencja to +1 personal queue + bindings.

### O niezawodnosci / produkcji

16. **Co zrobic zeby zlecenia nie ginely przy restarcie brokera?**
    `durable=True` na queues + `delivery_mode=2` na messages + (opcjonalnie)
    `confirm_delivery()` po stronie publishera.

17. **Co zrobi RabbitMQ gdy konsument konsumuje wolniej niz publisher
    publikuje?** Bez prefetch limit i max-length: kolejka rosnie do
    skutku (RAM/disk fill -> blokada calej node). Z `x-max-length` lub
    `x-max-length-bytes`: drop-newest/drop-oldest/dead-letter.

18. **Co `basic.reject(requeue=true)` vs `basic.nack`?** `nack` to
    rozszerzenie pozwalajace acknowledgowac wiele jednoczesnie
    (`multiple=true`), `reject` jest historycznie pierwszy i obsluguje
    jedna wiadomosc. Ten sam efekt: broker wraca wiadomosc do kolejki
    lub do DLX.

19. **Co to dead letter exchange?** Specjalny exchange na ktory ida
    wiadomosci wygasle / odrzucone / przerosniete. Sluzy do analizy
    incydentow.

### O architekturze

20. **Czemu dwoch polaczen w agencji?** Pika BlockingConnection nie jest
    thread-safe. Konsument w osobnym watku potrzebuje swojego polaczenia.

21. **Czemu admin tez ma dwa polaczenia?** Dokladnie ten sam powod -
    konsument SPY w jednym watku, publisher broadcastow w drugim.

22. **Czemu przewoznik ma jedno?** Bo nie czyta stdin. Calosc dziala w
    consumer loopie + callback robi publish na tym samym kanale.

## 9. Mapping problemow rozproszenia

| problem | rozwiazanie u nas |
|---|---|
| heterogeniczne aplikacje | wszystko mowi AMQP do brokera, jezyk implementacji nie wazny |
| coupling producent-konsument | broker buforuje, producent NIE wie kto czyta |
| "wybierz wolnego pracownika" | competing consumers + prefetch=1 + manual ack |
| nie zgubic zadania w trakcie | manual ack -> broker wraca wiadomosc po crashu |
| identyfikacja zlecenia | agency name + per-agency licznik w body JSON |
| rozne typy ruchu w jednym kanale | routing key + topic exchange |
| broadcast do grupy | binding wildcardami + osobne kolejki personal queue |
| podsluchanie calego ruchu (audyt/admin) | bindings na prefixy `*.` |
| wiele typow wiadomosci do tego samego konsumenta | multi-binding na 1 kolejke + dispatch po routing_key |
| izolacja agencja-agencja (potwierdzenia adresowane) | direct routing key `confirm.<NAZWA>` + queue per agencja |
| persistence (gdy potrzebna) | `durable=True` + `delivery_mode=2` |
| flow control | prefetch_count + RabbitMQ blocks publisher when memory high |

## 10. Co mozna by dodac w prodzie

| brakuje | jak dodac |
|---|---|
| TLS | RabbitMQ na 5671 z certami, `pika.SSLOptions` w ConnectionParameters |
| auth | RabbitMQ ma user/perm system, plugin LDAP, OAuth2 |
| persistence | `durable=True` + `delivery_mode=2` + publisher confirms |
| HA | klaster RabbitMQ z quorum queues |
| dead letter | osobny DLX dla zlecen ktore z jakiegos powodu padly |
| obserwability | Prometheus exporter (`rabbitmq_exporter`), management plugin |
| limit liczby zlecen w kolejce | `x-max-length` na declare |
| retry z backoffem dla bledow biznesowych | dedykowana retry queue z `x-message-ttl` -> DLX (delayed retry pattern) |
| schema na message body | Avro/protobuf zamiast plain JSON |
| transakcje | `tx_select` + `tx_commit` na kanale (rzadko - publisher confirms zwykle wystarcza) |
| idempotency po stronie konsumenta | dedup key (np. `agency#order_id`) + sprawdzanie w bazie |
| trace context propagation | header `x-trace-id` propagowany przez properties |

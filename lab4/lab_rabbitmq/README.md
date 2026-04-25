# Lab 4 — RabbitMQ / MOM

Ściąga przed zajęciami. Bez ściemy, bez żargonu, po kolei co i dlaczego.

---

## 1. O co w ogóle chodzi?

### Problem: jak dwa programy mają ze sobą gadać?

Klasycznie — program A woła funkcję programu B (RPC, REST, gRPC). Wszystko ładnie, **dopóki obaj żyją jednocześnie**. Jak B padnie albo jest przeciążony — A czeka, crashuje albo gubi dane.

### Rozwiązanie: pośrednik (broker) + wiadomości

Zamiast A → B, robimy **A → 📬 skrzynka → B**. Skrzynka to **broker** (RabbitMQ). Wrzucasz list, broker trzyma go u siebie, a odbiorca bierze kiedy mu pasuje.

To jest **MOM — Message Oriented Middleware**. Middleware = warstwa pośrednia. Message oriented = gada wiadomościami, nie wywołaniami funkcji.

### Synchroniczna vs asynchroniczna komunikacja

| | Synchroniczna | Asynchroniczna (RabbitMQ) |
|---|---|---|
| Kto musi żyć jednocześnie | A i B | Tylko broker + ten kto akurat coś robi |
| Wywołanie | blokujące (czekasz na odpowiedź) | nieblokujące (wrzucasz i lecisz dalej) |
| Gdy B padnie | A ma problem | Wiadomość siedzi w kolejce, poczeka |
| Potwierdzenia | wynikają z odpowiedzi | opcjonalne (ACK) |

**Analogia:** telefon (sync) vs SMS (async). Jak ktoś nie odbierze telefonu — nici z rozmowy. SMS doleci jak wstanie.

---

## 2. Elementy układanki

```
┌──────────┐     ┌─────────────────────────────┐     ┌──────────┐
│ Producer │────▶│  RabbitMQ (broker)          │────▶│ Consumer │
│ (nadawca)│     │  [Exchange] → [Queue]       │     │ (odbiorca)│
└──────────┘     └─────────────────────────────┘     └──────────┘
                          ▲
                          │ Admin UI (http://localhost:15672)
```

- **Producer** — wysyła wiadomość.
- **Consumer** — odbiera wiadomość.
- **Broker** (serwer RabbitMQ) — pośrednik trzymający wiadomości.
- **Connection** — pojedyncze połączenie TCP do brokera (kosztowne).
- **Channel** — lekki „sub-kanał" wewnątrz Connection. **Zawsze używaj Channel do operacji**, nie otwieraj nowego Connection do każdej wiadomości.
- **Queue (kolejka)** — FIFO-ish bufor, w którym siedzą wiadomości aż ktoś je pobierze.
- **Exchange (centrala)** — router. **Producent NIE wysyła do kolejki** — wysyła do Exchange, a Exchange decyduje, do której kolejki (lub kolejek) to trafi.
- **Binding** — połączenie Exchange ↔ Queue. Dopóki kolejka nie jest zbindowana z Exchange, nic z niego nie dostanie.

**Kluczowe przewartościowanie:** jak pierwszy raz widzisz `basicPublish("", "queue1", ...)` i myślisz „wysyłam do kolejki" — **to nieprawda**. Wysyłasz do **pustego/domyślnego Exchange** (pierwszy argument to `""`), który ma taką magiczną właściwość, że jak nazwa routing key pasuje do nazwy kolejki, to tam wrzuca. Wygląda jak bezpośrednio do kolejki, ale technicznie przechodzi przez Exchange.

---

## 3. Typy Exchange (routing)

Tutaj się wszystko rozstrzyga. Exchange ma **typ**, który mówi jak routować:

### 3.1 Default / Nameless (`""`)
Dla noobów. Wysyłasz z routing key = nazwa kolejki → trafia do tej kolejki. Z tego korzysta **Z1_Producer**.

### 3.2 Fanout — „rozgłoś wszystkim"
Publish/subscribe. Wiadomość leci **do wszystkich kolejek** zbindowanych z tym exchange. Routing key ignorowany.
```
P ──▶ X(fanout) ──┬──▶ Q1 ──▶ C1
                  └──▶ Q2 ──▶ C2
```
Użycie: eventy, powiadomienia, logi broadcastowe. To jest **Z2**.

### 3.3 Direct — „dokładnie ten klucz"
Wiadomość trafia do kolejki, której **binding key == routing key** wiadomości. Dokładne dopasowanie.
```
P ──▶ X(direct) ──orange──▶ Q1
             └──black/green──▶ Q2
```
Użycie: segregacja po kategorii (np. logi error vs info na różne kolejki).

### 3.4 Topic — „pasujące wzorce"
Jak Direct, ale binding key to **wzorzec** z kropkami:
- `*` = dokładnie jedno słowo
- `#` = zero lub więcej słów

Przykłady wzorców:
- `*.orange.*` → pasuje do `quick.orange.rabbit`, nie do `lazy.orange.male.rabbit`
- `lazy.#` → pasuje do `lazy`, `lazy.brown.fox`, `lazy.cat.eats.mice`
- `#.rabbit` → pasuje do wszystkiego kończącego się na `.rabbit`

Użycie: routing po wielowymiarowej kategorii (np. `log.kern.critical`, `log.user.info`).

### 3.5 Headers
Jak Direct, ale dopasowanie po nagłówkach wiadomości, nie po routing key. Rzadko używane — pomiń.

### Ściąga tabelą

| Typ | Dopasowanie | Kiedy użyć |
|---|---|---|
| Default (`""`) | routing key == nazwa kolejki | Pierwsze zabawki |
| Fanout | wszyscy zbindowani | Broadcast, pub/sub |
| Direct | routing key == binding key | Segregacja po pojedynczej kategorii |
| Topic | wzorce `*` / `#` | Segregacja wielowymiarowa |
| Headers | nagłówki wiadomości | (rzadko) |

---

## 4. Niezawodność — co jak coś padnie

### 4.1 Acknowledgements (ACK) — potwierdzenia od konsumenta
Kiedy konsument dostaje wiadomość, broker musi wiedzieć: „ok, przetworzone, mogę ją usunąć". To robi ACK.

- **`autoAck=true`** — broker kasuje wiadomość jak tylko ją wyśle. Szybko, ale jak konsument padnie w trakcie przetwarzania — wiadomość tracisz.
- **`autoAck=false`** + `channel.basicAck(deliveryTag, false)` — konsument sam potwierdza **po przetworzeniu**. Jak padnie bez ACK — broker wysyła ją do innego konsumenta. **To chcesz w prawdziwym kodzie.**

### 4.2 Durability + Persistence — co jak padnie broker
- **Durable queue** (`queueDeclare(name, true, ...)`) — kolejka przeżyje restart brokera.
- **Persistent message** (`MessageProperties.PERSISTENT_TEXT_PLAIN` w `basicPublish`) — wiadomość zapisywana na dysk.

**Żeby przeżyło naprawdę, musisz oba** — durable queue i persistent message. Jedno bez drugiego nie wystarczy.

### 4.3 Prefetch / QoS — load balancing
Domyślnie RabbitMQ rozdaje wiadomości round-robin, nie patrząc czy konsument nadąża. Wynik: jeden konsument zawalony, drugi się nudzi.

```java
channel.basicQos(1);
```
Mówi: „nie wysyłaj mi kolejnej, dopóki nie potwierdzę poprzedniej". Efekt: wolniejszy dostaje mniej, szybszy więcej → **load balancing**.

---

## 5. Sprzęt który masz postawiony

RabbitMQ chodzi w Dockerze (kontener `rabbitmq` — już wystartowany, sam wstanie po reboocie).

### Sprawdź że działa
```bash
docker ps --filter name=rabbitmq
```

### Management UI
- http://localhost:15672
- login: `guest` / hasło: `guest`
- tam podejrzysz queue'y, exchange'y, bindings, wiadomości w locie

### Porty
- **5672** — AMQP (tam gada Twój kod Javy, `factory.setHost("localhost")`)
- **15672** — Management UI

### Przydatne komendy
```bash
docker logs -f rabbitmq          # logi na żywo
docker restart rabbitmq          # restart (test durability!)
docker stop rabbitmq             # wyłącz
docker start rabbitmq            # włącz
docker exec rabbitmq rabbitmqctl list_queues     # kolejki z CLI
docker exec rabbitmq rabbitmqctl list_exchanges  # exchange'y z CLI
```

---

## 6. Pliki w tym folderze

- `Z1_Producer.java` / `Z1_Consumer.java` — **Hello World**, default exchange, jedna kolejka `queue1`
- `Z2_Producer.java` / `Z2_Consumer.java` — **Fanout** exchange `exchange1`, każdy consumer ma swoją tymczasową kolejkę
- `amqp-client-5.11.0.jar` — klient RabbitMQ dla Javy (biblioteka główna)
- `slf4j-api-*.jar`, `slf4j-simple-*.jar` — logger, wymagany przez `amqp-client`

### Kompilacja i uruchomienie (bez IDE)

```bash
cd lab4/lab_rabbitmq

# kompilacja (wszystkie 4 klasy)
javac -cp "amqp-client-5.11.0.jar:slf4j-api-1.7.29.jar" Z1_Producer.java Z1_Consumer.java Z2_Producer.java Z2_Consumer.java

# Z1 — uruchom najpierw konsumenta (w terminalu 1), potem producenta (terminal 2)
java -cp ".:amqp-client-5.11.0.jar:slf4j-api-1.7.29.jar:slf4j-simple-1.6.2.jar" Z1_Consumer
java -cp ".:amqp-client-5.11.0.jar:slf4j-api-1.7.29.jar:slf4j-simple-1.6.2.jar" Z1_Producer

# Z2 — fanout: odpal 2 konsumentów + 1 producenta
java -cp ".:amqp-client-5.11.0.jar:slf4j-api-1.7.29.jar:slf4j-simple-1.6.2.jar" Z2_Consumer  # terminal 1
java -cp ".:amqp-client-5.11.0.jar:slf4j-api-1.7.29.jar:slf4j-simple-1.6.2.jar" Z2_Consumer  # terminal 2
java -cp ".:amqp-client-5.11.0.jar:slf4j-api-1.7.29.jar:slf4j-simple-1.6.2.jar" Z2_Producer  # terminal 3
```

Na Windows separator classpath to `;`, nie `:`.

---

## 7. Szablon kodu (API Javy w trzech krokach)

### Nawiązanie połączenia (to samo dla P i C)
```java
ConnectionFactory factory = new ConnectionFactory();
factory.setHost("localhost");                  // broker w Dockerze lokalnie
Connection connection = factory.newConnection();
Channel channel = connection.createChannel();
```

### Producent
```java
// tylko jeśli używasz default exchange — zadeklaruj kolejkę
channel.queueDeclare("queue1", false, false, false, null);
//                    name    durable exclusive autoDelete args

// publikacja: exchange, routingKey, props, body
channel.basicPublish("", "queue1", null, "Hello!".getBytes());

channel.close();
connection.close();
```

### Konsument
```java
channel.queueDeclare("queue1", false, false, false, null);

DeliverCallback callback = (tag, delivery) -> {
    String msg = new String(delivery.getBody(), "UTF-8");
    System.out.println("Got: " + msg);
    // jeśli autoAck=false, tu byłoby: channel.basicAck(delivery.getEnvelope().getDeliveryTag(), false);
};

channel.basicConsume("queue1", true, callback, tag -> {});
//                   queue    autoAck

// NIE zamykaj connection! Konsument musi żyć żeby słuchać.
```

### `queueDeclare(name, durable, exclusive, autoDelete, args)` — flagi
| Flaga | Co robi |
|---|---|
| `durable` | Przeżyje restart brokera (jeśli wiadomości też są persistent) |
| `exclusive` | Tylko to połączenie może używać; kasowana przy rozłączeniu |
| `autoDelete` | Kasowana gdy ostatni konsument się odpina |

### Fanout Exchange (Z2)
```java
// Producer:
channel.exchangeDeclare("exchange1", BuiltinExchangeType.FANOUT);
channel.basicPublish("exchange1", "", null, msg.getBytes());
//                    exchange     routingKey (ignorowany dla fanout)

// Consumer:
channel.exchangeDeclare("exchange1", BuiltinExchangeType.FANOUT);
String queue = channel.queueDeclare().getQueue();  // losowa nazwa, exclusive, autoDelete
channel.queueBind(queue, "exchange1", "");         // <-- TO jest kluczowe!
channel.basicConsume(queue, true, callback, tag -> {});
```

### Direct / Topic
```java
channel.exchangeDeclare("ex", BuiltinExchangeType.DIRECT);   // lub TOPIC
channel.queueBind(queue, "ex", "orange");                    // direct: exact match
channel.queueBind(queue, "ex", "*.orange.*");                // topic: wzorzec
channel.basicPublish("ex", "orange", null, msg.getBytes());  // routingKey ma znaczenie
```

---

## 8. Zadania z instrukcji

- **Zad 1 (2pkt)** — niezawodność + load-balancing. Chodzi o: `autoAck=false` + `basicAck`, `basicQos(1)`, durable queue + persistent message. Testuj restartem brokera (`docker restart rabbitmq`) i zabijaniem konsumenta w trakcie przetwarzania.
- **Zad 2 (2pkt)** — Direct + Topic. Napisz producenta wysyłającego z różnymi routing key i konsumentów bindujących na różne wzorce.
- **Zadanie domowe** — treść na UPEL.

---

## 9. Pułapki (żeby nie tracić czasu na debugowanie)

1. **„Konsument nic nie dostaje"** — sprawdź bindings w Management UI. Fanout/Direct/Topic **wymagają** `queueBind`, a domyślny exchange tego nie widać bo to hackerski skrót.
2. **Kasowanie kolejki z innymi parametrami** — jak raz zadeklarowałeś `queue1` jako `durable=false`, a teraz próbujesz `durable=true`, to rzuci wyjątkiem. Skasuj ją w Management UI albo zmień nazwę.
3. **Connection leak** — zapomniałeś `connection.close()` w producencie → kolejki się mnożą. W konsumencie odwrotnie: **nie zamykaj**, bo przestanie słuchać.
4. **`guest`/`guest` działa tylko z localhost** — z innej maszyny trzeba nowego użytkownika (tu nieistotne, wszystko na localu).
5. **Classpath na Windows** — `;` zamiast `:`. Przy kopiowaniu z instrukcji Linuxowej łatwo zgubić.
6. **`channel.basicPublish("", QUEUE_NAME, ...)`** — pusty string to default exchange, nie „brak exchange". Nie pomyl z fanoutem.

---

## 10. Co doczytać

- Oficjalne tutoriale (6 sztuk, krótkie, z diagramami): https://www.rabbitmq.com/getstarted.html
- AMQP 0-9-1 model (jak to działa „pod spodem"): https://www.rabbitmq.com/tutorials/amqp-concepts.html
- Management UI: wyklikaj tam queue i exchange ręcznie, zobacz bindings — często lepiej wbija do głowy niż czytanie

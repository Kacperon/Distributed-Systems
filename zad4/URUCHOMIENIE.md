# Uruchomienie i opis dzialania

## Wymagania

- Python 3 + `pika` (`pip install -r requirements.txt`)
- RabbitMQ na localhost:5672 (host konfigurowalny przez `RABBITMQ_HOST`).
  - `docker run --rm -p 5672:5672 -p 15672:15672 rabbitmq:3-management`
  - Panel zarzadzania: http://localhost:15672 (guest/guest) — pokazuje exchange'a, kolejki i wiazania na zywo.

## Struktura projektu

| Plik              | Co robi                                                     |
|-------------------|-------------------------------------------------------------|
| `common.py`       | nazwa exchange'a, lista uslug, pomocnik na host RabbitMQ    |
| `agencja.py`      | klient agencji (publikuje zlecenia, sluchaja potwierdzen)   |
| `przewoznik.py`   | klient przewoznika (sluch zlecen, publikuje potwierdzenia)  |
| `administrator.py`| szpieg + nadawca broadcastow                                |
| `schemat.d2`      | zrodlo diagramu                                             |
| `schemat.svg/png` | wyrenderowany diagram (`d2 --layout=elk schemat.d2 ...`)    |
| `schemat.md`      | tabela z kolejkami, kluczami i wiazaniami + osadzony diagram|

## Architektura — co i dlaczego

### Jeden topic exchange `space.events`

Wszystkie wiadomosci ida przez ten sam topic exchange. Topic, bo:

- chcemy oddzielic typy ruchu po prefiksie klucza (`order.*`, `confirm.*`, `broadcast.*`),
- chcemy aby admin mogl podpiac sie pod calosc (`order.*` + `confirm.*` + `broadcast.*`) bez zmian po stronie agencji/przewoznikow,
- chcemy precyzyjnego adresowania potwierdzen (`confirm.NASA` vs `confirm.ESA`).

Direct exchange tez by zadzialal dla potwierdzen, ale topic daje admina za darmo (jedno wiazanie na `confirm.*`).

### Kolejki robocze `service.<typ>`

Trzy kolejki, jedna na typ uslugi. Wiazane na `order.<typ>`. Kazdy przewoznik podpina sie jako consumer do kolejek odpowiadajacych jego dwum uslugom.

- Kolejka jest **wspoldzielona** miedzy przewoznikow tego samego typu — RabbitMQ rozdziela zlecenia round-robin.
- `basic_qos(prefetch_count=1)` — RabbitMQ nie wysle nowego zlecenia do przewoznika, ktory ma juz jedno w obrobce. To realizuje wymaganie "pierwszy wolny przewoznik".
- **Manual ack** (`basic_ack` po wyslaniu potwierdzenia) — jesli przewoznik padnie w trakcie obrobki, RabbitMQ przekaze zlecenie nastepnemu wolnemu przewoznikowi tego typu. To realizuje wymaganie "zlecenie nie zaginie" przy zachowaniu "dokladnie jeden przewoznik".

### Kolejki adresowane `agency.<NAZWA>`, `carrier.<ID>`, `admin.<auto>`

Po jednej na konsumenta — kazda jest `exclusive=True` (znika po rozlaczeniu, jeden konsument).

- `agency.<NAZWA>` — wiazania na `confirm.<NAZWA>` (potwierdzenia tylko dla tej agencji), `broadcast.agencies` i `broadcast.all` (admin do wszystkich agencji / wszystkich).
- `carrier.<ID>` — wiazania `broadcast.carriers` i `broadcast.all`. NIE pobiera stad zlecen — zlecenia ida przez kolejki robocze. Ta kolejka istnieje tylko po to, zeby kazdy przewoznik dostal swoja kopie broadcastu admina (bo kolejki robocze sa wspoldzielone, broadcast tez bylby rozdystrybuowany — niedobrze).
- `admin.<auto>` — anonimowa exclusive kolejka, wiazana na `order.*`, `confirm.*`, `broadcast.*`. Admin widzi wszystko.

### Identyfikacja zlecenia: agencja + nr

Body wiadomosci to JSON `{agency, order_id, service}`. `order_id` to lokalny licznik agencji (rosnacy od 1, niezalezny dla NASA i ESA). Spelnia wymaganie "identyfikowane przez nazwe Agencji oraz wewnetrzny numer".

### Watki w agencji i administratorze

`pika.BlockingConnection` nie jest thread-safe, wiec konsument i producent dziel sie na **dwa watki z osobnymi polaczeniami**:

- watek glowny — czeka na komendy z stdin i publikuje,
- watek konsumenta — `start_consuming()` blokuje sie na callbackach, drukuje przychodzace wiadomosci.

Przewoznik nie czyta stdin, wiec ma jeden watek (glowny w `start_consuming`).

## Scenariusz prezentacji

W **piec terminali** (kazda komenda w osobnym):

```
python administrator.py                           # admin
python agencja.py NASA                            # agencja 1
python agencja.py ESA                             # agencja 2
python przewoznik.py C1 osoby ladunek             # przewoznik 1
python przewoznik.py C2 ladunek satelita          # przewoznik 2
```

### Komendy

**Agencja** (`agencja.py <NAZWA>`):
- `osoby` / `ladunek` / `satelita` — wysyla zlecenie danego typu (numer kolejny rosnie automatycznie)
- `quit` — wyjscie

**Przewoznik** (`przewoznik.py <ID> <usluga1> <usluga2>`):
- bez interakcji — odbiera zlecenia, drukuje, wysyla potwierdzenia, ack
- `Ctrl+C` — wyjscie

**Administrator** (`administrator.py`):
- `agencies <text>` — komunikat do wszystkich agencji
- `carriers <text>` — komunikat do wszystkich przewoznikow
- `all <text>` — komunikat do wszystkich
- `quit` — wyjscie

### Co pokazac

1. **Prosta sciezka zlecenia.** W NASA: `osoby`. W konsoli widac:
   - NASA: `sent order #1 for osoby`, potem `[CONFIRM] order #1 (osoby) done by carrier C1`
   - C1: `[ORDER] NASA #1 osoby`, potem `confirmed NASA #1`
   - admin: `[SPY order.osoby] {...}` i `[SPY confirm.NASA] {...}` (kopia obu)
   - C2: cisza — nie obsluguje osob.

2. **Routing po typie.** W NASA: `satelita` -> trafia do C2 (C1 nie obsluguje). W admin widac `[SPY order.satelita]` i `[SPY confirm.NASA]` z `carrier: C2`.

3. **Round-robin u wolnego przewoznika.** W ESA wpisz `ladunek` 4 razy z rzedu. C1 i C2 oboje obsluguja ladunek, RabbitMQ rozdaje na przemian: order #1 -> C1, #2 -> C2, #3 -> C1, #4 -> C2 (potwierdzone w tescie automatycznym). Kazde zlecenie dokladnie u jednego przewoznika.

4. **Admin -> agencje.** W admin: `agencies powiadomienie`. NASA i ESA wypisuja `[ADMIN -> broadcast.agencies] powiadomienie`. C1 i C2 nic nie widza.

5. **Admin -> przewoznicy.** `carriers maintenance` -> tylko C1 i C2 widza `[ADMIN -> broadcast.carriers] maintenance`.

6. **Admin -> wszyscy.** `all attention` -> NASA, ESA, C1, C2 wszyscy widza `[ADMIN -> broadcast.all] attention`. Admin sam tez widzi to przez swoja spy queue.

7. **Spy admina.** Przy kazdej wiadomosci w terminalu admina pojawia sie `[SPY <routing-key>] {...}` — kazde zlecenie, kazde potwierdzenie, kazdy broadcast.

## Mapa wymagan -> implementacja

| Wymaganie z brief'a                                                                      | Realizacja                                                                                          |
|------------------------------------------------------------------------------------------|------------------------------------------------------------------------------------------------------|
| 3 typy uslug                                                                             | `common.SERVICES = ('osoby', 'ladunek', 'satelita')`                                                |
| Przewoznik dokladnie 2 z 3                                                               | `przewoznik.py`: walidacja `len(set(services)) != 2`                                                |
| Zlecenie do pierwszego wolnego przewoznika obslugujacego ten typ                         | `service.<typ>` shared queue + `basic_qos(prefetch_count=1)` + manual ack                           |
| Zlecenie nie do wiecej niz jednego przewoznika                                           | competing consumers — kolejka dostarcza wiadomosc do dokladnie jednego konsumenta                   |
| Zlecenie identyfikowane przez nazwe agencji + nr wewn.                                   | body JSON `{agency, order_id}`, `order_id` to per-agency licznik                                    |
| Po wykonaniu uslugi przewoznik wysyla potwierdzenie do agencji                           | publish do `space.events` z kluczem `confirm.<agencja>` po `basic_ack`                              |
| Premium: admin kopia wszystkich wiadomosci                                               | exclusive queue admina z wiazaniami `order.*`, `confirm.*`, `broadcast.*`                          |
| Admin: 3 tryby (do agencji / do przewoznikow / do obu)                                   | publish z kluczem `broadcast.agencies`, `broadcast.carriers`, `broadcast.all`                       |
| Schemat elektroniczny z uzytkownikami, exchange, kolejkami, kluczami                     | `schemat.d2` + `schemat.svg`/`schemat.png` + tabela w `schemat.md`                                  |
| Zlecenia obslugiwane natychmiast                                                         | callback przewoznika od razu publikuje potwierdzenie i `basic_ack` — brak symulacji opoznienia      |

## Uwagi co do oddania

- Diagram (`schemat.svg`/`schemat.png`) jest postaci elektronicznej, wygenerowany z `schemat.d2` przez d2 z layoutem ELK — zaden skan recznego rysunku.
- Jezyk: Python 3 z `pika`. Bez stub'ow — RabbitMQ nie wymaga IDL.
- Komunikaty agencja/przewoznik nie maja stempla czasowego ani signatur — celowo proste, brief tego nie wymaga.
- Po `Ctrl+C`/`quit` exclusive queues znikaja same z RabbitMQ. Kolejki robocze `service.*` zostaja, ale sa puste.

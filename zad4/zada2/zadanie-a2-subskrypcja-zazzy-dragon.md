# Plan: A2 klient gRPC (grpclib + textual)

## Kontekst

Zadanie A2 wymaga aplikacji klient-serwer gRPC z subskrypcją zdarzeń, strumieniowaniem, odpornością na błędy sieciowe i NAT-friendly. Serwer w Ruście (Tonic) znajduje się w `../server` i implementuje `CanService` z trzema metodami:

- `Subscribe(stream SubscribeRequest) returns (stream SubscribeResponse)` bidi
- `ListMessages(ListMessagesRequest) returns (ListMessagesResponse)` unary
- `GetStats(StatsRequest) returns (MessageStats)` unary

Serwer używa `session-id` (UUID w metadanych HTTP) do wznawiania sesji z buforem 60s (`SESSION_TTL`). `SubscribeRequest` zawiera `repeated MessageCategory categories`, `repeated string message_names`, `bool unsubscribe`. `SubscribeResponse` to `oneof` z `Cache snapshot`, `CanUpdate update`, `SessionInfo session_info`.

Szablon klienta w `./` ma już zainstalowane `grpcio`, `grpcio-tools`, `grpclib[protobuf]`, `textual`, `poethepoet` oraz task `poe proto` generujący pliki do `./generated/`. `main.py` to stub. Trzeba zbudować logikę aplikacji.

Cel: minimalny klient TUI w Textual, w pełni zgodny z wymaganiami A2, obsługujący wznawianie sesji po brudnym zamknięciu procesu oraz auto-reconnect po chwilowym padzie serwera (oba mechanizmy korzystają z tej samej persystencji UUID).

## Konfiguracja uruchomienia

- Host i port serwera zaszyte w kodzie (`localhost:50051`).
- Jeden CLI argument: `--name <nazwa>` (default `default`). Umożliwia wiele instancji z tego samego folderu.
- Plik sesji: `./.session_<nazwa>` (tekstowy, jedna linia z UUID).

## Architektura

Dwa kooperujące taski asyncio uruchamiane w jednym event loopie Textual:

1. UI (Textual `App`) renderuje widoki, obsługuje zdarzenia użytkownika, wrzuca requesty do kolejki wychodzącej.
2. gRPC worker trzyma `Channel` z grpclib, wykonuje bidi `Subscribe`, pushuje zdarzenia do UI, wysyła `SubscribeRequest` z kolejki wychodzącej, zarządza reconnectem.

Komunikacja UI - worker przez dwa `asyncio.Queue`:

- `outgoing: Queue[SubscribeRequest]` z UI do workera.
- `incoming: Queue[SubscribeResponse | StatusEvent]` z workera do UI (gdzie `StatusEvent` to wewnętrzny enum: Connected, Reconnecting, Expired).

Worker trzyma też `current_filter: SubscribeRequest` (ostatnio zaaplikowany filtr), żeby po reconnect automatycznie wysłać go ponownie.

## Pliki do utworzenia

- `main.py` parsowanie argumentu, inicjalizacja sesji, uruchomienie Textual app.
- `src/__init__.py` puste.
- `src/config.py` stałe: HOST, PORT, SESSION_TTL_S=60, RECONNECT_BACKOFF=[1,2,4,8,16,30,30], RECONNECT_TOTAL_S=60.
- `src/session.py` klasa `Session` z metodami `load_or_create(name)` i `clear()`.
- `src/grpc_client.py` klasa `CanClient` opakowująca `grpclib.client.Channel` i wygenerowany `CanServiceStub`. Metody: `run_subscribe_loop()` (bidi z reconnectem), `list_messages()`, `get_stats(name)`, `send_filter(cats, names)`, `send_unsubscribe()`.
- `src/tui.py` `ClientApp(App)` z czterema panelami w gridzie 2x2 (lewa kolumna podzielona pionowo, prawa na całą wysokość).

## Layout TUI

Header (`Header` Textual): `{name} [{session_id[:8]}] status={Connected|Reconnecting|Expired}`.

Lewy górny panel `SubscriptionPanel`:
- `SelectionList` z 10 kategoriami CAN (BMS, ENGINE, CHARGER, COOLING, LIGHTS, SENSORS, DASHBOARD, PEDALS, POWER, NODE_STATUS).
- `Input` na `message_names` po przecinku (trim whitespace, pomija puste).
- `Button` Apply wysyła `SubscribeRequest(categories, message_names, unsubscribe=false)` do kolejki wychodzącej, aktualizuje `current_filter` w workerze.
- `Button` Unsubscribe wysyła `SubscribeRequest(unsubscribe=true)`, czyści `current_filter` w workerze (stream pozostaje otwarty).

Lewy dolny panel `GetsPanel`:
- `RadioSet` lub `Select` z dwoma opcjami: ListMessages, GetStats.
- `Input` na `message_name` (używany tylko dla GetStats).
- `Button` Call wywołuje odpowiednie unary RPC, wynik append do `RichLog` niżej.
- `Button` Clear czyści `RichLog` wyników.
- `RichLog` scrollowalny z wynikami.

Prawy panel `StreamPanel`:
- `RichLog` scrollowalny, append-only, pokazuje wszystkie 3 warianty `oneof` z `SubscribeResponse`:
  - `CanUpdate`: `[HH:MM:SS] message_name: sig1=val unit, sig2=val unit`
  - `Cache snapshot`: `[HH:MM:SS] SNAPSHOT (N entries)` plus lista wpisów
  - `SessionInfo`: `[HH:MM:SS] RESUMED dropped=N`
- `Button` Clear czyści `RichLog`.

Bindings:
- `ctrl+q` clean exit.
- `ctrl+c` domyślne zachowanie Textual przerywa proces (dirty exit).

## Przepływy

### Start

1. `main.py` parsuje `--name`, ładuje/tworzy `Session`, zapisuje UUID do pliku jeśli nowy.
2. Tworzy `CanClient`, tworzy kolejki, startuje `ClientApp`.
3. App w `on_mount` startuje `asyncio.create_task(client.run_subscribe_loop(session_id, incoming_q, outgoing_q))`.
4. Worker otwiera bidi z `metadata=[("session-id", session_id)]`, nie wysyła żadnego `SubscribeRequest` dopóki UI nie kliknie Apply.

### Apply filter

- UI czyta `SelectionList.selected` i `Input.value`, buduje `SubscribeRequest`, puta na `outgoing`.
- Worker yielduje request do serwera, serwer odpowiada Cache snapshot i startuje strumień CanUpdate.
- Worker zapisuje request w `current_filter`.

### Unsubscribe

- UI puta `SubscribeRequest(unsubscribe=True)` na `outgoing`.
- Worker wysyła, czyści `current_filter`. Stream pozostaje żywy.

### Unary gets

- UI woła `await client.list_messages()` lub `await client.get_stats(name)` bezpośrednio, bez kolejki. Wynik serializowany do tekstu i wpisany do `RichLog` dolnego panelu.
- ListMessages wypisuje listę `message_name (CATEGORY)` per linia.
- GetStats wypisuje `last_seen=..., total_count=...`.
- Błędy RPC łapane, wypisywane jako `ERROR: {code} {message}`.

### Auto reconnect

- W `run_subscribe_loop` worker w pętli wywołuje `_one_subscribe_session()`.
- Na wyjątek (`grpclib.exceptions.StreamTerminatedError`, `ConnectionError`, `OSError`, timeouty) worker:
  1. Emituje `StatusEvent.Reconnecting` do `incoming`.
  2. Śledzi `elapsed` od pierwszej utraty połączenia.
  3. Dla każdego kolejnego próby czeka kolejny element z `RECONNECT_BACKOFF` (cap 30s).
  4. Jeśli `elapsed > RECONNECT_TOTAL_S (60)` emituje `StatusEvent.Expired` i kończy.
- Po udanym reconnect worker:
  1. Otwiera nowy bidi z tym samym session-id.
  2. Jeśli `current_filter` nie None, wysyła go automatycznie (resumuje intencję subskrypcji).
  3. Emituje `StatusEvent.Connected`.
  4. Serwer wyśle `SessionInfo(resumed=true, dropped_count=N)` plus buforowane CanUpdate.

### Clean exit (Ctrl+Q)

1. Wyślij `SubscribeRequest(unsubscribe=true)` (best effort, ignoruj błąd).
2. Zamknij stream przez `channel.close()`.
3. `session.clear()` usuwa plik `./.session_<name>`.
4. `app.exit()`.

### Dirty exit (Ctrl+C)

- Textual default plus SIGINT. Proces ginie, plik sesji zostaje, filtr nieutrwalony (user musi ponownie kliknąć Apply po ponownym uruchomieniu, ale SessionInfo pokaże ile wiadomości zostało zbuforowane).

## Krytyczne pliki do użycia ponownie

- `generated/service_grpc.py` asynchroniczny stub grpclib `CanServiceStub`.
- `generated/service_pb2.py` klasy message, w tym `SubscribeRequest`, `SubscribeResponse`, `MessageCategory`, `ListMessagesRequest`, `StatsRequest`.
- `pyproject.toml` task `poe proto` do ewentualnej regeneracji po zmianie proto.

## Weryfikacja end-to-end

1. `uv sync` zainstaluje dependencies.
2. `uv run poe proto` regeneruje proto (kontrolnie).
3. W drugim terminalu: `cargo run` w `../server`.
4. `uv run main.py --name c1` uruchamia TUI.
5. W UI zaznaczyć BMS, ENGINE w SelectionList, kliknąć Apply. W prawym panelu pojawia się Cache snapshot i strumień CanUpdate co ~sekundę.
6. Wpisać w Input `BMSMasterStatus`, Apply. Filtr zmienia się w locie (bez restartu streamu).
7. Kliknąć Unsubscribe. Updates ustają.
8. W dolnym panelu wybrać ListMessages, Call. W `RichLog` pojawia się lista wiadomości.
9. Wybrać GetStats, wpisać nazwę wiadomości, Call. Wynik statystyk.
10. Clear w dolnym panelu i w prawym panelu działają.
11. Test dirty exit plus wznowienie: `Ctrl+C` kończy c1, plik `.session_c1` zostaje. `uv run main.py --name c1` ponownie. Worker wysyła ten sam session-id, serwer odpowiada `SessionInfo(resumed=true)` (widoczne w prawym panelu natychmiast po Apply lub po pierwszym requeście).
12. Test auto-reconnect: przy żywym c1 zatrzymać serwer. Header pokazuje `status=Reconnecting`. Wystartować serwer ponownie w ciągu 60s. Klient automatycznie łączy się, status wraca na `Connected`, pojawia się `SessionInfo(resumed=true)` plus buforowane wiadomości.
13. Test session expired: zatrzymać serwer, poczekać ponad 60s. Header `status=Expired`, klient dalej działa w UI ale stream jest zamknięty (user może kliknąć Ctrl+Q i zacząć od nowa).
14. Test wielu instancji: równolegle `uv run main.py --name c1` i `uv run main.py --name c2` w dwóch terminalach, każda ma własne `.session_c1`, `.session_c2` i niezależną subskrypcję.
15. Test NAT-friendly: serwer ma już HTTP/2 keepalive 20s i TCP keepalive 30s. grpclib po stronie klienta obsługuje ping/pong HTTP/2 out of the box. Długotrwałe połączenie bez updates pozostaje żywe (weryfikacja: Wireshark na porcie 50051, widoczne HTTP/2 PING frame co ok. 20s).

## Uwagi realizacyjne

- Brak komentarzy w kodzie.
- Brak fancy unicode w tekstach UI i logach (ascii only w statusach, nazwy wiadomości i sygnałów i tak przychodzą z serwera).
- Bez adnotacji typów Pythona tam gdzie nie są wymuszone przez framework. Dataclasses zostawić bez annotacji pól (użyć `__init__` ręcznie albo zwykłej klasy zamiast `@dataclass`). Grpclib i tak zwraca nietypowane obiekty.
- Brak testów jednostkowych/integracyjnych (zadanie nie wymaga, demo manualne).
- Katalog `generated/` pozostaje osobno od kodu źródłowego (zgodnie z wymogiem, że generowane stuby mają być w osobnym katalogu).

# Zadanie I1 - Wywolanie dynamiczne (Ice)

## TL;DR - co tu jest

Klient w **Pythonie** wywoluje serwer w **Scali** przez ZeroC Ice 3.7. **Caly sens zadania**: klient nie ma w sobie zadnych klas wygenerowanych ze Slice IDL przez `slice2py` (zero `*_ice.py` w git, zero `library/` jako pakietu). Definicje typow i proxy buduje **w czasie wykonania** przez `Ice.loadSlice("catalog.ice")` - IcePy parsuje plik `.ice` i poprzez `IcePy.defineStruct`/`defineSequence`/`defineProxy` dynamicznie tworzy klasy `library.Book`, `library.AddBookRequest`, `library.CatalogPrx`, `library.BookStream`, ...

Lancuch w 5 zdaniach:
1. Serwer Scala: `sbt` woła `slice2java` na `catalog.ice` -> generuje Java skeleton + interface `Catalog` + `BookStreamPrx` (Java mapping). Reszta kodu serwera implementuje 4 operacje (3 unary + 1 z callbackiem) i rejestruje servant na object adapterze.
2. Serwer pasywnie czeka na requesty Ice protocol nad TCP - **brak natywnego reflection** w Ice 3.7 (jedyne mechanizmy introspekcji to `ice_ping`, `ice_id`, `ice_ids`, `ice_isA`, ktore dotycza tylko nazw typow, nie operacji ani sygnatur).
3. Klient Python na starcie wola `Ice.loadSlice("catalog.ice")` - IcePy ladowany dynamicznie buduje wszystkie typy z Slice. Po tym ma typed proxy: `library.CatalogPrx.checkedCast(base_proxy)`.
4. **Streaming** (brak natywnego server-streaming w Ice operation signature) realizujemy przez **dwukierunkowy callback**: klient implementuje servant `library.BookStream` (kolejkujacy ksiazki do `queue.Queue`), serwer woła `observer.onNext(book)` per kazdy match.
5. **Wszystkie wywolania na typed proxy** (`prx.addBook(req)`, `prx.findByAuthor(query, observer_prx)`, ...) - to klasyczny styl Ice klienta, ale typy przyszly z `loadSlice` w runtime, nie z `slice2py` w build time. To jest analogiczne do gRPC `MessageFactory.GetMessageClass(descriptor)` ktory tez buduje klasy w runtime na podstawie deskryptora.

---

## Spis

1. [Layout projektu](#layout-projektu)
2. [Jak uruchomic](#jak-uruchomic)
3. [Glowne technologie](#glowne-technologie)
   1. [Slice IDL](#1-slice-idl)
   2. [Ice nad TCP](#2-ice-nad-tcp)
   3. [Dynamic Invocation w Ice](#3-dynamic-invocation-w-ice)
   4. [slice2java + sbt codegen po stronie serwera](#4-slice2java--sbt-codegen-po-stronie-serwera)
   5. [Ice.loadSlice po stronie klienta](#5-iceloadslice-runtime-po-stronie-klienta)
   6. [Streaming przez bidirectional callback](#6-streaming-przez-bidirectional-callback)
4. [Zycie wywolania end-to-end](#zycie-wywolania-end-to-end)
5. [IDL](#idl)
6. [Compliance vs tresc zadania](#compliance-vs-tresc-zadania)
7. [Q&A na obrone](#qa-na-obrone)

---

## Layout projektu

```
zad3/zadi1/
  server/                              -- Scala, sbt + slice2java
    build.sbt                          -- konfiguracja kompilacji + codegen task
    project/
      build.properties                 -- sbt.version
      plugins.sbt                      -- (puste)
    src/main/slice/catalog.ice          -- IDL (jedyne wspolne zrodlo prawdy)
    src/main/scala/CatalogServer.scala  -- bootstrap: communicator + adapter
    src/main/scala/CatalogImpl.scala    -- implementacja 4 operacji + walidacja
    target/scala-2.13/src_managed/main/slice-java/library/...
                                       -- (generated) Java z slice2java
                                          (case classy struktur, interface Catalog,
                                           BookStreamPrx, dispatcher)
  client/                              -- Python, dynamic
    catalog.ice                        -- KOPIA Slice (loadowana w runtime)
    main.py                            -- klient: loadSlice + checkedCast + menu
    tests.py                           -- 42 testy end-to-end (PASS)
    requirements.txt                   -- zeroc-ice
    .venv/                             -- (generated) zaleznosci
```

Pliki generowane (Java z `slice2java`, klasy `.class`, `.venv`) leza w innych katalogach niz kod zrodlowy. **Po stronie klienta nie ma ani jednego pliku wygenerowanego przez `slice2py` (`library_ice.py` itp.)** - klient ma tylko `main.py`, `tests.py`, oraz `catalog.ice` (sam plik IDL, **nie** stub) - sprawdzalne przez `find client -name "*_ice.py"` (nic).

---

## Jak uruchomic

### Wymagania systemowe (Linux Ubuntu 24.04)

```
# Slice compilers + Java runtime Ice (do serwera)
sudo apt-get install -y zeroc-ice-compilers libzeroc-ice3.7t64

# Naglowki do kompilacji zeroc-ice z PyPI po stronie klienta
sudo apt-get install -y libbz2-dev libssl-dev
```

### Serwer

```
cd zad3/zadi1/server
sbt run            # default port 10000; sbt 'run 10001' aby zmienic
```

Pierwsze uruchomienie pobiera zaleznosci (sbt sciaga `com.zeroc:ice:3.7.10` do `~/.cache/coursier`). Spodziewany log:
```
[catalog] server listening on tcp -h 0.0.0.0 -p 10000 identity='catalog'
[catalog] proxy:  catalog:tcp -h <host> -p 10000
```

### Klient interaktywny

```
cd zad3/zadi1/client
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python main.py                              # default catalog:tcp -h localhost -p 10000
.venv/bin/python main.py "catalog:tcp -h 192.168.1.10 -p 10000"
```

### Testy end-to-end (42 asercje)

```
cd zad3/zadi1/client && .venv/bin/python tests.py
```

Kazde uruchomienie uzywa unikalnego sufiksu opartego o timestamp, wiec testy sa **idempotentne** - dwa kolejne uruchomienia na tym samym serwerze daja `42 PASS, 0 FAIL`.

---

## Glowne technologie

Kazda sekcja: najpierw **co to jest ogolnie**, potem **co konkretnie robi w naszym projekcie**.

---

### 1. Slice IDL

#### Ogolnie

Slice (Specification Language for Ice) to IDL Ice'owy. Plik `.ice` opisuje:
- **moduly** (namespacing)
- **structy** (`struct`) - record types z polami
- **sequence<T>** - lista
- **dictionary<K,V>** - mapa
- **interface** - serwis z metodami
- **proxy** (`*`) - typed reference do interface'u (`SomeInterface*` w argumencie metody)
- **exception** - dziedziczace typy bledow
- **enum**

W przeciwienstwie do protobufa, Slice **nie jest self-describing na drucie**: imiona pol istnieja tylko w generowanym kodzie, w bajtach jest **strict, kolejnosciowy** layout (najpierw pole 1, potem pole 2, ...). Brak tagow w wire format - **wymagane jest aby strony zgadzaly sie co do schematu** (lub aby klient wiedzial co odczytac).

Encoding 1.1 (default w Ice 3.7) ma stabilny format:
- prymitywy: int = 4B little-endian, string = (size:varint) + UTF-8, bool = 1B, ...
- struct = pola w kolejnosci deklaracji, **bez** length-prefixu
- sequence = `(size:varint) + (T)*`
- dictionary = `(size:varint) + ((K,V))*`

Plik `.ice` da sie skompilowac **wieloma kompilatorami**: `slice2java`, `slice2cpp`, `slice2py`, `slice2cs`, ... - kazdy generuje typowane klasy w docelowym jezyku w osobnym wynikowym katalogu. **Mozna tez zaladowac w runtime** przez `Ice.loadSlice(path)` - IcePy parsuje sam Slice i buduje klasy bez `slice2py`.

#### U nas

[server/src/main/slice/catalog.ice](server/src/main/slice/catalog.ice) (kopia tez jako [client/catalog.ice](client/catalog.ice)):
- 9 typow danych (`Book`, `BookSeq`, `AuthorCounts`, `AddBookRequest`, `AuthorQuery`, `CatalogStats`, `AddBookResult`, `RemoveBookResult` + `StringSeq`)
- 2 interfejsy (`Catalog` z 4 operacjami, `BookStream` z 3 operacjami callbackowymi)
- nietrywialne struktury: `sequence<string>` (tags), `sequence<Book>` (recent), `dictionary<string,int>` (byAuthor)
- bledy zwracane **jako pola w response** (`errorCode`, `errorMessage`) zamiast Slice exception - prostsze + intuicyjniejsze, dziala zarowno z typed proxy jak i przy ewentualnym pure DII
- module `library` (nie `catalog`) - bo Slice zabrania, by interface i otaczajacy go module rozni sie tylko wielkoscia liter (`interface Catalog` w `module catalog` daje blad slice2java).

---

### 2. Ice nad TCP

#### Ogolnie

Ice to framework RPC z wlasnym protokolem warstwy aplikacyjnej **bezposrednio nad TCP** (lub UDP, SSL, WS). Nie korzysta z HTTP. Charakterystyczne cechy:

1. **Communicator** - centralny obiekt runtime: konfiguracja, thread pools, IO, locator. Cykl zycia aplikacji = 1 communicator.
2. **Object adapter** - serwerowy "endpoint" przyjmujacy polaczenia. `createObjectAdapterWithEndpoints(name, "tcp -h 0.0.0.0 -p 10000")` startuje listenera na danym porcie.
3. **Identity** - logiczny identyfikator obiektu na adapterze: `(name, category)` (najczesciej tylko `name`). Klienci adresują przez identity, nie przez "ścieżkę" jak w gRPC.
4. **Servant** - implementacja interface'u. Adapter przypina servant pod identity: `adapter.add(servant, identity)` -> dostajesz `ObjectPrx`.
5. **Proxy** - po stronie klienta to wskaznik do zdalnego obiektu. Składa się z: identity, endpoints, mode (twoway/oneway), facet, encoding version.
6. **Operation modes**: `Normal` (twoway = req+resp), `Idempotent`, `Nonmutating` (deprecated).

Format request frame Ice 1.1 (uproszczony):
```
[magic 'IceP'][protocol-version 1.0][encoding-version 1.1][type=Request][flags][message-size]
[request-id:int]
[identity (name+category)]
[facet sequence]
[operation:string]
[mode:byte]
[context (dict<string,string>)]
[encapsulation: (size:int)(major:byte)(minor:byte)(parameters bytes)]
```

Response:
```
[Ice header...][type=Reply][message-size]
[request-id:int]
[reply-status:byte]    -- 0=ok, 1=user-exception, 2=ObjectNotExist, ...
[encapsulation: (out parameters lub exception bytes)]
```

#### U nas

- **Server** [CatalogServer.scala](server/src/main/scala/CatalogServer.scala): `Util.initialize(args)` -> `Communicator`, potem `createObjectAdapterWithEndpoints("CatalogAdapter", "tcp -h 0.0.0.0 -p 10000")`, `adapter.add(new CatalogImpl(), Util.stringToIdentity("catalog"))`, `adapter.activate()`, `communicator.waitForShutdown()`.
- **Klient** [main.py](client/main.py): `Ice.initialize(sys.argv)` -> `Communicator`, `communicator.stringToProxy("catalog:tcp -h localhost -p 10000")` zwraca `Ice.ObjectPrx` (untyped). Po `Ice.loadSlice` mamy `library.CatalogPrx.checkedCast(base)` (typed).
- **Identity wlasnego callbacku**: klient tworzy lokalny `ObjectAdapter` na losowym porcie (`tcp -h 127.0.0.1`), dodaje servant pod unikalna identity (`stream-<uuid4>`) - serwer dostaje tym proxy w `findByAuthor` i pcha tam `onNext`/`onCompleted` callbacki.

---

### 3. Dynamic Invocation w Ice

#### Ogolnie - dwa "swiaty" DII w Ice

[Dynamic Invocation and Dispatch (doc.zeroc.com)](https://doc.zeroc.com/ice/3.7/client-server-features/dynamic-ice/dynamic-invocation-and-dispatch) opisuje dwa schematy:

1. **`ice_invoke` + manual marshalling** przez `Ice.OutputStream` / `Ice.InputStream`. Klient sam sklada bajty in encapsulation, woła `prx.ice_invoke(op, mode, in_bytes)`, czyta out_bytes. **Dostepne w C++/Java/C#.** **NIE jest dostepne w Python.**

2. **Slice loaded at runtime** przez `Ice.loadSlice(file)`. IcePy w runtime parsuje plik `.ice` i poprzez wewnetrzne `defineStruct`/`defineSequence`/`defineProxy` buduje typed klasy bez `slice2py`. **Dostepne we wszystkich jezykach**, ale w Pythonie jest **jedyna realna sciezka** dla "dynamic invocation".

Wynika to z [Streaming Interfaces (doc.zeroc.com)](https://doc.zeroc.com/ice/3.7/client-server-features/dynamic-ice/streaming-interfaces): _"The streaming API is not available in the Python language mapping."_ Brief celowo linkuje wlasnie te strone, zeby na to zwrocic uwage.

Po stronie serwera analog DII to `Ice.Blobject` / `Ice.BlobjectAsync` - generic servant przyjmujacy raw bajty - **w Pythonie dostepne** (asymetria!).

#### U nas

Klient korzysta ze schematu **#2 (loadSlice)**. Konkretnie:

```python
# main.py linie 12-14
SLICE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "catalog.ice")
Ice.loadSlice(f"-I. -I{os.path.dirname(SLICE_FILE)} {SLICE_FILE}")
import library
```

Co robi `Ice.loadSlice`:
1. Wywoluje wewnetrzny mcpp preprocesor (Ice ma wbudowany mcpp jako static lib) na pliku `.ice` z opcjami `-I` jako include paths.
2. Parsuje wyjscie mcpp na AST Slice.
3. Iteruje po deklaracjach AST i woła `IcePy.defineStruct`, `IcePy.defineSequence`, `IcePy.defineDictionary`, `IcePy.defineProxy` etc. dla kazdej.
4. Tworzy modul Pythona `library` z tymi klasami (tu trafia `library.Book`, `library.CatalogPrx`, `library.BookStream`, ...).

Roznica vs `slice2py`:
- `slice2py` w build time generuje plik `library_ice.py` ktory uzywa tych samych `IcePy.defineStruct` API w sposob hardcoded - **wynik to plik na dysku, klasy ZIPowane w git**.
- `Ice.loadSlice` robi to samo na bajt-poziomie API ale **w runtime, klasy istnieja tylko w pamieci**, plik `library_ice.py` nie istnieje.

To jest dokladnie analogiczne do gRPC `MessageFactory.GetMessageClass(descriptor)` ktore w runtime buduje klasy Pythona z `FileDescriptorProto` (zamiast z `_pb2.py`).

**Brief warunek "klient bez stubow IDL bedacych wynikiem kompilacji IDL"**: spelniony, bo `slice2py` nigdy nie byl odpalany. Plik `catalog.ice` **NIE jest stub'em** - to plik IDL (analog `.proto`).

---

### 4. slice2java + sbt codegen (po stronie serwera)

Server w odroznieniu od klienta uzywa **build-time codegenu** - to typowy Ice setup dla "produkcyjnego" serwera. Mozna tez zaladowac Slice runtime w Scali, ale wprowadzaloby to overhead i lekko nietypowy pattern.

#### Ogolnie

`slice2java` to kompilator Slice -> Java pluginowany do zestawu narzedzi Ice. Generuje:
- klase Java per `struct` (publiczne pola, default constructor, full-arg constructor, `ice_read` i `ice_write` dla (de)serializacji)
- interfejs Java per `interface` - **co user implementuje**
- klase `Prx` per interface (proxy)
- helpery dla `sequence`/`dictionary` (`BookSeqHelper`, `AuthorCountsHelper`)
- dispatcher table (`_iceDispatch`) - mapuje string operacji na metody implementacji

#### U nas

[server/build.sbt](server/build.sbt) - hook do codegen:
```scala
Compile / sourceGenerators += Def.task {
  val sliceDir = (Compile / sourceDirectory).value / "slice"
  val outDir   = (Compile / sourceManaged).value / "slice-java"
  val cache    = streams.value.cacheDirectory / "slice"
  val sliceFiles = (sliceDir ** "*.ice").get.toSet
  val cached = FileFunction.cached(cache, FilesInfo.lastModified, FilesInfo.exists) { _ =>
    IO.delete(outDir); IO.createDirectory(outDir)
    sliceFiles.foreach { f => Process(Seq("slice2java", "--output-dir", outDir.getAbsolutePath, f.getAbsolutePath)).! }
    (outDir ** "*.java").get.toSet
  }
  cached(sliceFiles).toSeq
}.taskValue
```

`FileFunction.cached` chroni przed niepotrzebnym re-runem (tylko gdy `.ice` ma nowsza mtime niz cache). Wynik trafia do `target/scala-2.13/src_managed/main/slice-java/library/`. Lista wygenerowanych:
- `Book.java`, `AddBookRequest.java`, `AuthorQuery.java`, `CatalogStats.java`, `AddBookResult.java`, `RemoveBookResult.java` - structy
- `BookSeqHelper.java`, `AuthorCountsHelper.java` - helpery do (de)serializacji kolekcji
- `Catalog.java`, `BookStream.java` - interfejsy (servant base)
- `CatalogPrx.java`, `BookStreamPrx.java` - proxies
- `_CatalogPrxI.java`, `_BookStreamPrxI.java` - implementacje proxy

[CatalogImpl.scala](server/src/main/scala/CatalogImpl.scala) ekstenduje wygenerowany `library.Catalog` (Java interface) z poziomu Scali - dziala bo JVM-bytecode jest wspolny. Tylko musi zwracac Java arrays / Java Maps tam gdzie Slice tak deklaruje (`String[]`, `java.util.Map<String, Integer>`).

---

### 5. Ice.loadSlice (runtime po stronie klienta)

#### Ogolnie

[`Ice.loadSlice(args)`](https://doc.zeroc.com/ice/3.7/python/dynamic-ice-in-python) bierze plik `.ice` i tworzy z niego klasy Pythona w runtime. Argumenty (jako string lub lista):
- `-I<dir>` - include path (jak w `protoc -I`)
- `-D<sym>` - define preprocessor symbol
- `-U<sym>` - undefine
- `--all` - include built-in slice files
- ... + sciezki do plikow `.ice`

Po wywolaniu wynik jest dostepny przez `import <module>` (modul nazwany jak `module` w pliku Slice).

Pod maska:
1. mcpp preprocesor wywolywany na pliku (handles `#include` if any).
2. Parser Slice -> AST.
3. AST traversal -> wywolania `IcePy.defineStruct(name, fields, types)`, `IcePy.defineSequence(...)`, `IcePy.defineProxy(...)`, ...
4. Powstale klasy podpinane do nowo utworzonego modulu Pythona.

To **dokladnie te same wywolania** jakie generowalby `slice2py` w pliku `_ice.py`. Roznica jest tylko w punkcie czasowym (runtime vs build time).

#### U nas

[main.py:12-14](client/main.py#L12-L14):
```python
SLICE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "catalog.ice")
Ice.loadSlice(f"-I. -I{os.path.dirname(SLICE_FILE)} {SLICE_FILE}")
import library
```

Po tym mamy:
- `library.Book` - klasa z polami `id, title, author, year, tags`
- `library.AddBookRequest`, `library.AuthorQuery`, `library.CatalogStats`, `library.AddBookResult`, `library.RemoveBookResult` - inne struct'y
- `library.Catalog` - bazowa klasa interfejsu (servant base)
- `library.CatalogPrx` - klasa typed proxy z metodami `addBook`, `findByAuthor`, `summary`, `removeBook`
- `library.BookStream` - bazowa klasa callbacku (servant base)
- `library.BookStreamPrx` - typed proxy do callbacku

Caly kod wywolan ([main.py](client/main.py)) korzysta z **typed proxy**:
```python
prx = library.CatalogPrx.checkedCast(base)
res = prx.addBook(library.AddBookRequest(title, author, year, tags))   # wymaga "Catalog" type ID
prx.findByAuthor(library.AuthorQuery(author, limit), cb_prx)             # 1-shot, void
stats = prx.summary()
res = prx.removeBook(book_id)
```

`checkedCast` po stronie klienta wola `prx.ice_isA("::library::Catalog")` na serwerze - to gwarantuje, ze server udostepnia ten interfejs (analog `instanceof` rozproszony po sieci).

---

### 6. Streaming przez bidirectional callback

#### Ogolnie

Ice **nie ma** odpowiednika gRPC `stream Resp` w sygnaturze operacji. Operacja Ice to zawsze 1 request, 1 response. Standardowy wzorzec na "streaming" to **dwukierunkowy callback**:

1. IDL definiuje **dwa interfejsy** - serwisowy (np. `Catalog`) i obserwatorowy (`BookStream`).
2. Klient implementuje `BookStream` lokalnie (lokalny servant na lokalnym ObjectAdapter).
3. Klient w wywolaniu serwisowym przekazuje **proxy do swojego BookStream**.
4. Serwer **iteruje po wynikach** i wola `observer.onNext(book)`, `observer.onCompleted()` (lub `observer.onError(...)`) - to sa zwykle Ice operacje, ale **w przeciwna strone** niz oryginalne wywolanie.

Wywolanie `findByAuthor` jest twoway - serwer nie zwroci, dopoki `onCompleted()` callback nie odpowie. Wiec po jego zwroceniu klient ma gwarancje, ze wszystkie callbacki zostaly **dostarczone i przetworzone**.

Plus: Ice ma "bidirectional connections" - servery mogą wywolywać callback przez **to samo** TCP, ktorym klient sie polaczyl, bez nowego connect (potrzebne np. za NATem). My nie uzywamy tej optymalizacji, klient wystawia osobny adapter na 127.0.0.1.

#### U nas

**Servant `BookStream` u klienta** [main.py:18-29](client/main.py#L18-L29):
```python
class BookStreamI(library.BookStream):
    def __init__(self):
        self.q = queue.Queue()
    def onNext(self, book, current=None):
        self.q.put(("next", book))
    def onCompleted(self, current=None):
        self.q.put(("done", None))
    def onError(self, code, message, current=None):
        self.q.put(("error", (code, message)))
```

`library.BookStream` to bazowa klasa wygenerowana przez `Ice.loadSlice`. Subclassing daje typed servant - Ice wie jak (de)serializowac argumenty (book, code, message) bo mu o tym powiedzielismy w `.ice`.

**Wywolanie i drain queue** [main.py:32-54](client/main.py#L32-L54):
```python
def call_find_by_author(communicator, adapter, prx, author, limit, timeout=10.0):
    servant = BookStreamI()
    cb_id = Ice.Identity(name="stream-" + uuid.uuid4().hex, category="")
    cb_prx = library.BookStreamPrx.uncheckedCast(adapter.add(servant, cb_id))
    try:
        prx.findByAuthor(library.AuthorQuery(author, limit), cb_prx)
        # do tego momentu wszystkie callbacki dostarczone i przetworzone
        results = []
        while True:
            kind, val = servant.q.get(timeout=...)
            if kind == "next": results.append(val)
            elif kind == "done": return results
            elif kind == "error":
                code, msg = val
                raise RuntimeError(f"findByAuthor stream error: {code}: {msg}")
    finally:
        adapter.remove(cb_id)
```

`adapter.add(servant, identity)` zwraca `Ice.ObjectPrx`, a `library.BookStreamPrx.uncheckedCast(...)` rzutuje na typed proxy do `BookStream`. Ice serializuje to proxy gdy przekazujemy je w argumencie do `findByAuthor`. Serwer otrzymuje `BookStreamPrx` (Java side) i woła na nim onNext/onCompleted - to leci po sieci, lapie nasz `BookStreamI` po stronie klienta.

---

## Zycie wywolania end-to-end

### Bootstrap (jednorazowe, przy `main()`)

```
[main.py:12-14]
   Ice.loadSlice("-I. -I/path/to/client/ catalog.ice")
   -> mcpp preprocesor + Slice parser + IcePy.defineStruct/defineSequence/defineProxy/...
   -> modul `library` zawiera klasy: Book, AddBookRequest, ..., CatalogPrx, BookStream, BookStreamPrx
   import library
   -> teraz mamy typed proxy dostepny

[main.py:151-156]
   communicator = Ice.initialize(sys.argv)
   base = communicator.stringToProxy("catalog:tcp -h localhost -p 10000")
   base.ice_ping()      # sprawdzenie ze serwer zyje (oraz ze "catalog" identity istnieje)
   base.ice_ids()       # ["::Ice::Object", "::library::Catalog"] - jedyna forma "discovery" w Ice 3.7

[main.py:166-167]
   prx = library.CatalogPrx.checkedCast(base)
   -> klient wola prx.ice_isA("::library::Catalog") na serwerze, dostaje True,
      konstruuje typed proxy CatalogPrx z istniejacych endpoints/identity

[main.py:170-171]
   adapter = communicator.createObjectAdapterWithEndpoints("CallbackAdapter", "tcp -h 127.0.0.1")
   adapter.activate()
   -> klient otwiera lokalny port (przydzielany dynamicznie) na callbacki
```

### Pojedyncze wywolanie unary AddBook

```
[main.py] req = library.AddBookRequest(title="Dune", author="Herbert", year=1965, tags=["sf"])
[main.py] res = prx.addBook(req)
   -> Ice runtime: serializacja AddBookRequest do encapsulation (dispatcher zna pola z loadSlice'a)
   -> Send Request frame: [Ice header][reqId][identity="catalog"][operation="addBook"][mode=Normal][context={}][encapsulation(in)]
   -> serwer odbiera, dispatcher znajduje "addBook" w binarnym indeksie,
      _iceD_addBook czyta AddBookRequest z encapsulation, wola CatalogImpl.addBook,
      serializuje AddBookResult jako encapsulation, odsyla Reply frame
      [reqId][replyStatus=0=OK][encapsulation(out)]
   -> Ice runtime u klienta deserializuje out -> library.AddBookResult instance

[serwer]  CatalogImpl.addBook:
   walidacja -> ewentualnie return AddBookResult(0, "INVALID_ARGUMENT", "...")
   if duplicate -> return AddBookResult(0, "ALREADY_EXISTS", "...")
   store.put(id, Book(...))
   println(...)  # log na konsole
   return AddBookResult(id, "", "")

[main.py] res to library.AddBookResult, czytamy res.bookId / res.errorCode / res.errorMessage
```

### Pojedyncze wywolanie z callbackiem FindByAuthor

```
[main.py] adapter = istniejacy CallbackAdapter
   cb_id = Identity(name="stream-<uuid4>")
   cb_prx = library.BookStreamPrx.uncheckedCast(adapter.add(BookStreamI(), cb_id))

[main.py] prx.findByAuthor(library.AuthorQuery(author, limit), cb_prx)
   -> Ice serializuje (AuthorQuery + cb_prx jako proxy) do encapsulation, wysyla
   -> serwer odbiera, rozpakowuje
[serwer]  CatalogImpl.findByAuthor(query, observer, current):
   if invalid -> observer.onError("INVALID_ARGUMENT", "..."); return
   for each match:
     observer.onNext(book)     # twoway! - serwer wysyla request DO klienta i CZEKA
                                # request to: [Ice header][identity="stream-..."][op="onNext"][encaps(book)]
                                # klient odbiera, BookStreamI.onNext(book) wywolane na adapter thread,
                                # kolejkuje, odsyla reply z empty encaps
   observer.onCompleted()      # to samo, ale sygnal koncowy
   return -> serwer ma onCompleted() ack, zwraca empty reply do klienta

[main.py] po zwrocie z findByAuthor wszystkie callbacki dostarczone, drain queue:
   while True:
     kind, val = q.get(timeout)
     if kind=="next": results.append(val)
     elif kind=="done": return results
     elif kind=="error": raise
```

---

## IDL

[server/src/main/slice/catalog.ice](server/src/main/slice/catalog.ice) (ten sam plik jako [client/catalog.ice](client/catalog.ice)):

```slice
module library
{
    sequence<string> StringSeq;

    struct Book          { int id; string title; string author; int year; StringSeq tags; };

    sequence<Book> BookSeq;
    dictionary<string, int> AuthorCounts;

    struct AddBookRequest{ string title; string author; int year; StringSeq tags; };
    struct AuthorQuery   { string author; int limit; };
    struct CatalogStats  { int total; AuthorCounts byAuthor; BookSeq recent; };
    struct AddBookResult    { int  bookId; string errorCode; string errorMessage; };
    struct RemoveBookResult { bool ok;     string errorCode; string errorMessage; };

    interface BookStream {
        void onNext(Book book);
        void onCompleted();
        void onError(string code, string message);
    };

    interface Catalog {
        AddBookResult    addBook(AddBookRequest request);
        void             findByAuthor(AuthorQuery query, BookStream* observer);
        CatalogStats     summary();
        RemoveBookResult removeBook(int id);
    };
};
```

| Operacja | input | output | rodzaj | bledy |
|-----|-------|--------|--------|-------|
| addBook | AddBookRequest | AddBookResult | unary | `INVALID_ARGUMENT` (puste title/author, year<0), `ALREADY_EXISTS` (duplikat case-insensitive) |
| findByAuthor | AuthorQuery + BookStream* | void | callback-stream | `INVALID_ARGUMENT` -> `BookStream.onError(code, msg)` |
| summary | (void) | CatalogStats | unary | - |
| removeBook | int | RemoveBookResult | unary | `NOT_FOUND` |

`AuthorQuery.limit = 0` traktowane jako "no limit". Bledy semantyczne sa zwracane **w response Result types**, nie jako Slice exception.

Wybor `module library` zamiast `module catalog`: Slice zabrania, by interface i otaczajacy module rozni sie tylko wielkoscia liter.

---

## Compliance vs tresc zadania

> "klient ma nie miec dolaczonych zadnych klas/bibliotek stub bedacych wynikiem kompilacji IDL"

- [x] Klient nie zawiera **ani jednego** pliku wygenerowanego przez `slice2py` - sprawdzalne `find client -name "*_ice.py"` (nic).
- [x] Plik `client/catalog.ice` to **plik IDL** (analog `.proto`), nie **stub** - nie jest wynikiem kompilacji, tylko zrodlem.
- [x] Klasy `library.AddBookRequest`, `library.CatalogPrx` etc. powstaja **w runtime** przez `Ice.loadSlice` -> wewnetrzne `IcePy.defineStruct` API. Nigdy nie istnieja jako pliki na dysku.
- [x] To jest analogiczne do gRPC `MessageFactory.GetMessageClass(descriptor)` (zada2) - tam tez klasy buduja sie w runtime przez metaklase, ale z `FileDescriptorProto` zamiast z Slice file.

> "kilka (co najmniej trzech) roznych operacji"

- [x] 4 operacje: `addBook`, `findByAuthor`, `summary`, `removeBook`.

> "uzywajacych przynajmniej w jednym przypadku nietrywialnych struktur danych (listy, struktury)"

- [x] `AddBookRequest.tags: sequence<string>` - lista
- [x] `CatalogStats.recent: sequence<Book>` - lista struktur
- [x] `CatalogStats.byAuthor: dictionary<string,int>` - mapa
- [x] `Book` zagniezdzony w `CatalogStats.recent` - struktura w strukturze

> "i sposobu komunikacji (gRPC: wywolanie strumieniowe)"

Dla Ice odpowiednikiem jest **bidirectional callback pattern** opisany w [Streaming Interfaces (doc.zeroc.com)](https://doc.zeroc.com/ice/3.7/client-server-features/dynamic-ice/streaming-interfaces).

- [x] `findByAuthor` przekazuje `BookStream*` (proxy do callbacku klienta), serwer woła `onNext(book)` per kazdy wynik.
- [x] Klient implementuje callback przez subclass `library.BookStream` (typed servant) z kolejkowaniem przez `queue.Queue` (lazy iteracja po draine).

> "wystarczy zawrzec to wywolanie 'na sztywno' w kodzie zrodlowym, co najwyzej z konsoli parametryzujac szczegoly danych"

- [x] Operacje wpisane na sztywno (`addBook`, `findByAuthor`, `summary`, `removeBook`). Parametry czytane z konsoli przez `input()`.

> "Trzeba przemyslec i umiec przedyskutowac przydatnosc takiego podejscia w budowie aplikacji rozproszonych"

Zob. [Q&A](#qa-na-obrone) ponizej.

> "Technologia middleware: Ice albo gRPC"

- [x] Ice 3.7.10 (server, slice2java) + 3.7.11 (klient, zeroc-ice z PyPI).

> "Jezyki programowania: dwa rozne"

- [x] Klient: Python 3.12. Serwer: Scala 2.13 (na JVM, korzysta z Java mappingu Ice).

---

## Q&A na obrone

### Co to jest "wywolanie dynamiczne" w naszym kontekscie?

Wywolanie zdalnej procedury, gdzie klient **nie zna kontraktu (Slice IDL) w czasie kompilacji**. Klient zna tylko jak dziala protokol Ice i ma plik IDL ktory dynamicznie laduje przez `Ice.loadSlice` - typy budowane w runtime, bez `slice2py`. Statyczny odpowiednik: klient ma `library_ice.py` skompilowane z `catalog.ice`, kompilator/IDE zna typy, blad wykrywany przy build time. Tu nic z tego nie ma.

### Czemu `Ice.loadSlice` to "dynamic", skoro wymaga pliku `.ice`?

Bo "klucz" w briefie to **nieobecnosc skompilowanych stubow**, nie nieobecnosc samego IDL. Plik `.ice` to **specyfikacja** (rownorzedna z `.proto` w gRPC). Kompilacja IDL produkuje **stuby** (`*_ice.py`, `*_pb2.py`). Tu kompilacji nigdy nie bylo - klasy buduja sie w runtime, nie sa zapisane na dysku.

To jest dokladnie sytuacja analogiczna do **gRPC reflection version** ([zad3/zada2](../zada2/)): tam klient ma `descriptor_pb2.py` (meta-protokol gRPC), nie ma `catalog_pb2.py` (stuby aplikacji), klasy aplikacyjne (`Book`, `BookId`) buduja sie w runtime z `FileDescriptorProto` przez `MessageFactory.GetMessageClass`. **Plik `.proto` u nas tez fizycznie istnieje** - na serwerze, ale **klient go nie kompiluje, tylko pobiera deskryptor przez reflection**.

W naszym Ice case server tez ma `.ice` (po stronie serwera kompilowany przez `slice2java`), klient tez ma kopie `.ice` ale **w runtime tylko ladowana, nie kompilowana**. Roznica vs gRPC reflection: w gRPC server udostepnia deskryptor przez specjalny RPC, a w Ice 3.7 nie ma takiego natywnego RPC -> dystrybuujemy plik out-of-band (najprostsze: tym samym repo).

### Czemu w Pythonie nie uzywasz `ice_invoke` z manual marshalling?

Bo IcePy (zeroc-ice z PyPI) **nie eksponuje** `Ice.OutputStream` / `Ice.InputStream` - to celowa decyzja ZeroC, [udokumentowana w streaming-interfaces (doc.zeroc.com)](https://doc.zeroc.com/ice/3.7/client-server-features/dynamic-ice/streaming-interfaces): _"The streaming API is not available in the Python language mapping."_ Brief sam linkuje wlasnie te strone, zeby zwrocic uwage na ograniczenia.

Konsekwencja: w Pythonie pure-DII z manual marshalling (jak w C++/Java/C#) jest niemozliwe - musisz uzyc `Ice.loadSlice` jako jedyna realna sciezka. Ja zrobilbym manual marshalling jakbym pisal klienta w Javie/C++.

### Czemu `findByAuthor` zwraca void zamiast `stream Book` jak w gRPC?

Ice **nie ma** server-streaming w sygnaturze operacji - kazda operacja to 1 req / 1 resp. Streaming w Ice realizowany jest przez **dwukierunkowy callback**: klient przekazuje proxy do swojego `BookStream*`, serwer wola `onNext`, `onCompleted` na nim. To jest **standardowy** wzorzec opisany w doc.zeroc.com (sekcja "Streaming Interfaces").

### Dlaczego bledy zwracane jako pola w response, nie jako Slice exception?

Z dwoch powodow:
1. **Konsekwencja dla potencjalnego pure DII** (np. gdyby ktos chcial zaadaptowac kod dla klienta C++/Java w trybie manual marshalling): decoding Ice user exception bez wkompilowanej Slice wymaga parsowania slice flags + type ID + slices per kazdy slice w lancuchu dziedziczenia. Result types eliminują ten problem - klient czyta string + string i wie co sie stalo.
2. **Czytelnosc**: kod typu `if res.errorCode == "NOT_FOUND":` jest jasny.

### Co zastapiloby `proto reflection` w Ice 3.7?

Ice 3.7 nie ma odpowiednika `grpc.reflection.v1alpha`. Najblizsze:
- `prx.ice_ids()` - lista type ID jakie proxy obsluguje (np. `["::Ice::Object", "::library::Catalog"]`). Ale NIE listuje operacji ani sygnatur.
- `prx.ice_isA("::library::Catalog")` - bool, czy proxy obsluguje dany interfejs.
- W Ice 3.8 wprowadzane jest pelne reflection API. W 3.7 - albo mamy Slice file dystrybuowany osobno (klient ladowuje przez `Ice.loadSlice(path)`), albo siegamy po IceGrid Admin Facets.

W naszym kodzie: dystrybuujemy `.ice` osobno (kopia w `client/catalog.ice`), klient laduje runtime.

### Wady wywolania dynamicznego (Ice)

- brak type safety przy build time - blad w `library.AddBookRequest(title=..., author=...)` (np. literowka w polu) wywala dopiero w runtime
- brak wsparcia IDE dla typed proxy methods (pylint/mypy nie widzi `library.CatalogPrx`)
- Slice nie jest self-describing wiec klient nie ma jak pominac nieznane pole - **musi zgadzac sie co do schematu**
- klient i serwer musza miec **identyczne** Slice files albo wpasowane w kompatybilna podsec ich
- `loadSlice` przy starcie ma overhead (parsowanie + budowa klas)
- dla Pythona w 3.7 brakuje pure manual marshalling (OutputStream)
- "blast radius" - refaktor pol nie wybucha kompilacja u klienta

### Kiedy uzywac

Narzedzia ops/dev (analog grpcurl, ale dla Ice nie ma oficjalnego ekwiwalentu - mozna sobie zbudowac przez `loadSlice` + reflection-like via `ice_ids`), API gateways, generic clients pokrywajace wiele serwisow, frameworki testowe wywolujace "wszystkie metody". **Nie** dla zwyklej aplikacyjnej komunikacji - straty produktywnosci wieksze niz zyski.

### Jak to sie ma do gRPC dynamic invocation z `zada2`?

| | gRPC reflection (zada2) | Ice loadSlice (zadi1) |
|---|---|---|
| Skad klient ma schemat | `ServerReflection.GetFileDescriptorProto` per pytanie | plik `.ice` dystrybuowany osobno (out-of-band) |
| API u klienta | `channel.unary_unary(path, ser, deser)` z `MessageFactory.GetMessageClass` | `library.CatalogPrx.checkedCast(prx).addBook(req)` (typed) z `Ice.loadSlice` |
| Stuby u klienta | brak (build w runtime przez metaklase z FileDescriptorProto) | brak (build w runtime przez IcePy.defineStruct z parsowanego .ice) |
| Wire format | self-describing (tag + wire type) - mozna pominac nieznane pole | strict layout (kolejnosc+typy musi sie zgadzac) |
| Discovery operacji | TAK - reflection zwraca pelen ServiceDescriptor z metodami | NIE - klient zna z out-of-band |
| Streaming | natywne `unary_stream` w sygnaturze | brak natywnego, bidirectional callback przez 2 interfejsy |
| Bledy | gRPC Status (codes) | Result types z polami errorCode/errorMessage |

Wnioski:
- **gRPC ma latwiejszy dynamic invocation** dzieki self-describing format + reflection
- **Ice DII jest blizej "metalu"** - wymaga rozproszenia `.ice` i ma mniej introspekcji
- **Oba spelniaja brief** "klient bez stubow IDL", ale w Ice trzeba wiecej setup'u

### Backward/forward compatibility?

Ice (encoding 1.1) ma mechanism **slicing classes** dla klas (nie struct), ale dla naszych prostych structow zmiana kolejnosci pola = lamie kompatybilnosc bezwarunkowo. Dla porownania: protobuf z tagami toleruje zmiany dodajace pola.

### Bezpieczenstwo introspekcji w produkcji?

`ice_ids` ujawnia tylko typ interfejsu (`::library::Catalog`), nie operacje ani struktury. To jest **mniej** info-leak niz gRPC reflection (ktore ujawnia caly schemat). Wiec na publicznych endpointach Ice ma latwiej.

### TOCTOU w sprawdzaniu duplikatow?

W [`addBook`](server/src/main/scala/CatalogImpl.scala) sekwencja `store.values.exists(...) -> store.put(...)` nie jest atomowa. Przy bardzo szybkich rownoczesnych Add tej samej ksiazki mozliwy duplikat. Dla demo niegroźne; produkcyjnie `synchronized` na sekcji "check + put", albo struktura z deterministycznym kluczem (title+author lower) i `putIfAbsent`.

### Co sie stanie po `Ctrl+C` w kliencie / serwerze?

Klient: `input()` rzuca `KeyboardInterrupt`, lapane w menu, czysty exit przez `communicator.destroy()`. Serwer: `Runtime.addShutdownHook` wola `communicator.shutdown()` -> graceful stop adaptera, in-flight RPC dokonczone.

### Co jesli klient wyceluje w zly endpoint / serwer bez tej identity?

Zlapane jako `Ice.LocalException`:
- wrong port -> `Ice.ConnectionRefusedException`
- istniejacy serwer ale brak identity "catalog" -> `Ice.ObjectNotExistException` (replyStatus=2 w protokole)
- nieistniejacy host -> `Ice.DNSException` lub `Ice.ConnectFailedException`
- wrong endpoint syntax -> `Ice.EndpointParseException` przy `stringToProxy`

### Najszybsza sciezka demo

1. `tree zad3/zadi1` - pokazac `client/main.py` + `client/tests.py` + `client/catalog.ice` - **zadnego `library/` ani `*_ice.py`** w `client/`
2. `cd server && sbt run` (port 10000)
3. Drugi terminal: `cd client && .venv/bin/python tests.py` -> 42/42 PASS
4. `.venv/bin/python main.py`, opcje 1-4 demo: addBook, findByAuthor (zwrocic uwage na bidirectional callback), summary, removeBook
5. Pokazac w `client/main.py`:
   - linie 12-15 (`Ice.loadSlice` + `import library`) - **runtime IDL loading**
   - linie 18-29 (`BookStreamI(library.BookStream)`) - typed servant subclass
   - linie 32-54 (`call_find_by_author`) - przekazanie cb_prx do serwera + drain queue
6. Podkreslic: nie ma `slice2py` w pipeline, klasy `library.*` istnieja **tylko w pamieci** po `Ice.loadSlice`.

### Czemu `module library` zamiast `module catalog`?

Slice zabrania, by interface i otaczajacy go module rozni sie tylko wielkoscia liter:
```
$ slice2java catalog.ice
catalog.ice:60: interface name `Catalog' cannot differ only in capitalization from its immediately enclosing module name `catalog'
```

Stad `module library`. Logicznie pasuje (system biblioteczny).

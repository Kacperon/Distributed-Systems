# Notatka do nauki - zadi1 (Wywolanie dynamiczne przez Ice)

Klient Python wywoluje serwer Scala bez wkompilowanych stubow Slice
(zero `slice2py`-output w git, zero `library_ice.py`, zero pakietu
`library/` na dysku). Typy buduja sie **w runtime** przez
`Ice.loadSlice("catalog.ice")` - IcePy parsuje plik IDL i poprzez
wewnetrzne `IcePy.defineStruct`/`defineSequence`/`defineProxy`
tworzy klasy Pythona w pamieci.

Glowne tematy: Slice IDL, encoding 1.1 wire format, **dwa schematy DII
w Ice (manual marshalling vs loadSlice) i dlaczego w Pythonie tylko
loadSlice dziala**, bidirectional callback jako "streaming" w Ice 3.7,
roznice vs gRPC reflection.

## 1. Slice IDL - co to jest, czym sie rozni od protobufa

### 1.1 Czym jest Slice

Slice to **specification language** Ice'a - skladnia podobna do C++/Java,
opisuje:
- moduly (namespacing)
- structy (record types)
- sequence<T> (lista)
- dictionary<K,V> (mapa)
- interface (serwis z metodami)
- proxy (`*` po interfejsie - reference do innego interfejsu)
- exception (z dziedziczeniem)
- enum, const, classes (specjalna kategoria z dziedziczeniem)

Plik `.ice` mozna skompilowac wieloma plug-inami:
`slice2java`, `slice2cpp`, `slice2py`, `slice2cs`, `slice2js`, ...

Albo zaladowac w runtime przez `Ice.loadSlice(path)` - ten sam
mechanism, tylko bez generowania pliku na dysku.

### 1.2 KLUCZOWA ROZNICA z protobufem: Slice NIE jest self-describing

Protobuf wire format ma **tagi pol w bajtach**: `[tag<<3 | wire_type] [value]`.
Wiec dynamic deser wie, ze pole #5 ma wire type 2 (length-delimited) i moze
go pominac jesli nie zna typu.

Slice encoding 1.1 ma **strict, kolejnosciowy layout**: pola w bajtach w
**dokladnie tej samej kolejnosci** co w `.ice`, **bez** tagow, **bez**
length-prefixu na strukturze, tylko surowe wartosci.

Konsekwencje:
- klient **musi znac schemat** (kolejnosc, typy pol) zeby cokolwiek odczytac
- nie da sie pominac nieznanego pola - bo nie ma jak rozpoznac gdzie sie
  konczy
- forward/backward compatibility nie ma takiego naturalnego wsparcia

To wlasnie dlatego DII w Ice w 3.7 wymaga aby klient mial Slice (np.
przez `loadSlice`) - bez tego nie wie nawet jak deserializowac response.

### 1.3 Encoding 1.1 - wybrany wire format

```
prymitywy:
  bool      - 1B
  byte      - 1B
  short     - 2B little-endian
  int       - 4B little-endian
  long      - 8B little-endian
  float     - 4B IEEE 754 LE
  double    - 8B IEEE 754 LE
  string    - (size:varint)(UTF-8 bytes)

kompozyty:
  struct       - pola w kolejnosci deklaracji, bez prefixu
  sequence<T>  - (size:varint)(T)*
  dictionary   - (size:varint)((K,V))*
  proxy        - identity + endpoints + facet + mode + protocol/encoding wersje

encapsulation:
  (encaps_size:int)(major:byte)(minor:byte)(parameters bytes)
```

`size:varint` (Ice nazywa "Size") to: < 255 -> 1 bajt; >= 255 -> 5 bajtow
(255 + 4 bajty int32 LE).

### 1.4 IDL u nas (catalog.ice)

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

Cechy:
- 4 operacje (3 unary + 1 z BookStream callbackiem)
- nietrywialne struktury: `sequence<string>`, `dictionary<string,int>`, `sequence<Book>`
- bledy zwracane **w response (Result types)** zamiast Slice exception
- `module library` zamiast `module catalog` - bo Slice zabrania, by
  interface i otaczajacy go module rozni sie tylko wielkoscia liter

## 2. Ice nad TCP - protokol warstwy aplikacyjnej

### 2.1 Wlasny protokol, nie HTTP

Ice nie korzysta z HTTP. Buduje wlasny protokol bezposrednio nad TCP
(lub UDP, SSL, WebSocket). Charakterystyka:
- jeden TCP connection moze obsluzyc wiele rownoczesnych RPC (multipleksowanie po `request-id`)
- request/response pattern (synchroniczne twoway), oraz oneway (fire-and-forget), idempotent (powtarzalne)
- bidirectional connections - servery moga wywolywac callback przez to samo TCP, ktorym klient sie polaczyl (gateway dla NAT)

### 2.2 Anatomia frame'a

Request:
```
magic 'IceP'              # 4B
protocol version          # 2B (1.0)
encoding version          # 2B (1.1)
type                      # 1B  - 0=Request, 2=Reply, 5=ValidateConnection
compression flag          # 1B
message size              # 4B int LE
[body of message:]
  request-id              # 4B int LE (do dispatch reply)
  identity                # (name:string)(category:string)
  facet                   # sequence<string> (zazwyczaj pusta)
  operation               # string (np. "addBook")
  mode                    # 1B  - 0=Normal, 1=Nonmutating, 2=Idempotent
  context                 # dictionary<string,string>
  encapsulation           # (size:int)(major:1)(minor:1)(parameter bytes)
```

Reply:
```
[Ice header...] type = 2
[body:]
  request-id              # 4B (matchowane do req-id z requesta)
  reply-status            # 1B
                          # 0 = OK
                          # 1 = UserException
                          # 2 = ObjectNotExistException
                          # 3 = FacetNotExistException
                          # 4 = OperationNotExistException
                          # 5 = UnknownLocalException
                          # 6 = UnknownUserException
                          # 7 = UnknownException
  encapsulation           # (out parameters lub serialized exception bytes)
```

### 2.3 Communicator + ObjectAdapter + Servant + Proxy

| pojecie | rola |
|---|---|
| Communicator | centralny obiekt runtime - thread pool, IO, locator, config |
| ObjectAdapter | serwerowy "endpoint" - przyjmuje polaczenia |
| Identity | logiczny ID obiektu na adapterze: `(name, category)` |
| Servant | implementacja interface'u, przypiety pod identity przez `adapter.add(servant, identity)` |
| Proxy (`Ice.ObjectPrx`) | po stronie klienta - reference do zdalnego obiektu |
| String proxy | tekstowa reprezentacja: `"identity:tcp -h host -p port"` |

### 2.4 Naszych przypadek

Server: `tcp -h 0.0.0.0 -p 10000`, identity = `"catalog"`, jeden servant
`CatalogImpl`. Klient: `stringToProxy("catalog:tcp -h localhost -p 10000")`.

Klient OPROCZ tego ma swoj wlasny ObjectAdapter na losowym porcie 127.0.0.1
dla callbackow `BookStream`.

## 3. DII - Dynamic Invocation w Ice

### 3.1 Dwa schematy DII

[Dynamic Invocation and Dispatch](https://doc.zeroc.com/ice/3.7/client-server-features/dynamic-ice/dynamic-invocation-and-dispatch) opisuje **dwa** sposoby:

**Schemat A: ice_invoke + manual marshalling**

Klient sklada bajty in encapsulation **recznie** przez `Ice.OutputStream`:
```cpp
Ice::OutputStream out(communicator);
out.startEncapsulation();
out.write("Dune");
out.write(1965);
out.endEncapsulation();
auto bytes = out.finished();
auto [ok, outBytes] = prx->ice_invoke("addBook", Ice::Normal, bytes);
```

Czyta odpowiedz przez `Ice.InputStream`:
```cpp
Ice::InputStream in(communicator, outBytes);
in.startEncapsulation();
int bookId = in.readInt();
string error = in.readString();
in.endEncapsulation();
```

**WAZNE: dostepne tylko w C++/Java/C#/JavaScript. NIE w Python.**

[Streaming Interfaces](https://doc.zeroc.com/ice/3.7/client-server-features/dynamic-ice/streaming-interfaces) wprost stwierdza:
> "The streaming API is not available in the Python language mapping."

Brief sam linkuje wlasnie te strone, zeby zwrocic uwage na ograniczenie.

**Schemat B: Slice loaded at runtime**

Klient laduje plik `.ice` w runtime - IcePy parsuje + buduje typed
klasy bez `slice2py`:
```python
Ice.loadSlice("-I. catalog.ice")
import library
prx = library.CatalogPrx.checkedCast(base)
res = prx.addBook(library.AddBookRequest("Dune", "Herbert", 1965, []))
```

**Dostepne we wszystkich jezykach.** W Pythonie jest **jedynym** schematem
DII.

### 3.2 Ktorego my uzywamy

**Glownie schemat B (loadSlice)** - bo wysokopoziomowe API streaming
nie jest w IcePy. **Dodatkowo bonusowo schemat A** dla jednej operacji
(`removeBook`) - skladamy bajty rosrednio bez OutputStream.

```python
# main.py
SLICE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "catalog.ice")
Ice.loadSlice(f"-I. -I{os.path.dirname(SLICE_FILE)} {SLICE_FILE}")
import library
```

Po tym `library.AddBookRequest`, `library.CatalogPrx`, `library.BookStream`
istnieja **w pamieci**. Plik `library_ice.py` na dysku **nie istnieje**
(zaden `slice2py` nie byl odpalany).

Menu w `main.py` pokazuje to jeszcze mocniej - nazwy operacji wyciagamy
przez `dir(library.CatalogPrx)` w `discover_business_ops()`. Gdyby
`loadSlice` nie zadzialal, lista by byla pusta. Linia w demo:
```
discovered business ops on library.CatalogPrx: ['addBook', 'findByAuthor', 'removeBook', 'summary']
```

### 3.2a Schemat A bez OutputStream - workaround przez struct.pack

IcePy nie ma OutputStream/InputStream, ale `prx.ice_invoke()` jest. Wiec
zbudujemy encapsulation **recznie** przez `struct.pack`, a odpowiedz
sparsujemy bajt po bajcie wedlug encoding 1.1.

Operacja: `removeBook(int id) -> RemoveBookResult{bool ok, string err, string msg}`.

**Marshal in-params** (int -> encapsulation):
```python
payload = struct.pack("<i", bid)                        # 4B little-endian int
in_bytes = struct.pack("<I", 4 + 2 + len(payload))      # encap size = 4 (size sam) + 2 (encoding) + payload
in_bytes += b"\x01\x01"                                 # encoding 1.1
in_bytes += payload
ok, reply = prx.ice_invoke("removeBook", Ice.OperationMode.Normal, in_bytes)
```

Dla `bid=128` to dokladnie 10 bajtow: `0a 00 00 00 01 01 80 00 00 00`
(widac w demo).

**Unmarshal reply** (encap -> bool + 2 stringi):
```python
off = 6                                                  # skip 4B size + 2B encoding
res_ok = bool(reply[off]); off += 1
err_code, off = _read_ice_string(reply, off)             # size byte + UTF-8
err_msg,  off = _read_ice_string(reply, off)
```

`_read_ice_string` implementuje Ice 1.1 string encoding: 1 bajt size
(0..254) lub `0xff` + 4-bajtowy LE int dla > 254 znakow.

Dla success: 9 bajtow `09 00 00 00 01 01 01 00 00` (encap-size 9,
enc 1.1, bool=true, 2x empty string).

Dla NOT_FOUND: 43 bajty - widac `09` (=length of "NOT_FOUND") + ASCII
`4e 4f 54 5f 46 4f 55 4e 44`, potem `19` (=25) + ASCII komunikatu.

**Pointe**: pure-DII w Pythonie **jest mozliwe**, tylko bez wygodnego
API. Streaming interfaces sa wygodne, ale niepotrzebne - encapsulation
to po prostu `[size:4][encoding:2][payload]`.

### 3.3 Czemu to jest "dynamic"?

"Klucz" w briefie: **brak skompilowanych stubow**. Klasyczne stuby to
`*_ice.py` wyprodukowane przez `slice2py`, zacommitowane do repo. U nas
ich nie ma.

Plik `.ice` to **specyfikacja**, **nie stub**. Analog: w gRPC version
(zad3/zada2) tez mamy `catalog.proto` na serwerze, klient pobiera
deskryptor przez reflection - **klient w czasie kompilacji nie ma
catalog.proto ani catalog_pb2.py**. U nas: klient dostaje `.ice` przez
out-of-band (kopia pliku w repo) i laduje runtime - **nie ma stubow ani
nie kompiluje IDL**.

### 3.4 Po stronie serwera DII tez istnieje - `Ice.Blobject`

`Ice.Blobject` to bazowa klasa generic servanta:
```python
class MyBlob(Ice.Blobject):
    def ice_invoke(self, in_bytes, current):
        # current.operation = nazwa operacji
        # in_bytes = encapsulation z parametrami
        # zwraca (ok: bool, out_bytes: bytes)
        return (True, b"...")
```

Adapter dispatchuje wszystkie wywolania na `ice_invoke` - servant sam
decyduje co z nimi zrobic. **W Pythonie Blobject jest dostepny** -
asymetria z klientem!

W naszym kodzie nie uzywamy Blobject (nie jest potrzebny do tego, co
robimy), ale to istotne ze **server-side DII w Pythonie jest mozliwy**.

## 4. Klient krok po kroku

### Krok 1: Bootstrap

```python
import Ice
Ice.loadSlice(f"-I. -I{os.path.dirname(SLICE_FILE)} {SLICE_FILE}")
import library

communicator = Ice.initialize(sys.argv)
base = communicator.stringToProxy("catalog:tcp -h localhost -p 10000")
base.ice_ping()              # sprawdzenie ze zyje
ids = base.ice_ids()         # ["::Ice::Object", "::library::Catalog"]
prx = library.CatalogPrx.checkedCast(base)   # typed proxy
```

`checkedCast` po stronie klienta wola `prx.ice_isA("::library::Catalog")`
na serwerze. Jesli True -> zwraca typed proxy. Jesli False -> None.

### Krok 2: Wywolanie unary

```python
req = library.AddBookRequest(title="Dune", author="Herbert", year=1965, tags=["sf"])
res = prx.addBook(req)
print(res.bookId, res.errorCode, res.errorMessage)
```

Pod maska:
1. `prx.addBook(req)` - dispatcher zna pola AddBookRequest z `loadSlice`,
   serializuje do encapsulation w protocol order (title, author, year, tags).
2. Send Request frame: [Ice header][reqId][identity="catalog"][operation="addBook"][mode=Normal][context={}][encapsulation(in)]
3. Czeka na Reply z odpowiednim req-id.
4. Deserializuje out_params -> `library.AddBookResult` instance z polami bookId/errorCode/errorMessage.

### Krok 3: Bidirectional callback dla streamingu

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

I uzycie:
```python
adapter = communicator.createObjectAdapterWithEndpoints("CallbackAdapter", "tcp -h 127.0.0.1")
adapter.activate()

cb_id = Ice.Identity(name="stream-" + uuid.uuid4().hex, category="")
cb_prx = library.BookStreamPrx.uncheckedCast(adapter.add(BookStreamI(), cb_id))

prx.findByAuthor(library.AuthorQuery(author, limit), cb_prx)
# do tego momentu wszystkie callbacki dostarczone i przetworzone

while True:
    kind, val = servant.q.get(timeout=10)
    if kind == "next": results.append(val)
    elif kind == "done": return results
    elif kind == "error": raise RuntimeError(...)
```

Ice serializuje proxy `cb_prx` jako kolejny element w encapsulation
(identity + facet + mode + secure + protocol/encoding versions + endpoints).

Serwer odbiera to jako `BookStreamPrx.uncheckedCast(istr.readProxy())` i ma
w rece typed proxy do callbacku po stronie klienta.

`onNext` jest **twoway** - serwer wysyla request DO klienta i czeka na ack
(empty reply). Daje to back-pressure (serwer nie pcha szybciej niz klient
przetworzy) i wykrywa rozlaczenie klienta przez `Ice.LocalException`.

## 5. Serwer Scala - co konkretnie robi

### 5.1 Bootstrap

```scala
val communicator = Util.initialize(args)
val adapter = communicator.createObjectAdapterWithEndpoints("CatalogAdapter", "tcp -h 0.0.0.0 -p 10000")
val identity = Util.stringToIdentity("catalog")
adapter.add(new CatalogImpl(), identity)
adapter.activate()
communicator.waitForShutdown()
```

`Util.initialize(args)` parsuje `--Ice.X.Y=Z` z args, robi communicator z
thread pool. `stringToIdentity("catalog")` daje `Ice.Identity{name="catalog", category=""}`.

### 5.2 Implementacja servanta

```scala
class CatalogImpl extends library.Catalog {
  private val store = new ConcurrentHashMap[Integer, Book]()
  private val nextId = new AtomicInteger(0)

  override def addBook(request: AddBookRequest, current: Current): AddBookResult = {
    if (request.title.trim.isEmpty)
      return new AddBookResult(0, "INVALID_ARGUMENT", "title must not be empty")
    if (duplicate)
      return new AddBookResult(0, "ALREADY_EXISTS", "...")
    ...
    new AddBookResult(id, "", "")
  }

  override def findByAuthor(query: AuthorQuery, observer: BookStreamPrx, current: Current): Unit = {
    if (query.author.trim.isEmpty) {
      observer.onError("INVALID_ARGUMENT", "author must not be empty")
      return
    }
    val matches = store.values.iterator.asScala.toList
      .filter(_.author.toLowerCase.contains(query.author.toLowerCase))
      .sortBy(_.id)
    val limited = if (query.limit > 0) matches.take(query.limit) else matches
    limited.foreach(observer.onNext)
    observer.onCompleted()
  }
}
```

`extends library.Catalog` - generowany Java interface z slice2java. Scala mowi
w Java mappingu Ice. `Current` (ostatni parametr wszystkich operacji) - kontekst
RPC: identity, facet, operation, mode, context.

`observer.onNext(book)` - synchroniczne, twoway. Serwer czeka na ack. Jesli
klient pada - `Ice.LocalException` (ConnectionLost) - serwer przerywa iteracje.

### 5.3 sbt + slice2java codegen (build.sbt)

```scala
import scala.sys.process.Process

libraryDependencies ++= Seq(
  "com.zeroc" % "ice" % "3.7.10"
)

Compile / sourceGenerators += Def.task {
  val sliceDir = (Compile / sourceDirectory).value / "slice"
  val outDir   = (Compile / sourceManaged).value / "slice-java"
  val cache    = streams.value.cacheDirectory / "slice"
  val sliceFiles = (sliceDir ** "*.ice").get.toSet
  val cached = FileFunction.cached(cache, FilesInfo.lastModified, FilesInfo.exists) { _ =>
    IO.delete(outDir); IO.createDirectory(outDir)
    sliceFiles.foreach { f =>
      val rc = Process(Seq("slice2java", "--output-dir", outDir.getAbsolutePath, f.getAbsolutePath)).!
      if (rc != 0) sys.error(s"slice2java failed for ${f.getName}")
    }
    (outDir ** "*.java").get.toSet
  }
  cached(sliceFiles).toSeq
}.taskValue
```

Wynik:
- `Book.java`, `AddBookRequest.java`, etc - publicznymi polami, default + full-arg constructor
- `Catalog.java` (interface) z metodami operacji + dispatcher table (`_iceDispatch`)
- `CatalogPrx.java` (proxy klienta dla typed calls)
- `BookStream.java`, `BookStreamPrx.java`
- helpery `BookSeqHelper`, `AuthorCountsHelper`

`FileFunction.cached` zapobiega re-generation - tylko gdy `.ice` ma nowsza
mtime.

## 6. Porownanie z gRPC dynamic (zada2)

| | gRPC reflection (zada2) | Ice loadSlice (zadi1) |
|---|---|---|
| Skad klient ma schemat | RPC reflection (`ServerReflection.GetFileDescriptorProto`) | plik `.ice` dystrybuowany osobno (out-of-band) |
| API u klienta | `channel.unary_unary(path, ser, deser)` z `MessageFactory.GetMessageClass` | `library.CatalogPrx.checkedCast(prx).addBook(req)` typed |
| Stuby u klienta | brak (build w runtime przez metaklase z FileDescriptorProto) | brak (build w runtime przez IcePy.defineStruct z parsowanego .ice) |
| Wire format | self-describing (tag + wire type) - mozna pominac nieznane | strict layout (kolejnosc+typy musi sie zgadzac) |
| Discovery operacji | TAK - ServiceDescriptor z metodami przez reflection | NIE - klient zna z out-of-band |
| Streaming | natywne `unary_stream` w sygnaturze | brak natywnego, bidirectional callback przez 2 interfejsy |
| Bledy | gRPC Status (codes) | Result types z polami errorCode/errorMessage |
| Manual marshalling | n/a (klasy auto z metaklasy) | dostepne tylko w C++/Java/C# (NIE Python) |

Wnioski:
- **gRPC ma latwiejszy dynamic invocation** dzieki self-describing format + reflection
- **Ice DII jest blizej "metalu"** - wymaga rozproszenia `.ice` (lub serwer ktory transmituje go w specjalnej operacji), brak introspekcji operacji
- **Oba spelniaja brief** "klient bez stubow IDL", ale w Ice trzeba wiecej setup'u
- **Python's Ice ma dodatkowe ograniczenie**: brak `OutputStream`/`InputStream`, wiec pure manual marshalling nie jest mozliwy

## 7. Zalety / wady DII (Ice loadSlice)

### Zalety
- klient bez kompilacji Slice (`slice2py` nigdy nie odpalany)
- typed proxy w Pythonie (autocomplete jak ze statycznymi stubami, w tym samym runtime)
- bidirectional connection - sciezka callback przez ten sam TCP, bez NAT issues (my nie uzywamy)
- ten sam plik `.ice` server kompiluje (slice2java), klient laduje runtime - jedna umowa kontraktu

### Wady
- klient musi miec **identyczna** Slice z serwerem - dystrybucja kontraktu out-of-band
- `loadSlice` przy starcie ma overhead (parsowanie + wywolania defineStruct)
- brak type safety w czasie pisania kodu (IDE nie wie o `library.CatalogPrx` przed runtime)
- Slice nie jest self-describing - klient nie ma jak pominac nieznane pole
- silne sprzezenie z encoding 1.1 layoutem - zmiana kolejnosci pol w `.ice` lamie klienta
- brak natywnego reflection - `ice_ids()` daje tylko nazwe interfejsu, nie operacji
- "blast radius" - refaktor pol nie wybucha kompilacja u klienta
- streaming wymaga drugiego interfejsu i lokalnego adaptera u klienta - wiecej infrastruktury

### Kiedy uzywac
Narzedzia ops/dev (dynamic clients ktorzy musza chodzic po wielu serwisach
bez build per kazdy), API gateways, generic frameworki testowe.
**Nie** dla zwyklej aplikacyjnej komunikacji - straty produktywnosci wieksze
niz zyski.

## 8. Pytania prowadzacego (kierunki)

### O DII

1. **Co to dynamic invocation w Ice?** Klient nie zna kontraktu Slice w
   czasie kompilacji. Ladowuje plik `.ice` w runtime przez `Ice.loadSlice`,
   IcePy buduje klasy w pamieci.

2. **Czy plik `.ice` to nie jest stub?** Nie. `.ice` to **plik IDL**
   (specyfikacja), analog `.proto`. **Stub** to `*_ice.py` wyprodukowany
   przez `slice2py` - tego u nas nie ma.

3. **Czemu nie uzywasz `ice_invoke` z manual marshalling?**
   Uzywam - dla bonusu na operacji `removeBook` w menu (pozycja 7).
   IcePy nie eksponuje `Ice.OutputStream`/`Ice.InputStream`
   ([dokumentacja ZeroC](https://doc.zeroc.com/ice/3.7/client-server-features/dynamic-ice/streaming-interfaces)),
   ale samo `prx.ice_invoke()` jest. Wiec sklada sie encapsulation
   recznie przez `struct.pack` (`"<I"` size + `"\x01\x01"` encoding 1.1 +
   payload) i parsuje reply bajt po bajcie. Sekcja 3.2a w notatce ma
   pelny hex breakdown. Glowne 4 operacje ida przez loadSlice (Schemat
   B) bo to czystsze.

4. **Czemu Result types zamiast Slice exceptions?** Czytelnosc + spojnosc
   (kod typu `if res.errorCode == "NOT_FOUND"` jest jasny). Decoding Ice
   user exception bez `loadSlice` (czysty manual marshalling) wymaga
   parsowania slice flags + type ID + slices per kazdy slice w lancuchu
   dziedziczenia - skomplikowane.

5. **Wady wywolania dynamicznego?** Brak type safety przy build, klient i
   serwer musza miec identyczne Slice files, brak introspection w 3.7,
   "blast radius" przy refactorze.

### O Slice / encoding

6. **Czemu Slice nie jest self-describing jak protobuf?** Encoding 1.1 ma
   strict, kolejnosciowy layout - bez tagow w bajtach. Implikacja: klient
   musi znac schemat zeby cokolwiek odczytac.

7. **Ice ma mechanism "slicing classes" - co to?** Tylko dla **classes** (nie
   structs). Pozwala klientowi zignorowac pochodzace klasy, ktorych nie zna,
   i traktowac je jako baza. Forma forward compatibility dla classes. My
   uzywamy structs, nie korzystamy.

8. **Pokaz na bajtach `Book{id=1, title="Dune", author="Herbert", year=1965, tags=["sf"]}`.**
   ```
   01 00 00 00              # int id=1 (LE)
   04 44 75 6e 65            # string title size=4 + "Dune"
   07 48 65 72 62 65 72 74   # string author size=7 + "Herbert"
   ad 07 00 00              # int year=1965 (LE)
   01                        # sequence size=1
   02 73 66                  # string tag size=2 + "sf"
   ```
   Bezposrednio. Brak header'ow per pole.

### O Ice runtime

9. **Co znajduje sie w Ice Request frame?** [Ice header][reqId][identity][facet][operation][mode][context][encapsulation].

10. **Czym sie rozni reply-status 1 (UserException) od 7 (UnknownException)?**
    1 = serwer rzucil zdefiniowany w Slice exception. 7 = serwer rzucil cos
    nieoczekiwanego (np. NPE) - klient dostaje tylko nazwe typu.

11. **Czemu request-id?** Wielo-RPC po jednym TCP. Reply muszi sie matchowac
    do odpowiedniego request-id, bo response moga przyjsc w innej kolejnosci.

### O streaming/callback

12. **Czemu Ice nie ma natywnego server-streaming jak gRPC?** Ice operacja to
    1 req / 1 resp. Streaming realizowany przez **dwukierunkowy callback** -
    klient daje proxy do swojego BookStream, serwer wola `onNext` na nim.

13. **Co sie stanie jak klient padnie w trakcie streamingu?** `onNext()` po
    stronie serwera rzuca `Ice.LocalException` (ConnectionLost), serwer
    przerywa iteracje. U nas catch w `findByAuthor` loguje i konczy.

14. **Czemu twoway callback a nie oneway?** Twoway daje gwarancje, ze klient
    odebral message przed nastepnym onNext. Oneway = fire-and-forget, mozna
    "pchac szybciej" ale tracisz back-pressure.

### O loadSlice

15. **Co `Ice.loadSlice` robi w srodku?** Wywoluje wbudowany mcpp preprocesor
    na pliku, parsuje Slice -> AST, traversuje AST i woła `IcePy.defineStruct`,
    `IcePy.defineSequence`, `IcePy.defineProxy` - tworzac klasy Pythona w pamieci.

16. **Co produkuje `slice2py` - czym sie rozni od `loadSlice`?** `slice2py`
    generuje plik `library_ice.py` zawierajacy te same wywolania `IcePy.defineStruct`
    co `loadSlice` - tylko hardcoded i wykonywane przy `import library_ice`. Roznica
    jest tylko w punkcie czasowym (build vs run).

17. **Czemu trzeba `import library` po `loadSlice`?** Bo `loadSlice` rejestruje
    nowy modul Pythona o nazwie `library` (z module name w `.ice`). Bez `import`
    nie mialbys uchwytu na klasy.

### O serwer/codegen

18. **Co generuje slice2java?** Per struct: klase z public fields, `ice_read`/
    `ice_write`. Per interface: Java interface (servant base) + Prx (proxy).
    Plus dispatcher table w interfejsie.

19. **Czemu module `library` a nie `catalog`?** Slice zabrania, by interface i
    otaczajacy go module rozni sie tylko wielkoscia liter - `interface Catalog`
    w `module catalog` to blad.

20. **Skad wiadomo, ze serwer Scala dziala poprawnie z generatedu Java?**
    JVM-bytecode jest wspolny. `class CatalogImpl extends library.Catalog`
    (Java interface) z poziomu Scali kompiluje sie - Scala konsumuje Java seamlessly.

### Porownanie z gRPC (zada2)

21. **Co Ice ma a gRPC nie?** Bidirectional connections (callback przez to samo
    TCP), wlasny protokol nad TCP (nie HTTP), proste mode'y twoway/oneway/idempotent.

22. **Co gRPC ma a Ice nie (3.7)?** Reflection w bazowym protokole (klient
    samodzielnie odkrywa schemat), server-streaming w sygnaturze operacji,
    self-describing wire format, manual marshalling we wszystkich jezykach.

23. **W ktorym latwiej zrobic dynamic invocation?** gRPC. Reflection daje
    klientowi pelny schemat, GetMessageClass buduje klasy z metaklasy, zero
    recznego marshallingu. W Ice klient potrzebuje pliku `.ice` wczytanego
    runtime.

### O zaletach Ice ogolnie

24. **Co mowi link "streaming-interfaces" z briefu?** Mowi ze Ice nie ma
    natywnego streamingu w sygnaturze, opisuje wzorzec "callback object",
    i wprost stwierdza ze "streaming API not available in Python language mapping".

25. **Czemu w Pythonie wybralem loadSlice a nie ice_invoke z manual?**
    Glowny path - loadSlice (Schemat B), bo high-level streaming API
    nie jest w IcePy. Ale dla pokazania ze rozumiem Schemat A, w menu
    jest pozycja 7 (`removeBook[ice_invoke]`) ktora robi encapsulation
    przez `struct.pack` i parsuje reply bajt po bajcie. To dziala bo
    `ice_invoke` samo w sobie jest w IcePy, tylko brakuje wygodnych
    klas OutputStream/InputStream do skladania payloadu.

## 9. Mapping problemow rozproszenia

| problem | rozwiazanie u nas |
|---|---|
| jezyki/platformy | slice2java/loadSlice - kazdy jezyk konsumuje to samo IDL |
| klient nie ma `.ice` | dystrybucja out-of-band (kopia w repo). W 3.8 byloby reflection. |
| klient za NATem | bidirectional connections - my nie uzywamy w demo |
| zlozone struktury | sequence, dictionary, struct, class, exception |
| streaming wynikow | bidirectional callback z osobnym BookStream interface |
| bledy semantyczne | Result types (errorCode/errorMessage) |
| concurrency w serwerze | ConcurrentHashMap + AtomicInteger |
| async unary | brak (wszystkie nasze operacje sync). Ice ma AMI/AMD dla async patterns. |

## 10. Co mozna by dodac w prodzie

| brakuje | jak dodac |
|---|---|
| TLS | endpoint `ssl -h ... -p ...` zamiast `tcp` + certyfikaty |
| auth | interceptor dla `Current.ctx` z token-em (np. JWT) |
| persystencja | ConcurrentHashMap -> baza danych |
| atomic check+put | putIfAbsent z det. kluczem (title+author lower) |
| bidirectional connection | klient invokuje sie do serwera, ustawia `Connection.setAdapter` - callback chodzi przez to samo TCP |
| ice grid (load balancing) | IceGrid registry + nodes |
| obserwability | Metrics admin facet |
| reflection w 3.7 | osobny "describeApi" operation w Slice + dystrybuowany `.ice` z `Ice.loadSlice` na podstawie odpowiedzi |
| pure-DII w Pythonie | u nas: workaround przez `struct.pack` dla `removeBook[ice_invoke]` (menu 7). W prodzie: przejscie na 3.8 z natywnym OutputStream lub C++/Java klient. |

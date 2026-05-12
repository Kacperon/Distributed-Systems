# Notatka do nauki - zadi1 (Wywolanie dynamiczne przez Ice)

Klient Python wywoluje serwer Scala **bez zadnych artefaktow IDL** -
zero `slice2py`-output, zero `library_ice.py`, zero `Ice.loadSlice`,
zero pliku `catalog.ice` w katalogu klienta. Wszystkie 4 operacje ida
**w runtime** przez `prx.ice_invoke(op, mode, encap)` z **recznie
skladanym** encapsulation Ice 1.1 (`struct.pack` + ASCII bajty).
Callback `BookStream*` jest realizowany przez `Ice.Blobject` z rownie
recznym dispatchem po `current.operation`.

Glowne tematy: Slice IDL jako kontrakt out-of-band (dystrybuowany
przez memorie programisty, a nie plik), encoding 1.1 wire format
**w dotyku** (size encoding, encapsulation, proxy encoding,
endpoint encapsulation), Ice DII Schemat A (manual marshalling)
**zrealizowany w Pythonie mimo braku OutputStream/InputStream**,
bidirectional callback w roli "streamingu" w Ice 3.7, asymetria
Schemat A: dostepny w C++/Java/C#/JavaScript przez OutputStream,
w Pythonie tylko hand-rolled, **server-side DII na kliencie** przez
`Ice.Blobject`, roznice vs gRPC reflection.

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

### 1.4 IDL u nas (na serwerze - `server/src/main/slice/catalog.ice`, u klienta NIE)

Pelny schemat ktory klient musi pamietac w glowie / w kodzie:

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

**Dostepne we wszystkich jezykach.** Ale formalnie klient nadal ma
"wkompilowane" typy IDL (tylko w pamieci, nie na dysku) - z punktu
widzenia briefu zadania I1 (klient nie ma "zadnych klas/bibliotek stub
bedacych wynikiem kompilacji IDL") **to jest na granicy**.

### 3.2 Ktorego my uzywamy

**Tylko Schemat A** - manual marshalling przez `struct.pack`.
Klient **nie ma w katalogu nawet `catalog.ice`** - schemat zadania
jest spelniony co do litery: zaden artefakt IDL nie istnieje po
stronie klienta.

Cena: musimy odtworzyc reczne API streamingu, ktorego IcePy nie
eksponuje. Robimy to przez dwie minibiblioteki w `main.py`:

```python
class OutBuf:
    def __init__(self): self.b = bytearray()
    def write_int(self, v):    self.b += struct.pack("<i", v)
    def write_bool(self, v):   self.b.append(1 if v else 0)
    def write_size(self, n):
        if n < 255: self.b.append(n)
        else:       self.b.append(255); self.b += struct.pack("<I", n)
    def write_string(self, s):
        data = s.encode("utf-8")
        self.write_size(len(data)); self.b += data
    def write_string_seq(self, seq):
        self.write_size(len(seq))
        for s in seq: self.write_string(s)
    def write_proxy(self, prx): ...           # patrz 3.2b
    def encapsulation(self):
        body = bytes(self.b)
        return struct.pack("<I", 4 + 2 + len(body)) + b"\x01\x01" + body

class InBuf:
    def __init__(self, data, start=6):        # start=6 zeskanie encap header
        self.b = data; self.off = start
    def read_int(self):    ...                # struct.unpack_from("<i", ...)
    def read_bool(self):   ...
    def read_size(self):   ...
    def read_string(self): ...
    def read_string_seq(self): ...
```

To zastepuje `Ice.OutputStream` / `Ice.InputStream` ktore w Pythonie
sa niedostepne. Ten kod **jest naszym wlasnym streaming API**.

### 3.2a Wszystkie 4 operacje na manualnym marshallingu

**`addBook(AddBookRequest) -> AddBookResult`** - struct in z sekwencja,
struct out:
```python
out = OutBuf()
out.write_string(title)
out.write_string(author)
out.write_int(year)
out.write_string_seq(tags)
ok, reply = prx.ice_invoke("addBook", Ice.OperationMode.Normal, out.encapsulation())
buf = InBuf(reply)
book_id, err_code, err_msg = buf.read_int(), buf.read_string(), buf.read_string()
```

Dla in `(title="Dynamic Demo Book", author="Kacper Dynamic", year=2026, tags=["demo","ice"])`:
```
35 00 00 00              # encap size = 53 (4 + 2 + 47)
01 01                    # encoding 1.1
11 44 79 6e 61 6d 69 63 20 44 65 6d 6f 20 42 6f 6f 6b   # size=17 + "Dynamic Demo Book"
0e 4b 61 63 70 65 72 20 44 79 6e 61 6d 69 63             # size=14 + "Kacper Dynamic"
ea 07 00 00              # year = 2026 (LE)
02                       # tags size = 2
04 64 65 6d 6f           # "demo"
03 69 63 65              # "ice"
```

Reply (success, bookId=249): 12 bajtow
`0c 00 00 00 01 01 f9 00 00 00 00 00` - encap size 12, encoding 1.1,
int 249, 2 puste stringi (errorCode=errorMessage="").

**`summary() -> CatalogStats`** - void in, zlozony out (dict + seq<Book>):
```python
ok, reply = prx.ice_invoke("summary", Ice.OperationMode.Normal, empty_encapsulation())
buf = InBuf(reply)
total = buf.read_int()
n = buf.read_size()
by_author = {buf.read_string(): buf.read_int() for _ in range(n)}
m = buf.read_size()
recent = [read_book(buf) for _ in range(m)]
```
`empty_encapsulation()` = `struct.pack("<I", 6) + b"\x01\x01"` = 6 bajtow.

**`removeBook(int) -> RemoveBookResult`** - prymityw in, struct out:
```python
out = OutBuf(); out.write_int(bid)
ok, reply = prx.ice_invoke("removeBook", Ice.OperationMode.Normal, out.encapsulation())
buf = InBuf(reply)
ok_flag, err_code, err_msg = buf.read_bool(), buf.read_string(), buf.read_string()
```
Dla `bid=249`: in = 10 bajtow `0a 00 00 00 01 01 f9 00 00 00`.
Reply (success): 9 bajtow `09 00 00 00 01 01 01 00 00`.

**`findByAuthor(AuthorQuery, BookStream*) -> void`** - patrz 3.2b nizej,
bo wymaga **encodingu proxy w bajtach**, ktore jest najbardziej
nietrywialne.

### 3.2b Manual encoding **proxy** - jak idzie callback proxy na drut

Encoding Ice 1.1 dla proxy (w `write_proxy(prx)`):
```
identity:
  name:string         # np. "stream-fd5e0b757fcc4718b5748ee51dee2aff"
  category:string     # zwykle pusty
if name pusty -> null proxy (koniec)
else:
  facet_path:size     # 0 lub 1 + facet:string (zwykle 0)
  mode:byte           # 0 = twoway
  secure:bool         # 0
  protocol_major:1, protocol_minor:0
  encoding_major:1,  encoding_minor:1
  endpoints_count:size
  for each endpoint:
    endpoint_type:short    # 1 = TCP
    encap_size:int         # rozmiar encapsulation tego endpoint'a
    encoding:1.1           # 2 bajty
    body (dla TCP):
      host:string
      port:int
      timeout:int          # -1 = none, my dostajemy 60000 (60s)
      compress:bool
```

Z proxy'a wyciagamy te informacje przez Python Ice API:
```python
ident   = prx.ice_getIdentity()       # name, category
facet   = prx.ice_getFacet()
eps     = prx.ice_getEndpoints()
for ep in eps:
    info = ep.getInfo()               # .host, .port, .timeout, .compress, .type()
```

Wynik dla `findByAuthor` (author="Kacper Dynamic", limit=0, cb_prx=lokalny callback):
```
65 00 00 00              # encap size = 101
01 01                    # encoding 1.1
0e 4b 61 63 70 65 72 ...  # "Kacper Dynamic"
00 00 00 00              # limit = 0
27 73 74 72 65 61 6d ...  # identity.name = "stream-<uuid>"
00                       # identity.category = ""
00                       # facet path size = 0
00                       # mode = twoway
00                       # secure = false
01 00                    # protocol 1.0
01 01                    # encoding 1.1
01                       # 1 endpoint
01 00                    # endpoint type = TCP (1, short)
19 00 00 00              # endpoint encap size
01 01                    # endpoint encap encoding
09 31 32 37 2e 30 2e 30 2e 31   # host "127.0.0.1"
4f a7 00 00              # port (LE int)
60 ea 00 00              # timeout = 60000 (LE)
00                       # compress = false
```

Reply z `findByAuthor` jest 6 bajtow (`06 00 00 00 01 01`) - pusty
encapsulation, bo metoda zwraca `void`. **Wszystkie wyniki ida przez
callback** (kolejne requesty `onNext` od serwera).

### 3.2c Server-side DII na **kliencie**: Ice.Blobject jako callback

`BookStream` nie ma stuba u nas, ale serwer musi miec mozliwosc
wywolac na nim 3 metody. Robimy to przez **`Ice.Blobject`** - generic
servant ktory dostaje wszystkie operacje przez wlasne `ice_invoke`:

```python
class BookStreamBlobject(Ice.Blobject):
    def __init__(self, q): self.q = q

    def ice_invoke(self, in_encaps, current):
        op = current.operation
        if op == "onNext":
            book = read_book(InBuf(in_encaps))
            self.q.put(("next", book))
            return (True, empty_encapsulation())
        if op == "onCompleted":
            self.q.put(("done", None))
            return (True, empty_encapsulation())
        if op == "onError":
            buf = InBuf(in_encaps)
            self.q.put(("error", (buf.read_string(), buf.read_string())))
            return (True, empty_encapsulation())
        # ice_isA / ice_id / ice_ids / ice_ping - musimy obsluzyc sami
        # bo Blobject routuje WSZYSTKO przez ice_invoke
        if op == "ice_isA": ...
        if op == "ice_id":  ...
        if op == "ice_ids": ...
        if op == "ice_ping": return (True, empty_encapsulation())
        raise Ice.OperationNotExistException()
```

To jest fascynujace symetricznie do 3.4: server-side DII jest **w
IcePy w pelni dostepne**, mimo ze client-side OutputStream/InputStream
nie. **Klient tutaj jest w roli serwera dla callbackow** - i ta strona
DII dziala normalnie.

**Pointe**: pure-DII w Pythonie **jest mozliwe i u nas zrealizowane
end-to-end** dla wszystkich 4 operacji (3 typowe + 1 z proxy +
callback przez Blobject). Streaming interfaces sa wygodne, ale
niepotrzebne - encapsulation to po prostu `[size:4][encoding:2][payload]`.

### 3.3 Czemu to jest "dynamic"?

"Klucz" w briefie: **brak skompilowanych stubow**. Klasyczne stuby to
`*_ice.py` wyprodukowane przez `slice2py`, zacommitowane do repo. U nas
ich nie ma.

Plik `.ice` to **specyfikacja**, **nie stub**. Analog: w gRPC version
(zad3/zada2) tez mamy `catalog.proto` na serwerze, klient pobiera
deskryptor przez reflection - **klient w czasie kompilacji nie ma
catalog.proto ani catalog_pb2.py**. U nas: klient **nie dostaje nawet
`.ice` runtime'em** - schemat jest hardkodowany w kodzie klienta
(nazwy operacji, kolejnosc pol, typy) wedlug dokumentacji dostarczonej
out-of-band. Nie ma `loadSlice`, nie ma `slice2py`, nie ma stubow -
tylko `prx.ice_invoke(op, mode, bytes)` z bajtami sklejonymi recznie.

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
asymetria z client-side OutputStream/InputStream (ktore w IcePy NIE
sa eksponowane)!

**My uzywamy Blobject** - servant `BookStreamBlobject` w naszym
kliencie odbiera callbacki `onNext` / `onCompleted` / `onError` od
serwera. Patrz sekcja 3.2c powyzej. Klient w naszym demo to jednoczesnie
"serwer" dla BookStream, i ta strona DII jest pelna.

## 4. Klient krok po kroku

### Krok 1: Bootstrap - zaden loadSlice, zaden import library

```python
import Ice

communicator = Ice.initialize(sys.argv)
base = communicator.stringToProxy("catalog:tcp -h localhost -p 10000")
base.ice_ping()                                  # zyje?
base.ice_ids()                                   # ["::Ice::Object", "::library::Catalog"]
if not base.ice_isA("::library::Catalog"):       # weryfikacja kontraktu
    sys.exit(1)
```

Klient operuje na **gholym `ObjectPrx`**. `ice_ping`, `ice_ids`,
`ice_isA` to standardowe Ice runtime helpers, dostepne na kazdym
proxy bez stuba. **Brak `checkedCast`** - nie ma na co rzutowac, bo
nie ma typu `CatalogPrx`.

### Krok 2: Adapter dla callbackow BookStream

Nadal potrzebujemy lokalnego adaptera bo `findByAuthor` wymaga proxy
zwrotnego do nas:
```python
adapter = communicator.createObjectAdapterWithEndpoints("CallbackAdapter", "tcp -h 127.0.0.1")
adapter.activate()
```

### Krok 3: Wywolanie unary - `addBook`

```python
out = OutBuf()
out.write_string("Dune")
out.write_string("Herbert")
out.write_int(1965)
out.write_string_seq(["sf"])
ok, reply = prx.ice_invoke("addBook", Ice.OperationMode.Normal, out.encapsulation())
buf = InBuf(reply)                                # skip 6B encap header
book_id = buf.read_int()
err_code = buf.read_string()
err_msg  = buf.read_string()
```

Pod maska:
1. `out.encapsulation()` produkuje bajty wedlug encoding 1.1 (sekcja 3.2a).
2. `prx.ice_invoke(...)` opakowuje to w Ice Request frame:
   `[IceP][1.0][1.1][type=Request][...][reqId][identity="catalog"][facet=()][operation="addBook"][mode=0][context={}][encapsulation]`
3. Serwer dispatch'uje na `addBook(AddBookRequest)` - **on ma stuby**, my nie.
4. Reply przychodzi jako bajty - parsujemy field-by-field wedlug schematu
   ktory PAMIETAMY (kolejnosc: int, string, string).

### Krok 4: Bidirectional callback przez `Ice.Blobject` - `findByAuthor`

Servant:
```python
class BookStreamBlobject(Ice.Blobject):
    def __init__(self, q): self.q = q
    def ice_invoke(self, in_encaps, current):
        op = current.operation
        if op == "onNext":
            self.q.put(("next", read_book(InBuf(in_encaps))))
            return (True, empty_encapsulation())
        # onCompleted, onError, ice_isA, ice_id, ice_ids, ice_ping -> patrz 3.2c
```

Wywolanie:
```python
q = queue.Queue()
cb_id = Ice.Identity(name="stream-" + uuid.uuid4().hex, category="")
cb_prx = adapter.add(BookStreamBlobject(q), cb_id)   # zwraca ObjectPrx

out = OutBuf()
out.write_string(author)
out.write_int(limit)
out.write_proxy(cb_prx)                              # patrz 3.2b - encoding identity + endpoint
prx.ice_invoke("findByAuthor", Ice.OperationMode.Normal, out.encapsulation())
# reply = void (6 bajtow pustej encapsulation). Wyniki ida przez callback.

results = []
while True:
    kind, val = q.get(timeout=10)
    if kind == "next": results.append(val)
    elif kind == "done": break
    elif kind == "error": raise RuntimeError(...)
```

Serwer odbiera `cb_prx` jako `BookStreamPrx.uncheckedCast(istr.readProxy())`
i ma w rece typed proxy do **naszego Blobject'a**. Po wire wszystko jest
takie samo - Blobject jest po prostu generic dispatcher'em zamiast
typed servanta.

`onNext` jest **twoway** - serwer wysyla request DO klienta i czeka na ack
(empty reply). Daje to back-pressure (serwer nie pcha szybciej niz klient
przetworzy) i wykrywa rozlaczenie klienta przez `Ice.LocalException`.

### Krok 5: Co dziala bez stubow per definicje

| obiekt/akcja | dziala bez IDL? | dlaczego |
|---|---|---|
| `Ice.initialize` | tak | runtime, nie IDL |
| `stringToProxy` | tak | sam parser tekstu |
| `ice_ping`/`ice_ids`/`ice_isA`/`ice_id` | tak | wbudowane w `Ice.ObjectPrx` |
| `ice_invoke` | tak | generic call - nazwa op + bajty |
| `Ice.Identity`, `Ice.Current`, `Ice.OperationMode` | tak | runtime types |
| `Ice.Blobject` | tak | generic servant, nic nie wie o `BookStream` |
| `adapter.add(servant, identity)` | tak | adapter dispatch'uje po identity |
| **`prx.addBook(req)`** | NIE | bo `prx` nie jest `CatalogPrx`, jest `ObjectPrx` |
| **`library.AddBookRequest`** | NIE | klasa nie istnieje, nie ma `library` |

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

| | gRPC reflection (zada2) | Ice ice_invoke + manual marshalling (zadi1) |
|---|---|---|
| Skad klient ma schemat | RPC reflection (`ServerReflection.GetFileDescriptorProto`) | "z pamieci programisty" - hardkodowany w kodzie klienta (kolejnosc pol, typy) |
| API u klienta | `channel.unary_unary(path, ser, deser)` z `MessageFactory.GetMessageClass` | `prx.ice_invoke("op", mode, bytes)` + recznie sklejone `struct.pack` payload |
| Stuby u klienta | brak (build w runtime przez metaklase z FileDescriptorProto) | brak ZADNYCH - nawet nie wczytujemy `.ice` |
| Wire format | self-describing (tag + wire type) - mozna pominac nieznane | strict layout (kolejnosc+typy musi sie zgadzac) |
| Discovery operacji | TAK - ServiceDescriptor z metodami przez reflection | NIE - klient zna z out-of-band (zna nazwy z dokumentacji/spec) |
| Streaming | natywne `unary_stream` w sygnaturze | brak natywnego, bidirectional callback przez 2 interfejsy + Blobject |
| Bledy | gRPC Status (codes) | Result types z polami errorCode/errorMessage |
| Manual marshalling | n/a (klasy auto z metaklasy) | w Pythonie **trzeba pisac samemu** (OutBuf/InBuf), w C++/Java/C# jest natywne API |
| Kontrakt typu na drucie | `FileDescriptorProto` po reflection | brak - kontrakt to ustna umowa miedzy klientem a serwerem |

Wnioski:
- **gRPC ma latwiejszy dynamic invocation** dzieki self-describing format + reflection -
  klient sam odkrywa kontrakt, buduje klasy z metaklasy, zero recznego marshallingu
- **Ice DII jest blizej "metalu"** - klient musi pamietac schemat z dokumentacji, brak
  introspekcji operacji, w wire format nie ma tagow pol
- **Oba spelniaja brief "klient bez stubow IDL"**, ale w Ice (zwlaszcza Python) trzeba
  zaimplementowac duzo wiecej: encapsulation, varint size, proxy encoding, Blobject dispatch
- **Python's Ice ma asymetryczne ograniczenie**: client-side `OutputStream`/`InputStream`
  niedostepne, ale server-side `Blobject` jest. To pasuje do naszego setupu bo BookStream
  callback wymaga server-side a tam Python ma czego trzeba

## 7. Zalety / wady DII (Ice ice_invoke + manual marshalling)

### Zalety
- **zero artefaktow IDL u klienta** - co do litery briefu (brak nawet pliku `.ice`)
- **pelna kontrola nad bajtami** - widac wprost wire format Ice 1.1, idealne do nauki/debug
- jeden klient potrafi rozmawiac z wieloma serwerami o roznych interfejsach
  bez build-per-service (np. generic API gateway / orchestrator / monitoring)
- nie polega na zadnym IDL parserze w runtime - zaden mcpp, zaden `defineStruct`
- moze wywolac dowolna operacja po nazwie - test/exploration tool nieoczekiwanych endpointow
- bidirectional connection nadal dziala - serwer woła nasz `Ice.Blobject` przez ten sam TCP

### Wady
- **brak type safety calkowicie** - IDE nie wie o niczym, blad w kolejnosci pol = milcz blad parsowania
- klient musi **pamietac kontrakt** (kolejnosc pol, typy) - bez `.ice` dryf miedzy klientem a serwerem jest cichy
- duzo kodu boilerplate (encapsulation, varint size, proxy encoding) - patrz `OutBuf`/`InBuf` w `main.py`
- **w Pythonie szczegolnie boli** - brak `OutputStream`/`InputStream`, wszystko `struct.pack` recznie
- Slice nie jest self-describing - nie da sie pominac nieznane pole, brak natywnego reflection
- "blast radius" - refaktor pol w `.ice` na serwerze nie wybucha u klienta, tylko cichy mismatch w runtime
- streaming/callback wymaga `Ice.Blobject` z wlasnym dispatcher'em po `current.operation` plus obsluga `ice_isA`/`ice_id`/`ice_ids`/`ice_ping`
- proxy encoding ma kilka non-obvious detali (facet path size encoding, endpoint encapsulation, timeout=60000 default)

### Kiedy uzywac
Narzedzia ops/dev (dynamic clients ktorzy musza chodzic po wielu serwisach
bez build per kazdy), API gateways, generic frameworki testowe, exploit / fuzzing
narzedzia na endpointy Ice. **Nie** dla zwyklej aplikacyjnej komunikacji - straty
produktywnosci olbrzymie. W prodzie - albo stuby + typed proxy, albo (jezeli musisz)
`Ice.loadSlice` jako kompromis (klient ma typed API w runtime, plik `.ice` nadal jest).

## 8. Pytania prowadzacego (kierunki)

### O DII

1. **Co to dynamic invocation w Ice?** Klient nie zna kontraktu Slice w
   czasie kompilacji. U mnie posunalem to maksymalnie: klient **nie ma
   nawet pliku `.ice`** ani `loadSlice` - kazda z 4 operacji idzie przez
   `prx.ice_invoke(op_name, mode, bytes_encapsulation)`, gdzie bajty
   ukladam recznie wedlug encoding 1.1.

2. **Czemu nie uzywasz `loadSlice` skoro by bylo prosciej?** Bo `loadSlice`
   formalnie nadal "kompiluje" IDL (`mcpp` + parser + `defineStruct`),
   tyle ze do pamieci. Brief mowi `klient ma nie miec dolaczonych zadnych
   klas/bibliotek stub bedacych wynikiem kompilacji IDL`. `loadSlice` jest
   na granicy - moj wariant z `ice_invoke` + `struct.pack` jest jednoznaczny.

3. **W jezykach C++/Java/C# byloby latwiej, czemu Python?** Ano wlasnie -
   drugi link w briefie ([streaming-interfaces](https://doc.zeroc.com/ice/3.7/client-server-features/dynamic-ice/streaming-interfaces))
   konkretnie wskazuje Python jako ten gdzie streaming API nie ma. Czyli
   "ograniczenia" o ktorych mam dyskutowac sa literally dotykane przez ten kod.
   W C++ to byloby `Ice::OutputStream out; out.write(...); out.write(...);`
   - tutaj jest `struct.pack` + wlasna `OutBuf`/`InBuf` klasa zastepujaca te API.

4. **Pokaz jak idzie `addBook` na drucie.** Encoding 1.1, in-encapsulation:
   `[encap_size:4][1.1:2][title:string][author:string][year:int][tags:seq<string>]`.
   Dla `("Dynamic Demo Book", "Kacper Dynamic", 2026, ["demo","ice"])` =
   53 bajty (sekcja 3.2a ma pelny hex breakdown). Reply (success bookId=249):
   12 bajtow - `[12:4][1.1:2][249:int][0:size][0:size]`.

5. **Jak callback BookStream dziala bez stuba `BookStreamPrx`?** Przez
   `Ice.Blobject` - generic servant. Rejestruje go pod losowa identity
   na lokalnym adapterze, dostaje `ObjectPrx`, **serializuje to proxy
   recznie** (sekcja 3.2b) jako parametr `findByAuthor`. Serwer wola
   `observer.onNext(book)` - request leci do naszego adaptera, Blobject
   dispatch'uje po `current.operation` ("onNext"/"onCompleted"/"onError").
   Blobject **musi tez obsluzyc** `ice_isA`/`ice_id`/`ice_ids`/`ice_ping`
   bo Ice routuje WSZYSTKO przez `ice_invoke`.

6. **Co dokladnie idzie w bajtach proxy?** Identity (name+category), opcjonalny
   facet, mode, secure, protocol 1.0, encoding 1.1, liczba endpointow,
   per endpoint: typ (short, 1=TCP) + encapsulation (host, port, timeout,
   compress). Sekcja 3.2b ma pelny breakdown 101-bajtowy.

7. **Czemu Result types zamiast Slice exceptions?** Czytelnosc + spojnosc
   (kod typu `if res.errorCode == "NOT_FOUND"` jest jasny). Decoding Ice
   user exception czystym manual marshallingiem wymaga parsowania slice
   flags + type ID + slices per kazdy slice w lancuchu dziedziczenia -
   bardzo skomplikowane, malo wartosci dydaktycznej.

8. **Wady wywolania dynamicznego?** Patrz sekcja 7: zero type safety,
   klient pamieta kontrakt z dokumentacji (cichy dryf jest mozliwy), duzo
   kodu boilerplate. W Pythonie szczegolnie - brak `OutputStream`/`InputStream`
   zmusza do `struct.pack` na piechote, plus encoding proxy i endpoint
   encapsulation tez recznie.

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

### O alternatywach (czego NIE robimy)

15. **Co `Ice.loadSlice` robi w srodku?** Wywoluje wbudowany mcpp preprocesor
    na pliku, parsuje Slice -> AST, traversuje AST i woła `IcePy.defineStruct`,
    `IcePy.defineSequence`, `IcePy.defineProxy` - tworzac klasy Pythona w pamieci.
    Roznica vs `slice2py`: ten sam mechanizm, tylko w runtime zamiast w build time.

16. **Co produkuje `slice2py` - czym sie rozni od `loadSlice`?** `slice2py`
    generuje plik `library_ice.py` zawierajacy te same wywolania `IcePy.defineStruct`
    co `loadSlice` - tylko hardcoded i wykonywane przy `import library_ice`. Roznica
    jest tylko w punkcie czasowym (build vs run).

17. **Czemu wybralem manual marshalling a nie loadSlice?** `loadSlice` formalnie
    dalej kompiluje IDL do pamieci - jest na granicy briefu. Manual marshalling
    nie zostawia zadnej watpliwosci: klient nie ma `.ice`, nie wywoluje `loadSlice`,
    `IcePy.defineStruct` ani podobnych. Plus to bezposrednio dotyka "ograniczenia"
    o ktorym mowi drugi link w briefu (brak `OutputStream`/`InputStream` w Pythonie).

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

25. **Czemu w Pythonie zdecydowales sie na manual marshalling skoro
    OutputStream nie ma?** Bo brak `OutputStream`/`InputStream` w IcePy
    nie znaczy ze pure-DII jest niemozliwy - znaczy tylko ze nie ma
    convenience API. `prx.ice_invoke()` samo w sobie JEST w Pythonie,
    a encapsulation w encoding 1.1 to po prostu `[size:4][1.1:2][payload]`.
    Wiec napisalem `OutBuf`/`InBuf` ktore robia to samo co `OutputStream`/
    `InputStream` z C++, tylko `struct.pack` zamiast metod. Cala roznica
    to ergonomia - wire format identyczny. Plus na server-side (callback)
    `Ice.Blobject` jest w pelni dostepny - tutaj asymetria Python jest **na
    nasza korzysc**.

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
| ergonomia pure-DII w Pythonie | u nas: hand-rolled `OutBuf`/`InBuf` mini-API zamiast `OutputStream`/`InputStream`. W prodzie: zostac przy ICE 3.7 z hand-rolled albo przejsc na inne mapping (C++/Java/C#/JS) ktore maja natywne streaming API. |
| typed proxy bez stuba | u nas: `prx.ice_invoke(...)` z hardkodowanymi nazwami operacji. Alternatywa: `Ice.loadSlice` (kompromis - kontrakt w pliku, ale typed proxy w runtime). |

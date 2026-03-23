# Dzienny plan posiłków (FastAPI)

Prosty projekt studencki: formularz HTML -> backend FastAPI -> jedno publiczne API -> wynik HTML.

## Co robi aplikacja

1. Użytkownik wpisuje: wzrost, wagę, wiek, płeć, aktywność, preferencję.
2. Backend liczy:
   - BMI
   - cel (`redukcja` / `utrzymanie` / `przyrost`)
   - BMR
   - TDEE
   - dzienne kcal
3. Backend pyta **jedno API**: TheMealDB.
4. Dla każdego posiłku (breakfast/lunch/dinner/snack) pobiera dane asynchronicznie i wybiera 1 danie losowo.
5. Robi prosty podział kalorii na makro i zwraca wynik do HTML.

## Użyte API (jedno)

- **TheMealDB**
- Endpoint: `https://www.themealdb.com/api/json/v1/1/search.php?s=<query>`

> Uwaga: TheMealDB nie podaje kalorii i makro. Dlatego makro jest liczone lokalnie (proste oszacowanie).

## Ważne założenia

- Brak fallbacków do lokalnych posiłków.
- Jeśli API nie zwróci danych dla któregoś posiłku, użytkownik dostaje komunikat błędu.
- Losowanie jest niedeterministyczne, więc każde kliknięcie może dać inny plan.
- Serwer robi zapytania do API równolegle (`asyncio.gather` + `httpx.AsyncClient`).
- Walidacja wejścia działa po stronie API (Pydantic).

## Endpointy

- `GET /` – formularz HTML
- `POST /generate` – generowanie planu (formularz lub JSON)
- `POST /api/generate` – REST JSON in/out (najlepszy do Swagger i Postmana)
- `GET /api/external-check?query=chicken` – test połączenia serwer -> API publiczne
- `GET /healthz` – prosty healthcheck (`ok`)

## Jak uruchomić

```bash
python3 -m venv .venv
.venv/bin/pip install -r requierements.txt
.venv/bin/uvicorn backend:app --reload
```

Otwórz:

- `http://127.0.0.1:8000/` – formularz
- `http://127.0.0.1:8000/docs` – Swagger/OpenAPI

## Wymagania z zadania (checklista)

- [x] Testy klient-serwer: SwaggerUI + Postman
- [x] Testy serwer-serwis_publiczny: endpoint `GET /api/external-check`
- [x] Obsługa asynchroniczności: równoległe zapytania do TheMealDB
- [x] Walidacja danych wejściowych: Pydantic (`UserInput`)
- [x] Obsługa błędów API zewnętrznego: zwracamy komunikat 502/422, nie 501

## Gotowe testy do demo

W repo jest gotowa kolekcja Postmana:

- `postman/meal-planner.postman_collection.json`

Scenariusze w kolekcji:

1. Healthcheck (`GET /healthz`)
2. Poprawne generowanie planu (`POST /api/generate`)
3. Walidacja błędnych danych (`POST /api/generate` z niepoprawnym `activity`)
4. Test połączenia z API publicznym (`GET /api/external-check`)

## Przykładowe testy (curl)

### HTML (formularz)

```bash
curl -X POST http://127.0.0.1:8000/generate \
  -d 'height=183' -d 'weight=80' -d 'age=22' \
  -d 'gender=m' -d 'activity=medium' -d 'preference='
```

### JSON (REST)

```bash
curl -X POST 'http://127.0.0.1:8000/api/generate' \
  -H 'Content-Type: application/json' \
  -d '{"height":183,"weight":80,"age":22,"gender":"m","activity":"medium","preference":""}'
```

### Test połączenia z API publicznym

```bash
curl 'http://127.0.0.1:8000/api/external-check?query=chicken'
```

## Struktura plików

- `backend.py` – cała logika backendu
- `templates/index.html` – formularz
- `templates/result.html` – wynik
- `requierements.txt` – zależności

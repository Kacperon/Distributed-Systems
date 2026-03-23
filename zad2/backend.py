from datetime import datetime
import asyncio
import random

import httpx
from fastapi import FastAPI, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, ValidationError, field_validator

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")

templates = Jinja2Templates(directory="templates")

# --- Configuration --------------------------------------------------------
THEMEALDB_SEARCH = "https://www.themealdb.com/api/json/v1/1/search.php"

ACTIVITY_FACTORS = {
    "low": 1.2,
    "medium": 1.55,
    "high": 1.725,
}

MEAL_SPLIT = {
    "breakfast": 0.25,
    "lunch": 0.40,
    "dinner": 0.30,
    "snack": 0.05,
}

QUERIES_BY_PREFERENCE = {
    "default": {
        "breakfast": "egg",
        "lunch": "chicken",
        "dinner": "pasta",
        "snack": "salad",
    },
    "wegetariańska": {
        "breakfast": "porridge",
        "lunch": "vegetarian",
        "dinner": "mushroom",
        "snack": "fruit",
    },
    "bez nabiału": {
        "breakfast": "oatmeal",
        "lunch": "rice",
        "dinner": "fish",
        "snack": "nuts",
    },
    "mięsożerna": {
        "breakfast": "egg",
        "lunch": "beef",
        "dinner": "chicken",
        "snack": "tuna",
    },
}

ALLOWED_PREFERENCES = {"wegetariańska", "bez nabiału", "mięsożerna"}


class UserInput(BaseModel):
    height: float = Field(..., ge=50, le=260, description="Wzrost w cm")
    weight: float = Field(..., ge=20, le=350, description="Waga w kg")
    age: int = Field(..., ge=10, le=120)
    gender: str = Field(..., description="m/f/o")
    activity: str = Field(..., description="low/medium/high")
    preference: str | None = Field(default=None)

    @field_validator("height", "weight", mode="before")
    @classmethod
    def normalize_float(cls, value):
        if value is None:
            return value
        return float(str(value).strip().replace(",", "."))

    @field_validator("age", mode="before")
    @classmethod
    def normalize_int(cls, value):
        if value is None:
            return value
        return int(float(str(value).strip().replace(",", ".")))

    @field_validator("gender", mode="before")
    @classmethod
    def validate_gender(cls, value):
        gender = str(value or "").strip().lower()
        if gender not in {"m", "f", "o"}:
            raise ValueError("gender musi być jednym z: m, f, o")
        return gender

    @field_validator("activity", mode="before")
    @classmethod
    def validate_activity(cls, value):
        activity = str(value or "").strip().lower()
        if activity not in ACTIVITY_FACTORS:
            raise ValueError("activity musi być jednym z: low, medium, high")
        return activity

    @field_validator("preference", mode="before")
    @classmethod
    def validate_preference(cls, value):
        pref = str(value or "").strip().lower()
        if not pref:
            return None
        if pref not in ALLOWED_PREFERENCES:
            raise ValueError("preference musi być jednym z: wegetariańska, bez nabiału, mięsożerna")
        return pref


# --- Health calculations --------------------------------------------------

def calc_bmi(weight_kg: float, height_cm: float) -> float:
    """Body Mass Index"""
    if height_cm <= 0:
        return 0
    return weight_kg / ((height_cm / 100) ** 2)


def estimate_goal(bmi: float) -> str:
    """Estimate goal based on BMI."""
    if bmi < 18.5:
        return "przyrost"
    if bmi >= 25:
        return "redukcja"
    return "utrzymanie"


def calc_bmr(weight_kg: float, height_cm: float, age: int, gender: str) -> float:
    """Mifflin-St Jeor equation."""
    gender = (gender or "").strip().lower()
    if gender.startswith("m"):
        # male
        return 10 * weight_kg + 6.25 * height_cm - 5 * age + 5
    if gender.startswith("f"):
        # female
        return 10 * weight_kg + 6.25 * height_cm - 5 * age - 161
    # fallback average
    return 10 * weight_kg + 6.25 * height_cm - 5 * age - 78


def adjust_calories_for_goal(calories: float, goal: str) -> float:
    """Adjust daily calories for goal (redukcja/przyrost/utrzymanie)."""
    goal = (goal or "").lower()
    if goal == "redukcja":
        return max(1200, calories - 300)
    if goal == "przyrost":
        return calories + 300
    return calories


# --- ThemealDB -------------------------------------------------------------


def build_query(meal_key: str, preference: str) -> str:
    pref = (preference or "").strip().lower()
    pref_queries = QUERIES_BY_PREFERENCE.get(pref) or QUERIES_BY_PREFERENCE["default"]
    return pref_queries.get(meal_key, meal_key)


async def search_themealdb(client: httpx.AsyncClient, query: str, page_size: int = 20) -> list[dict]:
    """Asynchroniczne pobranie listy posiłków z TheMealDB."""
    try:
        response = await client.get(THEMEALDB_SEARCH, params={"s": query}, timeout=8.0)
    except httpx.RequestError as e:
        raise RuntimeError(f"Błąd połączenia z TheMealDB: {e}") from e

    if response.status_code != 200:
        raise RuntimeError(f"TheMealDB zwrócił status {response.status_code}")

    try:
        data = response.json() or {}
    except ValueError as e:
        raise RuntimeError("TheMealDB zwrócił niepoprawny JSON") from e

    meals = data.get("meals") or []
    return meals[:page_size]


async def fetch_meal_candidates(preference: str | None) -> dict[str, list[dict]]:
    """Pobiera równolegle listy kandydatów dla wszystkich posiłków."""
    pref = (preference or "").strip().lower()
    meal_keys = list(MEAL_SPLIT.keys())
    queries = [build_query(meal_key, pref) for meal_key in meal_keys]

    async with httpx.AsyncClient() as client:
        tasks = [search_themealdb(client, query, page_size=20) for query in queries]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    out: dict[str, list[dict]] = {}
    for meal_key, query, result in zip(meal_keys, queries, results):
        if isinstance(result, Exception):
            raise RuntimeError(f"Nie udało się pobrać danych dla '{meal_key}' (fraza: '{query}'): {result}")
        out[meal_key] = result
    return out


def pick_meal_from_products(products: list[dict], seed: int) -> dict | None:
    """Wybór losowego posiłku z listy."""
    if not products:
        return None
    rnd = random.Random(seed)
    return rnd.choice(products)


def estimate_nutrients_from_meal(meal: dict, target_calories: float) -> dict:
    """
    Proste, lokalne oszacowanie makro.
    TheMealDB nie podaje makro, więc liczymy je lokalnie.
    """
    ingredients = 0
    for i in range(1, 21):
        value = (meal.get(f"strIngredient{i}") or "").strip()
        if value:
            ingredients += 1

    protein_pct = min(0.32, 0.18 + ingredients * 0.006)
    fat_pct = 0.27
    carbs_pct = max(0.30, 1 - protein_pct - fat_pct)

    total_pct = protein_pct + fat_pct + carbs_pct
    protein_pct /= total_pct
    fat_pct /= total_pct
    carbs_pct /= total_pct

    return {
        "calories": round(target_calories),
        "protein": round((target_calories * protein_pct) / 4, 1),
        "fat": round((target_calories * fat_pct) / 9, 1),
        "carbs": round((target_calories * carbs_pct) / 4, 1),
    }


def build_meal_description(meal: dict) -> dict:
    """Short description of the meal for UI (category, area, ingredients preview, instructions preview)."""
    ingredients = []
    for i in range(1, 21):
        ing = (meal.get(f"strIngredient{i}") or "").strip()
        mea = (meal.get(f"strMeasure{i}") or "").strip()
        if ing:
            ingredients.append(f"{ing} ({mea})" if mea else ing)

    return {
        "category": meal.get("strCategory") or "brak",
        "area": meal.get("strArea") or "brak",
        "ingredients_preview": ", ".join(ingredients[:5]) if ingredients else "brak",
        "youtube": meal.get("strYoutube") or "",
        "source_url": meal.get("strSource") or "",
    }


async def build_meal_plan(user: UserInput) -> dict:
    """Create a meal plan for a single day."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # Always non-deterministic selection
    seed = random.SystemRandom().randint(1, 10**12)

    # Base calorie and macro targets
    bmi = calc_bmi(user.weight, user.height)
    goal = estimate_goal(bmi)
    bmr = calc_bmr(user.weight, user.height, user.age, user.gender)
    activity_factor = ACTIVITY_FACTORS[user.activity]
    tdee = bmr * activity_factor
    daily_calories = adjust_calories_for_goal(tdee, goal)

    candidates = await fetch_meal_candidates(user.preference)

    meals = []
    total = {"calories": 0, "protein": 0, "fat": 0, "carbs": 0}

    # build each meal type
    for meal_key, ratio in MEAL_SPLIT.items():
        target_cals = daily_calories * ratio
        products = candidates.get(meal_key, [])
        picked = pick_meal_from_products(products, seed + hash(meal_key))

        if picked is None:
            raise ValueError(f"Brak wyników z API dla posiłku: {meal_key}")

        nutrients = estimate_nutrients_from_meal(picked, target_cals)
        details = build_meal_description(picked)
        meal = {
            "name": picked.get("strMeal") or "produkt",
            "target_calories": round(target_cals),
            "calories": nutrients["calories"],
            "protein": nutrients["protein"],
            "fat": nutrients["fat"],
            "carbs": nutrients["carbs"],
            "source": "themealdb",
            "category": details["category"],
            "area": details["area"],
            "ingredients_preview": details["ingredients_preview"],
            "youtube": details["youtube"],
            "source_url": details["source_url"],
        }
        meals.append({"type": meal_key, **meal})
        total["calories"] += meal["calories"]
        total["protein"] += meal["protein"]
        total["fat"] += meal["fat"]
        total["carbs"] += meal["carbs"]

    # Determine macro distribution
    calories_from_protein = total["protein"] * 4
    calories_from_carbs = total["carbs"] * 4
    calories_from_fat = total["fat"] * 9
    total_macro_cals = calories_from_protein + calories_from_carbs + calories_from_fat
    # Avoid division by zero
    def pct(part: float, whole: float) -> float:
        return round(100 * part / whole, 1) if whole > 0 else 0.0

    macro_percentages = {
        "protein": pct(calories_from_protein, total_macro_cals),
        "carbs": pct(calories_from_carbs, total_macro_cals),
        "fat": pct(calories_from_fat, total_macro_cals),
    }

    return {
        "user": user.model_dump(),
        "generated_at": now,
        "bmi": round(bmi, 1),
        "goal": goal,
        "bmr": round(bmr),
        "tdee": round(tdee),
        "daily_calories": round(daily_calories),
        "meals": meals,
        "total": {k: round(v, 1) for k, v in total.items()},
        "macros_pct": macro_percentages,
    }


# --- Routes ---------------------------------------------------------------


def validation_error_to_text(exc: ValidationError) -> str:
    parts = []
    for err in exc.errors():
        field = ".".join(str(x) for x in err.get("loc", []))
        msg = err.get("msg", "niepoprawna wartość")
        parts.append(f"{field}: {msg}")
    return "; ".join(parts)

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    try:
        return templates.TemplateResponse(request, "index.html", {"request": request})
    except Exception as e:
        return templates.TemplateResponse(request, "result.html", {"request": request, "error": str(e)}, status_code=status.HTTP_400_BAD_REQUEST)


@app.post("/generate", response_class=HTMLResponse)
async def generate(request: Request):
    # endpoint HTML/form + opcjonalnie JSON
    try:
        content_type = request.headers.get("content-type", "")
        if content_type.startswith("application/json"):
            raw_data = await request.json()
        else:
            form = await request.form()
            raw_data = dict(form)

        payload = UserInput.model_validate(raw_data)
    except ValidationError as e:
        return templates.TemplateResponse(
            request,
            "result.html",
            {"request": request, "error": f"Błędne dane wejściowe: {validation_error_to_text(e)}"},
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    except Exception as e:
        return templates.TemplateResponse(
            request,
            "result.html",
            {"request": request, "error": f"Błędne dane wejściowe: {e}"},
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    try:
        plan = await build_meal_plan(payload)
    except RuntimeError as e:
        return templates.TemplateResponse(
            "result.html",
            {"request": request, "error": f"Błąd komunikacji z API zewnętrznym: {e}"},
            status_code=status.HTTP_502_BAD_GATEWAY,
        )
    except ValueError as e:
        return templates.TemplateResponse(
            "result.html",
            {"request": request, "error": str(e)},
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )
    except Exception as e:
        return templates.TemplateResponse(
            request,
            "result.html",
            {"request": request, "error": f"Błąd podczas generowania planu: {e}"},
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    # If client wants JSON (REST), return JSON response
    accept_header = request.headers.get("accept", "")
    if accept_header.lower().startswith("application/json") or request.query_params.get("format") == "json":
        return JSONResponse(plan)

    return templates.TemplateResponse(request, "result.html", {"request": request, "plan": plan})


@app.post("/api/generate")
async def api_generate(payload: UserInput):
    """REST endpoint pod Swagger/Postman (JSON in -> JSON out)."""
    try:
        plan = await build_meal_plan(payload)
    except RuntimeError as e:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Błąd komunikacji z API zewnętrznym: {e}")
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Błąd serwera: {e}")
    return plan


@app.get("/api/external-check")
async def external_check(query: str = Query("chicken", min_length=2, max_length=40)):
    """Test połączenia serwer -> serwis publiczny (TheMealDB)."""
    try:
        async with httpx.AsyncClient() as client:
            meals = await search_themealdb(client, query, page_size=5)
    except RuntimeError as e:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(e))

    return {
        "query": query,
        "upstream": "themealdb",
        "count": len(meals),
        "sample": [m.get("strMeal") for m in meals[:3]],
    }


@app.get("/healthz", response_class=PlainTextResponse)
async def healthz():
    return "ok"


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("backend:app", host="0.0.0.0", port=5000, reload=True)

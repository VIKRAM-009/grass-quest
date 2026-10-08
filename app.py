#!/usr/bin/env python3
"""Grass Quest - a daily outdoor mission generator and photo checker.

It uses open-weight models through Ollama:
  - a local text model writes today's mission from the real weather
  - a vision model looks at your photos and checks the mission
    (Gemma 4 on Ollama Cloud by default, or a local one like qwen2.5vl:3b)
Open it on your phone (same WiFi) and the screen time stays tiny.
"""

import base64
import io
import json
import os
import re
import urllib.parse
import urllib.request
from datetime import date, timedelta

from flask import Flask, redirect, render_template_string, request, url_for
from PIL import Image, ImageOps

OLLAMA = "http://localhost:11434/api/chat"
TEXT_MODEL = os.environ.get("TEXT_MODEL", "llama3.2")
VISION_MODEL = os.environ.get("VISION_MODEL", "gemma4:31b-cloud")
DATA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "quest_data.json")

WEATHER_CODES = {
    0: "clear sky", 1: "mostly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "fog", 51: "light drizzle", 53: "drizzle", 55: "heavy drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain", 71: "light snow", 73: "snow",
    75: "heavy snow", 80: "rain showers", 81: "rain showers", 82: "violent rain showers",
    95: "thunderstorm", 96: "thunderstorm with hail", 99: "thunderstorm with hail",
}

app = Flask(__name__)


# ---------- storage ----------

def load():
    try:
        with open(DATA_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"city": None, "points": 0, "streak": 0, "last_done": None, "quests": {}, "history": []}


def save(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)


# ---------- open APIs and local models ----------

def get_json(url):
    with urllib.request.urlopen(url, timeout=15) as resp:
        return json.loads(resp.read())


def find_city(name):
    q = urllib.parse.urlencode({"name": name, "count": 1})
    results = get_json(f"https://geocoding-api.open-meteo.com/v1/search?{q}").get("results")
    if not results:
        return None
    r = results[0]
    return {"name": r["name"], "country": r.get("country", ""), "lat": r["latitude"], "lon": r["longitude"]}


def get_weather(city):
    q = urllib.parse.urlencode({
        "latitude": city["lat"], "longitude": city["lon"], "timezone": "auto",
        "current": "temperature_2m,precipitation,weather_code,wind_speed_10m",
        "daily": "sunrise,sunset,temperature_2m_max,precipitation_probability_max",
        "forecast_days": 1,
    })
    w = get_json(f"https://api.open-meteo.com/v1/forecast?{q}")
    cur, day = w["current"], w["daily"]
    return {
        "temp": round(cur["temperature_2m"]),
        "max_temp": round(day["temperature_2m_max"][0]),
        "sky": WEATHER_CODES.get(cur["weather_code"], "unknown"),
        "rain_chance": day["precipitation_probability_max"][0],
        "wind": round(cur["wind_speed_10m"]),
        "sunrise": day["sunrise"][0][-5:],
        "sunset": day["sunset"][0][-5:],
    }


def ask_model(model, prompt, schema, images=None):
    """Ask an Ollama model and get JSON back in the exact shape of `schema`."""
    msg = {"role": "user", "content": prompt}
    if images:
        msg["images"] = images
    body = json.dumps({"model": model, "messages": [msg], "stream": False, "format": schema,
                       "options": {"temperature": 0.8}}).encode()
    req = urllib.request.Request(OLLAMA, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=600) as resp:
        content = json.loads(resp.read())["message"]["content"]
    # Some models wrap the JSON in ```json ... ``` fences, so cut them off.
    content = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", content)
    return json.loads(content)


QUEST_PROMPT = """You create one short, fun outdoor mission for today. The goal is to get someone
off their screen and outside for 15-45 minutes, near their home, for free.

Place: {city}
Weather now: {sky}, {temp}°C (max {max_temp}°C), rain chance {rain_chance}%, wind {wind} km/h
Sunrise {sunrise}, sunset {sunset}
Recent missions (do NOT repeat these): {recent}

Rules:
- Fit the weather. If it is rainy or very hot, pick something short, safe and shaded.
- The mission must end with 1-3 photos that prove it was done (nature, sky, trees, plants, birds, paths...).
- Never ask for photos of people's faces or private property.

Reply with JSON only:
{{"title": "short fun name", "mission": "one sentence", "steps": ["step 1", "step 2", "step 3"],
  "photo_goal": "exactly what the photos should show", "minutes": 30,
  "best_time": "when to go today", "tip": "one safety or comfort tip"}}"""

CHECK_PROMPT = """You are the friendly judge of an outdoor photo mission.
Mission: {mission}
The photo should show: {photo_goal}

Look at this photo carefully. Reply with JSON only:
{{"what_i_see": "one sentence describing the photo",
  "outdoors": true or false,
  "matches": true or false,
  "points": 0-10 (10 = clearly completes the photo goal, 0 = unrelated or a screen/indoor photo),
  "comment": "one short, encouraging sentence"}}"""


def schema(**props):
    return {"type": "object", "properties": props, "required": list(props)}


QUEST_SCHEMA = schema(title={"type": "string"}, mission={"type": "string"},
                      steps={"type": "array", "items": {"type": "string"}}, photo_goal={"type": "string"},
                      minutes={"type": "integer"}, best_time={"type": "string"}, tip={"type": "string"})
CHECK_SCHEMA = schema(what_i_see={"type": "string"}, outdoors={"type": "boolean"}, matches={"type": "boolean"},
                      points={"type": "integer"}, comment={"type": "string"})


def make_quest(city, weather, recent):
    q = ask_model(TEXT_MODEL, QUEST_PROMPT.format(city=city["name"], recent=recent or "none", **weather), QUEST_SCHEMA)
    steps = q.get("steps") or []
    return {
        "title": str(q.get("title", "Go Outside")),
        "mission": str(q.get("mission", "Take a short walk and notice three new things.")),
        # Small models sometimes leak JSON keys like `photo_goal):` into a step, so drop those.
        "steps": [re.sub(r"^\s*(step\s*)?\d+[:.)]\s*", "", str(s), flags=re.I) for s in steps
                  if not re.search(r"\b(photo_goal|best_time|minutes|tip)\b\W*[:)]", str(s))][:4],
        "photo_goal": str(q.get("photo_goal", "something interesting you found outside")),
        "minutes": q.get("minutes", 30),
        "best_time": str(q.get("best_time", "")),
        "tip": str(q.get("tip", "")),
    }


def photo_to_b64(file):
    """Shrink phone photos so the vision model runs fast on a laptop CPU."""
    img = ImageOps.exif_transpose(Image.open(file.stream)).convert("RGB")
    img.thumbnail((768, 768))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=85)
    b64 = base64.b64encode(buf.getvalue()).decode()
    return b64, "data:image/jpeg;base64," + b64


def check_photo(quest, b64):
    r = ask_model(VISION_MODEL, CHECK_PROMPT.format(**quest), CHECK_SCHEMA, images=[b64])
    try:
        points = max(0, min(10, int(r.get("points", 0))))
    except (TypeError, ValueError):
        points = 0
    if r.get("outdoors") is False:
        points = min(points, 2)
    return {"what_i_see": r.get("what_i_see", ""), "matches": bool(r.get("matches")),
            "points": points, "comment": r.get("comment", "")}


# ---------- pages ----------

PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Grass Quest</title>
<style>
  body { font-family: system-ui, sans-serif; margin: 0; background: #f2f7ee; color: #1d2b1a; }
  main { max-width: 520px; margin: 0 auto; padding: 16px; }
  h1 { margin: 8px 0 4px; font-size: 1.6rem; } h2 { margin: 0 0 8px; font-size: 1.3rem; }
  .card { background: #fff; border-radius: 14px; padding: 16px; margin: 12px 0; box-shadow: 0 1px 4px #0001; }
  .muted { color: #5b6b57; font-size: .9rem; }
  .stats { display: flex; gap: 10px; } .stats div { flex: 1; text-align: center; background: #fff; border-radius: 12px; padding: 10px; }
  .stats b { display: block; font-size: 1.4rem; color: #2f7d32; }
  button, .btn { display: block; width: 100%; padding: 14px; border: 0; border-radius: 12px; font-size: 1rem;
                 background: #2f7d32; color: #fff; text-align: center; text-decoration: none; margin-top: 10px; }
  .btn.light { background: #e3eedd; color: #1d2b1a; }
  input[type=text] { width: 100%; box-sizing: border-box; padding: 12px; border-radius: 10px; border: 1px solid #c5d3bf; font-size: 1rem; }
  input[type=file] { width: 100%; margin-top: 8px; }
  img.photo { width: 100%; border-radius: 10px; margin-bottom: 8px; }
  .pass { color: #2f7d32; font-weight: 600; } .fail { color: #b3541e; font-weight: 600; }
  ol { padding-left: 20px; } li { margin: 4px 0; }
  .error { background: #fde8e4; color: #8a2a12; }
</style>
<script>function busy(f, msg){ f.querySelector('button').innerText = msg; f.querySelector('button').disabled = true; }</script>
</head><body><main>
<h1>🌿 Grass Quest</h1>
{% if error %}<div class="card error">{{ error }}</div>{% endif %}

{% if not data.city %}
  <div class="card"><h2>Where do you live?</h2>
  <form method="post" action="{{ url_for('set_city') }}" onsubmit="busy(this, 'Finding...')">
    <input type="text" name="city" placeholder="e.g. Dehradun" required>
    <button>Start</button></form></div>

{% elif results %}
  <div class="card"><h2>{{ total }} points earned {% if total >= 10 %}🎉{% endif %}</h2>
  <p class="muted">{% if passed %}Quest complete! Your streak is {{ data.streak }} day(s).{% else %}Not quite. Look at the hints below and try again.{% endif %}</p></div>
  {% for r in results %}<div class="card">
    <img class="photo" src="{{ r.src }}">
    <p class="{{ 'pass' if r.matches else 'fail' }}">{{ '✅' if r.matches else '❌' }} {{ r.points }}/10</p>
    <p>{{ r.what_i_see }}</p><p class="muted">{{ r.comment }}</p></div>{% endfor %}
  <a class="btn" href="{{ url_for('home') }}">Back to today's quest</a>

{% else %}
  <div class="stats"><div><b>{{ data.points }}</b>points</div><div><b>{{ data.streak }}</b>day streak</div>
    <div><b>{{ data.history|length }}</b>quests done</div></div>
  <div class="card"><p class="muted">📍 {{ data.city.name }} · {{ weather.sky }}, {{ weather.temp }}°C ·
    rain {{ weather.rain_chance }}% · sunset {{ weather.sunset }}</p>
    <h2>{{ quest.title }}</h2><p>{{ quest.mission }}</p>
    <ol>{% for s in quest.steps %}<li>{{ s }}</li>{% endfor %}</ol>
    <p><b>📸 Bring back:</b> {{ quest.photo_goal }}</p>
    <p class="muted">⏱ about {{ quest.minutes }} min{% if quest.best_time %} · 🕒 {{ quest.best_time }}{% endif %}</p>
    {% if quest.tip %}<p class="muted">💡 {{ quest.tip }}</p>{% endif %}
    {% if done_today %}<p class="pass">✅ Done today. See you tomorrow!</p>{% endif %}
  </div>
  <div class="card"><h2>Back from outside?</h2>
  <p class="muted">Now put the phone away and go. Come back with up to 3 photos.</p>
  <form method="post" action="{{ url_for('check') }}" enctype="multipart/form-data" onsubmit="busy(this, 'Judging your photos... (up to a minute)')">
    <input type="file" name="photos" accept="image/*" multiple required>
    <button>Check my quest</button></form></div>
  <form method="post" action="{{ url_for('new_quest') }}" onsubmit="busy(this, 'Thinking...')"><button class="btn light">Give me a different quest</button></form>
  <form method="post" action="{{ url_for('reset_city') }}"><button class="btn light">Change city</button></form>
{% endif %}
</main></body></html>"""


def todays_quest(data, fresh=False):
    today = date.today().isoformat()
    weather = get_weather(data["city"])
    if fresh or today not in data["quests"]:
        recent = "; ".join(q["title"] for q in list(data["quests"].values())[-5:])
        data["quests"][today] = make_quest(data["city"], weather, recent)
        save(data)
    return data["quests"][today], weather


@app.route("/")
def home(error=None):
    data = load()
    if not data["city"]:
        return render_template_string(PAGE, data=data, error=error)
    try:
        quest, weather = todays_quest(data)
    except OSError as e:
        return render_template_string(PAGE, data={"city": None}, error=f"Could not reach the weather API or Ollama: {e}")
    return render_template_string(PAGE, data=data, quest=quest, weather=weather, error=error,
                                  done_today=data["last_done"] == date.today().isoformat())


@app.post("/city")
def set_city():
    data = load()
    city = find_city(request.form["city"].strip())
    if not city:
        return render_template_string(PAGE, data=data, error="City not found. Try a bigger town nearby.")
    data["city"] = city
    save(data)
    return redirect(url_for("home"))


@app.post("/reset-city")
def reset_city():
    data = load()
    data["city"] = None
    save(data)
    return redirect(url_for("home"))


@app.post("/new")
def new_quest():
    data = load()
    todays_quest(data, fresh=True)
    return redirect(url_for("home"))


@app.post("/check")
def check():
    data = load()
    quest, _ = todays_quest(data)
    results = []
    for file in request.files.getlist("photos")[:3]:
        try:
            b64, src = photo_to_b64(file)
        except Exception:
            results.append({"src": "", "what_i_see": "I couldn't open this file. Try a JPG or PNG photo.",
                            "matches": False, "points": 0, "comment": ""})
            continue
        results.append({"src": src, **check_photo(quest, b64)})

    total = sum(r["points"] for r in results)
    passed = any(r["matches"] for r in results)
    today = date.today().isoformat()
    if passed and data["last_done"] != today:
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        data["streak"] = data["streak"] + 1 if data["last_done"] == yesterday else 1
        data["last_done"] = today
        data["points"] += total
        data["history"].append({"date": today, "title": quest["title"], "points": total})
        save(data)
    return render_template_string(PAGE, data=data, results=results, total=total, passed=passed)


if __name__ == "__main__":
    # 0.0.0.0 lets your phone open it on the same WiFi: http://<laptop-ip>:5000
    app.run(host="0.0.0.0", port=5000)

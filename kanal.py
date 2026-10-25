#!/usr/bin/env python3
"""
AI-Hekaye kanal (alman dilində, uydurma mif/dram videoları)

İstifadə:
  python kanal.py library            # şəkil kitabxanasını genişləndirir (Cloudflare)
  python kanal.py video  [--minutes 10] [--offline]   # hekayə + səs + video yaradır
  python kanal.py upload             # output/ qovluğundakı videonu YouTube-a yükləyir
"""
import argparse
import asyncio
import base64
import io
import json
import math
import os
import random
import re
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent
IMG_DIR = ROOT / "images"
STATE_FILE = ROOT / "state" / "used_topics.json"
OUT_DIR = ROOT / "output"
WORK_DIR = ROOT / "work"

W, H, FPS = 1280, 720, 25
CHUNK_SECONDS = 3.2          # təxminən hər 3 saniyədən bir şəkil dəyişir
VOICE = os.environ.get("TTS_VOICE", "de-DE-ConradNeural")
VARIANTS = 3                 # hər (personaj, emosiya) üçün neçə şəkil

# ----------------------------------------------------------------------------
# Personajlar və emosiyalar (şəkil kitabxanası bunlardan qurulur)
# ----------------------------------------------------------------------------
STYLE = ("manhwa style, korean webtoon illustration, bold black outlines, dramatic cel shading, "
         "highly detailed, expressive face, upper body portrait, soft blurred background")
SCENE_STYLE = ("manhwa style, korean webtoon illustration, bold black outlines, dramatic lighting, "
               "highly detailed, wide establishing shot, no people")

EMOTIONS = {
    "wuetend": "furious, angry shouting, clenched teeth, veins on forehead",
    "ueberrascht": "surprised, wide open eyes and mouth",
    "traurig": "sad, downcast eyes, sorrowful expression",
    "lachend": "laughing out loud, big smile",
    "aengstlich": "terrified, trembling, fearful eyes, sweat drops",
    "entschlossen": "determined, fierce focused gaze, confident smirk",
    "ruhig": "calm neutral expression, gentle look",
    "spoettisch": "mocking sneer, smug contemptuous grin",
    "schockiert": "shocked, pale face, jaw dropped",
    "weinend": "crying, tears streaming down the face",
}

CHARACTERS = {
    "held": "young man in his twenties, short messy black hair, plain worn brown tunic, thin but strong build",
    "heldin": "young woman in her twenties, long straight black hair, small wooden hairpin, modest light blue dress",
    "schurke": "arrogant middle-aged man, slicked back grey hair, sharp cruel features, expensive dark red robe",
    "meister": "old wise man, long white beard, kind wrinkled eyes, grey traditional robe",
    "dorfbewohner": "ordinary village man, rough face, straw hat, patched clothes",
    "herrscher": "powerful ruler, strict face, black hair tied up, ornate golden crown and robe",
}

SCENES = {
    "dorf": "small old village with wooden houses at sunset",
    "feld": "dry barren field with cracked earth under a pale sky",
    "nacht": "dark forest at night with moonlight and mist",
    "berg": "huge misty mountains with a narrow path",
    "burg": "grand ancient castle on a cliff",
    "sturm": "dramatic storm with lightning over the land",
    "gold": "glowing golden treasure hidden in a cave",
    "quelle": "magical glowing spring of water in a rocky valley",
}

ALL_SCENE = "szene"


def all_items():
    """(char, emotion, prompt) siyahısı"""
    items = []
    for c, desc in CHARACTERS.items():
        for e, ep in EMOTIONS.items():
            items.append((c, e, f"{STYLE}, {desc}, {ep}"))
    for s, sp in SCENES.items():
        items.append((ALL_SCENE, s, f"{SCENE_STYLE}, {sp}"))
    return items


# ----------------------------------------------------------------------------
# Kitabxana (Cloudflare Workers AI ilə şəkil yaratma)
# ----------------------------------------------------------------------------
class RateLimit(Exception):
    pass


def cf_image(prompt):
    acc = os.environ["CLOUDFLARE_ACCOUNT_ID"].strip()
    tok = os.environ["CLOUDFLARE_API_TOKEN"].strip()
    models = [
        ("@cf/black-forest-labs/flux-1-schnell", {"prompt": prompt, "steps": 6}),
        ("@cf/stabilityai/stable-diffusion-xl-base-1.0", {"prompt": prompt, "num_steps": 20}),
    ]
    last = ""
    for model, payload in models:
        url = f"https://api.cloudflare.com/client/v4/accounts/{acc}/ai/run/{model}"
        try:
            r = requests.post(url, headers={"Authorization": f"Bearer {tok}"}, json=payload, timeout=180)
        except requests.RequestException as ex:
            last = str(ex)
            continue
        text = r.text[:300] if "image" not in r.headers.get("content-type", "") else ""
        if r.status_code == 429 or "daily free allocation" in text.lower() or "neurons" in text.lower():
            raise RateLimit(text)
        if r.status_code != 200:
            last = f"{model}: {r.status_code} {text}"
            continue
        if "image" in r.headers.get("content-type", ""):
            return r.content
        try:
            return base64.b64decode(r.json()["result"]["image"])
        except Exception as ex:  # noqa
            last = f"{model}: parse error {ex}"
    raise RuntimeError(last)


def cmd_library(max_per_run):
    IMG_DIR.mkdir(exist_ok=True)
    todo = []
    items = all_items()
    for v in range(1, VARIANTS + 1):          # əvvəlcə hamısının 1-ci variantı
        for c, e, p in items:
            path = IMG_DIR / c / f"{e}_{v}.jpg"
            if not path.exists():
                todo.append((path, p + f", variation {v}"))
    print(f"Kitabxanada çatışmayan şəkil: {len(todo)}. Bu işdə maksimum {max_per_run}.")
    made = 0
    for path, prompt in todo[:max_per_run]:
        try:
            data = cf_image(prompt)
        except RateLimit as ex:
            print("Gündəlik pulsuz limit doldu, dayanıram:", str(ex)[:120])
            break
        except Exception as ex:  # noqa
            print("Şəkil xətası, keçirəm:", str(ex)[:200])
            time.sleep(5)
            continue
        try:
            img = Image.open(io.BytesIO(data)).convert("RGB").resize((1024, 1024))
        except Exception as ex:  # noqa
            print("Şəkil oxunmadı:", ex)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        img.save(path, "JPEG", quality=88)
        made += 1
        print("OK", path.relative_to(ROOT))
        time.sleep(1.5)
    print(f"Bu işdə yaradıldı: {made}")


def load_library():
    lib = {}
    for p in sorted(IMG_DIR.glob("*/*_*.jpg")):
        c = p.parent.name
        e = p.stem.rsplit("_", 1)[0]
        lib.setdefault((c, e), []).append(p)
    return lib


class Picker:
    def __init__(self, lib, offline_dir):
        self.lib = lib
        self.last_used = {}
        self.counter = 0
        self.offline_dir = offline_dir

    def _placeholder(self, c, e):
        self.offline_dir.mkdir(parents=True, exist_ok=True)
        p = self.offline_dir / f"{c}_{e}.jpg"
        if not p.exists():
            rnd = random.Random(f"{c}{e}")
            img = Image.new("RGB", (1024, 1024), (rnd.randint(40, 200), rnd.randint(40, 200), rnd.randint(40, 200)))
            d = ImageDraw.Draw(img)
            try:
                f = ImageFont.truetype(find_font(), 72)
            except Exception:  # noqa
                f = ImageFont.load_default()
            d.text((60, 440), f"{c}\n{e}", fill="white", font=f)
            img.save(p, "JPEG", quality=85)
        return p

    def pick(self, c, e):
        cands = self.lib.get((c, e))
        if not cands:
            cands = [p for (cc, _), ps in self.lib.items() if cc == c for p in ps]
        if not cands:
            cands = [p for ps in self.lib.values() for p in ps]
        if not cands:
            return self._placeholder(c, e)
        self.counter += 1
        best = min(cands, key=lambda p: self.last_used.get(p, -1))
        self.last_used[best] = self.counter
        return best


# ----------------------------------------------------------------------------
# Hekayə (Groq)
# ----------------------------------------------------------------------------
def groq_chat(messages, max_tokens=4096):
    key = os.environ["GROQ_API_KEY"].strip()
    models = [m for m in [os.environ.get("GROQ_MODEL"), "llama-3.3-70b-versatile",
                          "openai/gpt-oss-120b", "qwen/qwen3-32b", "llama-3.1-8b-instant"] if m]
    last = ""
    for m in models:
        for attempt in range(4):
            try:
                r = requests.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers={"Authorization": f"Bearer {key}"},
                    json={"model": m, "messages": messages, "temperature": 0.9,
                          "max_tokens": max_tokens, "response_format": {"type": "json_object"}},
                    timeout=240,
                )
            except requests.RequestException as ex:
                last = str(ex)
                time.sleep(10)
                continue
            if r.status_code == 200:
                return r.json()["choices"][0]["message"]["content"]
            last = f"{m}: {r.status_code} {r.text[:200]}"
            if r.status_code == 401:
                raise RuntimeError("Groq açarı yanlışdır: " + last)
            if r.status_code in (429, 500, 502, 503):
                wait = r.headers.get("retry-after")
                time.sleep(min(float(wait) if wait else 20 * (attempt + 1), 90))
                continue
            break  # model problemi -> növbəti model
        print("Groq modeli keçildi:", last)
    raise RuntimeError("Groq cavab vermədi: " + last)


CF_TEXT_MODELS = [
    "@cf/meta/llama-3.3-70b-instruct-fp8-fast",
    "@cf/mistralai/mistral-small-3.1-24b-instruct",
    "@cf/openai/gpt-oss-120b",
    "@cf/meta/llama-3.1-8b-instruct",
]


def _cf_text_once(model, messages, max_tokens):
    acc = os.environ["CLOUDFLARE_ACCOUNT_ID"].strip()
    tok = os.environ["CLOUDFLARE_API_TOKEN"].strip()
    url = f"https://api.cloudflare.com/client/v4/accounts/{acc}/ai/run/{model}"
    last = ""
    for attempt in range(4):
        try:
            r = requests.post(url, headers={"Authorization": f"Bearer {tok}"},
                              json={"messages": messages, "max_tokens": max_tokens, "temperature": 0.9},
                              timeout=300)
        except requests.RequestException as ex:
            last = str(ex)
            time.sleep(10)
            continue
        body = r.text[:300]
        if r.status_code == 429 or "daily free allocation" in body.lower():
            raise RateLimit(body)
        if r.status_code == 200:
            res = r.json().get("result") or {}
            text = res.get("response")
            if text is None and res.get("choices"):
                text = res["choices"][0].get("message", {}).get("content")
            if isinstance(text, (dict, list)):
                text = json.dumps(text, ensure_ascii=False)
            if text and str(text).strip():
                return str(text)
            last = "boş cavab: " + body
        else:
            last = f"{r.status_code} {body}"
            if r.status_code in (400, 404):
                break
        time.sleep(15 * (attempt + 1))
    raise RuntimeError(f"{model}: {last}")


_CF_GOOD = []


def cf_chat(messages, max_tokens=4096):
    acc = os.environ["CLOUDFLARE_ACCOUNT_ID"].strip()
    tok = os.environ["CLOUDFLARE_API_TOKEN"].strip()
    models = [m for m in _CF_GOOD + [os.environ.get("CF_TEXT_MODEL")] + CF_TEXT_MODELS if m]
    tried, last = set(), ""
    discovered = False
    while models:
        m = models.pop(0)
        if m in tried:
            continue
        tried.add(m)
        try:
            out = _cf_text_once(m, messages, max_tokens)
            _CF_GOOD[:] = [m]
            return out
        except RateLimit:
            raise
        except Exception as ex:  # noqa
            last = str(ex)
            print("Cloudflare modeli keçildi:", last[:200])
        if not models and not discovered:
            discovered = True
            try:  # mövcud mətn modellərini özü tapır
                r = requests.get(
                    f"https://api.cloudflare.com/client/v4/accounts/{acc}/ai/models/search",
                    headers={"Authorization": f"Bearer {tok}"},
                    params={"task": "Text Generation", "per_page": 100}, timeout=60)
                names = [x.get("name", "") for x in r.json().get("result", [])]
                good = [n for n in names if re.search(r"llama-3\.3|llama-3\.1-70b|gpt-oss|mistral-small|qwen|gemma", n)]
                models = [n for n in good if n not in tried][:5]
            except Exception as ex:  # noqa
                print("Model siyahısı alınmadı:", ex)
    raise RuntimeError("Cloudflare mətn modeli işləmədi: " + last)


def gemini_chat(messages, max_tokens=4096):
    key = os.environ["GEMINI_API_KEY"].strip()
    text = "\n\n".join(m["content"] for m in messages)
    last = ""
    for model in [os.environ.get("GEMINI_MODEL"), "gemini-2.5-flash", "gemini-flash-latest"]:
        if not model:
            continue
        for attempt in range(3):
            r = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                headers={"x-goog-api-key": key},
                json={"contents": [{"role": "user", "parts": [{"text": text}]}],
                      "generationConfig": {"maxOutputTokens": max_tokens, "temperature": 0.9,
                                           "responseMimeType": "application/json"}},
                timeout=240)
            if r.status_code == 200:
                try:
                    return r.json()["candidates"][0]["content"]["parts"][0]["text"]
                except Exception:  # noqa
                    last = "boş cavab"
                    break
            last = f"{model}: {r.status_code} {r.text[:200]}"
            if r.status_code in (429, 500, 503):
                time.sleep(20 * (attempt + 1))
                continue
            break
    raise RuntimeError("Gemini cavab vermədi: " + last)


def llm_chat(messages, max_tokens=4096):
    """Mətn provayderləri ardıcıllıqla: Groq (açar varsa) -> Cloudflare -> Gemini (açar varsa)"""
    providers = []
    if os.environ.get("GROQ_API_KEY", "").strip():
        providers.append(("Groq", groq_chat))
    if os.environ.get("CLOUDFLARE_API_TOKEN", "").strip():
        providers.append(("Cloudflare", cf_chat))
    if os.environ.get("GEMINI_API_KEY", "").strip():
        providers.append(("Gemini", gemini_chat))
    errors = []
    for name, fn in providers:
        try:
            out = fn(messages, max_tokens)
            print("Mətn provayderi:", name)
            return out
        except Exception as ex:  # noqa
            errors.append(f"{name}: {str(ex)[:300]}")
            print("Provayder işləmədi ->", errors[-1])
    raise RuntimeError("Heç bir mətn provayderi işləmədi. " + " | ".join(errors))


def parse_json(text):
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, flags=re.S)
        if m:
            return json.loads(m.group(0))
        raise


def used_topics():
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:  # noqa
        return []


def save_topic(title):
    topics = used_topics()
    topics.append(title)
    STATE_FILE.parent.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(topics[-300:], ensure_ascii=False, indent=1))


CHAR_HELP = (
    "Figuren-IDs: held (männlicher Protagonist), heldin (weibliche Hauptfigur), schurke (Gegenspieler), "
    "meister (alter weiser Mann), dorfbewohner (spöttisches Volk), herrscher (mächtiger Herrscher), "
    f"szene (Landschaft/Ort; dann ist 'e' eine der Szenen: {', '.join(SCENES)}). "
    f"Emotionen für Personen: {', '.join(EMOTIONS)}."
)


def make_story(minutes):
    parts_n = max(1, math.ceil(minutes / 5))
    sys_msg = ("Du bist ein packender Geschichtenerzähler für einen deutschen YouTube-Kanal. "
               "Du schreibst frei erfundene Mythen, Legenden und Dramen auf Deutsch. "
               "Antworte immer ausschließlich als gültiges JSON.")
    plan_prompt = (
        f"Erfinde eine völlig neue, frei erfundene Geschichte (Mythos/Legende/Drama), z.B. ein Underdog, "
        f"über den alle lachen, ein verborgenes Geheimnis, späte Genugtuung. Bereits verwendet (nicht wiederholen): "
        f"{used_topics()[-40:]}. Plane {parts_n} Teile. Gib JSON: "
        '{"title": "...", "description": "2-3 Sätze, spannend, ohne Spoiler", '
        '"tags": ["..."], "parts": ["Ein Satz Zusammenfassung pro Teil"]}. '
        "Der Titel soll neugierig machen (max. 80 Zeichen)."
    )
    plan = parse_json(llm_chat([{"role": "system", "content": sys_msg}, {"role": "user", "content": plan_prompt}], 2000))
    plan["parts"] = (plan.get("parts") or [])[:parts_n]
    while len(plan["parts"]) < parts_n:
        plan["parts"].append("Die Geschichte entwickelt sich weiter und steuert auf das Ende zu.")
    sentences, prev_tail = [], ""
    for i, summary in enumerate(plan["parts"], 1):
        last = i == parts_n
        p = (
            f"Titel: {plan['title']}\nGesamtplan: {json.dumps(plan['parts'], ensure_ascii=False)}\n"
            f"Schreibe jetzt Teil {i} von {parts_n}: {summary}\n"
            f"Bisheriges Ende: {prev_tail or '(Anfang der Geschichte)'}\n"
            f"{'Dies ist der letzte Teil: bringe die Geschichte zu einem befriedigenden Ende. ' if last else 'Ende mit einem Spannungsmoment. '}"
            "Schreibe 55-70 kurze, gesprochene Sätze (je max. 22 Wörter), lebendig und emotional, nur Deutsch. "
            "Erfinde passende Namen. Zu jedem Satz gehört eine Figur 'c' und Emotion/Szene 'e'. "
            f"{CHAR_HELP} Gib JSON: " '{"sentences": [{"t": "Satz", "c": "held", "e": "wuetend"}]}'
        )
        data = parse_json(llm_chat([{"role": "system", "content": sys_msg}, {"role": "user", "content": p}]))
        part = []
        for s in data.get("sentences", []):
            t = str(s.get("t", "")).strip()
            c, e = str(s.get("c", "")).strip(), str(s.get("e", "")).strip()
            if not t:
                continue
            if c == ALL_SCENE:
                if e not in SCENES:
                    e = random.choice(list(SCENES))
            else:
                if c not in CHARACTERS:
                    c = "held"
                if e not in EMOTIONS:
                    e = "ruhig"
            part.append({"t": t, "c": c, "e": e})
        if not part:
            raise RuntimeError(f"Teil {i} boş gəldi")
        sentences += part
        prev_tail = " ".join(x["t"] for x in part[-3:])
        print(f"Teil {i}/{parts_n}: {len(part)} Sätze")
    return plan, sentences


def fake_story():
    plan = {"title": "Test: Das leere Feld", "description": "Testvideo.", "tags": ["test"], "parts": ["x"]}
    s = [
        ("Alle lachten über den jungen Mann, der das leere Feld gekauft hatte.", "dorfbewohner", "spoettisch"),
        ("Er sagte nichts und schaute nur auf die trockene Erde.", "held", "ruhig"),
        ("In der Nacht begann er zu graben, ganz allein.", "szene", "nacht"),
        ("Plötzlich hörte er ein seltsames Geräusch aus der Tiefe, das ihm Angst machte.", "held", "aengstlich"),
        ("Dort unten glänzte etwas Goldenes im Mondlicht.", "szene", "gold"),
        ("Am nächsten Morgen standen alle Dorfbewohner sprachlos vor dem Feld.", "dorfbewohner", "schockiert"),
    ]
    return plan, [{"t": t, "c": c, "e": e} for t, c, e in s]


# ----------------------------------------------------------------------------
# Səs (edge-tts)
# ----------------------------------------------------------------------------
def run(cmd, **kw):
    r = subprocess.run(cmd, capture_output=True, text=True, **kw)
    if r.returncode != 0:
        raise RuntimeError("Komanda xətası: " + " ".join(map(str, cmd[:6])) + "\n" + r.stderr[-1500:])
    return r


def probe_duration(path):
    r = run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)])
    return float(r.stdout.strip())


async def _tts_all(texts, paths):
    import edge_tts
    sem = asyncio.Semaphore(4)

    async def one(t, p):
        async with sem:
            for attempt in range(4):
                try:
                    await edge_tts.Communicate(t, VOICE).save(str(p))
                    return
                except Exception as ex:  # noqa
                    if attempt == 3:
                        raise
                    await asyncio.sleep(3 * (attempt + 1))

    await asyncio.gather(*(one(t, p) for t, p in zip(texts, paths)))


def make_audio(sentences, offline):
    """Hər cümlə üçün wav yaradır, müddətləri qaytarır."""
    adir = WORK_DIR / "audio"
    adir.mkdir(parents=True, exist_ok=True)
    mp3s = [adir / f"s{i:04d}.mp3" for i in range(len(sentences))]
    if not offline:
        asyncio.run(_tts_all([s["t"] for s in sentences], mp3s))
    wavs, durs = [], []
    for i, s in enumerate(sentences):
        wav = adir / f"s{i:04d}.wav"
        if offline:
            d = max(1.5, len(s["t"].split()) / 2.4)
            run(["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono", "-t", f"{d:.3f}", str(wav)])
        else:
            run(["ffmpeg", "-y", "-i", str(mp3s[i]), "-ar", "24000", "-ac", "1",
                 "-af", "apad=pad_dur=0.25", str(wav)])
        wavs.append(wav)
        durs.append(probe_duration(wav))
    return wavs, durs


# ----------------------------------------------------------------------------
# Video
# ----------------------------------------------------------------------------
def find_font():
    for p in ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
              "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
              "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf"]:
        if Path(p).exists():
            return p
    raise RuntimeError("DejaVu şrifti tapılmadı (fonts-dejavu-core quraşdırın)")


def split_chunks(text, dur):
    words = text.split()
    n = max(1, round(dur / CHUNK_SECONDS))
    n = min(n, max(1, len(words)))
    size = math.ceil(len(words) / n)
    groups = [words[i:i + size] for i in range(0, len(words), size)]
    total = sum(len(g) for g in groups)
    out = []
    for g in groups:
        out.append((" ".join(g), dur * len(g) / total))
    return out


def build_clips(sentences, durs, picker):
    cdir = WORK_DIR / "clips"
    tdir = WORK_DIR / "txt"
    cdir.mkdir(parents=True, exist_ok=True)
    tdir.mkdir(parents=True, exist_ok=True)
    font = find_font()
    clips, cum_prev_frames, cum, k = [], 0, 0.0, 0
    for s, d in zip(sentences, durs):
        for text, cd in split_chunks(s["t"], d):
            cum += cd
            end_frames = round(cum * FPS)
            frames = max(1, end_frames - cum_prev_frames)
            cum_prev_frames = end_frames
            img = picker.pick(s["c"], s["e"])
            txt = tdir / f"c{k:05d}.txt"
            txt.write_text("\n".join(textwrap.wrap(text, 46)), encoding="utf-8")
            if k % 2 == 0:
                z = "min(1+0.0016*on,1.4)"
            else:
                z = "max(1.3-0.0016*on,1.0)"
            dur = frames / FPS
            fc = (
                f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
                f"gblur=sigma=25,eq=brightness=-0.18[bg];"
                f"[1:v]scale=1280:1280,zoompan=z='{z}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
                f"d={frames}:s=640x640:fps={FPS}[fg];"
                f"[bg][fg]overlay=(W-w)/2:(H-h)/2,"
                f"drawtext=fontfile={font}:textfile={txt}:expansion=none:fontsize=36:fontcolor=white:"
                f"borderw=2:bordercolor=black:box=1:boxcolor=black@0.55:boxborderw=12:"
                f"x=(w-text_w)/2:y=h-text_h-36,format=yuv420p[v]"
            )
            out = cdir / f"c{k:05d}.mp4"
            run(["ffmpeg", "-y", "-loglevel", "error",
                 "-loop", "1", "-framerate", str(FPS), "-t", f"{dur:.3f}", "-i", str(img),
                 "-i", str(img),
                 "-filter_complex", fc, "-map", "[v]", "-frames:v", str(frames),
                 "-c:v", "libx264", "-preset", "veryfast", "-crf", "24", "-r", str(FPS), str(out)])
            clips.append(out)
            k += 1
            if k % 50 == 0:
                print(f"  klip {k} hazırdır")
    return clips


def assemble(clips, wavs, out_path):
    lst = WORK_DIR / "clips.txt"
    lst.write_text("".join(f"file '{c}'\n" for c in clips))
    vid = WORK_DIR / "video_noaudio.mp4"
    run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(vid)])
    alst = WORK_DIR / "audio.txt"
    alst.write_text("".join(f"file '{w}'\n" for w in wavs))
    aud = WORK_DIR / "audio_all.wav"
    run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(alst), "-c", "copy", str(aud)])
    OUT_DIR.mkdir(exist_ok=True)
    run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(vid), "-i", str(aud),
         "-c:v", "copy", "-c:a", "aac", "-b:a", "128k", "-shortest", "-movflags", "+faststart", str(out_path)])


def cmd_video(minutes, offline):
    import shutil
    if WORK_DIR.exists():
        shutil.rmtree(WORK_DIR)
    WORK_DIR.mkdir()
    OUT_DIR.mkdir(exist_ok=True)
    plan, sentences = fake_story() if offline else make_story(minutes)
    print("Başlıq:", plan["title"], "| cümlə sayı:", len(sentences))
    lib = load_library()
    print("Kitabxana şəkilləri:", sum(len(v) for v in lib.values()))
    picker = Picker(lib, WORK_DIR / "placeholders")
    wavs, durs = make_audio(sentences, offline)
    print(f"Səs müddəti: {sum(durs) / 60:.1f} dəq")
    clips = build_clips(sentences, durs, picker)
    out = OUT_DIR / "video.mp4"
    assemble(clips, wavs, out)
    desc = (plan.get("description", "") + "\n\nDiese Geschichte ist frei erfunden. Text, Bilder und Stimme "
            "wurden mit KI erstellt.\n\n#geschichte #mythos #legende")
    meta = {"title": plan["title"][:100], "description": desc, "tags": (plan.get("tags") or [])[:15] + ["Geschichte", "KI"],
            "duration_min": round(probe_duration(out) / 60, 1)}
    (OUT_DIR / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1))
    if not offline:
        save_topic(plan["title"])
    print("Video hazırdır:", out, meta["duration_min"], "dəq")


# ----------------------------------------------------------------------------
# YouTube-a yükləmə
# ----------------------------------------------------------------------------
def cmd_upload():
    need = ["YT_CLIENT_ID", "YT_CLIENT_SECRET", "YT_REFRESH_TOKEN"]
    if not all(os.environ.get(n) for n in need):
        print("YouTube açarları hələ əlavə olunmayıb, yükləmə keçildi.")
        return
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload
    meta = json.loads((OUT_DIR / "meta.json").read_text())
    creds = Credentials(None, refresh_token=os.environ["YT_REFRESH_TOKEN"],
                        token_uri="https://oauth2.googleapis.com/token",
                        client_id=os.environ["YT_CLIENT_ID"], client_secret=os.environ["YT_CLIENT_SECRET"],
                        scopes=["https://www.googleapis.com/auth/youtube.upload"])
    yt = build("youtube", "v3", credentials=creds)
    body = {
        "snippet": {"title": meta["title"], "description": meta["description"], "tags": meta["tags"],
                    "categoryId": "24", "defaultLanguage": "de", "defaultAudioLanguage": "de"},
        "status": {"privacyStatus": os.environ.get("VIDEO_PRIVACY", "public"),
                   "selfDeclaredMadeForKids": False, "containsSyntheticMedia": True},
    }
    req = yt.videos().insert(part="snippet,status", body=body,
                             media_body=MediaFileUpload(str(OUT_DIR / "video.mp4"), chunksize=8 * 1024 * 1024, resumable=True))
    resp = None
    while resp is None:
        status, resp = req.next_chunk()
        if status:
            print(f"Yüklənir: {int(status.progress() * 100)}%")
    print("YouTube video ID:", resp.get("id"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["library", "video", "upload"])
    ap.add_argument("--minutes", type=int, default=int(os.environ.get("MINUTES", "10") or 10))
    ap.add_argument("--max", type=int, default=int(os.environ.get("MAX_IMAGES", "60") or 60))
    ap.add_argument("--offline", action="store_true")
    a = ap.parse_args()
    if a.cmd == "library":
        cmd_library(a.max)
    elif a.cmd == "video":
        cmd_video(a.minutes, a.offline)
    else:
        cmd_upload()


if __name__ == "__main__":
    main()

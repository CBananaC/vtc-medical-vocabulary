import json
import os
import re
import secrets
import time
from urllib.parse import urlencode
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
from flask import Flask, jsonify, request, send_from_directory, redirect, make_response

try:
    import google.auth
    from google.auth.transport.requests import Request as GoogleAuthRequest
except ImportError:  # pragma: no cover - dependency is supplied by google-cloud-storage
    google = None
    GoogleAuthRequest = None

BASE_DIR = Path(__file__).resolve().parent

VOCABULARY_MANIFEST_RELATIVE_PATH = "data/vocabulary-manifest.json"
VOCABULARY_MANIFEST_PATH = BASE_DIR / VOCABULARY_MANIFEST_RELATIVE_PATH


def _load_bundled_vocabulary_files() -> Tuple[str, ...]:
    """Read the public vocabulary paths from the checked-in data manifest."""
    try:
        manifest = json.loads(VOCABULARY_MANIFEST_PATH.read_text(encoding="utf-8"))
    except Exception:
        return ()

    paths = []
    for dataset in manifest.get("datasets", []) if isinstance(manifest, dict) else []:
        if not isinstance(dataset, dict):
            continue
        path = str(dataset.get("path") or "").strip().lstrip("/")
        if path.startswith("data/") and path.endswith(".json"):
            paths.append(path)
    return tuple(dict.fromkeys(paths))


BUNDLED_VOCABULARY_FILES = _load_bundled_vocabulary_files()

def _vtc_vocab_entry_count() -> int:
    """Return the unique bundled VTC vocabulary count without exposing contents."""
    terms = set()
    for filename in BUNDLED_VOCABULARY_FILES:
        try:
            payload = json.loads((BASE_DIR / filename).read_text(encoding="utf-8"))
            rows = []
            if isinstance(payload, dict):
                rows = payload.get("entries", []) or payload.get("items", [])
            for row in rows:
                if not isinstance(row, dict):
                    continue
                term = row.get("term") or row.get("word") or ""
                key = re.sub(r"\s+", " ", str(term).strip().lower())
                if key:
                    terms.add(key)
        except Exception:
            continue
    return len(terms)

MW_LEARNERS_KEY = os.environ.get("MW_LEARNERS_KEY", "").strip()
MW_DICTIONARY_KEY = os.environ.get("MW_DICTIONARY_KEY", "").strip()
MW_MEDICAL_KEY = os.environ.get("MW_MEDICAL_KEY", "").strip()
MW_KEY = MW_DICTIONARY_KEY
GOOGLE_TRANSLATE_KEY = os.environ.get("GOOGLE_TRANSLATE_KEY", "").strip()
WHO_ICD_API_TOKEN = os.environ.get("WHO_ICD_API_TOKEN", "").strip()
UMLS_API_KEY = os.environ.get("UMLS_API_KEY", "").strip()
GOOGLE_CLOUD_PROJECT = os.environ.get("GOOGLE_CLOUD_PROJECT", "").strip()
GOOGLE_OAUTH_CLIENT_ID = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "").strip()
GOOGLE_OAUTH_CLIENT_SECRET = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "").strip()
GOOGLE_OAUTH_REDIRECT_URI = os.environ.get("GOOGLE_OAUTH_REDIRECT_URI", "").strip()
APP_SECRET_KEY = os.environ.get("APP_SECRET_KEY", "").strip()
SYNC_ALLOWED_ORIGINS = {
    origin.strip().rstrip("/")
    for origin in os.environ.get("SYNC_ALLOWED_ORIGINS", "").split(",")
    if origin.strip()
}

# Optional persistent storage.
# If this env var is set, generated_vocab.json is stored in this GCS bucket.
# If not set, the app falls back to a local generated_vocab.json file.
GENERATED_VOCAB_BUCKET = os.environ.get("GENERATED_VOCAB_BUCKET", "").strip()
GENERATED_VOCAB_BLOB = os.environ.get("GENERATED_VOCAB_BLOB", "generated_vocab.json").strip() or "generated_vocab.json"
LOCAL_GENERATED_VOCAB_PATH = BASE_DIR / "generated_vocab.json"

CLOUD_SYNC_BUCKET = os.environ.get("CLOUD_SYNC_BUCKET", "").strip() or GENERATED_VOCAB_BUCKET
CLOUD_SYNC_TOKEN_BLOB = os.environ.get("CLOUD_SYNC_TOKEN_BLOB", "vocab_sync_tokens.json").strip() or "vocab_sync_tokens.json"
CLOUD_SYNC_DRIVE_FILE_NAME = os.environ.get("CLOUD_SYNC_DRIVE_FILE_NAME", "ielts-vocab-data.json").strip() or "ielts-vocab-data.json"
APP_ENTRYPOINT = os.environ.get("APP_ENTRYPOINT", "index.html").strip() or "index.html"
if APP_ENTRYPOINT not in {"index.html", "index.simplified.html"}:
    APP_ENTRYPOINT = "index.html"

# Static files are served only by the explicit allowlist below. Flask's
# automatic static route would otherwise expose source/config files by name.
app = Flask(__name__, static_folder=None)

PUBLIC_STATIC_FILES = {
    APP_ENTRYPOINT,
    VOCABULARY_MANIFEST_RELATIVE_PATH,
    *BUNDLED_VOCABULARY_FILES,
    "app.js",
    "styles.css",
    "sw.js",
    "manifest.webmanifest",
    "apple-touch-icon.png",
    "icon-192.png",
    "icon-512.png",
    "icon.png",
    "sample_api_demo.html",
}

PUBLIC_STATIC_PREFIXES = (
    "whoami-assets/",
)


@app.after_request
def add_sync_cors_headers(resp):
    if request.path.startswith("/api/sync/"):
        origin = request.headers.get("Origin", "")
        if origin and origin.rstrip("/") in SYNC_ALLOWED_ORIGINS:
            resp.headers["Access-Control-Allow-Origin"] = origin
            resp.headers["Vary"] = "Origin"
            resp.headers["Access-Control-Allow-Credentials"] = "true"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return resp


@app.route("/api/sync/<path:_path>", methods=["OPTIONS"])
def api_sync_options(_path):
    return ("", 204)


@app.get("/")
def root():
    return send_from_directory(BASE_DIR, APP_ENTRYPOINT)


@app.get("/favicon.ico")
def favicon():
    return send_from_directory(BASE_DIR, "icon.png", mimetype="image/png")


@app.get("/<path:path>")
def static_files(path: str):
    is_public_prefix = any(path.startswith(prefix) for prefix in PUBLIC_STATIC_PREFIXES)
    if path not in PUBLIC_STATIC_FILES and not is_public_prefix:
        return send_from_directory(BASE_DIR, APP_ENTRYPOINT)
    file_path = BASE_DIR / path
    if file_path.exists() and file_path.is_file():
        return send_from_directory(BASE_DIR, path)
    return send_from_directory(BASE_DIR, APP_ENTRYPOINT)


@app.get("/api/health")
@app.get("/health")
def health():
    return jsonify({
        "ok": True,
        "definition_source": "merriam-webster-collegiate",
        "has_mw_dictionary_key": bool(MW_DICTIONARY_KEY),
        "has_mw_learners_key": bool(MW_LEARNERS_KEY),
        "has_mw_medical_key": bool(MW_MEDICAL_KEY),
        "has_google_translate_key": bool(GOOGLE_TRANSLATE_KEY),
        "has_who_icd_api_token": bool(WHO_ICD_API_TOKEN),
        "has_umls_api_key": bool(UMLS_API_KEY),
        "google_tts_auth": "application-default-credentials",
        "google_cloud_project_configured": bool(GOOGLE_CLOUD_PROJECT),
        "has_google_oauth_client_id": bool(GOOGLE_OAUTH_CLIENT_ID),
        "has_google_oauth_client_secret": bool(GOOGLE_OAUTH_CLIENT_SECRET),
        "cloud_sync_token_storage": "gcs" if CLOUD_SYNC_BUCKET else "local-file",
        "parser_version": "mw-structured-v5-cease-fix",
        "app_entrypoint": APP_ENTRYPOINT,
        "generated_vocab_storage": "gcs" if GENERATED_VOCAB_BUCKET else "local-file",
        "sync_cors_origins_configured": bool(SYNC_ALLOWED_ORIGINS),
        "vtc_vocab_entries": _vtc_vocab_entry_count(),
    })


@app.get("/api/config")
def client_config():
    """Public client-side config — OAuth client ID for Google sign-in."""
    return jsonify({
        "google_oauth_client_id": GOOGLE_OAUTH_CLIENT_ID,
    })


@app.get("/api/define")
def define():
    word = (request.args.get("word") or "").strip()
    if not word:
        return jsonify({"detail": "Missing word"}), 400

    if not MW_KEY:
        return jsonify({"detail": "MW_DICTIONARY_KEY is not configured on the server"}), 500

    url = f"https://dictionaryapi.com/api/v3/references/collegiate/json/{requests.utils.quote(word)}"
    try:
        r = requests.get(url, params={"key": MW_KEY}, timeout=15)
        r.raise_for_status()
        raw_data = r.json()
    except Exception as exc:
        return jsonify({"detail": f"Merriam-Webster request failed: {exc}"}), 502

    try:
        parsed = parse_mw_collegiate_response(word, raw_data)
        return jsonify(parsed)
    except Exception as exc:
        return jsonify({"detail": f"Could not parse Merriam-Webster response: {type(exc).__name__}: {exc}"}), 502


@app.get("/api/define-medical")
def define_medical():
    """Return a parsed Merriam-Webster Medical Dictionary entry.

    This uses its own key because Merriam-Webster limits a free key to two
    reference APIs. The key is never sent to the browser.
    """
    word = (request.args.get("word") or "").strip()
    if not word:
        return jsonify({"detail": "Missing word"}), 400

    if not MW_MEDICAL_KEY:
        return jsonify({"detail": "MW_MEDICAL_KEY is not configured on the server"}), 503

    url = f"https://www.dictionaryapi.com/api/v3/references/medical/json/{requests.utils.quote(word)}"
    try:
        r = requests.get(url, params={"key": MW_MEDICAL_KEY}, timeout=15)
        r.raise_for_status()
        raw_data = r.json()
    except Exception as exc:
        return jsonify({"detail": f"Merriam-Webster Medical request failed: {exc}"}), 502

    try:
        # The Medical API uses the same core JSON entry structures needed by
        # the existing parser: hwi, prs, shortdef, def, and meta.
        parsed = parse_mw_collegiate_response(word, raw_data)
        parsed["source"] = "merriam-webster-medical"
        parsed["fetchedAt"] = now_iso()
        return jsonify(parsed)
    except Exception as exc:
        return jsonify({"detail": f"Could not parse Merriam-Webster Medical response: {type(exc).__name__}: {exc}"}), 502


def _who_text(value: Any) -> str:
    """Extract a displayable label from a WHO JSON-LD language value."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return str(value.get("@value") or value.get("value") or value.get("label") or value.get("title") or "")
    return ""


@app.get("/api/demo/who-icd11-zh")
def demo_who_icd11_zh():
    """Search the authenticated WHO ICD-11 API in Chinese when configured.

    The short-lived OAuth bearer token is server-side only. The demo deliberately
    returns normalized matches rather than exposing the upstream response or token.
    """
    word = (request.args.get("word") or "").strip()
    if not word:
        return jsonify({"detail": "Missing word"}), 400
    if not WHO_ICD_API_TOKEN:
        return jsonify({"detail": "WHO_ICD_API_TOKEN is not configured on the server"}), 503

    url = "https://id.who.int/icd/release/11/2026-01/mms/search"
    try:
        response = requests.get(
            url,
            params={"q": word},
            headers={
                "API-Version": "v2",
                "Accept": "application/json",
                "Accept-Language": "zh",
                "Authorization": f"Bearer {WHO_ICD_API_TOKEN}",
            },
            timeout=15,
        )
        response.raise_for_status()
        raw = response.json()
    except Exception:
        return jsonify({"detail": "WHO ICD-11 Chinese request failed"}), 502

    candidates = []
    if isinstance(raw, dict):
        for key in ("destinationEntities", "results", "entities", "items"):
            if isinstance(raw.get(key), list):
                candidates = raw[key]
                break
        if not candidates and isinstance(raw.get("result"), list):
            candidates = raw["result"]

    matches = []
    for item in candidates[:5]:
        if not isinstance(item, dict):
            continue
        title = _who_text(item.get("title") or item.get("label") or item.get("name"))
        definition = _who_text(item.get("definition") or item.get("description"))
        code = _who_text(item.get("theCode") or item.get("code"))
        uri = _who_text(item.get("@id") or item.get("id") or item.get("foundationUri"))
        if title or definition:
            matches.append({"title": title, "definition": definition, "code": code, "uri": uri})
    return jsonify({"source": "who-icd11", "language": "zh", "matches": matches})


@app.get("/api/demo/umls")
def demo_umls():
    """Return a small UMLS concept sample with Chinese atoms and source definitions."""
    word = (request.args.get("word") or "").strip()
    if not word:
        return jsonify({"detail": "Missing word"}), 400
    if not UMLS_API_KEY:
        return jsonify({"detail": "UMLS_API_KEY is not configured on the server"}), 503

    base_url = "https://uts-ws.nlm.nih.gov/rest"
    try:
        search_response = requests.get(
            f"{base_url}/search/current",
            params={
                "string": word,
                "searchType": "exact",
                "returnIdType": "concept",
                "pageSize": 3,
                "apiKey": UMLS_API_KEY,
            },
            timeout=15,
        )
        search_response.raise_for_status()
        search_raw = search_response.json()
    except Exception:
        return jsonify({"detail": "UMLS search request failed"}), 502

    search_result = (search_raw.get("result") or {}) if isinstance(search_raw, dict) else {}
    concepts = search_result.get("results") if isinstance(search_result, dict) else []
    if not isinstance(concepts, list):
        concepts = []

    matches = []
    for concept in concepts[:3]:
        if not isinstance(concept, dict):
            continue
        cui = str(concept.get("ui") or "")
        if not cui:
            continue
        definitions = []
        chinese_terms = []
        try:
            definitions_response = requests.get(
                f"{base_url}/content/current/CUI/{requests.utils.quote(cui)}/definitions",
                params={"apiKey": UMLS_API_KEY, "pageSize": 5},
                timeout=15,
            )
            definitions_response.raise_for_status()
            definitions_raw = definitions_response.json()
            for definition in (definitions_raw.get("result") or [])[:5]:
                if isinstance(definition, dict) and definition.get("value"):
                    definitions.append({
                        "source": definition.get("rootSource") or "UMLS source",
                        "text": definition["value"],
                    })
        except Exception:
            pass
        try:
            atoms_response = requests.get(
                f"{base_url}/content/current/CUI/{requests.utils.quote(cui)}/atoms",
                params={"apiKey": UMLS_API_KEY, "pageSize": 200},
                timeout=15,
            )
            atoms_response.raise_for_status()
            atoms_raw = atoms_response.json()
            for atom in (atoms_raw.get("result") or []):
                name = atom.get("name") if isinstance(atom, dict) else ""
                if name and re.search(r"[\u3400-\u9fff]", name) and name not in chinese_terms:
                    chinese_terms.append(name)
        except Exception:
            pass
        matches.append({
            "cui": cui,
            "name": concept.get("name") or "",
            "semanticTypes": concept.get("semanticTypes") or [],
            "chineseTerms": chinese_terms[:8],
            "definitions": definitions,
        })
    return jsonify({"source": "umls", "matches": matches})


def _synthesize_google_tts(text: str):
    """Return a JSON-safe Google Cloud TTS result and HTTP status."""
    text = str(text or "").strip()
    if not text:
        return {"detail": "Missing text"}, 400
    if len(text) > 200:
        return {"detail": "Text is too long"}, 400
    if google is None or GoogleAuthRequest is None:
        return {"detail": "google-auth is not installed on the server"}, 503

    try:
        credentials, detected_project = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
        credentials.refresh(GoogleAuthRequest())
        access_token = credentials.token
        project = GOOGLE_CLOUD_PROJECT or detected_project or ""
        if not access_token:
            return {"detail": "Google application credentials did not return an access token"}, 503
    except Exception:
        return {"detail": "Google application-default credentials are not available on the server"}, 503

    payload = {
        "input": {"text": text},
        "voice": {"languageCode": "en-US", "ssmlGender": "NEUTRAL"},
        "audioConfig": {"audioEncoding": "MP3", "speakingRate": 0.82},
    }
    try:
        response = requests.post(
            "https://texttospeech.googleapis.com/v1/text:synthesize",
            headers={
                "Authorization": f"Bearer {access_token}",
                **({"x-goog-user-project": project} if project else {}),
            },
            json=payload,
            timeout=20,
        )
        response.raise_for_status()
        audio_content = (response.json() or {}).get("audioContent")
        if not audio_content:
            return {"detail": "Google Cloud TTS returned no audio"}, 502
    except Exception:
        return {"detail": "Google Cloud TTS request failed"}, 502

    return {
        "source": "google-cloud-tts",
        "text": text,
        "projectConfigured": bool(project),
        "mimeType": "audio/mpeg",
        "audioDataUri": f"data:audio/mpeg;base64,{audio_content}",
    }, 200


@app.get("/api/audio")
def api_audio():
    """Synthesize one vocabulary term with server-side Google Cloud TTS."""
    text = (request.args.get("text") or request.args.get("word") or "").strip()
    result, status = _synthesize_google_tts(text)
    return jsonify(result), status


@app.get("/api/demo/google-tts")
def demo_google_tts():
    """Compatibility demo endpoint for one English medical term."""
    word = (request.args.get("word") or "").strip()
    result, status = _synthesize_google_tts(word)
    if status == 200:
        result["word"] = result.pop("text", word)
    return jsonify(result), status


@app.get("/api/demo/open-dictionary")
def demo_open_dictionary():
    """Proxy the no-key open dictionary fallback used by the API sample."""
    word = (request.args.get("word") or "").strip()
    if not word:
        return jsonify({"detail": "Missing word"}), 400
    url = f"https://api.dictionaryapi.dev/api/v2/entries/en/{requests.utils.quote(word)}"
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        return jsonify(r.json())
    except Exception as exc:
        return jsonify({"detail": f"Open dictionary request failed: {exc}"}), 502


# ============================================================
# EASY MODE — Learner's dictionary + Chinese translation
# ============================================================

@app.get("/api/define-easy")
def define_easy():
    """Easy Mode: returns Learner's Dictionary entry with Chinese (zh-TW) translations
    of each definition inline. One endpoint = one round-trip from the client."""
    word = (request.args.get("word") or "").strip()
    if not word:
        return jsonify({"detail": "Missing word"}), 400

    if not MW_LEARNERS_KEY:
        return jsonify({"detail": "MW_LEARNERS_KEY is not configured on the server"}), 500

    url = f"https://dictionaryapi.com/api/v3/references/learners/json/{requests.utils.quote(word)}"
    try:
        r = requests.get(url, params={"key": MW_LEARNERS_KEY}, timeout=15)
        r.raise_for_status()
        raw_data = r.json()
    except Exception as exc:
        return jsonify({"detail": f"MW Learner's request failed: {exc}"}), 502

    try:
        parsed = parse_mw_learners_response(word, raw_data)
    except Exception as exc:
        return jsonify({"detail": f"Could not parse Learner's response: {type(exc).__name__}: {exc}"}), 502

    # Translate each definition's text into Traditional Chinese, if a key is set.
    if GOOGLE_TRANSLATE_KEY and parsed.get("definitions"):
        texts_to_translate = [d.get("text", "") for d in parsed["definitions"]]
        try:
            translations = google_translate_batch(texts_to_translate, target="zh-TW")
            for d, t in zip(parsed["definitions"], translations):
                d["chinese"] = t
            zh_by_text = {
                d.get("text", ""): d.get("chinese", "")
                for d in parsed["definitions"]
                if d.get("text")
            }
            for ent in parsed.get("entries", []) or []:
                for sense in ent.get("senses", []) or []:
                    text = sense.get("text") or sense.get("definition") or ""
                    if text in zh_by_text:
                        sense["chinese"] = zh_by_text[text]
        except Exception as exc:
            # Don't fail the whole request if translate is down; mark and continue.
            for d in parsed["definitions"]:
                d["chinese"] = ""
            for ent in parsed.get("entries", []) or []:
                for sense in ent.get("senses", []) or []:
                    sense["chinese"] = ""
            parsed["translateError"] = f"{type(exc).__name__}: {exc}"

    return jsonify(parsed)


@app.post("/api/translate")
def translate():
    """Translate an array of strings into a target language. Body: {q: [..], target: 'zh-TW'}."""
    if not GOOGLE_TRANSLATE_KEY:
        return jsonify({"detail": "GOOGLE_TRANSLATE_KEY is not configured on the server"}), 500
    data = request.get_json(silent=True) or {}
    q = data.get("q")
    target = (data.get("target") or "zh-TW").strip()
    if not q or not isinstance(q, list):
        return jsonify({"detail": "Missing or invalid 'q' (must be a list of strings)"}), 400
    try:
        translations = google_translate_batch(q, target=target)
        return jsonify({"translations": translations})
    except Exception as exc:
        return jsonify({"detail": f"Translate failed: {type(exc).__name__}: {exc}"}), 502


def google_translate_batch(texts: List[str], target: str = "zh-TW") -> List[str]:
    """Call Google Cloud Translate v2 with an API key. Returns a list of translated strings
    in the same order as the input, with empty strings preserved."""
    if not texts:
        return []
    # Preserve empty entries — Translate API rejects empty strings in the q array.
    nonempty_indices = [i for i, t in enumerate(texts) if t]
    nonempty_texts = [texts[i] for i in nonempty_indices]
    if not nonempty_texts:
        return [""] * len(texts)

    url = "https://translation.googleapis.com/language/translate/v2"
    payload = {
        "q": nonempty_texts,
        "target": target,
        "format": "text",
    }
    r = requests.post(url, params={"key": GOOGLE_TRANSLATE_KEY}, json=payload, timeout=15)
    r.raise_for_status()
    body = r.json()
    items = body.get("data", {}).get("translations", []) or []
    translated = [it.get("translatedText", "") for it in items]
    # Pad if response shorter than expected
    while len(translated) < len(nonempty_texts):
        translated.append("")

    out = [""] * len(texts)
    for j, src_idx in enumerate(nonempty_indices):
        out[src_idx] = translated[j]
    return out


def parse_mw_learners_response(query: str, raw_data: Any) -> Dict[str, Any]:
    """Simplified parser for MW Learner's Dictionary. Returns:
      { word, pronunciation, audio, grammarLabels: [...],
        definitions: [{text, examples: [..], partOfSpeech}],
        source: 'merriam-webster-learners' }
    """
    if not isinstance(raw_data, list) or not raw_data:
        return {"word": query, "definitions": [], "source": "merriam-webster-learners"}

    dict_entries = [e for e in raw_data if isinstance(e, dict)]
    if not dict_entries:
        suggestions = [s for s in raw_data if isinstance(s, str)][:10]
        return {
            "word": query,
            "definitions": [],
            "source": "merriam-webster-learners",
            "suggestions": suggestions,
        }

    query_key = normalize_key(query)

    def entry_matches_query(entry: Dict[str, Any]) -> bool:
        meta = entry.get("meta") or {}
        hwi = entry.get("hwi") or {}
        id_base = str(meta.get("id") or "").split(":")[0]
        hw_base = str(hwi.get("hw") or "").split(":")[0]
        candidates = [id_base, hw_base]
        for stem in meta.get("stems") or []:
            candidates.append(stem)
        return any(normalize_key(clean_headword(c)) == query_key for c in candidates)

    exact_entries = [e for e in dict_entries if entry_matches_query(e)]
    if exact_entries:
        dict_entries = exact_entries

    def parse_learner_senses(entry: Dict[str, Any], part_of_speech: str) -> List[Dict[str, Any]]:
        senses: List[Dict[str, Any]] = []

        def walk(node: Any) -> None:
            if not isinstance(node, list):
                return
            if len(node) >= 2 and node[0] == "sense" and isinstance(node[1], dict):
                sense = node[1]
                text = ""
                examples: List[str] = []
                for dt_item in sense.get("dt", []) or []:
                    if not isinstance(dt_item, list) or len(dt_item) < 2:
                        continue
                    kind, val = dt_item[0], dt_item[1]
                    if kind == "text":
                        chunk = clean_mw_text(val)
                        if chunk:
                            text = f"{text} {chunk}".strip() if text else chunk
                    elif kind == "vis" and isinstance(val, list):
                        for vis in val:
                            if isinstance(vis, dict):
                                ex = clean_mw_text(vis.get("t", ""))
                            else:
                                ex = clean_mw_text(vis)
                            if ex and ex not in examples:
                                examples.append(ex)
                if text:
                    senses.append({
                        "number": clean_mw_text(sense.get("sn") or str(len(senses) + 1)),
                        "definition": text,
                        "text": text,
                        "examples": examples,
                        "partOfSpeech": part_of_speech,
                    })
                return
            for child in node:
                walk(child)

        for d in entry.get("def", []) or []:
            walk(d.get("sseq", []) if isinstance(d, dict) else d)
        return senses

    headword = ""
    pronunciation = ""
    ipa = ""
    audio_url = ""
    grammar_labels: List[str] = []
    all_defs: List[Dict[str, Any]] = []
    structured_entries: List[Dict[str, Any]] = []

    for entry in dict_entries:
        hwi = entry.get("hwi", {}) or {}
        raw_hw = hwi.get("hw", "")
        clean_hw = clean_headword(raw_hw) or query
        if not headword:
            headword = clean_hw
        if not pronunciation or not audio_url:
            prs = hwi.get("prs", []) or []
            if prs and isinstance(prs[0], dict):
                if not pronunciation:
                    pronunciation = clean_mw_text(prs[0].get("ipa") or prs[0].get("mw", ""))
                if not ipa:
                    ipa = clean_mw_text(prs[0].get("ipa") or prs[0].get("mw", ""))
                sound = prs[0].get("sound", {}) or {}
                if sound and not audio_url:
                    audio_url = mw_audio_url(sound.get("audio", ""))

        fl = clean_mw_text(entry.get("fl", ""))
        if fl and fl not in grammar_labels:
            grammar_labels.append(fl)

        prs = hwi.get("prs", []) or []
        first_pr = prs[0] if prs and isinstance(prs[0], dict) else {}
        sound = first_pr.get("sound", {}) or {}
        sound_audio = sound.get("audio", "") if isinstance(sound, dict) else ""
        entry_audio = mw_audio_url(sound_audio)
        senses = parse_learner_senses(entry, fl)
        short_defs = [clean_mw_text(sd) for sd in (entry.get("shortdef") or []) if clean_mw_text(sd)]
        infl = [clean_headword(item.get("if", "")) for item in (entry.get("ins") or []) if isinstance(item, dict)]
        infl = [x for x in infl if x]

        if senses or short_defs:
            structured_entries.append({
                "id": (entry.get("meta") or {}).get("id") or "",
                "headword": clean_hw,
                "plainHw": clean_hw,
                "hw": raw_hw or clean_hw,
                "displayHw": raw_hw or clean_hw,
                "partOfSpeech": fl,
                "functionalLabel": fl,
                "grammar": clean_mw_text(entry.get("gram") or ""),
                "ipa": clean_mw_text(first_pr.get("ipa") or first_pr.get("mw") or "") if isinstance(first_pr, dict) else "",
                "pronunciation": clean_mw_text(first_pr.get("ipa") or first_pr.get("mw") or "") if isinstance(first_pr, dict) else "",
                "audioUrl": entry_audio,
                "inflections": infl,
                "shortDefinitions": short_defs,
                "senses": senses,
            })

        for sense in senses:
            all_defs.append({
                "text": sense["definition"],
                "examples": sense["examples"],
                "partOfSpeech": fl,
            })

    # Fallback to shortdef if no full definitions were parsed
    if not all_defs:
        for entry in dict_entries:
            fl = clean_mw_text(entry.get("fl", ""))
            for sd in entry.get("shortdef", []) or []:
                text = clean_mw_text(sd)
                if text:
                    all_defs.append({"text": text, "examples": [], "partOfSpeech": fl})

    sense_by_text: Dict[str, List[Dict[str, Any]]] = {}
    for ent in structured_entries:
        for sense in ent.get("senses", []):
            text_key = sense.get("definition") or sense.get("text") or ""
            if text_key:
                sense_by_text.setdefault(text_key, []).append(sense)

    return {
        "word": headword or query,
        "headword": headword or query,
        "hw": structured_entries[0]["hw"] if structured_entries else (headword or query),
        "displayHw": structured_entries[0]["displayHw"] if structured_entries else (headword or query),
        "ipa": ipa or pronunciation,
        "pronunciation": pronunciation,
        "audio": audio_url,
        "audioUrl": audio_url,
        "grammarLabels": grammar_labels,
        "inflections": [x for ent in structured_entries for x in ent.get("inflections", [])],
        "entries": structured_entries,
        "definitions": all_defs,
        "source": "merriam-webster-learners",
    }


@app.get("/api/generated-vocab")
def get_generated_vocab():
    return jsonify(load_generated_vocab())


@app.post("/api/generated-vocab")
def create_generated_vocab():
    payload = request.get_json(silent=True) or {}
    term = clean_spaces(payload.get("term") or payload.get("word") or payload.get("headword") or "")
    if not term:
        return jsonify({"detail": "Missing term"}), 400

    key = normalize_key(term)
    existing = load_generated_vocab()
    items = existing.get("items", [])

    now = now_iso()
    item = {
        "key": key,
        "term": term,
        "word": term,
        "grammar_labels": clean_list(payload.get("grammar_labels") or payload.get("grammarLabels") or [payload.get("functionalLabel")] if payload.get("functionalLabel") else []),
        "level": "API Added",
        "levels": ["API Added"],
        "specialStatus": "created_from_related_entry",
        "source": "merriam-webster-collegiate",
        "sourceFromWord": clean_spaces(payload.get("sourceFromWord") or ""),
        "sourceFromKey": normalize_key(payload.get("sourceFromKey") or payload.get("sourceFromWord") or ""),
        "sourceEntryType": clean_spaces(payload.get("sourceEntryType") or "related_or_other_entry"),
        "insertAfterKey": normalize_key(payload.get("insertAfterKey") or payload.get("sourceFromKey") or ""),
        "createdAt": now,
        "updatedAt": now,
        "dictionaryCacheKey": key,
        "notes": "Generated from Merriam-Webster related/other entry and manually created by user.",
        "categories": [
            {
                "level": "API Added",
                "topHeader": "Generated from Merriam-Webster",
                "sectionTitle": "Related / Other Entries",
                "sourceWord": clean_spaces(payload.get("sourceFromWord") or ""),
            }
        ],
    }

    replaced = False
    for idx, old in enumerate(items):
        if normalize_key(old.get("key") or old.get("term") or old.get("word")) == key:
            item["createdAt"] = old.get("createdAt") or now
            items[idx] = item
            replaced = True
            break
    if not replaced:
        insert_after = item.get("insertAfterKey")
        # The generated_vocab file only contains generated cards, so usually append.
        # The frontend inserts it after the source card when merging the full list.
        items.append(item)

    existing["schema_version"] = "ielts-generated-vocab-v1"
    existing["updatedAt"] = now
    existing["items"] = items
    save_generated_vocab(existing)

    return jsonify({"ok": True, "created": not replaced, "item": item})


@app.delete("/api/generated-vocab/<path:key>")
def delete_generated_vocab(key: str):
    nk = normalize_key(key)
    existing = load_generated_vocab()
    before = len(existing.get("items", []))
    existing["items"] = [x for x in existing.get("items", []) if normalize_key(x.get("key") or x.get("term") or x.get("word")) != nk]
    existing["updatedAt"] = now_iso()
    save_generated_vocab(existing)
    return jsonify({"ok": True, "deleted": before - len(existing["items"])})


# ----------------------------
# Merriam-Webster parser
# ----------------------------

def parse_mw_collegiate_response(query: str, raw_data: Any) -> Dict[str, Any]:
    query_key = normalize_key(query)

    if not isinstance(raw_data, list):
        raw_data = []

    dict_entries = [x for x in raw_data if isinstance(x, dict) and isinstance(x.get("hwi"), dict)]
    suggestions = [x for x in raw_data if isinstance(x, str)]

    if not dict_entries:
        return {
            "query": query,
            "source": "merriam-webster-collegiate",
            "fetchedAt": now_iso(),
            "headword": query,
            "hw": query,
            "functionalLabel": "",
            "functionalLabels": [],
            "pronunciation": "",
            "audioUrl": "",
            "definitions": [],
            "shortDefinitions": [],
            "fullDefinitions": [],
            "examples": [],
            "mainEntries": [],
            "relatedBaseEntries": [],
            "otherEntries": [],
            "runOns": [],
            "synonymDiscussions": [],
            "stems": [],
            "suggestions": suggestions[:20],
        }

    parsed_entries = [parse_entry(e) for e in dict_entries]

    main_entries: List[Dict[str, Any]] = []
    related_base_entries: List[Dict[str, Any]] = []
    other_entries: List[Dict[str, Any]] = []

    for original, parsed in zip(dict_entries, parsed_entries):
        relation = classify_entry(query_key, original, parsed)
        parsed["entryType"] = relation
        if relation == "primary":
            main_entries.append(parsed)
        elif relation == "related_base":
            related_base_entries.append(parsed)
        elif relation == "hidden_related":
            continue
        else:
            other_entries.append(parsed)

    if not main_entries and parsed_entries:
        parsed_entries[0]["entryType"] = "primary"
        main_entries = [parsed_entries[0]]
        other_entries = [x for x in parsed_entries[1:] if x is not parsed_entries[0]]

    primary = main_entries[0] if main_entries else parsed_entries[0]

    # Overview labels should describe only exact entries for the searched headword.
    # Example: "cease" should show verb/noun from cease:1 and cease:2,
    # not phrase/adjective from other returned entries.
    all_labels = []
    for ent in main_entries:
        lab = ent.get("functionalLabel") or ""
        if lab and lab not in all_labels:
            all_labels.append(lab)

    all_stems = []
    for ent in main_entries + related_base_entries + other_entries:
        for s in ent.get("stems", []):
            if s and s not in all_stems:
                all_stems.append(s)

    synonym_discussions = []
    for ent in main_entries + related_base_entries:
        for sd in ent.get("synonymDiscussions", []):
            synonym_discussions.append(sd)

    # Backward compatibility for old frontend/games.
    definitions = []
    examples = []
    for ent in main_entries:
        for m in ent.get("meanings", []):
            for seg in m.get("definitionSegments", []):
                d = seg.get("definition")
                if d and d not in definitions:
                    definitions.append(d)
                for ex in seg.get("examples", []):
                    t = ex.get("text") if isinstance(ex, dict) else str(ex)
                    if t and t not in examples:
                        examples.append(t)

    if not definitions:
        definitions = primary.get("shortDefinitions", [])[:]

    result = {
        "query": query,
        "source": "merriam-webster-collegiate",
        "fetchedAt": now_iso(),
        "headword": primary.get("headword") or query,
        "hw": primary.get("hw") or query,
        "functionalLabel": primary.get("functionalLabel") or "",
        "functionalLabels": all_labels,
        "pronunciation": primary.get("pronunciation") or "",
        "audioUrl": primary.get("audioUrl") or "",
        "definitions": definitions[:20],
        "shortDefinitions": primary.get("shortDefinitions", [])[:20],
        "fullDefinitions": definitions[:20],
        "examples": examples[:20],
        "mainEntries": main_entries,
        "relatedBaseEntries": related_base_entries,
        "otherEntries": other_entries,
        "runOns": dedupe_runons([ro for ent in main_entries + related_base_entries + other_entries for ro in ent.get("runOns", [])]),
        "synonymDiscussions": synonym_discussions,
        "stems": all_stems,
        "offensive": any(bool(ent.get("offensive")) for ent in main_entries),
        "rawEntryIds": [ent.get("entryId") for ent in main_entries + related_base_entries + other_entries if ent.get("entryId")],
    }
    return result


def classify_entry(query_key: str, original: Dict[str, Any], parsed: Dict[str, Any]) -> str:
    hw_key = normalize_key(parsed.get("headword") or parsed.get("hw") or "")
    entry_id = str((original.get("meta") or {}).get("id") or "")
    entry_base_key = normalize_key(entry_id.split(":")[0])
    stems = [normalize_key(s) for s in ((original.get("meta") or {}).get("stems") or [])]

    # Primary exact entry:
    # cease:1 / cease:2 -> cease
    # abandon:1 / abandon:2 -> abandon
    # abandoned -> abandoned
    if entry_base_key == query_key or hw_key == query_key:
        return "primary"

    # Related base entry:
    # abandoned query may also return abandon:1 because "abandoned" is in abandon stems.
    # Keep only close base-form matches. Do NOT keep cease -> fire merely because
    # "cease fire" is in fire's stems.
    if query_key in stems:
        if query_key in hw_key or hw_key in query_key or levenshtein_close(query_key, hw_key):
            return "related_base"
        return "hidden_related"

    # Other entry:
    # Only show other entries if the returned headword itself contains the query
    # as a word/phrase component, e.g. cease and desist, ceasefire.
    if query_key in hw_key:
        return "other"

    return "hidden_related"


def levenshtein_close(a: str, b: str) -> bool:
    """Small helper for close base forms, e.g. abandoned -> abandon."""
    a = normalize_key(a)
    b = normalize_key(b)
    if not a or not b:
        return False
    if a.startswith(b) or b.startswith(a):
        return True
    for suffix in ("ed", "ing", "s", "es", "d"):
        if a.endswith(suffix) and a[:-len(suffix)] == b:
            return True
        if b.endswith(suffix) and b[:-len(suffix)] == a:
            return True
    return False


def parse_entry(entry: Dict[str, Any]) -> Dict[str, Any]:
    hwi = entry.get("hwi") or {}
    hw = str(hwi.get("hw") or "")
    headword = clean_headword(hw) or clean_headword((entry.get("meta") or {}).get("id") or "")

    prs = first_pronunciation(hwi)
    sound_id = ((prs.get("sound") or {}).get("audio") or "") if isinstance(prs, dict) else ""

    meta = entry.get("meta") or {}
    stems = clean_list(meta.get("stems") or [])

    result = {
        "entryId": meta.get("id") or "",
        "headword": headword,
        "hw": hw or headword,
        "functionalLabel": entry.get("fl") or "",
        "pronunciation": prs.get("mw") or "" if isinstance(prs, dict) else "",
        "audioUrl": mw_audio_url(sound_id),
        "inflections": parse_inflections(entry.get("ins")),
        "shortDefinitions": unique_clean_texts(entry.get("shortdef") or []),
        "meanings": parse_definitions(entry.get("def")),
        "runOns": parse_uros(entry.get("uros")),
        "synonymDiscussions": parse_synonym_discussions(entry.get("syns")),
        "etymology": parse_text_pairs(entry.get("et")),
        "date": clean_mw_text(entry.get("date") or ""),
        "stems": stems,
        "offensive": bool(meta.get("offensive")) if isinstance(meta, dict) else False,
    }
    return result


def parse_inflections(ins: Any) -> List[Dict[str, str]]:
    out = []
    seen = set()
    if not isinstance(ins, list):
        return out
    for item in ins:
        if not isinstance(item, dict):
            continue
        form = clean_headword(item.get("if") or "")
        label = clean_mw_text(item.get("il") or "")
        if not form:
            continue
        key = (label.lower(), form.lower())
        if key in seen:
            continue
        seen.add(key)
        out.append({"label": label, "form": form})
    return out


def parse_uros(uros: Any) -> List[Dict[str, Any]]:
    out = []
    seen = set()
    if not isinstance(uros, list):
        return out
    for u in uros:
        if not isinstance(u, dict):
            continue
        hw = str(u.get("ure") or "")
        word = clean_headword(hw)
        if not word:
            continue
        prs = first_pronunciation(u)
        sound_id = ((prs.get("sound") or {}).get("audio") or "") if isinstance(prs, dict) else ""
        examples = []
        for block in u.get("utxt") or []:
            if isinstance(block, list) and len(block) >= 2 and block[0] == "vis":
                examples.extend(parse_vis_examples(block[1]))
        key = (word.lower(), (u.get("fl") or "").lower())
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "word": word,
            "hw": hw or word,
            "functionalLabel": u.get("fl") or "",
            "pronunciation": prs.get("mw") or "" if isinstance(prs, dict) else "",
            "audioUrl": mw_audio_url(sound_id),
            "examples": examples,
        })
    return out


def dedupe_runons(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    seen = set()
    for x in items:
        key = (normalize_key(x.get("word")), x.get("functionalLabel") or "")
        if key in seen:
            continue
        seen.add(key)
        out.append(x)
    return out


def parse_synonym_discussions(syns: Any) -> List[Dict[str, Any]]:
    discussions = []
    if not isinstance(syns, list):
        return discussions

    for block in syns:
        if not isinstance(block, dict):
            continue
        label = clean_mw_text(block.get("pl") or "synonyms")
        notes = []
        examples = []
        for part in block.get("pt") or []:
            if not isinstance(part, list) or len(part) < 2:
                continue
            tag = part[0]
            val = part[1]
            if tag == "text" and isinstance(val, str):
                text = clean_mw_text(val)
                if text:
                    notes.append(text)
            elif tag == "vis" and isinstance(val, list):
                examples.extend(parse_vis_examples(val))
        see_also = unique_clean_texts(block.get("sarefs") or [])
        discussions.append({
            "label": label,
            "notes": notes,
            "examples": examples,
            "seeAlso": see_also,
        })
    return discussions


def parse_definitions(defs: Any) -> List[Dict[str, Any]]:
    meanings: List[Dict[str, Any]] = []
    if not isinstance(defs, list):
        return meanings

    def add_sense(sense_obj: Dict[str, Any], inherited_label: str = ""):
        label = clean_spaces(sense_obj.get("sn") or inherited_label or "")
        segments = parse_dt_segments(sense_obj.get("dt"))
        if segments:
            meanings.append({
                "label": label,
                "definitionSegments": segments,
            })

        # sdsense means a secondary/subdivided sense, often "especially".
        sd = sense_obj.get("sdsense")
        if isinstance(sd, dict):
            sd_label = clean_spaces(sd.get("sd") or "")
            sd_segments = parse_dt_segments(sd.get("dt"))
            if sd_segments:
                combined_label = (label + " " + sd_label).strip()
                meanings.append({
                    "label": combined_label,
                    "definitionSegments": sd_segments,
                })

    def walk_sseq(x: Any):
        if isinstance(x, list):
            if len(x) >= 2 and x[0] == "sense" and isinstance(x[1], dict):
                add_sense(x[1])
                return
            if len(x) >= 2 and x[0] == "bs" and isinstance(x[1], dict):
                # Binding substitute. It may contain sense-like data.
                if "sense" in x[1] and isinstance(x[1]["sense"], dict):
                    add_sense(x[1]["sense"])
                return
            for y in x:
                walk_sseq(y)

    for d in defs:
        if not isinstance(d, dict):
            continue
        vd = clean_mw_text(d.get("vd") or "")
        before_count = len(meanings)
        walk_sseq(d.get("sseq"))
        if vd and len(meanings) > before_count:
            for m in meanings[before_count:]:
                m.setdefault("verbDivider", vd)

    return meanings


def parse_dt_segments(dt: Any) -> List[Dict[str, Any]]:
    if not isinstance(dt, list):
        return []

    segments: List[Dict[str, Any]] = []
    current: Optional[Dict[str, Any]] = None

    def ensure_current():
        nonlocal current
        if current is None:
            current = {"definition": "", "examples": []}
            segments.append(current)
        return current

    def add_text(value: str):
        nonlocal current
        text = clean_mw_text(value)
        if text:
            current = {"definition": text, "examples": []}
            segments.append(current)

    def add_examples(value: Any):
        examples = parse_vis_examples(value)
        if examples:
            ensure_current()["examples"].extend(examples)

    def walk_uns(value: Any):
        # UNS can be deeply nested:
        # ["uns", [[[ "text", "usually used with..." ], ["vis", [...]]]]]
        if isinstance(value, list):
            if len(value) >= 2 and isinstance(value[0], str):
                tag, val = value[0], value[1]
                if tag == "text" and isinstance(val, str):
                    add_text(val)
                    return
                if tag == "vis" and isinstance(val, list):
                    add_examples(val)
                    return
            for child in value:
                walk_uns(child)

    for item in dt:
        if not isinstance(item, list) or len(item) < 2:
            continue
        tag, val = item[0], item[1]

        if tag == "text" and isinstance(val, str):
            add_text(val)

        elif tag == "vis" and isinstance(val, list):
            add_examples(val)

        elif tag == "uns":
            walk_uns(val)

    clean_segments = []
    seen = set()
    for seg in segments:
        definition = clean_spaces(seg.get("definition") or "")
        examples = dedupe_examples(seg.get("examples") or [])
        if not definition and not examples:
            continue
        key = (definition, tuple(ex.get("text", "") for ex in examples))
        if key in seen:
            continue
        seen.add(key)
        clean_segments.append({"definition": definition, "examples": examples})
    return clean_segments


def parse_vis_examples(vis: Any) -> List[Dict[str, str]]:
    out = []
    if not isinstance(vis, list):
        return out
    for item in vis:
        if not isinstance(item, dict):
            continue
        text = clean_mw_text(item.get("t") or "")
        if not text:
            continue
        aq = item.get("aq") or {}
        author = clean_mw_text(aq.get("auth") or "") if isinstance(aq, dict) else ""
        out.append({"text": text, "author": author})
    return dedupe_examples(out)


def dedupe_examples(examples: List[Dict[str, str]]) -> List[Dict[str, str]]:
    out = []
    seen = set()
    for ex in examples:
        if not isinstance(ex, dict):
            continue
        text = clean_spaces(ex.get("text") or "")
        author = clean_spaces(ex.get("author") or "")
        if not text:
            continue
        key = (text, author)
        if key in seen:
            continue
        seen.add(key)
        out.append({"text": text, "author": author})
    return out


def parse_text_pairs(value: Any) -> List[str]:
    out = []
    if not isinstance(value, list):
        return out
    for item in value:
        if isinstance(item, list) and len(item) >= 2 and item[0] == "text":
            text = clean_mw_text(item[1])
            if text and text not in out:
                out.append(text)
    return out


def first_pronunciation(obj: Dict[str, Any]) -> Dict[str, Any]:
    prs = obj.get("prs")
    if isinstance(prs, list) and prs:
        for p in prs:
            if isinstance(p, dict) and (p.get("mw") or p.get("sound")):
                return p
        if isinstance(prs[0], dict):
            return prs[0]
    return {}


def mw_audio_url(audio: str) -> str:
    audio = str(audio or "").strip()
    if not audio:
        return ""
    if audio.startswith("bix"):
        sub = "bix"
    elif audio.startswith("gg"):
        sub = "gg"
    elif re.match(r"^[0-9_]", audio):
        sub = "number"
    else:
        sub = audio[0]
    return f"https://media.merriam-webster.com/audio/prons/en/us/mp3/{sub}/{audio}.mp3"


def clean_mw_text(s: Any) -> str:
    s = str(s or "")

    # Formatting tags with visible meaning.
    replacements = {
        "{bc}": ": ",
        "{it}": "",
        "{/it}": "",
        "{wi}": "",
        "{/wi}": "",
        "{sc}": "",
        "{/sc}": "",
        "{ldquo}": "“",
        "{rdquo}": "”",
        "{qword}": "",
        "{/qword}": "",
        "{inf}": "",
        "{/inf}": "",
        "{sup}": "",
        "{/sup}": "",
        "{dx}": "",
        "{/dx}": "",
        "{ma}": "",
        "{/ma}": "",
    }
    for a, b in replacements.items():
        s = s.replace(a, b)

    # Keep visible cross-reference text.
    s = re.sub(r"\{sx\|([^|}]+)[^}]*\}", r"\1", s)
    s = re.sub(r"\{d_link\|([^|}]+)[^}]*\}", r"\1", s)
    s = re.sub(r"\{a_link\|([^|}]+)[^}]*\}", r"\1", s)
    s = re.sub(r"\{et_link\|([^|}]+)[^}]*\}", r"\1", s)
    s = re.sub(r"\{mat\|([^|}]+)[^}]*\}", r"\1", s)

    # Drop source-date tags and remaining markup.
    s = re.sub(r"\{ds\|[^}]*\}", "", s)
    s = re.sub(r"\{[^}]+\}", "", s)

    # Clean colon artifacts.
    s = re.sub(r"\s*:\s*:\s*", ": ", s)
    s = re.sub(r"^\s*:\s*", "", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip(" ;,")


def clean_headword(s: Any) -> str:
    s = clean_mw_text(s)
    s = s.replace("*", "")
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def clean_spaces(s: Any) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip()


def clean_list(values: Any) -> List[str]:
    if values is None:
        return []
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, list):
        return []
    out = []
    for v in values:
        text = clean_headword(v)
        if text and text not in out:
            out.append(text)
    return out


def unique_clean_texts(values: Any) -> List[str]:
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, list):
        return []
    out = []
    for v in values:
        text = clean_mw_text(v)
        if text and text not in out:
            out.append(text)
    return out


def normalize_key(s: Any) -> str:
    return clean_headword(s).lower().replace("’", "'")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ----------------------------
# Generated vocabulary storage
# ----------------------------

def default_generated_vocab() -> Dict[str, Any]:
    return {
        "schema_version": "ielts-generated-vocab-v1",
        "updatedAt": now_iso(),
        "items": [],
    }


def load_generated_vocab() -> Dict[str, Any]:
    if GENERATED_VOCAB_BUCKET:
        try:
            from google.cloud import storage
            client = storage.Client()
            bucket = client.bucket(GENERATED_VOCAB_BUCKET)
            blob = bucket.blob(GENERATED_VOCAB_BLOB)
            if not blob.exists():
                return default_generated_vocab()
            data = json.loads(blob.download_as_text())
            if not isinstance(data, dict):
                return default_generated_vocab()
            data.setdefault("items", [])
            return data
        except Exception:
            # Do not crash the app if storage is temporarily unavailable.
            return default_generated_vocab()

    if not LOCAL_GENERATED_VOCAB_PATH.exists():
        return default_generated_vocab()
    try:
        data = json.loads(LOCAL_GENERATED_VOCAB_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return default_generated_vocab()
        data.setdefault("items", [])
        return data
    except Exception:
        return default_generated_vocab()


def save_generated_vocab(data: Dict[str, Any]) -> None:
    if GENERATED_VOCAB_BUCKET:
        from google.cloud import storage
        client = storage.Client()
        bucket = client.bucket(GENERATED_VOCAB_BUCKET)
        blob = bucket.blob(GENERATED_VOCAB_BLOB)
        blob.upload_from_string(json.dumps(data, ensure_ascii=False, indent=2), content_type="application/json")
        return

    LOCAL_GENERATED_VOCAB_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")




# ============================================================
# BACKEND GOOGLE DRIVE SYNC v1
# Real auto-sync after reload:
# - user connects Google Drive once
# - backend stores refresh token
# - frontend auto-syncs through /api/sync/merge without browser Google popup
# ============================================================

BACKEND_SYNC_LOCAL_TOKEN_PATH = BASE_DIR / "vocab_sync_tokens.local.json"
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"
GOOGLE_DRIVE_API = "https://www.googleapis.com/drive/v3"
GOOGLE_DRIVE_UPLOAD = "https://www.googleapis.com/upload/drive/v3"


def _sync_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sync_public_base() -> str:
    return request.host_url.rstrip("/")


def _sync_redirect_uri() -> str:
    return GOOGLE_OAUTH_REDIRECT_URI or (_sync_public_base() + "/api/google/callback")


def _sync_load_store() -> Dict[str, Any]:
    if CLOUD_SYNC_BUCKET:
        try:
            from google.cloud import storage
            client = storage.Client()
            blob = client.bucket(CLOUD_SYNC_BUCKET).blob(CLOUD_SYNC_TOKEN_BLOB)
            if not blob.exists():
                return {"users": {}, "pendingStates": {}}
            return json.loads(blob.download_as_text(encoding="utf-8") or "{}")
        except Exception as e:
            print("Cloud sync token store GCS load failed:", e)
            return {"users": {}, "pendingStates": {}}

    if BACKEND_SYNC_LOCAL_TOKEN_PATH.exists():
        try:
            return json.loads(BACKEND_SYNC_LOCAL_TOKEN_PATH.read_text(encoding="utf-8") or "{}")
        except Exception:
            return {"users": {}, "pendingStates": {}}
    return {"users": {}, "pendingStates": {}}


def _sync_save_store(store: Dict[str, Any]) -> None:
    store.setdefault("users", {})
    store.setdefault("pendingStates", {})

    if CLOUD_SYNC_BUCKET:
        from google.cloud import storage
        client = storage.Client()
        blob = client.bucket(CLOUD_SYNC_BUCKET).blob(CLOUD_SYNC_TOKEN_BLOB)
        blob.upload_from_string(
            json.dumps(store, ensure_ascii=False, indent=2),
            content_type="application/json",
        )
        return

    BACKEND_SYNC_LOCAL_TOKEN_PATH.write_text(
        json.dumps(store, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _sync_signer():
    from itsdangerous import URLSafeSerializer
    if not APP_SECRET_KEY:
        raise RuntimeError("APP_SECRET_KEY must be configured before Google Drive sync is enabled")
    return URLSafeSerializer(APP_SECRET_KEY, salt="ielts-vocab-sync-user-v1")


def _sync_signed_email_from_cookie() -> str:
    raw = request.cookies.get("vocab_sync_user", "")
    if not raw:
        return ""
    try:
        data = _sync_signer().loads(raw)
        return str(data.get("email") or "").strip().lower()
    except Exception:
        return ""


def _sync_set_user_cookie(resp, email: str):
    token = _sync_signer().dumps({"email": email, "iat": int(time.time())})
    resp.set_cookie(
        "vocab_sync_user",
        token,
        max_age=60 * 60 * 24 * 365,
        secure=True,
        httponly=True,
        samesite="Lax",
    )
    return resp


def _sync_clear_user_cookie(resp):
    resp.delete_cookie("vocab_sync_user")
    return resp


def _sync_get_user_record(email: str) -> Optional[Dict[str, Any]]:
    if not email:
        return None
    store = _sync_load_store()
    return (store.get("users") or {}).get(email)


def _sync_store_user_record(email: str, patch: Dict[str, Any]) -> Dict[str, Any]:
    email = email.strip().lower()
    store = _sync_load_store()
    users = store.setdefault("users", {})
    cur = users.get(email, {})
    cur.update(patch)
    cur["email"] = email
    cur["updatedAt"] = _sync_now_iso()
    users[email] = cur
    _sync_save_store(store)
    return cur


def _sync_require_user() -> Tuple[str, Dict[str, Any]]:
    email = _sync_signed_email_from_cookie()
    rec = _sync_get_user_record(email)
    if not email or not rec or not rec.get("refreshToken"):
        raise PermissionError("Google Drive is not connected")
    return email, rec


def _sync_refresh_access_token(email: str, rec: Dict[str, Any]) -> str:
    refresh_token = rec.get("refreshToken", "")
    if not refresh_token:
        raise PermissionError("Missing refresh token")

    resp = requests.post(
        GOOGLE_TOKEN_URL,
        data={
            "client_id": GOOGLE_OAUTH_CLIENT_ID,
            "client_secret": GOOGLE_OAUTH_CLIENT_SECRET,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
        timeout=25,
    )
    if not resp.ok:
        raise RuntimeError(f"Google token refresh failed: {resp.status_code} {resp.text[:300]}")
    data = resp.json()
    return data["access_token"]


def _sync_drive_headers(access_token: str) -> Dict[str, str]:
    return {"Authorization": f"Bearer {access_token}"}


def _sync_drive_find(access_token: str, name: str = "") -> Optional[Dict[str, Any]]:
    name = name or CLOUD_SYNC_DRIVE_FILE_NAME
    q_name = name.replace("'", "\\'")
    resp = requests.get(
        f"{GOOGLE_DRIVE_API}/files",
        headers=_sync_drive_headers(access_token),
        params={
            "q": f"name = '{q_name}' and trashed = false",
            "spaces": "drive",
            "fields": "files(id,name,modifiedTime,mimeType)",
            "pageSize": 10,
        },
        timeout=25,
    )
    if not resp.ok:
        raise RuntimeError(f"Drive find failed: {resp.status_code} {resp.text[:300]}")
    files = resp.json().get("files", [])
    return files[0] if files else None


def _sync_drive_get(access_token: str, file_id: str) -> Dict[str, Any]:
    resp = requests.get(
        f"{GOOGLE_DRIVE_API}/files/{file_id}",
        headers=_sync_drive_headers(access_token),
        params={"alt": "media"},
        timeout=25,
    )
    if not resp.ok:
        raise RuntimeError(f"Drive get failed: {resp.status_code} {resp.text[:300]}")
    try:
        return resp.json()
    except Exception:
        return {}


def _sync_drive_create(access_token: str, payload: Dict[str, Any], name: str = "") -> Dict[str, Any]:
    name = name or CLOUD_SYNC_DRIVE_FILE_NAME
    boundary = "----ieltsvocabbackendboundary"
    metadata = {"name": name, "mimeType": "application/json"}
    content_json = json.dumps(payload, ensure_ascii=False, indent=2)

    multipart_body = (
        f"--{boundary}\r\n"
        "Content-Type: application/json; charset=UTF-8\r\n\r\n"
        f"{json.dumps(metadata)}\r\n"
        f"--{boundary}\r\n"
        "Content-Type: application/json; charset=UTF-8\r\n\r\n"
        f"{content_json}\r\n"
        f"--{boundary}--"
    ).encode("utf-8")

    resp = requests.post(
        f"{GOOGLE_DRIVE_UPLOAD}/files",
        headers={**_sync_drive_headers(access_token), "Content-Type": f"multipart/related; boundary={boundary}"},
        params={"uploadType": "multipart", "fields": "id,name,modifiedTime"},
        data=multipart_body,
        timeout=30,
    )
    if not resp.ok:
        raise RuntimeError(f"Drive create failed: {resp.status_code} {resp.text[:300]}")
    return resp.json()


def _sync_drive_update(access_token: str, file_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    resp = requests.patch(
        f"{GOOGLE_DRIVE_UPLOAD}/files/{file_id}",
        headers={**_sync_drive_headers(access_token), "Content-Type": "application/json; charset=UTF-8"},
        params={"uploadType": "media", "fields": "id,name,modifiedTime"},
        data=json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"),
        timeout=30,
    )
    if not resp.ok:
        raise RuntimeError(f"Drive update failed: {resp.status_code} {resp.text[:300]}")
    return resp.json()


def _sync_arr(v: Any) -> List[str]:
    if isinstance(v, list):
        vals = v
    elif isinstance(v, dict):
        vals = [k for k, ok in v.items() if ok]
    else:
        vals = []
    out, seen = [], set()
    for x in vals:
        s = str(x or "").strip().lower()
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)
    return sorted(out)


def _sync_sessions(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    sessions = (((payload.get("practice") or {}).get("sessions")) or
                ((payload.get("data") or {}).get("practiceHistory")) or
                payload.get("practiceHistory") or [])
    if not isinstance(sessions, list):
        return []

    out = []
    for s in sessions:
        if not isinstance(s, dict):
            continue
        wrong_keys = _sync_arr(s.get("wrongKeys") or [])
        correct_keys = _sync_arr(s.get("correctKeys") or [])
        mastered = _sync_arr(s.get("masteredWords") or [])
        learning = _sync_arr(s.get("learningWords") or wrong_keys)
        out.append({
            "id": str(s.get("id") or s.get("sessionId") or s.get("startedAt") or ""),
            "startedAt": s.get("startedAt") or "",
            "endedAt": s.get("endedAt") or "",
            "mode": s.get("mode") or "",
            "total": int(s.get("total") or s.get("sessionLength") or 0),
            "correct": int(s.get("correct") or s.get("correctAnswered") or 0),
            # wrong = actual wrong answers in this session.
            # learningWords / wrongKeys can still keep the wider set of words needing learning.
            "wrong": max(
                0,
                int(s.get("totalAnswered") or 0) - int(s.get("correctAnswered") or s.get("correct") or 0)
            ),
            "masteredWords": mastered,
            "learningWords": learning,
            "correctKeys": correct_keys,
            "loadedWordKeys": _sync_arr(s.get("loadedWordKeys") or []),
            "poolWordKeys": _sync_arr(s.get("poolWordKeys") or s.get("fullPoolWordKeys") or []),
            "poolConfig": s.get("poolConfig") if isinstance(s.get("poolConfig"), dict) else None,
            "poolDescription": s.get("poolDescription") or "",
            "poolSize": int(s.get("poolSize") or 0),
            "sessionLength": int(s.get("sessionLength") or s.get("total") or 0),
            "studyUntilMastered": bool(s.get("studyUntilMastered")),
            "totalAnswered": int(s.get("totalAnswered") or 0),
            "correctAnswered": int(s.get("correctAnswered") or s.get("correct") or 0),
            "uniqueWordsCorrect": int(s.get("uniqueWordsCorrect") or 0),
            "bestStreak": int(s.get("bestStreak") or 0),
            "wrongKeys": wrong_keys,
            "completed": bool(s.get("completed")) if "completed" in s else True,
        })
    return [s for s in out if s["id"]]


def _sync_goal(payload: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    return ((payload.get("goalTracking") or {}).get("goal") or
            (payload.get("data") or {}).get("goal") or
            payload.get("goal") or {}) or {}


def _sync_daily(payload: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    return ((payload.get("goalTracking") or {}).get("dailyRecord") or
            (payload.get("data") or {}).get("dailyRecord") or
            payload.get("dailyRecord") or {}) or {}


def _sync_lean_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    payload = payload or {}
    ws = payload.get("wordState") or {}
    data = payload.get("data") or {}

    mastered = set(_sync_arr(ws.get("masteredWords") or data.get("known") or payload.get("known") or {}))
    learning = set(_sync_arr(ws.get("learningWords") or data.get("needsReview") or payload.get("needsReview") or {}))

    sessions = _sync_sessions(payload)
    for s in sessions:
        mastered.update(_sync_arr(s.get("masteredWords") or []))
        learning.update(_sync_arr(s.get("learningWords") or s.get("wrongKeys") or []))

    learning.difference_update(mastered)

    now = _sync_now_iso()
    return {
        "schema": "ielts-vocab-cloud-sync-v3",
        "meta": {
            "exportedAt": now,
            "updatedAt": now,
            "mergedAt": now,
            "app": "IELTS Vocabulary Webapp",
            "storageMode": "lean-user-learning-backup",
        },
        "wordState": {
            "masteredWords": sorted(mastered),
            "learningWords": sorted(learning),
        },
        "practice": {"sessions": sessions},
        "goalTracking": {
            "goal": _sync_goal(payload),
            "dailyRecord": _sync_daily(payload),
        },
        "syncInfo": {
            "masteredCount": len(mastered),
            "learningCount": len(learning),
            "sessionCount": len(sessions),
        },
    }


def _sync_merge_payloads(local_payload: Dict[str, Any], remote_payload: Dict[str, Any]) -> Dict[str, Any]:
    local = _sync_lean_payload(local_payload or {})
    remote = _sync_lean_payload(remote_payload or {})

    mastered = set(local["wordState"]["masteredWords"]) | set(remote["wordState"]["masteredWords"])
    learning = (set(local["wordState"]["learningWords"]) | set(remote["wordState"]["learningWords"])) - mastered

    by_id: Dict[str, Dict[str, Any]] = {}
    for s in (remote["practice"]["sessions"] + local["practice"]["sessions"]):
        sid = str(s.get("id") or "")
        if sid:
            by_id[sid] = {**by_id.get(sid, {}), **s}
    sessions = sorted(by_id.values(), key=lambda s: str(s.get("startedAt") or s.get("endedAt") or ""))

    daily = {}
    for src in [remote["goalTracking"].get("dailyRecord") or {}, local["goalTracking"].get("dailyRecord") or {}]:
        if not isinstance(src, dict):
            continue
        for day, row in src.items():
            if not isinstance(row, dict):
                daily[day] = row
                continue
            cur = daily.get(day, {})
            merged = {**cur, **row}
            for field in ["masteredWords", "masteredWordIds", "newWordsMastered", "practicedWords", "practicedWordIds", "sessionIds"]:
                merged[field] = sorted(set(_sync_arr(cur.get(field) or [])) | set(_sync_arr(row.get(field) or [])))
            merged["goalMet"] = bool(cur.get("goalMet") or row.get("goalMet"))
            daily[day] = merged

    goal = local["goalTracking"].get("goal") or remote["goalTracking"].get("goal") or {}

    now = _sync_now_iso()
    return {
        "schema": "ielts-vocab-cloud-sync-v3",
        "meta": {
            "exportedAt": now,
            "updatedAt": now,
            "mergedAt": now,
            "app": "IELTS Vocabulary Webapp",
            "storageMode": "lean-user-learning-backup",
        },
        "wordState": {
            "masteredWords": sorted(mastered),
            "learningWords": sorted(learning),
        },
        "practice": {"sessions": sessions},
        "goalTracking": {"goal": goal, "dailyRecord": daily},
        "syncInfo": {
            "masteredCount": len(mastered),
            "learningCount": len(learning),
            "sessionCount": len(sessions),
        },
    }


def _sync_get_remote_payload_and_file(email: str, rec: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]], str]:
    access_token = _sync_refresh_access_token(email, rec)
    file_id = rec.get("driveFileId") or ""
    file = {"id": file_id} if file_id else None

    if not file:
        file = _sync_drive_find(access_token)

    remote = None
    if file and file.get("id"):
        remote = _sync_drive_get(access_token, file["id"])
        if not rec.get("driveFileId"):
            _sync_store_user_record(email, {"driveFileId": file["id"]})

    return remote, file, access_token


@app.get("/api/google/start")
def api_google_start():
    if not GOOGLE_OAUTH_CLIENT_ID or not GOOGLE_OAUTH_CLIENT_SECRET:
        return jsonify({"detail": "GOOGLE_OAUTH_CLIENT_ID / GOOGLE_OAUTH_CLIENT_SECRET not configured"}), 500

    state = secrets.token_urlsafe(24)
    store = _sync_load_store()
    pending = store.setdefault("pendingStates", {})
    pending[state] = {"createdAt": int(time.time())}
    cutoff = int(time.time()) - 600
    for k in list(pending.keys()):
        if int(pending.get(k, {}).get("createdAt", 0)) < cutoff:
            pending.pop(k, None)
    _sync_save_store(store)

    params = {
        "client_id": GOOGLE_OAUTH_CLIENT_ID,
        "redirect_uri": _sync_redirect_uri(),
        "response_type": "code",
        "scope": "openid email profile https://www.googleapis.com/auth/drive.file",
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state,
    }
    return redirect(GOOGLE_AUTH_URL + "?" + urlencode(params))


@app.get("/api/google/callback")
def api_google_callback():
    code = request.args.get("code", "")
    state = request.args.get("state", "")
    if not code or not state:
        return jsonify({"detail": "Missing OAuth code/state"}), 400

    store = _sync_load_store()
    pending = store.setdefault("pendingStates", {})
    if state not in pending:
        return jsonify({"detail": "Invalid or expired OAuth state"}), 400
    pending.pop(state, None)
    _sync_save_store(store)

    token_resp = requests.post(
        GOOGLE_TOKEN_URL,
        data={
            "code": code,
            "client_id": GOOGLE_OAUTH_CLIENT_ID,
            "client_secret": GOOGLE_OAUTH_CLIENT_SECRET,
            "redirect_uri": _sync_redirect_uri(),
            "grant_type": "authorization_code",
        },
        timeout=25,
    )
    if not token_resp.ok:
        return jsonify({"detail": "Google token exchange failed", "google_response": token_resp.text}), 502

    token_data = token_resp.json()
    access_token = token_data.get("access_token", "")
    refresh_token = token_data.get("refresh_token", "")

    user_resp = requests.get(
        GOOGLE_USERINFO_URL,
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=20,
    )
    if not user_resp.ok:
        return jsonify({"detail": "Google userinfo failed", "google_response": user_resp.text}), 502

    email = str(user_resp.json().get("email") or "").strip().lower()
    if not email:
        return jsonify({"detail": "Google account has no email"}), 400

    existing = _sync_get_user_record(email) or {}
    if not refresh_token:
        refresh_token = existing.get("refreshToken", "")
    if not refresh_token:
        return jsonify({"detail": "No refresh token returned. Reconnect with prompt=consent."}), 400

    _sync_store_user_record(email, {
        "refreshToken": refresh_token,
        "connectedAt": existing.get("connectedAt") or _sync_now_iso(),
    })

    resp = make_response(redirect("/?cloud=connected#settings"))
    _sync_set_user_cookie(resp, email)
    return resp


@app.get("/api/sync/status")
def api_sync_status():
    email = _sync_signed_email_from_cookie()
    rec = _sync_get_user_record(email) if email else None
    return jsonify({
        "ok": True,
        "connected": bool(rec and rec.get("refreshToken")),
        "email": email if rec else "",
        "driveFileId": (rec or {}).get("driveFileId", ""),
        "tokenStorage": "gcs" if CLOUD_SYNC_BUCKET else "local-file",
    })


@app.post("/api/sync/disconnect")
def api_sync_disconnect():
    email = _sync_signed_email_from_cookie()
    if email:
        store = _sync_load_store()
        (store.get("users") or {}).pop(email, None)
        _sync_save_store(store)
    resp = make_response(jsonify({"ok": True, "connected": False}))
    _sync_clear_user_cookie(resp)
    return resp


@app.get("/api/sync/pull")
def api_sync_pull():
    try:
        email, rec = _sync_require_user()
        remote, file, access_token = _sync_get_remote_payload_and_file(email, rec)
        # Return the Drive file exactly as stored.
        # Do not call _sync_lean_payload() here, because it can collapse
        # wordState arrays / practice sessions into summary counts.
        payload = remote or {}
        return jsonify({"ok": True, "payload": payload, "file": file})
    except PermissionError as e:
        return jsonify({"ok": False, "detail": str(e), "connectUrl": "/api/google/start"}), 401
    except Exception as e:
        return jsonify({"ok": False, "detail": str(e)}), 500


# ============================================================
# CLEAN SYNC v6 — server-side union merge (mirrors app.js cleanSyncV6).
# Backend merges too, so concurrent devices converge regardless of order.
# ============================================================
V6_SCHEMA = "ielts-vocab-sync-v6"


def _v6_norm(v) -> str:
    return str(v or "").strip().lower()


def _v6_is_obj(o) -> bool:
    return isinstance(o, dict)


def _v6_max_iso(*vals) -> str:
    xs = sorted([str(v) for v in vals if v])
    return xs[-1] if xs else ""


def _v6_looks_v6(p) -> bool:
    return _v6_is_obj(p) and (
        p.get("schema") == V6_SCHEMA
        or _v6_is_obj(p.get("knownMeaning"))
        or _v6_is_obj(p.get("knownSpelling"))
        or _v6_is_obj(p.get("progress"))
    )


def _v6_upgrade(p) -> Dict[str, Any]:
    """Convert any payload (v6 or old v3/v4/v5) into the v6 shape."""
    if not _v6_is_obj(p):
        return {}
    if _v6_looks_v6(p):
        return {
            "schema": V6_SCHEMA,
            "meta": p.get("meta") if _v6_is_obj(p.get("meta")) else {"updatedAt": _sync_now_iso()},
            "knownMeaning": p.get("knownMeaning") if _v6_is_obj(p.get("knownMeaning")) else {},
            "knownSpelling": p.get("knownSpelling") if _v6_is_obj(p.get("knownSpelling")) else {},
            "progress": p.get("progress") if _v6_is_obj(p.get("progress")) else {},
            "legacyKnown": p.get("legacyKnown") if _v6_is_obj(p.get("legacyKnown")) else {},
            "goal": p.get("goal") if _v6_is_obj(p.get("goal")) else {"v2": None, "v1": None},
            "daily": p.get("daily") if _v6_is_obj(p.get("daily")) else {"v2": {}, "v1": {}},
            "practice": p.get("practice") if _v6_is_obj(p.get("practice")) else {"sessions": [], "lifetimeSessions": 0},
            "preferences": p.get("preferences") if _v6_is_obj(p.get("preferences")) else {},
        }

    ws = p.get("wordState") if _v6_is_obj(p.get("wordState")) else {}
    meta = p.get("meta") if _v6_is_obj(p.get("meta")) else {}
    updated_at = meta.get("updatedAt") or meta.get("mergedAt") or meta.get("exportedAt") or _sync_now_iso()

    meaning_known = _sync_arr(ws.get("meaningKnownWords"))
    spelling_known = _sync_arr(ws.get("spellingKnownWords"))
    if not meaning_known and not spelling_known:
        mastered = _sync_arr(ws.get("masteredWords"))
        known_only = _sync_arr(ws.get("knownWords"))
        meaning_known = list(mastered) + list(known_only)
        spelling_known = list(mastered)

    meaning_learning = _sync_arr(ws.get("meaningLearningWords"))
    spelling_learning = _sync_arr(ws.get("spellingLearningWords"))
    if not meaning_learning and not spelling_learning:
        learn = _sync_arr(ws.get("learningWords"))
        meaning_learning = list(learn)
        spelling_learning = list(learn)

    known_meaning = {}
    known_spelling = {}
    for w in meaning_known:
        n = _v6_norm(w)
        if n:
            known_meaning[n] = updated_at
    for w in spelling_known:
        n = _v6_norm(w)
        if n:
            known_spelling[n] = updated_at

    # Synthesize progress so learning words still render as "learning".
    prog: Dict[str, Any] = {}
    for w in meaning_learning:
        n = _v6_norm(w)
        if n and n not in known_meaning:
            prog.setdefault(n, {})["matching"] = {"attempts": 1, "correct": 0}
    for w in spelling_learning:
        n = _v6_norm(w)
        if n and n not in known_spelling:
            prog.setdefault(n, {})["spelling"] = {"attempts": 1, "correct": 0}

    gt = p.get("goalTracking") if _v6_is_obj(p.get("goalTracking")) else {}
    pr = p.get("practice") if _v6_is_obj(p.get("practice")) else {}
    sessions = [s for s in pr.get("sessions", []) if _v6_is_obj(s)] if isinstance(pr.get("sessions"), list) else []

    return {
        "schema": V6_SCHEMA,
        "meta": {"app": "IELTS Vocabulary Webapp", "source": "upgraded-from-" + str(p.get("schema") or "old"), "updatedAt": updated_at},
        "knownMeaning": known_meaning,
        "knownSpelling": known_spelling,
        "progress": prog,
        "legacyKnown": {},
        "goal": {"v2": gt.get("goalV2"), "v1": gt.get("goal")},
        "daily": {"v2": gt.get("dailyRecordV2") or {}, "v1": gt.get("dailyRecord") or {}},
        "practice": {"sessions": sessions, "lifetimeSessions": pr.get("lifetimeSessions") or len(sessions), "updatedAt": pr.get("practiceHistoryUpdatedAt") or updated_at},
        "preferences": p.get("preferences") if _v6_is_obj(p.get("preferences")) else {},
    }


def _v6_merge_known(a, b) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for m in (a, b):
        if _v6_is_obj(m):
            for k, v in m.items():
                n = _v6_norm(k)
                if n:
                    out[n] = _v6_max_iso(out.get(n, ""), v)
    return out


def _v6_merge_block(a, b) -> Dict[str, int]:
    a = a if _v6_is_obj(a) else {}
    b = b if _v6_is_obj(b) else {}
    return {
        "attempts": max(int(a.get("attempts") or 0), int(b.get("attempts") or 0)),
        "correct": max(int(a.get("correct") or 0), int(b.get("correct") or 0)),
    }


def _v6_merge_progress(a, b) -> Dict[str, Any]:
    a = a if _v6_is_obj(a) else {}
    b = b if _v6_is_obj(b) else {}
    out: Dict[str, Any] = {}
    for k in set(list(a.keys()) + list(b.keys())):
        ra = a.get(k) if _v6_is_obj(a.get(k)) else {}
        rb = b.get(k) if _v6_is_obj(b.get(k)) else {}
        merged: Dict[str, Any] = {}
        for sk in ("matching", "wordToMeaning", "meaningToWord", "spelling"):
            if _v6_is_obj(ra.get(sk)) or _v6_is_obj(rb.get(sk)):
                merged[sk] = _v6_merge_block(ra.get(sk), rb.get(sk))
        merged["_consecMeaning"] = max(int(ra.get("_consecMeaning") or 0), int(rb.get("_consecMeaning") or 0))
        merged["_consecSpelling"] = max(int(ra.get("_consecSpelling") or 0), int(rb.get("_consecSpelling") or 0))
        if _v6_progress_meaningful(merged):  # drop junk so Drive converges (no perpetual re-download)
            out[k] = merged
    return out


def _v6_progress_meaningful(r) -> bool:
    """Mirror of the client's hasMeaningfulProgressRecord: keep only records with
    real practice signal, so Drive doesn't accumulate empty entries that the
    client strips on save (which would cause an endless 'downloaded' diff)."""
    if not _v6_is_obj(r):
        return False
    if int(r.get("attempts") or 0) > 0 or int(r.get("correct") or 0) > 0 or int(r.get("wrong") or 0) > 0 or int(r.get("_consecCorrect") or 0) > 0:
        return True
    for v in r.values():
        if _v6_is_obj(v) and (int(v.get("attempts") or 0) > 0 or int(v.get("correct") or 0) > 0 or int(v.get("wrong") or 0) > 0 or v.get("lastAttemptAt") or v.get("lastCorrectAt")):
            return True
    return False


def _v6_merge_sessions(a, b) -> List[Dict[str, Any]]:
    by_id: Dict[str, Dict[str, Any]] = {}
    for s in (b if isinstance(b, list) else []) + (a if isinstance(a, list) else []):
        if not _v6_is_obj(s):
            continue
        sid = str(s.get("id") or s.get("sessionId") or s.get("startedAt") or "")
        if not sid:
            continue
        by_id[sid] = {**by_id.get(sid, {}), **s}
    out = sorted(by_id.values(), key=lambda s: str(s.get("startedAt") or s.get("endedAt") or ""))
    return out


def _v6_pick_newer(a, b):
    ta = (a or {}).get("updatedAt", "") if _v6_is_obj(a) else ""
    tb = (b or {}).get("updatedAt", "") if _v6_is_obj(b) else ""
    if a and not b:
        return a
    if b and not a:
        return b
    return a if str(ta) >= str(tb) else b


def _v6_merge_daily(a, b) -> Dict[str, Any]:
    a = a if _v6_is_obj(a) else {}
    b = b if _v6_is_obj(b) else {}
    out: Dict[str, Any] = {}
    for d in set(list(a.keys()) + list(b.keys())):
        ra, rb = a.get(d), b.get(d)
        if not _v6_is_obj(ra) or not _v6_is_obj(rb):
            out[d] = ra or rb
            continue
        m = {**ra, **rb}
        if _v6_is_obj(ra.get("events")) or _v6_is_obj(rb.get("events")):
            ev = {}
            for f in ("meaningKnown", "spellingKnown", "mastered"):
                av = (ra.get("events") or {}).get(f) or []
                bv = (rb.get("events") or {}).get(f) or []
                ev[f] = sorted(set(_sync_arr(av)) | set(_sync_arr(bv)))
            m["events"] = ev
        for f in ("newWordsMastered", "practicedWords", "sessionIds"):
            if isinstance(ra.get(f), list) or isinstance(rb.get(f), list):
                m[f] = sorted(set(_sync_arr(ra.get(f))) | set(_sync_arr(rb.get(f))))
        m["goalMet"] = bool(ra.get("goalMet") or rb.get("goalMet"))
        out[d] = m
    return out


def _v6_merge_prefs(a, b) -> Dict[str, Any]:
    a = a if _v6_is_obj(a) else {}
    b = b if _v6_is_obj(b) else {}

    def pick(field, ts):
        at, bt = a.get(ts) or "", b.get(ts) or ""
        return (a.get(field), at) if str(at) >= str(bt) else (b.get(field), bt)

    dv, dt = pick("dictionarySource", "dictionarySourceUpdatedAt")
    zv, zt = pick("translationMode", "translationModeUpdatedAt")
    iv, it = pick("modeIntroDone", "modeIntroUpdatedAt")
    return {
        "dictionarySource": dv if dv in ("learner", "collegiate") else (a.get("dictionarySource") or b.get("dictionarySource") or "collegiate"),
        "dictionarySourceUpdatedAt": dt,
        "translationMode": zv if zv in ("on", "blur", "off") else (a.get("translationMode") or b.get("translationMode") or "off"),
        "translationModeUpdatedAt": zt,
        "modeIntroDone": bool(iv),
        "modeIntroUpdatedAt": it,
    }


def _v6_merge(local_payload, remote_payload) -> Dict[str, Any]:
    L = _v6_upgrade(local_payload) or {}
    R = _v6_upgrade(remote_payload) or {}
    lp = L.get("practice") or {}
    rp = R.get("practice") or {}
    sessions = _v6_merge_sessions(lp.get("sessions"), rp.get("sessions"))
    return {
        "schema": V6_SCHEMA,
        "meta": {"app": "IELTS Vocabulary Webapp", "source": "backend-merge-v6", "updatedAt": _sync_now_iso()},
        "knownMeaning": _v6_merge_known(L.get("knownMeaning"), R.get("knownMeaning")),
        "knownSpelling": _v6_merge_known(L.get("knownSpelling"), R.get("knownSpelling")),
        "progress": _v6_merge_progress(L.get("progress"), R.get("progress")),
        "legacyKnown": {**(R.get("legacyKnown") or {}), **(L.get("legacyKnown") or {})},
        "goal": {
            "v2": _v6_pick_newer((L.get("goal") or {}).get("v2"), (R.get("goal") or {}).get("v2")),
            "v1": _v6_pick_newer((L.get("goal") or {}).get("v1"), (R.get("goal") or {}).get("v1")),
        },
        "daily": {
            "v2": _v6_merge_daily((L.get("daily") or {}).get("v2"), (R.get("daily") or {}).get("v2")),
            "v1": _v6_merge_daily((L.get("daily") or {}).get("v1"), (R.get("daily") or {}).get("v1")),
        },
        "practice": {
            "sessions": sessions,
            # Distinct sessions only — _v6_merge_sessions already dedupes by id.
            # Never carry forward an inflated counter from old clients.
            "lifetimeSessions": len(sessions),
            "updatedAt": _v6_max_iso(lp.get("updatedAt"), rp.get("updatedAt")),
        },
        "preferences": _v6_merge_prefs(L.get("preferences"), R.get("preferences")),
    }


@app.post("/api/sync/merge")
def api_sync_merge():
    try:
        email, rec = _sync_require_user()
        body = request.get_json(silent=True) or {}
        local_payload = body.get("payload") or body

        remote, file, access_token = _sync_get_remote_payload_and_file(email, rec)

        # Clean v6 path: union-merge local with whatever is on Drive, write back.
        merged = _v6_merge(local_payload, remote or {})
        merged["syncInfo"] = {
            "knownMeaningCount": len(merged.get("knownMeaning") or {}),
            "knownSpellingCount": len(merged.get("knownSpelling") or {}),
            "learningCount": len(merged.get("progress") or {}),
            "sessionCount": len((merged.get("practice") or {}).get("sessions") or []),
            "lifetimeSessions": (merged.get("practice") or {}).get("lifetimeSessions"),
            "schemaVersion": 6,
        }

        if not file:
            created = _sync_drive_create(access_token, merged)
            _sync_store_user_record(email, {"driveFileId": created.get("id", "")})
            file = created
        else:
            _sync_drive_update(access_token, file["id"], merged)

        return jsonify({"ok": True, "status": "backend-merged-v6", "payload": merged, "file": file})
    except PermissionError as e:
        return jsonify({"ok": False, "detail": str(e), "connectUrl": "/api/google/start"}), 401
    except Exception as e:
        return jsonify({"ok": False, "detail": str(e)}), 500


def _api_sync_merge_legacy_DISABLED():
    """Old v5/v4 merge path, kept for reference only. No longer routed."""
    try:
        local_payload, remote, file, access_token = {}, {}, None, ""
        # Preserve frontend v5 per-skill payloads exactly.
        # Older backend merge/normalise paths may collapse arrays into counts,
        # which breaks restore on other devices.
        ws = (local_payload or {}).get("wordState") or {}
        pr = (local_payload or {}).get("practice") or {}

        is_v5_array_payload = (
            (local_payload or {}).get("schema") == "ielts-vocab-sync-v5-per-skill"
            and isinstance(ws.get("masteredWords"), list)
            and isinstance(ws.get("knownWords"), list)
            and isinstance(ws.get("learningWords"), list)
            and isinstance(ws.get("meaningKnownWords"), list)
            and isinstance(ws.get("spellingKnownWords"), list)
            and isinstance(ws.get("meaningLearningWords"), list)
            and isinstance(ws.get("spellingLearningWords"), list)
            and isinstance(pr.get("sessions"), list)
        )

        remote_ws = (remote or {}).get("wordState") or {}
        remote_pr = (remote or {}).get("practice") or {}
        remote_is_clean_v5 = (
            (remote or {}).get("schema") == "ielts-vocab-sync-v5-per-skill"
            and isinstance(remote_ws.get("meaningKnownWords"), list)
            and isinstance(remote_ws.get("spellingKnownWords"), list)
            and isinstance(remote_ws.get("meaningLearningWords"), list)
            and isinstance(remote_ws.get("spellingLearningWords"), list)
            and isinstance(remote_pr.get("sessions"), list)
        )

        # Emergency protection:
        # If Drive already has clean v5 arrays, do not let an older/dirty cached
        # device send a non-v5 or malformed payload through backend-auto-merge.
        if remote_is_clean_v5 and not is_v5_array_payload:
            merged = remote or {}
            merged["meta"] = {
                **(merged.get("meta") or {}),
                "source": "backend-kept-clean-v5-blocked-dirty-merge",
                "blockedDirtyMergeAt": _sync_now_iso(),
            }
            merged["syncInfo"] = {
                **(merged.get("syncInfo") or {}),
                "lastSyncType": "blocked-dirty-merge",
            }

        elif is_v5_array_payload:
            now = _sync_now_iso()
            merged = dict(local_payload)
            merged["schema"] = "ielts-vocab-sync-v5-per-skill"
            merged["meta"] = {
                **(merged.get("meta") or {}),
                "app": "IELTS Vocabulary Webapp",
                "storageMode": "per-skill-user-learning-backup",
                "source": "backend-preserved-v5-arrays",
                "exportedAt": now,
                "mergedAt": now,
                "updatedAt": ((merged.get("meta") or {}).get("updatedAt") or now),
            }
            merged["syncInfo"] = {
                **(merged.get("syncInfo") or {}),
                "masteredCount": len(ws.get("masteredWords") or []),
                "knownCount": len(ws.get("knownWords") or []),
                "learningCount": len(ws.get("learningWords") or []),
                "meaningKnownCount": len(ws.get("meaningKnownWords") or []),
                "spellingKnownCount": len(ws.get("spellingKnownWords") or []),
                "meaningLearningCount": len(ws.get("meaningLearningWords") or []),
                "spellingLearningCount": len(ws.get("spellingLearningWords") or []),
                "sessionCount": len(pr.get("sessions") or []),
                "lifetimeSessions": pr.get("lifetimeSessions"),
                "schemaVersion": 5,
                "lastSyncType": "backend-preserved-v5-arrays",
            }
        else:
            merged = _sync_merge_payloads(local_payload, remote or {})

        if not file:
            created = _sync_drive_create(access_token, merged)
            _sync_store_user_record(email, {"driveFileId": created.get("id", "")})
            file = created
        else:
            _sync_drive_update(access_token, file["id"], merged)

        return jsonify({"ok": True, "status": "backend-merged-pushed", "payload": merged, "file": file})
    except PermissionError as e:
        return jsonify({"ok": False, "detail": str(e), "connectUrl": "/api/google/start"}), 401
    except Exception as e:
        return jsonify({"ok": False, "detail": str(e)}), 500




# ============================================================
# BACKEND LEAN JSON v4 OVERRIDES
# Keeps backward compatibility with v3 but writes v4.
# ============================================================

def _sync_word_state_from_payload_v4(payload: Dict[str, Any]) -> Tuple[set, set, Dict[str, str]]:
    payload = payload or {}
    ws = payload.get("wordState") or {}
    data = payload.get("data") or {}

    mastered = set(_sync_arr(ws.get("masteredWords") or data.get("known") or payload.get("known") or {}))
    learning = set(_sync_arr(ws.get("learningWords") or data.get("needsReview") or payload.get("needsReview") or {}))
    learning.difference_update(mastered)

    word_updated_at = ws.get("wordUpdatedAt") or {}
    if not isinstance(word_updated_at, dict):
        word_updated_at = {}

    fallback = (
        (payload.get("meta") or {}).get("updatedAt")
        or (payload.get("meta") or {}).get("exportedAt")
        or payload.get("updatedAt")
        or _sync_now_iso()
    )

    per_skill_words = set()
    for field in ["meaningKnownWords", "spellingKnownWords", "meaningLearningWords", "spellingLearningWords"]:
        per_skill_words.update(_sync_arr(ws.get(field) or []))

    clean_ts: Dict[str, str] = {}
    for w in mastered | learning | per_skill_words:
        clean_ts[w] = str(word_updated_at.get(w) or fallback)

    return mastered, learning, clean_ts


def _sync_per_skill_word_state_from_payload_v4(payload: Dict[str, Any]) -> Dict[str, set]:
    payload = payload or {}
    ws = payload.get("wordState") or {}

    mastered, learning, _ = _sync_word_state_from_payload_v4(payload)

    has_meaning_known = isinstance(ws.get("meaningKnownWords"), list)
    has_spelling_known = isinstance(ws.get("spellingKnownWords"), list)
    has_meaning_learning = isinstance(ws.get("meaningLearningWords"), list)
    has_spelling_learning = isinstance(ws.get("spellingLearningWords"), list)

    meaning_known = set(_sync_arr(ws.get("meaningKnownWords") or []))
    spelling_known = set(_sync_arr(ws.get("spellingKnownWords") or []))

    # Legacy payloads only know "mastered", so preserve those as full mastered.
    if not has_meaning_known and not has_spelling_known:
      meaning_known.update(mastered)
      spelling_known.update(mastered)
    else:
      meaning_known.update(mastered)
      spelling_known.update(mastered)

    meaning_learning = set(_sync_arr(ws.get("meaningLearningWords") or []))
    spelling_learning = set(_sync_arr(ws.get("spellingLearningWords") or []))

    # Legacy aggregate learningWords has no axis; keep the frontend rule:
    # import/merge it as Meaning Learning only.
    if not has_meaning_learning and not has_spelling_learning:
      meaning_learning.update(learning)

    meaning_learning.difference_update(meaning_known)
    spelling_learning.difference_update(spelling_known)

    return {
        "meaningKnownWords": meaning_known,
        "spellingKnownWords": spelling_known,
        "meaningLearningWords": meaning_learning,
        "spellingLearningWords": spelling_learning,
    }


def _sync_axis_timestamps_from_payload_v4(
    payload: Dict[str, Any],
    per_skill: Optional[Dict[str, set]] = None,
) -> Tuple[Dict[str, str], Dict[str, str]]:
    payload = payload or {}
    ws = payload.get("wordState") or {}
    if per_skill is None:
        per_skill = _sync_per_skill_word_state_from_payload_v4(payload)
    _, _, generic_ts = _sync_word_state_from_payload_v4(payload)

    meaning_raw = ws.get("meaningWordUpdatedAt") or {}
    spelling_raw = ws.get("spellingWordUpdatedAt") or {}
    if not isinstance(meaning_raw, dict):
        meaning_raw = {}
    if not isinstance(spelling_raw, dict):
        spelling_raw = {}

    fallback = (
        (payload.get("meta") or {}).get("updatedAt")
        or (payload.get("meta") or {}).get("exportedAt")
        or payload.get("updatedAt")
        or _sync_now_iso()
    )

    meaning_words = set(per_skill.get("meaningKnownWords") or set()) | set(per_skill.get("meaningLearningWords") or set())
    spelling_words = set(per_skill.get("spellingKnownWords") or set()) | set(per_skill.get("spellingLearningWords") or set())

    meaning_ts = {
        w: str(meaning_raw.get(w) or generic_ts.get(w) or fallback)
        for w in meaning_words
    }
    spelling_ts = {
        w: str(spelling_raw.get(w) or generic_ts.get(w) or fallback)
        for w in spelling_words
    }
    return meaning_ts, spelling_ts


def _sync_related_created_words_v4(payload: Dict[str, Any]) -> List[Dict[str, str]]:
    ws = (payload or {}).get("wordState") or {}
    raw = ws.get("relatedCreatedWords") or []
    if not isinstance(raw, list):
        return []
    out: Dict[str, Dict[str, str]] = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        key = _sync_arr([item.get("key") or item.get("word") or item.get("term")])
        if not key:
            continue
        k = key[0]
        out[k] = {
            "key": k,
            "word": str(item.get("word") or item.get("term") or k).strip() or k,
            "sourceFromKey": (_sync_arr([item.get("sourceFromKey") or item.get("originKey") or ""]) or [""])[0],
            "sourceFromWord": str(item.get("sourceFromWord") or item.get("originWord") or "").strip(),
        }
    return sorted(out.values(), key=lambda x: x.get("word", ""))


def _sync_sessions_v4(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    sessions = _sync_sessions(payload)

    out = []
    for s in sessions:
        total_answered = int(s.get("totalAnswered") or 0)
        correct_answered = int(s.get("correctAnswered") or s.get("correct") or 0)

        row = dict(s)
        row["updatedAt"] = row.get("updatedAt") or row.get("endedAt") or row.get("startedAt") or _sync_now_iso()
        row["wrong"] = max(0, total_answered - correct_answered)
        row["masteredWords"] = _sync_arr(row.get("masteredWords") or [])
        row["learningWords"] = _sync_arr(row.get("learningWords") or row.get("wrongKeys") or [])
        row["wrongKeys"] = _sync_arr(row.get("wrongKeys") or [])
        row["correctKeys"] = _sync_arr(row.get("correctKeys") or [])
        row["loadedWordKeys"] = _sync_arr(row.get("loadedWordKeys") or [])
        row["poolWordKeys"] = _sync_arr(row.get("poolWordKeys") or [])
        if not isinstance(row.get("poolConfig"), dict):
            row["poolConfig"] = None
        out.append(row)

    return out


def _sync_last_loaded_pool_v4(payload: Dict[str, Any]) -> Dict[str, Any]:
    pool = ((payload or {}).get("practice") or {}).get("lastLoadedPool") or {}
    if not isinstance(pool, dict):
        return {}

    word_keys = _sync_arr(pool.get("wordKeys") or [])
    if not word_keys:
        return {}

    out = {
        "sessionId": str(pool.get("sessionId") or ""),
        "wordKeys": word_keys,
        "savedAt": str(pool.get("savedAt") or (payload.get("meta") or {}).get("updatedAt") or _sync_now_iso()),
    }
    if isinstance(pool.get("poolConfig"), dict):
        out["poolConfig"] = pool.get("poolConfig")
    return out


def _sync_goal_v4(payload: Dict[str, Any]) -> Dict[str, Any]:
    goal = _sync_goal(payload)
    if not isinstance(goal, dict):
        goal = {}

    fallback = (
        (payload.get("meta") or {}).get("updatedAt")
        or (payload.get("meta") or {}).get("exportedAt")
        or _sync_now_iso()
    )

    goal = dict(goal)
    goal["updatedAt"] = goal.get("updatedAt") or fallback
    return goal


def _sync_daily_v4(payload: Dict[str, Any]) -> Dict[str, Any]:
    daily = _sync_daily(payload)
    if not isinstance(daily, dict):
        return {}

    fallback = (
        (payload.get("meta") or {}).get("updatedAt")
        or (payload.get("meta") or {}).get("exportedAt")
        or _sync_now_iso()
    )

    out = {}
    for day, row in daily.items():
        if not isinstance(row, dict):
            out[day] = row
            continue
        r = dict(row)
        r["updatedAt"] = r.get("updatedAt") or fallback
        out[day] = r

    return out


def _sync_goal_v2(payload: Dict[str, Any]) -> Dict[str, Any]:
    gt = (payload or {}).get("goalTracking") or {}
    goal = gt.get("goalV2") or {}
    if not isinstance(goal, dict):
        return {}

    fallback = (
        (payload.get("meta") or {}).get("updatedAt")
        or (payload.get("meta") or {}).get("exportedAt")
        or _sync_now_iso()
    )

    out = dict(goal)
    out["updatedAt"] = out.get("updatedAt") or fallback
    return out


def _sync_daily_v2(payload: Dict[str, Any]) -> Dict[str, Any]:
    gt = (payload or {}).get("goalTracking") or {}
    daily = gt.get("dailyRecordV2") or {}
    if not isinstance(daily, dict):
        return {}

    fallback = (
        (payload.get("meta") or {}).get("updatedAt")
        or (payload.get("meta") or {}).get("exportedAt")
        or _sync_now_iso()
    )

    out: Dict[str, Any] = {}
    for day, row in daily.items():
        if not isinstance(row, dict):
            out[day] = row
            continue
        r = dict(row)
        r["updatedAt"] = r.get("updatedAt") or fallback
        if isinstance(r.get("events"), dict):
            events = dict(r.get("events") or {})
            for field in ["meaningKnown", "spellingKnown", "mastered", "meaningLearning", "spellingLearning"]:
                events[field] = _sync_arr(events.get(field) or [])
            r["events"] = events
        out[day] = r
    return out


def _sync_preferences_v4(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Normalise synced preferences without dropping timestamp fields.

    Important:
    dictionarySourceUpdatedAt and translationModeUpdatedAt must survive backend
    merge/write. Otherwise a second browser window cannot know which preference
    is newer.
    """
    prefs = (payload or {}).get("preferences") or {}
    if not isinstance(prefs, dict):
        prefs = {}

    meta = (payload or {}).get("meta") or {}
    fallback_ts = str(
        prefs.get("preferencesUpdatedAt")
        or prefs.get("modeIntroUpdatedAt")
        or meta.get("updatedAt")
        or meta.get("mergedAt")
        or meta.get("exportedAt")
        or ""
    )

    dictionary_source = prefs.get("dictionarySource")
    if dictionary_source not in ("learner", "collegiate"):
        dictionary_source = "learner"

    translation_mode = prefs.get("translationMode")
    if translation_mode not in ("off", "blur", "on"):
        translation_mode = "off"

    dictionary_ts = str(
        prefs.get("dictionarySourceUpdatedAt")
        or prefs.get("preferencesUpdatedAt")
        or fallback_ts
        or ""
    )

    translation_ts = str(
        prefs.get("translationModeUpdatedAt")
        or prefs.get("preferencesUpdatedAt")
        or fallback_ts
        or ""
    )

    intro_ts = str(
        prefs.get("modeIntroUpdatedAt")
        or prefs.get("preferencesUpdatedAt")
        or fallback_ts
        or ""
    )

    preferences_updated_at = max(
        str(prefs.get("preferencesUpdatedAt") or ""),
        dictionary_ts,
        translation_ts,
        intro_ts,
        fallback_ts,
    )

    return {
        "dictionarySource": dictionary_source,
        "dictionarySourceUpdatedAt": dictionary_ts,
        "translationMode": translation_mode,
        "translationModeUpdatedAt": translation_ts,
        "modeIntroDone": bool(prefs.get("modeIntroDone")),
        "modeIntroUpdatedAt": intro_ts,
        "preferencesUpdatedAt": preferences_updated_at,
    }
def _sync_pref_ts(prefs: Dict[str, Any], field: str) -> str:
    if not isinstance(prefs, dict):
        return ""
    if field == "dictionarySource":
        return str(
            prefs.get("dictionarySourceUpdatedAt")
            or prefs.get("preferencesUpdatedAt")
            or prefs.get("modeIntroUpdatedAt")
            or ""
        )
    if field == "translationMode":
        return str(
            prefs.get("translationModeUpdatedAt")
            or prefs.get("preferencesUpdatedAt")
            or prefs.get("modeIntroUpdatedAt")
            or ""
        )
    if field == "modeIntroDone":
        return str(
            prefs.get("modeIntroUpdatedAt")
            or prefs.get("preferencesUpdatedAt")
            or ""
        )
    return ""


def _sync_merge_preferences_v4(local_payload: Dict[str, Any], remote_payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Merge preferences by field timestamp.

    Important:
    Preferences are not like word progress.
    - wordState / practice sessions can be combined elsewhere.
    - dictionarySource and translationMode are single-value settings.
    - therefore each field needs its own updatedAt comparison.

    This prevents a stale browser window from forcing old Drive preferences back.
    """
    local = _sync_preferences_v4(local_payload or {})
    remote = _sync_preferences_v4(remote_payload or {})

    merged: Dict[str, Any] = dict(remote or {})

    # dictionarySource: newer field timestamp wins.
    if "dictionarySource" in local:
        local_ts = _sync_pref_ts(local, "dictionarySource")
        remote_ts = _sync_pref_ts(remote, "dictionarySource")
        if not remote.get("dictionarySource") or not remote_ts or local_ts >= remote_ts:
            merged["dictionarySource"] = "learner" if local.get("dictionarySource") == "learner" else "collegiate"
            merged["dictionarySourceUpdatedAt"] = local_ts or str(
                (local_payload or {}).get("meta", {}).get("updatedAt") or ""
            )

    # translationMode: newer field timestamp wins.
    if "translationMode" in local:
        local_ts = _sync_pref_ts(local, "translationMode")
        remote_ts = _sync_pref_ts(remote, "translationMode")
        if not remote.get("translationMode") or not remote_ts or local_ts >= remote_ts:
            mode = local.get("translationMode")
            merged["translationMode"] = mode if mode in ("off", "blur", "on") else "off"
            merged["translationModeUpdatedAt"] = local_ts or str(
                (local_payload or {}).get("meta", {}).get("updatedAt") or ""
            )

    # modeIntroDone: newer intro timestamp wins.
    if "modeIntroDone" in local:
        local_ts = _sync_pref_ts(local, "modeIntroDone")
        remote_ts = _sync_pref_ts(remote, "modeIntroDone")
        if "modeIntroDone" not in remote or not remote_ts or local_ts >= remote_ts:
            merged["modeIntroDone"] = bool(local.get("modeIntroDone"))
            merged["modeIntroUpdatedAt"] = local_ts or str(
                (local_payload or {}).get("meta", {}).get("updatedAt") or ""
            )

    # Preserve remote field timestamps if remote wins.
    if "dictionarySource" in remote and "dictionarySource" not in merged:
        merged["dictionarySource"] = remote.get("dictionarySource")
    if "translationMode" in remote and "translationMode" not in merged:
        merged["translationMode"] = remote.get("translationMode")

    merged["preferencesUpdatedAt"] = max(
        str(merged.get("dictionarySourceUpdatedAt") or ""),
        str(merged.get("translationModeUpdatedAt") or ""),
        str(merged.get("modeIntroUpdatedAt") or ""),
        str(merged.get("preferencesUpdatedAt") or ""),
        str((local_payload or {}).get("meta", {}).get("updatedAt") or ""),
        str((remote_payload or {}).get("meta", {}).get("updatedAt") or ""),
    )

    return merged
def _sync_lean_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    payload = payload or {}

    mastered, learning, word_updated_at = _sync_word_state_from_payload_v4(payload)
    per_skill = _sync_per_skill_word_state_from_payload_v4(payload)
    meaning_word_updated_at, spelling_word_updated_at = _sync_axis_timestamps_from_payload_v4(payload, per_skill)
    related_created_words = _sync_related_created_words_v4(payload)
    sessions = _sync_sessions_v4(payload)
    last_loaded_pool = _sync_last_loaded_pool_v4(payload)
    preferences = _sync_preferences_v4(payload)

    # Preserve session-derived states.
    fallback = (payload.get("meta") or {}).get("updatedAt") or _sync_now_iso()

    for s in sessions:
        ts = s.get("updatedAt") or fallback

        for w in _sync_arr(s.get("masteredWords") or []):
            mastered.add(w)
            learning.discard(w)
            per_skill["meaningKnownWords"].add(w)
            per_skill["spellingKnownWords"].add(w)
            per_skill["meaningLearningWords"].discard(w)
            per_skill["spellingLearningWords"].discard(w)
            word_updated_at[w] = word_updated_at.get(w) or ts
            meaning_word_updated_at[w] = meaning_word_updated_at.get(w) or ts
            spelling_word_updated_at[w] = spelling_word_updated_at.get(w) or ts

        for w in _sync_arr(s.get("learningWords") or []):
            if w not in mastered:
                learning.add(w)
                if w not in per_skill["meaningKnownWords"]:
                    per_skill["meaningLearningWords"].add(w)
                    meaning_word_updated_at[w] = meaning_word_updated_at.get(w) or ts
                word_updated_at[w] = word_updated_at.get(w) or ts

    updated_at = (
        (payload.get("meta") or {}).get("updatedAt")
        or payload.get("updatedAt")
        or max(word_updated_at.values(), default=_sync_now_iso())
    )

    now = _sync_now_iso()

    return {
        "schema": "ielts-vocab-cloud-sync-v4",
        "meta": {
            "app": "IELTS Vocabulary Webapp",
            "storageMode": "lean-user-learning-backup",
            "exportedAt": now,
            "updatedAt": updated_at,
            "mergedAt": now,
            "source": (payload.get("meta") or {}).get("source") or "backend-normalize",
        },
        "wordState": {
            "masteredWords": sorted(per_skill["meaningKnownWords"] & per_skill["spellingKnownWords"]),
            "learningWords": sorted((per_skill["meaningLearningWords"] | per_skill["spellingLearningWords"]) - (per_skill["meaningKnownWords"] & per_skill["spellingKnownWords"])),
            "meaningKnownWords": sorted(per_skill["meaningKnownWords"]),
            "spellingKnownWords": sorted(per_skill["spellingKnownWords"]),
            "meaningLearningWords": sorted(per_skill["meaningLearningWords"] - per_skill["meaningKnownWords"]),
            "spellingLearningWords": sorted(per_skill["spellingLearningWords"] - per_skill["spellingKnownWords"]),
            "wordUpdatedAt": word_updated_at,
            "meaningWordUpdatedAt": meaning_word_updated_at,
            "spellingWordUpdatedAt": spelling_word_updated_at,
            "relatedCreatedWords": related_created_words,
        },
        "practice": {
            "sessions": sessions,
            "lastLoadedPool": last_loaded_pool or None,
        },
        "goalTracking": {
            "goal": _sync_goal_v4(payload),
            "dailyRecord": _sync_daily_v4(payload),
            "goalV2": _sync_goal_v2(payload) or None,
            "dailyRecordV2": _sync_daily_v2(payload),
        },
        "preferences": preferences,
        "syncInfo": {
            "masteredCount": len(per_skill["meaningKnownWords"] & per_skill["spellingKnownWords"]),
            "learningCount": len((per_skill["meaningLearningWords"] | per_skill["spellingLearningWords"]) - (per_skill["meaningKnownWords"] & per_skill["spellingKnownWords"])),
            "meaningKnownCount": len(per_skill["meaningKnownWords"]),
            "spellingKnownCount": len(per_skill["spellingKnownWords"]),
            "sessionCount": len(sessions),
            "schemaVersion": 4,
        },
    }


def _sync_merge_word_states_v4(local: Dict[str, Any], remote: Dict[str, Any]) -> Tuple[set, set, Dict[str, str]]:
    lm, ll, lts = _sync_word_state_from_payload_v4(local)
    rm, rl, rts = _sync_word_state_from_payload_v4(remote)

    all_words = set(lm) | set(ll) | set(rm) | set(rl)
    mastered, learning, final_ts = set(), set(), {}

    for w in all_words:
        local_state = "mastered" if w in lm else ("learning" if w in ll else "new")
        remote_state = "mastered" if w in rm else ("learning" if w in rl else "new")

        lt = lts.get(w, "")
        rt = rts.get(w, "")

        if lt and rt:
            chosen = local_state if lt >= rt else remote_state
            final_ts[w] = max(lt, rt)
        elif lt:
            chosen = local_state
            final_ts[w] = lt
        elif rt:
            chosen = remote_state
            final_ts[w] = rt
        else:
            # No timestamp: preserve non-new state, prefer remote over local only for stability.
            chosen = remote_state if remote_state != "new" else local_state
            final_ts[w] = _sync_now_iso()

        if chosen == "mastered":
            mastered.add(w)
        elif chosen == "learning":
            learning.add(w)

    learning.difference_update(mastered)
    return mastered, learning, final_ts


def _sync_merge_per_skill_word_states_v4(local: Dict[str, Any], remote: Dict[str, Any]) -> Tuple[Dict[str, set], Dict[str, str], Dict[str, str], Dict[str, str]]:
    local_state = _sync_per_skill_word_state_from_payload_v4(local)
    remote_state = _sync_per_skill_word_state_from_payload_v4(remote)
    _, _, local_ts = _sync_word_state_from_payload_v4(local)
    _, _, remote_ts = _sync_word_state_from_payload_v4(remote)
    local_meaning_ts, local_spelling_ts = _sync_axis_timestamps_from_payload_v4(local, local_state)
    remote_meaning_ts, remote_spelling_ts = _sync_axis_timestamps_from_payload_v4(remote, remote_state)

    fields = ["meaningKnownWords", "spellingKnownWords", "meaningLearningWords", "spellingLearningWords"]
    all_words = set()
    for field in fields:
        all_words.update(local_state[field])
        all_words.update(remote_state[field])

    merged = {field: set() for field in fields}
    final_ts: Dict[str, str] = {}
    final_meaning_ts: Dict[str, str] = {}
    final_spelling_ts: Dict[str, str] = {}

    def axis_status(state: Dict[str, set], word: str, axis: str) -> str:
        known_field = "meaningKnownWords" if axis == "meaning" else "spellingKnownWords"
        learning_field = "meaningLearningWords" if axis == "meaning" else "spellingLearningWords"
        if word in state[known_field]:
            return "known"
        if word in state[learning_field]:
            return "learning"
        return "new"

    def write_axis(word: str, axis: str, status: str) -> None:
        if axis == "meaning":
            if status == "known":
                merged["meaningKnownWords"].add(word)
            elif status == "learning":
                merged["meaningLearningWords"].add(word)
        else:
            if status == "known":
                merged["spellingKnownWords"].add(word)
            elif status == "learning":
                merged["spellingLearningWords"].add(word)

    for word in all_words:
        axis_results: Dict[str, str] = {}
        axis_times: Dict[str, str] = {}

        for axis, l_axis_ts, r_axis_ts in [
            ("meaning", local_meaning_ts, remote_meaning_ts),
            ("spelling", local_spelling_ts, remote_spelling_ts),
        ]:
            ls = axis_status(local_state, word, axis)
            rs = axis_status(remote_state, word, axis)
            lt = l_axis_ts.get(word, "") or (local_ts.get(word, "") if ls != "new" else "")
            rt = r_axis_ts.get(word, "") or (remote_ts.get(word, "") if rs != "new" else "")

            if lt and rt:
                status = ls if lt >= rt else rs
                axis_times[axis] = max(lt, rt)
            elif lt:
                status = ls
                axis_times[axis] = lt
            elif rt:
                status = rs
                axis_times[axis] = rt
            else:
                # Old payloads can be timestamp-less. Merge conservatively:
                # Known wins over Learning for the same axis; otherwise keep Learning.
                status = "known" if "known" in (ls, rs) else ("learning" if "learning" in (ls, rs) else "new")
                axis_times[axis] = _sync_now_iso()

            axis_results[axis] = status
            write_axis(word, axis, status)

        if axis_results.get("meaning") != "new":
            final_meaning_ts[word] = axis_times["meaning"]
        if axis_results.get("spelling") != "new":
            final_spelling_ts[word] = axis_times["spelling"]
        final_ts[word] = max([t for t in axis_times.values() if t] or [local_ts.get(word, ""), remote_ts.get(word, ""), _sync_now_iso()])

    merged["meaningLearningWords"].difference_update(merged["meaningKnownWords"])
    merged["spellingLearningWords"].difference_update(merged["spellingKnownWords"])
    return merged, final_ts, final_meaning_ts, final_spelling_ts


def _sync_merge_related_created_words_v4(local: Dict[str, Any], remote: Dict[str, Any]) -> List[Dict[str, str]]:
    out: Dict[str, Dict[str, str]] = {}
    for item in _sync_related_created_words_v4(remote) + _sync_related_created_words_v4(local):
        key = item.get("key")
        if key:
            out[key] = item
    return sorted(out.values(), key=lambda x: x.get("word", ""))


def _sync_merge_daily_v2(local_daily: Dict[str, Any], remote_daily: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for src in [remote_daily or {}, local_daily or {}]:
        if not isinstance(src, dict):
            continue
        for day, row in src.items():
            if not isinstance(row, dict):
                out[day] = row
                continue
            cur = out.get(day, {}) if isinstance(out.get(day), dict) else {}
            merged = {**cur, **row}
            cur_events = cur.get("events") if isinstance(cur.get("events"), dict) else {}
            row_events = row.get("events") if isinstance(row.get("events"), dict) else {}
            events = {**cur_events, **row_events}
            for field in ["meaningKnown", "spellingKnown", "mastered", "meaningLearning", "spellingLearning"]:
                events[field] = sorted(set(_sync_arr(cur_events.get(field) or [])) | set(_sync_arr(row_events.get(field) or [])))
            merged["events"] = events
            cur_sub = cur.get("subGoalsMet") if isinstance(cur.get("subGoalsMet"), dict) else {}
            row_sub = row.get("subGoalsMet") if isinstance(row.get("subGoalsMet"), dict) else {}
            merged["subGoalsMet"] = {**cur_sub, **row_sub}
            for key in set(cur_sub) | set(row_sub):
                merged["subGoalsMet"][key] = bool(cur_sub.get(key) or row_sub.get(key))
            merged["goalMet"] = bool(cur.get("goalMet") or row.get("goalMet"))
            merged["newWordsMastered"] = sorted(set(_sync_arr(cur.get("newWordsMastered") or [])) | set(_sync_arr(row.get("newWordsMastered") or [])))
            merged["updatedAt"] = max(str(cur.get("updatedAt") or ""), str(row.get("updatedAt") or ""))
            out[day] = merged
    return out


def _sync_merge_payloads(local_payload: Dict[str, Any], remote_payload: Dict[str, Any]) -> Dict[str, Any]:
    local = _sync_lean_payload(local_payload or {})
    remote = _sync_lean_payload(remote_payload or {})

    per_skill, word_updated_at, meaning_word_updated_at, spelling_word_updated_at = _sync_merge_per_skill_word_states_v4(local, remote)
    mastered = per_skill["meaningKnownWords"] & per_skill["spellingKnownWords"]
    learning = (per_skill["meaningLearningWords"] | per_skill["spellingLearningWords"]) - mastered
    related_created_words = _sync_merge_related_created_words_v4(local, remote)

    by_id: Dict[str, Dict[str, Any]] = {}
    for s in (remote["practice"]["sessions"] + local["practice"]["sessions"]):
        sid = str(s.get("id") or "")
        if not sid:
            continue

        old = by_id.get(sid)
        if not old:
            by_id[sid] = s
        else:
            if str(s.get("updatedAt") or "") >= str(old.get("updatedAt") or ""):
                by_id[sid] = s

    sessions = sorted(
        by_id.values(),
        key=lambda s: str(s.get("startedAt") or s.get("endedAt") or s.get("updatedAt") or "")
    )
    local_last_pool = _sync_last_loaded_pool_v4(local)
    remote_last_pool = _sync_last_loaded_pool_v4(remote)
    last_loaded_pool = local_last_pool if str(local_last_pool.get("savedAt") or "") >= str(remote_last_pool.get("savedAt") or "") else remote_last_pool

    local_goal = local["goalTracking"].get("goal") or {}
    remote_goal = remote["goalTracking"].get("goal") or {}
    goal = local_goal if str(local_goal.get("updatedAt") or "") >= str(remote_goal.get("updatedAt") or "") else remote_goal
    local_goal_v2 = local["goalTracking"].get("goalV2") or {}
    remote_goal_v2 = remote["goalTracking"].get("goalV2") or {}
    goal_v2 = local_goal_v2 if str(local_goal_v2.get("updatedAt") or "") >= str(remote_goal_v2.get("updatedAt") or "") else remote_goal_v2
    daily_v2 = _sync_merge_daily_v2(
        local["goalTracking"].get("dailyRecordV2") or {},
        remote["goalTracking"].get("dailyRecordV2") or {},
    )
    preferences = _sync_merge_preferences_v4(local, remote)

    daily = {}
    for src in [remote["goalTracking"].get("dailyRecord") or {}, local["goalTracking"].get("dailyRecord") or {}]:
        if not isinstance(src, dict):
            continue
        for day, row in src.items():
            if not isinstance(row, dict):
                daily[day] = row
                continue

            cur = daily.get(day, {})
            merged = {**cur, **row}

            for field in [
                "masteredWords",
                "masteredWordIds",
                "newWordsMastered",
                "practicedWords",
                "practicedWordIds",
                "sessionIds"
            ]:
                merged[field] = sorted(set(_sync_arr(cur.get(field) or [])) | set(_sync_arr(row.get(field) or [])))

            merged["goalMet"] = bool(cur.get("goalMet") or row.get("goalMet"))
            merged["updatedAt"] = max(str(cur.get("updatedAt") or ""), str(row.get("updatedAt") or ""))
            daily[day] = merged

    updated_at_candidates = [
        local.get("meta", {}).get("updatedAt", ""),
        remote.get("meta", {}).get("updatedAt", ""),
        str((goal_v2 or {}).get("updatedAt") or ""),
        max([str(row.get("updatedAt") or "") for row in daily_v2.values() if isinstance(row, dict)], default=""),
        str((preferences or {}).get("modeIntroUpdatedAt") or ""),
        str((last_loaded_pool or {}).get("savedAt") or ""),
        max(word_updated_at.values(), default=""),
        max([str(s.get("updatedAt") or "") for s in sessions], default=""),
    ]

    updated_at = max([x for x in updated_at_candidates if x] or [_sync_now_iso()])
    now = _sync_now_iso()

    return {
        "schema": "ielts-vocab-cloud-sync-v4",
        "meta": {
            "app": "IELTS Vocabulary Webapp",
            "storageMode": "lean-user-learning-backup",
            "exportedAt": now,
            "updatedAt": updated_at,
            "mergedAt": now,
            "source": "backend-auto-merge",
        },
        "wordState": {
            "masteredWords": sorted(mastered),
            "learningWords": sorted(learning),
            "meaningKnownWords": sorted(per_skill["meaningKnownWords"]),
            "spellingKnownWords": sorted(per_skill["spellingKnownWords"]),
            "meaningLearningWords": sorted(per_skill["meaningLearningWords"] - per_skill["meaningKnownWords"]),
            "spellingLearningWords": sorted(per_skill["spellingLearningWords"] - per_skill["spellingKnownWords"]),
            "wordUpdatedAt": word_updated_at,
            "meaningWordUpdatedAt": meaning_word_updated_at,
            "spellingWordUpdatedAt": spelling_word_updated_at,
            "relatedCreatedWords": related_created_words,
        },
        "practice": {
            "sessions": sessions,
            "lastLoadedPool": last_loaded_pool or None,
        },
        "goalTracking": {
            "goal": goal,
            "dailyRecord": daily,
            "goalV2": goal_v2 or None,
            "dailyRecordV2": daily_v2,
        },
        "preferences": preferences,
        "syncInfo": {
            "masteredCount": len(mastered),
            "learningCount": len(learning),
            "meaningKnownCount": len(per_skill["meaningKnownWords"]),
            "spellingKnownCount": len(per_skill["spellingKnownWords"]),
            "sessionCount": len(sessions),
            "schemaVersion": 4,
            "lastSyncType": "backend-auto-merge",
        },
    }


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
# ============================================================
# GOOGLE DRIVE SYNC PROXY
# Frontend sends the user's Google access token to this backend.
# Backend talks to Google Drive, avoiding browser CORS/upload issues.
# ============================================================

DRIVE_FILE_NAME = "ielts-vocab-data.json"

def _drive_auth_headers():
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    return {"Authorization": auth}

def _drive_error_response(prefix, resp):
    try:
        detail = resp.json()
    except Exception:
        detail = resp.text
    return jsonify({
        "detail": prefix,
        "status": resp.status_code,
        "google_response": detail,
    }), resp.status_code

@app.get("/api/drive/find")
def api_drive_find():
    headers = _drive_auth_headers()
    if not headers:
        return jsonify({"detail": "Missing Google access token"}), 401

    name = (request.args.get("name") or DRIVE_FILE_NAME).strip() or DRIVE_FILE_NAME
    q = f"name = '{name.replace(chr(39), chr(92)+chr(39))}' and trashed = false"

    resp = requests.get(
        "https://www.googleapis.com/drive/v3/files",
        headers=headers,
        params={
            "q": q,
            "spaces": "drive",
            "fields": "files(id,name,modifiedTime,mimeType)",
            "pageSize": 10,
        },
        timeout=20,
    )

    if not resp.ok:
        return _drive_error_response("Drive find failed", resp)

    files = resp.json().get("files", [])
    return jsonify({"file": files[0] if files else None, "files": files})

@app.get("/api/drive/content/<file_id>")
def api_drive_get_content(file_id):
    headers = _drive_auth_headers()
    if not headers:
        return jsonify({"detail": "Missing Google access token"}), 401

    resp = requests.get(
        f"https://www.googleapis.com/drive/v3/files/{file_id}",
        headers=headers,
        params={"alt": "media"},
        timeout=20,
    )

    if not resp.ok:
        return _drive_error_response("Drive get content failed", resp)

    try:
        return jsonify(resp.json())
    except Exception:
        return jsonify({"detail": "Drive file content is not valid JSON"}), 502

@app.post("/api/drive/create")
def api_drive_create():
    headers = _drive_auth_headers()
    if not headers:
        return jsonify({"detail": "Missing Google access token"}), 401

    body = request.get_json(silent=True) or {}
    name = body.get("name") or DRIVE_FILE_NAME
    content = body.get("content") or {}

    boundary = "----ieltsvocabboundary"
    metadata = {"name": name, "mimeType": "application/json"}
    content_json = json.dumps(content, ensure_ascii=False, indent=2)

    multipart_body = (
        f"--{boundary}\r\n"
        "Content-Type: application/json; charset=UTF-8\r\n\r\n"
        f"{json.dumps(metadata)}\r\n"
        f"--{boundary}\r\n"
        "Content-Type: application/json; charset=UTF-8\r\n\r\n"
        f"{content_json}\r\n"
        f"--{boundary}--"
    ).encode("utf-8")

    upload_headers = {
        **headers,
        "Content-Type": f"multipart/related; boundary={boundary}",
    }

    resp = requests.post(
        "https://www.googleapis.com/upload/drive/v3/files",
        headers=upload_headers,
        params={"uploadType": "multipart", "fields": "id,name,modifiedTime"},
        data=multipart_body,
        timeout=30,
    )

    if not resp.ok:
        return _drive_error_response("Drive create failed", resp)

    return jsonify(resp.json())

@app.patch("/api/drive/content/<file_id>")
def api_drive_update_content(file_id):
    headers = _drive_auth_headers()
    if not headers:
        return jsonify({"detail": "Missing Google access token"}), 401

    body = request.get_json(silent=True) or {}
    content = body.get("content") or {}
    content_json = json.dumps(content, ensure_ascii=False, indent=2)

    upload_headers = {
        **headers,
        "Content-Type": "application/json; charset=UTF-8",
    }

    resp = requests.patch(
        f"https://www.googleapis.com/upload/drive/v3/files/{file_id}",
        headers=upload_headers,
        params={"uploadType": "media", "fields": "id,name,modifiedTime"},
        data=content_json.encode("utf-8"),
        timeout=30,
    )

    if not resp.ok:
        return _drive_error_response("Drive update failed", resp)

    return jsonify(resp.json())

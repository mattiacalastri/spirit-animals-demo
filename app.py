"""Spirit Animals API — Demo per developer.

Stack: FastAPI + Claude + fal.ai + Supabase
"""

import os
import json
import asyncio
import uuid
import time
from collections import defaultdict
from datetime import datetime, timezone

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from contextlib import asynccontextmanager
import anthropic
import fal_client
import httpx

load_dotenv()

CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-haiku-4-5-20251001")
FAL_ENABLED = bool(os.getenv("FAL_KEY"))
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")
SUPABASE_ENABLED = bool(SUPABASE_URL and SUPABASE_KEY)

claude = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

# API Key auth — protegge i POST che consumano crediti Claude/fal.ai
DEMO_API_KEY = os.getenv("SPIRIT_DEMO_API_KEY", "sa-demo-2026")
VALID_API_KEYS = {
    DEMO_API_KEY,  # embedded nel frontend per la demo
    *filter(None, os.getenv("SPIRIT_API_KEYS", "").split(",")),  # chiavi aggiuntive
}

# Rate limiting semplice: max N generazioni per IP per finestra
RATE_LIMIT_WINDOW = 3600  # 1 ora
RATE_LIMIT_MAX = 10  # max 10 generazioni/ora per IP
_rate_store: dict[str, list[float]] = defaultdict(list)


def _check_rate_limit(client_ip: str) -> None:
    now = time.time()
    hits = _rate_store[client_ip]
    # Pulisci entry vecchie
    _rate_store[client_ip] = [t for t in hits if now - t < RATE_LIMIT_WINDOW]
    if len(_rate_store[client_ip]) >= RATE_LIMIT_MAX:
        raise HTTPException(status_code=429, detail="Rate limit exceeded. Max 10 generations per hour.")
    _rate_store[client_ip].append(now)


def _verify_api_key(x_api_key: str | None) -> None:
    if not x_api_key or x_api_key not in VALID_API_KEYS:
        raise HTTPException(status_code=401, detail="Invalid or missing API key. Pass X-Api-Key header.")


# ---------------------------------------------------------------------------
# Supabase Client (PostgREST)
# ---------------------------------------------------------------------------

class SupabaseClient:
    """Thin wrapper around Supabase PostgREST API."""

    def __init__(self, url: str, key: str):
        self.base = f"{url}/rest/v1"
        self.headers = {
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Prefer": "return=representation",
        }
        self._client = httpx.AsyncClient(headers=self.headers, timeout=15.0)

    async def insert(self, table: str, data: dict) -> dict:
        r = await self._client.post(f"{self.base}/{table}", json=data)
        r.raise_for_status()
        rows = r.json()
        return rows[0] if rows else data

    async def select(self, table: str, params: dict | None = None) -> list[dict]:
        r = await self._client.get(f"{self.base}/{table}", params=params or {})
        r.raise_for_status()
        return r.json()

    async def select_one(self, table: str, column: str, value: str) -> dict | None:
        params = {column: f"eq.{value}", "limit": "1"}
        rows = await self.select(table, params)
        return rows[0] if rows else None

    async def close(self):
        await self._client.aclose()


db: SupabaseClient | None = None


# ---------------------------------------------------------------------------
# SQLite fallback (local dev without Supabase)
# ---------------------------------------------------------------------------

import sqlite3
from contextlib import contextmanager

SQLITE_PATH = os.getenv("DB_PATH", "spirit_animals.db")


@contextmanager
def get_sqlite():
    conn = sqlite3.connect(SQLITE_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_sqlite():
    with get_sqlite() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS souls (
                id TEXT PRIMARY KEY, team_id TEXT, name TEXT NOT NULL, role TEXT,
                animal TEXT NOT NULL, emoji TEXT, soul_name TEXT NOT NULL, archetype TEXT,
                traits TEXT, superpower TEXT, shadow TEXT, motto TEXT, color TEXT,
                element TEXT, collaboration_style TEXT, avatar_url TEXT,
                raw_input TEXT, created_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS teams (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT,
                soul_count INTEGER DEFAULT 0, synergy TEXT, created_at TEXT NOT NULL
            )
        """)


# ---------------------------------------------------------------------------
# Persistence abstraction
# ---------------------------------------------------------------------------

async def db_insert_soul(data: dict) -> None:
    if SUPABASE_ENABLED and db:
        payload = {**data, "traits": data["traits"] if isinstance(data["traits"], list) else json.loads(data["traits"])}
        await db.insert("souls", payload)
    else:
        with get_sqlite() as conn:
            traits_str = json.dumps(data["traits"]) if isinstance(data["traits"], list) else data["traits"]
            conn.execute(
                """INSERT INTO souls (id, team_id, name, role, animal, emoji, soul_name,
                   archetype, traits, superpower, shadow, motto, color, element,
                   collaboration_style, avatar_url, raw_input, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (data["id"], data.get("team_id"), data["name"], data.get("role"),
                 data["animal"], data.get("emoji", ""), data["soul_name"], data["archetype"],
                 traits_str, data["superpower"], data["shadow"], data["motto"],
                 data.get("color", "#00d4aa"), data.get("element", ""),
                 data.get("collaboration_style", ""), data.get("avatar_url"),
                 data.get("raw_input", ""), data["created_at"]),
            )


async def db_insert_team(data: dict) -> None:
    if SUPABASE_ENABLED and db:
        await db.insert("teams", data)
    else:
        with get_sqlite() as conn:
            conn.execute(
                "INSERT INTO teams (id, name, description, soul_count, synergy, created_at) VALUES (?,?,?,?,?,?)",
                (data["id"], data["name"], data.get("description"),
                 data["soul_count"], json.dumps(data["synergy"]), data["created_at"]),
            )


async def db_list_souls(limit: int = 50) -> list[dict]:
    if SUPABASE_ENABLED and db:
        return await db.select("souls", {"order": "created_at.desc", "limit": str(limit)})
    with get_sqlite() as conn:
        rows = conn.execute("SELECT * FROM souls ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


async def db_get_soul(soul_id: str) -> dict | None:
    if SUPABASE_ENABLED and db:
        return await db.select_one("souls", "id", soul_id)
    with get_sqlite() as conn:
        row = conn.execute("SELECT * FROM souls WHERE id = ?", (soul_id,)).fetchone()
    return dict(row) if row else None


async def db_list_teams(limit: int = 20) -> list[dict]:
    if SUPABASE_ENABLED and db:
        return await db.select("teams", {"order": "created_at.desc", "limit": str(limit)})
    with get_sqlite() as conn:
        rows = conn.execute("SELECT * FROM teams ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


async def db_get_team(team_id: str) -> dict | None:
    if SUPABASE_ENABLED and db:
        team = await db.select_one("teams", "id", team_id)
        if not team:
            return None
        souls = await db.select("souls", {"team_id": f"eq.{team_id}", "order": "created_at"})
        return {**team, "souls": souls}
    with get_sqlite() as conn:
        team = conn.execute("SELECT * FROM teams WHERE id = ?", (team_id,)).fetchone()
        if not team:
            return None
        souls = conn.execute("SELECT * FROM souls WHERE team_id = ? ORDER BY created_at", (team_id,)).fetchall()
    return {**dict(team), "souls": [dict(s) for s in souls]}


async def db_count() -> tuple[int, int]:
    if SUPABASE_ENABLED and db:
        try:
            all_souls = await db.select("souls", {"select": "id"})
            all_teams = await db.select("teams", {"select": "id"})
            return len(all_souls), len(all_teams)
        except Exception:
            return 0, 0
    with get_sqlite() as conn:
        sc = conn.execute("SELECT COUNT(*) FROM souls").fetchone()[0]
        tc = conn.execute("SELECT COUNT(*) FROM teams").fetchone()[0]
    return sc, tc


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    global db, SUPABASE_ENABLED
    if SUPABASE_ENABLED:
        db = SupabaseClient(SUPABASE_URL, SUPABASE_KEY)
        # Verify tables exist; fallback to SQLite if not
        try:
            await db.select("souls", {"select": "id", "limit": "1"})
        except Exception:
            print("WARNING: Supabase tables not found, falling back to SQLite")
            await db.close()
            db = None
            SUPABASE_ENABLED = False
            init_sqlite()
    else:
        init_sqlite()
    yield
    if db:
        await db.close()


app = FastAPI(title="Spirit Animals API", version="0.3.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Prompt Engineering
# ---------------------------------------------------------------------------

SOUL_SYSTEM_PROMPT = """Sei il motore Spirit Animals di Astra OS.
Dato un input sull'utente (nome, ruolo, tratti, valori), genera un profilo "AI Soul" in JSON:

{
  "animal": "nome animale spirito (es. Polpo, Lupo, Aquila, Volpe, Drago, Koala, Fenice, Orso)",
  "emoji": "emoji animale",
  "soul_name": "nome evocativo dell'anima (es. Il Tessitore, La Sentinella)",
  "archetype": "archetipo junghiano dominante",
  "traits": ["3-5 tratti chiave"],
  "superpower": "il superpotere unico di questa persona",
  "shadow": "la sfida interiore / ombra da integrare",
  "motto": "una frase che questa anima direbbe",
  "color": "colore esadecimale che rappresenta l'anima",
  "element": "elemento naturale (fuoco, acqua, terra, aria, etere)",
  "collaboration_style": "come lavora meglio con AI e team",
  "image_prompt": "prompt dettagliato per generare l'avatar: animale in stile artistico, colori, atmosfera, NO testo nell'immagine"
}

Rispondi SOLO con il JSON valido, nessun altro testo."""

TEAM_SYNERGY_PROMPT = """Sei un analista di team dynamics.
Dato un team di AI Souls, analizza le sinergie e genera un report JSON:

{
  "team_name": "nome evocativo del team",
  "synergy_score": 0-100,
  "strengths": ["3 punti di forza del team"],
  "blind_spots": ["2 aree scoperte"],
  "dynamic": "descrizione della dinamica di gruppo in 2 frasi",
  "recommended_role_map": {"nome_membro": "ruolo ideale nel team"},
  "missing_archetype": "l'archetipo che manca al team per essere completo"
}

Rispondi SOLO con il JSON valido, nessun altro testo."""


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class SoulRequest(BaseModel):
    name: str
    role: str | None = None
    traits: str | None = None
    values: str | None = None
    context: str | None = None


class SoulResponse(BaseModel):
    id: str
    animal: str
    emoji: str
    soul_name: str
    archetype: str
    traits: list[str]
    superpower: str
    shadow: str
    motto: str
    color: str
    element: str
    collaboration_style: str
    avatar_url: str | None = None
    team_id: str | None = None


class TeamRequest(BaseModel):
    team_name: str
    members: list[SoulRequest]


class TeamSynergy(BaseModel):
    team_name: str
    synergy_score: int
    strengths: list[str]
    blind_spots: list[str]
    dynamic: str
    recommended_role_map: dict[str, str]
    missing_archetype: str


class TeamResponse(BaseModel):
    team_id: str
    team_name: str
    souls: list[SoulResponse]
    synergy: TeamSynergy


# ---------------------------------------------------------------------------
# Core Engine
# ---------------------------------------------------------------------------

def _parse_claude_json(raw: str) -> dict:
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0]
    return json.loads(text)


def _call_claude(system: str, user_content: str) -> dict:
    message = claude.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=1024,
        system=system,
        messages=[{"role": "user", "content": user_content}],
    )
    return _parse_claude_json(message.content[0].text)


async def generate_soul_profile(user_input: str) -> dict:
    return await asyncio.to_thread(_call_claude, SOUL_SYSTEM_PROMPT, user_input)


async def generate_avatar(image_prompt: str, fallback_animal: str) -> str | None:
    if not FAL_ENABLED:
        return None
    try:
        result = await asyncio.to_thread(
            fal_client.subscribe,
            "fal-ai/flux/schnell",
            arguments={
                "prompt": image_prompt or f"Spirit animal {fallback_animal}, digital art",
                "image_size": "square_hd",
                "num_images": 1,
            },
        )
        if result and "images" in result and result["images"]:
            return result["images"][0]["url"]
    except Exception:
        pass
    return None


def build_user_input(req: SoulRequest) -> str:
    parts = [f"Nome: {req.name}"]
    if req.role:
        parts.append(f"Ruolo: {req.role}")
    if req.traits:
        parts.append(f"Tratti: {req.traits}")
    if req.values:
        parts.append(f"Valori: {req.values}")
    if req.context:
        parts.append(f"Contesto: {req.context}")
    return "\n".join(parts)


def make_soul_response(soul_data: dict, soul_id: str, team_id: str | None = None) -> SoulResponse:
    traits = soul_data["traits"]
    if isinstance(traits, str):
        traits = json.loads(traits)
    return SoulResponse(
        id=soul_id,
        animal=soul_data["animal"],
        emoji=soul_data.get("emoji", ""),
        soul_name=soul_data["soul_name"],
        archetype=soul_data["archetype"],
        traits=traits,
        superpower=soul_data["superpower"],
        shadow=soul_data["shadow"],
        motto=soul_data["motto"],
        color=soul_data.get("color", "#00d4aa"),
        element=soul_data.get("element", ""),
        collaboration_style=soul_data.get("collaboration_style", ""),
        avatar_url=soul_data.get("avatar_url"),
        team_id=team_id,
    )


# ---------------------------------------------------------------------------
# API Endpoints
# ---------------------------------------------------------------------------

@app.post("/api/generate-soul", response_model=SoulResponse)
async def api_generate_soul(req: SoulRequest, request: Request, x_api_key: str | None = Header(None)):
    _verify_api_key(x_api_key)
    _check_rate_limit(request.client.host)
    user_input = build_user_input(req)

    try:
        soul = await generate_soul_profile(user_input)
    except json.JSONDecodeError:
        raise HTTPException(status_code=500, detail="Claude ha generato JSON non valido")
    except anthropic.APIError as e:
        raise HTTPException(status_code=502, detail=f"Errore Claude API: {e}")

    soul["avatar_url"] = await generate_avatar(soul.get("image_prompt", ""), soul["animal"])

    soul_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    await db_insert_soul({
        "id": soul_id, "team_id": None, "name": req.name, "role": req.role,
        "animal": soul["animal"], "emoji": soul.get("emoji", ""),
        "soul_name": soul["soul_name"], "archetype": soul["archetype"],
        "traits": soul["traits"], "superpower": soul["superpower"],
        "shadow": soul["shadow"], "motto": soul["motto"],
        "color": soul.get("color", "#00d4aa"), "element": soul.get("element", ""),
        "collaboration_style": soul.get("collaboration_style", ""),
        "avatar_url": soul.get("avatar_url"), "raw_input": user_input,
        "created_at": now,
    })

    return make_soul_response(soul, soul_id)


@app.post("/api/generate-team", response_model=TeamResponse)
async def api_generate_team(req: TeamRequest, request: Request, x_api_key: str | None = Header(None)):
    _verify_api_key(x_api_key)
    _check_rate_limit(request.client.host)
    if len(req.members) < 2:
        raise HTTPException(status_code=400, detail="Servono almeno 2 membri")
    if len(req.members) > 10:
        raise HTTPException(status_code=400, detail="Massimo 10 membri per team")

    team_id = str(uuid.uuid4())

    async def process_member(member: SoulRequest) -> SoulResponse:
        user_input = build_user_input(member)
        soul = await generate_soul_profile(user_input)
        soul["avatar_url"] = await generate_avatar(soul.get("image_prompt", ""), soul["animal"])
        soul_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        await db_insert_soul({
            "id": soul_id, "team_id": team_id, "name": member.name, "role": member.role,
            "animal": soul["animal"], "emoji": soul.get("emoji", ""),
            "soul_name": soul["soul_name"], "archetype": soul["archetype"],
            "traits": soul["traits"], "superpower": soul["superpower"],
            "shadow": soul["shadow"], "motto": soul["motto"],
            "color": soul.get("color", "#00d4aa"), "element": soul.get("element", ""),
            "collaboration_style": soul.get("collaboration_style", ""),
            "avatar_url": soul.get("avatar_url"), "raw_input": user_input,
            "created_at": now,
        })
        return make_soul_response(soul, soul_id, team_id)

    souls = await asyncio.gather(*[process_member(m) for m in req.members])

    team_summary = "\n".join(
        f"- {s.emoji} {s.soul_name} ({s.animal}): {s.archetype}, superpotere={s.superpower}"
        for s in souls
    )

    try:
        synergy = await asyncio.to_thread(
            _call_claude, TEAM_SYNERGY_PROMPT, f"Team '{req.team_name}':\n{team_summary}"
        )
    except (json.JSONDecodeError, anthropic.APIError):
        synergy = {
            "team_name": req.team_name, "synergy_score": 0, "strengths": [],
            "blind_spots": [], "dynamic": "Analisi non disponibile",
            "recommended_role_map": {}, "missing_archetype": "N/A",
        }

    now = datetime.now(timezone.utc).isoformat()
    await db_insert_team({
        "id": team_id, "name": req.team_name, "description": None,
        "soul_count": len(souls), "synergy": synergy, "created_at": now,
    })

    return TeamResponse(
        team_id=team_id, team_name=req.team_name,
        souls=list(souls), synergy=TeamSynergy(**synergy),
    )


@app.get("/api/souls")
async def api_list_souls(limit: int = 50):
    return await db_list_souls(limit)


@app.get("/api/souls/{soul_id}")
async def api_get_soul(soul_id: str):
    soul = await db_get_soul(soul_id)
    if not soul:
        raise HTTPException(status_code=404, detail="Soul non trovata")
    return soul


@app.get("/api/teams")
async def api_list_teams(limit: int = 20):
    return await db_list_teams(limit)


@app.get("/api/teams/{team_id}")
async def api_get_team(team_id: str):
    team = await db_get_team(team_id)
    if not team:
        raise HTTPException(status_code=404, detail="Team non trovato")
    return team


@app.get("/api/health")
async def api_health():
    soul_count, team_count = await db_count()
    return {
        "status": "alive",
        "engine": "Spirit Animals v0.3.0",
        "model": CLAUDE_MODEL,
        "fal_enabled": FAL_ENABLED,
        "storage": "supabase" if SUPABASE_ENABLED else "sqlite",
        "souls_generated": soul_count,
        "teams_generated": team_count,
    }


# Serve frontend
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
async def index():
    return FileResponse("static/index.html")

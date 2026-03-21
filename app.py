"""Spirit Animals API — Demo per developer.

Stack: FastAPI + Claude + fal.ai + SQLite (demo) / Supabase (prod)
"""

import os
import json
import asyncio
import sqlite3
import uuid
from datetime import datetime, timezone
from contextlib import contextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
import anthropic
import fal_client

# Credenziali: env vars in produzione, file locali in dev
if os.path.exists(os.path.expanduser("~/.config/credentials/aurahome.env")):
    load_dotenv(os.path.expanduser("~/.config/credentials/aurahome.env"))
    load_dotenv(os.path.expanduser("~/claude_voice/.env"))

app = FastAPI(title="Spirit Animals API", version="0.2.0")

claude = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

DB_PATH = os.getenv("DB_PATH", "spirit_animals.db")

# ---------------------------------------------------------------------------
# Database (SQLite per demo, migration SQL per Supabase inclusa)
# ---------------------------------------------------------------------------

def init_db():
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS souls (
                id TEXT PRIMARY KEY,
                team_id TEXT,
                name TEXT NOT NULL,
                role TEXT,
                animal TEXT NOT NULL,
                emoji TEXT,
                soul_name TEXT NOT NULL,
                archetype TEXT,
                traits TEXT,
                superpower TEXT,
                shadow TEXT,
                motto TEXT,
                color TEXT,
                element TEXT,
                collaboration_style TEXT,
                avatar_url TEXT,
                raw_input TEXT,
                created_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS teams (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT,
                soul_count INTEGER DEFAULT 0,
                synergy TEXT,
                created_at TEXT NOT NULL
            )
        """)


@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


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

async def generate_soul_profile(user_input: str) -> dict:
    """Chiama Claude per generare il profilo Soul."""
    message = claude.messages.create(
        model=os.getenv("CLAUDE_MODEL", "claude-3-haiku-20240307"),
        max_tokens=1024,
        system=SOUL_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_input}],
    )
    raw = message.content[0].text
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1].rsplit("```", 1)[0]
    return json.loads(raw)


async def generate_avatar(image_prompt: str, fallback_animal: str) -> str | None:
    """Chiama fal.ai per generare l'avatar."""
    if not os.getenv("FAL_KEY"):
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


def save_soul(soul_data: dict, soul_id: str, name: str, role: str | None,
              raw_input: str, team_id: str | None = None) -> None:
    with get_db() as conn:
        conn.execute(
            """INSERT INTO souls (id, team_id, name, role, animal, emoji, soul_name,
               archetype, traits, superpower, shadow, motto, color, element,
               collaboration_style, avatar_url, raw_input, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                soul_id, team_id, name, role,
                soul_data["animal"], soul_data.get("emoji", ""),
                soul_data["soul_name"], soul_data["archetype"],
                json.dumps(soul_data["traits"]), soul_data["superpower"],
                soul_data["shadow"], soul_data["motto"],
                soul_data.get("color", "#00d4aa"), soul_data.get("element", ""),
                soul_data.get("collaboration_style", ""),
                soul_data.get("avatar_url"), raw_input,
                datetime.now(timezone.utc).isoformat(),
            ),
        )


def soul_to_response(soul_data: dict, soul_id: str, team_id: str | None = None) -> SoulResponse:
    return SoulResponse(
        id=soul_id,
        animal=soul_data["animal"],
        emoji=soul_data.get("emoji", ""),
        soul_name=soul_data["soul_name"],
        archetype=soul_data["archetype"],
        traits=soul_data["traits"] if isinstance(soul_data["traits"], list) else json.loads(soul_data["traits"]),
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
async def generate_soul(req: SoulRequest):
    """Genera un profilo AI Soul completo con avatar."""
    user_input = build_user_input(req)

    try:
        soul = await generate_soul_profile(user_input)
    except json.JSONDecodeError:
        raise HTTPException(status_code=500, detail="Claude ha generato JSON non valido")
    except anthropic.APIError as e:
        raise HTTPException(status_code=502, detail=f"Errore Claude API: {e}")

    soul["avatar_url"] = await generate_avatar(
        soul.get("image_prompt", ""), soul["animal"]
    )

    soul_id = str(uuid.uuid4())
    save_soul(soul, soul_id, req.name, req.role, user_input)

    return soul_to_response(soul, soul_id)


@app.post("/api/generate-team", response_model=TeamResponse)
async def generate_team(req: TeamRequest):
    """Genera un team completo di AI Souls con analisi sinergie."""
    team_id = str(uuid.uuid4())
    souls: list[SoulResponse] = []

    # Genera tutte le Souls in parallelo
    async def process_member(member: SoulRequest) -> SoulResponse:
        user_input = build_user_input(member)
        soul = await generate_soul_profile(user_input)
        soul["avatar_url"] = await generate_avatar(
            soul.get("image_prompt", ""), soul["animal"]
        )
        soul_id = str(uuid.uuid4())
        save_soul(soul, soul_id, member.name, member.role, user_input, team_id)
        return soul_to_response(soul, soul_id, team_id)

    tasks = [process_member(m) for m in req.members]
    souls = await asyncio.gather(*tasks)

    # Analisi sinergie del team
    team_summary = "\n".join(
        f"- {s.emoji} {s.soul_name} ({s.animal}): {s.archetype}, superpotere={s.superpower}"
        for s in souls
    )

    try:
        synergy_msg = claude.messages.create(
            model=os.getenv("CLAUDE_MODEL", "claude-3-haiku-20240307"),
            max_tokens=1024,
            system=TEAM_SYNERGY_PROMPT,
            messages=[{"role": "user", "content": f"Team '{req.team_name}':\n{team_summary}"}],
        )
        raw = synergy_msg.content[0].text
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0]
        synergy = json.loads(raw)
    except (json.JSONDecodeError, anthropic.APIError):
        synergy = {
            "team_name": req.team_name,
            "synergy_score": 0,
            "strengths": [],
            "blind_spots": [],
            "dynamic": "Analisi non disponibile",
            "recommended_role_map": {},
            "missing_archetype": "N/A",
        }

    # Salva team
    with get_db() as conn:
        conn.execute(
            "INSERT INTO teams (id, name, description, soul_count, synergy, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (team_id, req.team_name, None, len(souls), json.dumps(synergy),
             datetime.now(timezone.utc).isoformat()),
        )

    return TeamResponse(
        team_id=team_id,
        team_name=req.team_name,
        souls=list(souls),
        synergy=TeamSynergy(**synergy),
    )


@app.get("/api/souls")
async def list_souls(limit: int = 50):
    """Lista tutte le Souls generate."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM souls ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/souls/{soul_id}")
async def get_soul(soul_id: str):
    """Recupera una Soul per ID."""
    with get_db() as conn:
        row = conn.execute("SELECT * FROM souls WHERE id = ?", (soul_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Soul non trovata")
    return dict(row)


@app.get("/api/teams")
async def list_teams(limit: int = 20):
    """Lista tutti i team generati."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM teams ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/teams/{team_id}")
async def get_team(team_id: str):
    """Recupera un team con tutte le sue Souls."""
    with get_db() as conn:
        team = conn.execute("SELECT * FROM teams WHERE id = ?", (team_id,)).fetchone()
        if not team:
            raise HTTPException(status_code=404, detail="Team non trovato")
        souls = conn.execute(
            "SELECT * FROM souls WHERE team_id = ? ORDER BY created_at", (team_id,)
        ).fetchall()
    return {**dict(team), "souls": [dict(s) for s in souls]}


@app.get("/api/health")
async def health():
    with get_db() as conn:
        soul_count = conn.execute("SELECT COUNT(*) FROM souls").fetchone()[0]
        team_count = conn.execute("SELECT COUNT(*) FROM teams").fetchone()[0]
    return {
        "status": "alive",
        "engine": "Spirit Animals v0.2",
        "stack": "FastAPI + Claude + fal.ai + SQLite",
        "by": "Astra OS",
        "souls_generated": soul_count,
        "teams_generated": team_count,
    }


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

@app.on_event("startup")
async def startup():
    init_db()


# Serve frontend
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
async def index():
    return FileResponse("static/index.html")

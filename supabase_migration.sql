-- Spirit Animals — Supabase Migration
-- Eseguire da Supabase Dashboard > SQL Editor (progetto oimlamjilivrcnhztwvj)

-- Teams PRIMA (referenziata da souls)
CREATE TABLE IF NOT EXISTS public.teams (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL,
    description TEXT,
    soul_count INTEGER DEFAULT 0,
    synergy JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS public.souls (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    team_id UUID REFERENCES public.teams(id) ON DELETE SET NULL,
    name TEXT NOT NULL,
    role TEXT,
    animal TEXT NOT NULL,
    emoji TEXT,
    soul_name TEXT NOT NULL,
    archetype TEXT,
    traits JSONB DEFAULT '[]',
    superpower TEXT,
    shadow TEXT,
    motto TEXT,
    color TEXT DEFAULT '#00d4aa',
    element TEXT,
    collaboration_style TEXT,
    avatar_url TEXT,
    raw_input TEXT,
    created_at TIMESTAMPTZ DEFAULT now()
);

-- RLS
ALTER TABLE public.teams ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.souls ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Public read teams" ON public.teams FOR SELECT USING (true);
CREATE POLICY "Service insert teams" ON public.teams FOR INSERT WITH CHECK (true);
CREATE POLICY "Public read souls" ON public.souls FOR SELECT USING (true);
CREATE POLICY "Service insert souls" ON public.souls FOR INSERT WITH CHECK (true);

-- Indici
CREATE INDEX IF NOT EXISTS idx_souls_team_id ON public.souls(team_id);
CREATE INDEX IF NOT EXISTS idx_souls_created_at ON public.souls(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_teams_created_at ON public.teams(created_at DESC);

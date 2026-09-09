create table if not exists public.leaderboard (
    user_id bigint not null references public.users (id) on delete cascade,
    month text not null,
    first_name text,
    username text,
    chess_username text not null,
    points integer not null default 0,
    wins integer not null default 0,
    draws integer not null default 0,
    losses integer not null default 0,
    games integer not null default 0,
    chess_points integer not null default 0,
    casino_balance integer not null default 0,
    exchanged_chess_points integer not null default 0,
    daily_bonus_date date,
    daily_streak integer not null default 0,
    updated_at timestamptz not null default now(),
    primary key (user_id, month)
);

create index if not exists leaderboard_month_score_idx
on public.leaderboard (month, points desc, wins desc, games desc);

alter table public.leaderboard
    add column if not exists chess_points integer not null default 0;

alter table public.leaderboard
    add column if not exists casino_balance integer not null default 0;

alter table public.leaderboard
    add column if not exists daily_bonus_date date;

alter table public.leaderboard
    add column if not exists exchanged_chess_points integer not null default 0;

alter table public.leaderboard
    add column if not exists daily_streak integer not null default 0;

create table if not exists public.duels (
    id bigint generated always as identity primary key,
    challenger_id bigint not null references public.users (id) on delete cascade,
    opponent_id bigint not null references public.users (id) on delete cascade,
    amount integer not null check (amount > 0),
    status text not null default 'pending',
    opponent_side text,
    winner_id bigint references public.users (id) on delete set null,
    created_at timestamptz not null default now()
);

alter table public.duels
    add column if not exists opponent_side text;
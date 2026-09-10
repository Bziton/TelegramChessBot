alter table public.users
    add column if not exists chess_username text;

alter table public.users
    add column if not exists points integer not null default 0;

alter table public.users
    add column if not exists wins integer not null default 0;

alter table public.users
    add column if not exists draws integer not null default 0;

alter table public.users
    add column if not exists losses integer not null default 0;

alter table public.users
    add column if not exists games integer not null default 0;

alter table public.users
    add column if not exists chess_points integer not null default 0;

alter table public.users
    add column if not exists casino_balance integer not null default 0;

alter table public.users
    add column if not exists exchanged_chess_points integer not null default 0;

alter table public.users
    add column if not exists daily_bonus_date date;

alter table public.users
    add column if not exists daily_streak integer not null default 0;

alter table public.users
    add column if not exists updated_at timestamptz not null default now();

update public.users as u
set
    chess_username = coalesce(u.chess_username, l.chess_username),
    points = l.points,
    wins = l.wins,
    draws = l.draws,
    losses = l.losses,
    games = l.games,
    chess_points = l.chess_points,
    casino_balance = l.casino_balance,
    exchanged_chess_points = l.exchanged_chess_points,
    daily_bonus_date = l.daily_bonus_date,
    daily_streak = l.daily_streak,
    updated_at = now()
from public.leaderboard as l
where l.user_id = u.id
  and l.month = to_char(current_date, 'YYYY-MM');

create index if not exists users_points_idx
on public.users (points desc, wins desc, games desc);

drop table if exists public.leaderboard;

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

create table if not exists public.game_history (
    id bigint generated always as identity primary key,
    user_id bigint not null references public.users (id) on delete cascade,
    game_type text not null,
    bet integer not null default 0 check (bet >= 0),
    change integer not null default 0,
    result text not null,
    details jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now()
);

create index if not exists game_history_user_created_idx
on public.game_history (user_id, created_at desc);
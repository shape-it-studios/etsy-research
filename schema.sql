-- Etsy product research: tables
-- Run once in the Supabase SQL editor, then run scores.sql.
-- Listing text (titles, tags) is deliberately NOT stored: it is classified in memory
-- during each run and only our own derived fields (theme, subject, size) are kept.

create table if not exists shops (
  shop_id      bigint primary key,
  shop_name    text not null,
  tracked      boolean not null default true,
  is_own       boolean not null default false,
  source       text,                      -- 'seed' or 'discovered'
  first_seen   date not null default current_date
);

create table if not exists shop_snapshots (
  shop_id                bigint not null references shops(shop_id),
  snap_date              date   not null,
  transaction_sold_count integer,
  review_count           integer,
  review_average         numeric,
  num_favorers           integer,
  listing_active_count   integer,
  primary key (shop_id, snap_date)
);

create table if not exists listings (
  listing_id          bigint primary key,
  shop_id             bigint not null,
  original_created_at timestamptz,
  price               numeric,
  currency            text,
  is_digital          boolean,
  first_seen          date not null default current_date,
  last_seen           date
);
create index if not exists listings_shop_idx on listings(shop_id);

create table if not exists listing_snapshots (
  listing_id   bigint not null references listings(listing_id),
  snap_date    date   not null,
  views        integer,
  num_favorers integer,
  price        numeric,
  state        text,
  primary key (listing_id, snap_date)
);

-- Reviews for tracked shops (rating and date only, no review text)
create table if not exists reviews (
  review_key  text primary key,
  shop_id     bigint not null,
  listing_id  bigint,
  rating      integer,
  created_at  timestamptz not null
);
create index if not exists reviews_listing_idx on reviews(listing_id, created_at);
create index if not exists reviews_shop_idx on reviews(shop_id, created_at);

-- Where listings rank for each seed keyword, per day
create table if not exists search_hits (
  keyword    text   not null,
  snap_date  date   not null,
  listing_id bigint not null,
  shop_id    bigint not null,
  rank       integer not null,
  primary key (keyword, snap_date, listing_id)
);

-- Our own classification of each listing (derived data, kept permanently)
create table if not exists listing_class (
  listing_id    bigint primary key,
  fit           text,        -- 'yes' | 'maybe' | 'no'
  theme         text,        -- christmas, halloween, easter, july4, ...
  subject       text,        -- canonical subject, e.g. 'santa sleigh'
  size_ft       numeric,
  licensed_ip   boolean,
  reason        text,
  model         text,
  classified_at timestamptz not null default now()
);

-- Etsy product research: scoring views. Safe to re-run after edits.
-- Weights live in listing_scores (10 per review, 1 per favourite, 0.02 per view).
-- They only decide how a shop's real sales are split between its listings;
-- shop totals always come from Etsy's public transaction_sold_count.

-- 1. Change in views/favourites per listing over the last 30 days of snapshots
create or replace view listing_window as
with b as (
  select listing_id, min(snap_date) d0, max(snap_date) d1
  from listing_snapshots
  where snap_date >= current_date - 30
  group by listing_id
)
select b.listing_id, l.shop_id, b.d0, b.d1, (b.d1 - b.d0) as days,
       s1.views - s0.views               as d_views,
       s1.num_favorers - s0.num_favorers as d_favs
from b
join listings l          on l.listing_id = b.listing_id
join listing_snapshots s0 on s0.listing_id = b.listing_id and s0.snap_date = b.d0
join listing_snapshots s1 on s1.listing_id = b.listing_id and s1.snap_date = b.d1;

-- 2. Real sales per tracked shop over the same window
create or replace view shop_window as
with b as (
  select shop_id, min(snap_date) d0, max(snap_date) d1
  from shop_snapshots
  where snap_date >= current_date - 30
  group by shop_id
)
select b.shop_id, (b.d1 - b.d0) as days,
       s1.transaction_sold_count - s0.transaction_sold_count as d_sales
from b
join shop_snapshots s0 on s0.shop_id = b.shop_id and s0.snap_date = b.d0
join shop_snapshots s1 on s1.shop_id = b.shop_id and s1.snap_date = b.d1;

-- 3. Per-listing scores: demand signal for every listing, sales estimate for tracked shops
create or replace view listing_scores as
with lw as (
  select w.*, coalesce(r.n, 0) as d_reviews,
         10 * coalesce(r.n, 0)
         + greatest(coalesce(w.d_favs, 0), 0)
         + 0.02 * greatest(coalesce(w.d_views, 0), 0) as weight
  from listing_window w
  left join lateral (
    select count(*) n from reviews rv
    where rv.listing_id = w.listing_id
      and rv.created_at::date > w.d0 and rv.created_at::date <= w.d1
  ) r on true
),
tot as (select shop_id, sum(weight) w_sum from lw group by shop_id)
select lw.listing_id, lw.shop_id, lw.days, lw.d_views, lw.d_favs, lw.d_reviews, lw.weight,
       case when lw.days > 0 then round(lw.weight * 30.0 / lw.days, 1) end as demand_signal_30d,
       sw.d_sales as shop_sales_window,
       case when sw.days >= 7 and t.w_sum > 0
            then round(sw.d_sales * lw.weight / t.w_sum * 30.0 / sw.days, 1) end as est_sales_30d
from lw
join tot t using (shop_id)
left join shop_window sw using (shop_id);

-- 4. Cold-start view: lifetime sales split by lifetime review share (Alura-style),
--    usable from day one before 30-day history builds up
create or replace view listing_lifetime as
with latest_shop as (
  select distinct on (shop_id) shop_id, transaction_sold_count
  from shop_snapshots order by shop_id, snap_date desc
),
rev as (select listing_id, shop_id, count(*) n from reviews group by 1, 2),
shop_rev as (select shop_id, sum(n) n from rev group by 1)
select rev.listing_id, rev.shop_id, rev.n as reviews_total,
       round(ls.transaction_sold_count * rev.n::numeric / nullif(sr.n, 0)) as lifetime_est_sales
from rev
join shop_rev sr using (shop_id)
join latest_shop ls using (shop_id);

-- 5. Product ideas (theme + subject) ranked.
--    top_shop_share = biggest single shop's share of the demand signal (1.0 = one shop owns it)
create or replace view concept_scores as
with x as (
  select c.theme, c.subject, l.listing_id, l.shop_id, l.price, l.currency, l.original_created_at,
         ls.est_sales_30d, ls.demand_signal_30d, lt.lifetime_est_sales,
         s.is_own
  from listing_class c
  join listings l using (listing_id)
  left join shops s on s.shop_id = l.shop_id
  left join listing_scores ls using (listing_id)
  left join listing_lifetime lt using (listing_id)
  where c.fit in ('yes', 'maybe') and not coalesce(c.licensed_ip, false)
    and l.last_seen >= current_date - 3
),
by_shop as (
  select theme, subject, shop_id, sum(coalesce(demand_signal_30d, 0)) s from x group by 1, 2, 3
),
conc as (
  select theme, subject, round(max(s) / nullif(sum(s), 0), 2) as top_shop_share
  from by_shop group by 1, 2
)
select x.theme, x.subject,
       count(*)                                         as listings,
       count(distinct x.shop_id)                        as shops,
       round(sum(x.est_sales_30d))                      as est_sales_30d,
       round(sum(x.est_sales_30d * x.price) filter (where x.currency = 'USD')) as est_revenue_30d_usd,
       round(sum(x.demand_signal_30d))                  as demand_signal_30d,
       sum(x.lifetime_est_sales)                        as lifetime_est_sales,
       round(avg(x.price) filter (where x.currency = 'USD'), 2) as avg_price_usd,
       conc.top_shop_share,
       count(*) filter (where x.original_created_at > now() - interval '180 days'
                          and coalesce(x.est_sales_30d, 0) >= 1) as young_listings_selling,
       bool_or(coalesce(x.is_own, false))               as we_sell_it
from x
join conc using (theme, subject)
group by x.theme, x.subject, conc.top_shop_share
order by est_sales_30d desc nulls last, lifetime_est_sales desc nulls last;

-- 6. Shops worth looking at that we are not tracking yet
create or replace view candidate_shops as
select h.shop_id, count(distinct h.keyword) keywords, count(distinct h.listing_id) fit_listings,
       min(h.rank) best_rank
from search_hits h
join listing_class c on c.listing_id = h.listing_id and c.fit in ('yes', 'maybe')
left join shops s on s.shop_id = h.shop_id
where h.snap_date >= current_date - 7 and s.shop_id is null
group by h.shop_id
order by fit_listings desc;

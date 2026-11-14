# Etsy product research

Finds which yard-art template subjects sell best on Etsy, so Shape It Studios knows what to make next.

Every morning a GitHub Action snapshots competitor shops and keyword search results through Etsy's Open API, classifies new listings with Claude, and stores numbers in Supabase. SQL views turn the daily changes into a ranked list of product ideas.

## How the estimate works

- Each tracked shop's **real** daily sales come from Etsy's public `transaction_sold_count`.
- Those sales are split across the shop's listings by recent growth in reviews (weight 10), favourites (1) and views (0.02).
- Listings are grouped into ideas (theme + subject, e.g. christmas / santa sleigh), with licensed IP and non-fitting products filtered out.

Text (titles, tags) is used in memory for classification and never stored, to stay on the right side of Etsy's limits on caching listing content. Check Etsy's current API terms before you launch.

## Setup

1. **Etsy API key**: register an app at https://www.etsy.com/developers/register. Describe it honestly (market research for your own shop, read-only public data). Approval can take a few days. Copy the keystring and shared secret.
2. **Supabase**: create a project. In the SQL editor run `schema.sql`, then `scores.sql`. Copy the connection string (Project Settings > Database; use the session pooler URI, port 5432).
3. **Anthropic API key**: from https://console.anthropic.com. Classification uses a small model and only runs on listings it hasn't seen, so cost is low after the first run.
4. **GitHub**: push this folder to a private repo. Under Settings > Secrets and variables > Actions add `ETSY_KEYSTRING`, `ETSY_SHARED_SECRET`, `DATABASE_URL`, `ANTHROPIC_API_KEY`.
5. **First run**: Actions tab > Etsy daily snapshot > Run workflow. Check the log for "Done. Etsy API calls: N".

Run locally instead with:

```
pip install -r requirements.txt
export ETSY_KEYSTRING=... ETSY_SHARED_SECRET=... DATABASE_URL=... ANTHROPIC_API_KEY=...
python snapshot.py
```

## Reading the results

```sql
select * from concept_scores;      -- ranked product ideas
select * from listing_scores;      -- per-listing detail
select * from candidate_shops;     -- shops showing up in search that aren't tracked yet
select * from listing_class where fit = 'maybe';   -- review these to tune the fit definition
```

`concept_scores` columns:

| column | meaning |
|---|---|
| est_sales_30d | estimated sales per 30 days, anchored to real shop totals (needs 7+ days of history) |
| demand_signal_30d | weighted growth score; available for every listing, including untracked shops |
| lifetime_est_sales | cold-start number from day one: lifetime sales split by review share |
| top_shop_share | biggest shop's share of demand (1.0 = one shop owns the idea) |
| young_listings_selling | listings under 6 months old that are selling: evidence the idea is still open |
| we_sell_it | Shape It already has a listing for this subject |

**Expect:** `lifetime_est_sales` on day one, `est_sales_30d` after a week, trustworthy trends after a month. Seasonal patterns need a year.

## Tuning

- Keywords, seed shops, auto-tracking limits and the fit definition are in `config.json`.
- Review weights are at the top of `listing_scores` in `scores.sql`. They only change how a shop's real sales are divided between its listings, never the totals.
- To re-classify everything after changing the fit definition: `truncate listing_class;`

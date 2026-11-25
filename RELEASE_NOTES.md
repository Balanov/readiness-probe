# readiness-probe 1.0.0

First release of the readiness probe: what an AI shopping agent can actually read from a
storefront.

## What it measures

Anonymous GET only — no browser automation, no authorization, exactly the way a buyer
agent works. One script, `readiness_probe.py`. Python 3.10+, no dependencies.

```bash
python readiness_probe.py --targets targets.json --out out
```

Per storefront the probe records the HTTP status of the anonymous GET, the size of the
server-rendered HTML and the amount of visible text, the `application/ld+json` blocks and
the `Product`/`Offer` nodes inside them, per-field coverage of the required product
fields, microdata products, images declared at 600×600 or larger, product feed links,
agent-checkout traces (`checkout_sessions`, `/.well-known/ucp`, YCP) and agent surfaces
(`/robots.txt`, `/agents.txt`, `llms.txt`, `/.well-known/agent.json`,
`/.well-known/ucp`, `/openapi.json`). The required fields and the exact rule used to
find each of them are written down in the README, so every number in the report can be
re-checked.

Verdicts are thresholds on `agent_visible_share`: READY at 70 and above, PARTIAL from 35
to 69, INVISIBLE below 35.

## What the run shows

21 storefronts plus a 7-storefront control group. The raw results are committed in
`results/` and `results_control/`.

- None of the 75 product cards found carries the full required field set.
- `sku` is present in 2.7% of those 75 cards, against 100% in the control group — a
  37× difference.
- `availability` is present in 2.7% of the cards, against 40% in the control group.
- `name`, `price` and `priceCurrency` are present in 100% of the cards, `brand` in
  98.7%.
- 6 of the 17 reachable storefronts (35%) do not serve an anonymous agent at all: DNS 401,
  Спортмастер 401, ВсеИнструменты 403, Ozon 307, Wildberries 498, Avito 439.

`gtin` is 0% in the main set and 0% in the control group, so it is not used as an
argument: comparing two zeroes is uninformative. The arguments are built on `sku` and
`availability`.

## What the proof shows

The control group is a publication condition, not a formality. Every CI run re-measures
it against the live sites and fails the build if the Samsung RU control target loses its
`sku` coverage, if no product entities are parsed for it, or if the control `sku` rate
stops being higher than the rate in the main set. The raw control results are uploaded
as the `readiness-control-results` artifact on every run.

Six iterations of the control group caught six real defects in the probe — image links
counted as cards, sitemap files counted as products, unpacked `.xml.gz` archives, Russian
storefront URLs without `/product/`, the markup wrapper returned instead of the entity,
and price and availability read from an unnamed `Offer` node. Every one of those defects
changed the numbers.

## Known boundaries

- It does not judge card quality by eye.
- It does not check prices or stock inside a personal account.
- It does not model the behaviour of a specific assistant.
- It does not measure conversion or money — only the structural availability of the
  catalogue.
- It does not bypass protection: browser automation is deliberately not used, so the
  result is what an ordinary agent sees, not what a permitted robot would see.

If a storefront serves only an SPA shell or stands behind a WAF, that is the measurement
result and not a tool failure; `pages_probed`, `sitemap_product_urls` and `notes` in
the JSON show it.

MIT.

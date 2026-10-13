# Mapping proposal — for review before anything is written

Nothing here has been applied. Two parts: names that belong in themes we already
have, and one theme we do not have. Momentum and rank are as of 2026-10-12.

A note on how this list was NOT built: ETF holdings look like an obvious source
and are a bad one. ARKX holds NVDA, AMD and AMZN; ARKQ holds GOOG; ARKF holds
META. Mapping from holdings would put Nvidia in Space because a fund manager
bought it. Every name below is here on what the company does.

---

## Part 1 — additions to existing themes

### Confident (14)

Each is a straightforward fit for a link that already exists.

| Ticker | Company | Mom | Theme → link | Why |
|---|---|---|---|---|
| `CIEN` | Ciena | 94 | ai → optical | Optical transport systems — the long-haul and data-centre interconnect gear |
| `MTSI` | MACOM | 94 | ai → optical | RF and photonic components for optical links |
| `VIAV` | Viavi Solutions | 92 | ai → optical | Optical test and measurement; 3D sensing |
| `ADI` | Analog Devices | 93 | ai → power_delivery | Power management and signal chain on AI boards |
| `MXL` | MaxLinear | 95 | ai → networking | Interconnect and broadband silicon |
| `SYNA` | Synaptics | 97 | ai → compute | Edge-AI silicon and sensing |
| `PENG` | Penguin Solutions | 99 | ai → systems | Builds and integrates AI compute clusters |
| `COHU` | Cohu | 94 | ai → equipment | Semiconductor test handlers and inspection |
| `PDFS` | PDF Solutions | 94 | ai → equipment | Yield analytics for fabs |
| `KEYS` | Keysight | 94 | ai → equipment | Electronic test and measurement across chip and network design |
| `NTAP` | NetApp | 95 | ai → memory | Enterprise storage for AI workloads |
| `FFIV` | F5 | 92 | cyber → network | Application delivery and security; 4.5% of HACK, 3.6% of CIBR |
| `NTSK` | Netskope | 92 | cyber → cloud_app | Cloud access security broker |
| `ATEN` | A10 Networks | 92 | cyber → network | Application delivery and DDoS defence |

### Marginal (6) — judgement calls, happy to drop any

| Ticker | Company | Mom | Theme → link | The argument, and against it |
|---|---|---|---|---|
| `P` | Everpure | 96 | ai → memory | Storage hardware, held by SKYY and XLK. Against: the business is broader than AI |
| `GRAL` | GRAIL | 95 | genomics → diagnostics | Multi-cancer early detection from blood. Against: single-product, pre-revenue risk |
| `AZTA` | Azenta | 94 | genomics → tools | Sample management and genomic services. Against: thin link already |
| `KOD` | Kodiak Sciences | 100 | genomics → editing | Strongest name in the market, but it is an eye-disease biotech — only loosely genomics |
| `MTD` | Mettler Toledo | 95 | genomics → tools | Lab instruments. Against: sells to everyone, not genomics-specific |
| `PTC` | PTC Inc. | 99 | robotics → automation | Industrial design and PLM software. Against: as much manufacturing IT as robotics |

### Deliberately NOT mapped (10)

Strong companies having a good quarter. Not megatrends. Forcing them in would
make the map worse, and the Leaders tab already shows their industry instead.

`RXO` trucking · `MAT` Mattel · `KSS` Kohl's · `VFC` apparel · `AEO` apparel
retail · `STNE` Brazilian payments · `INTR` Brazilian bank · `GNRC` generators ·
`OPCH` home infusion · `CBRL` Cracker Barrel · `GME` GameStop · `CCL` Carnival

---

## Part 2 — photonics: expand the existing link, do not build a new theme

I pitched a standalone `photonics` theme. Having priced every candidate, I think
that is the wrong call, and the numbers are why.

Every name scored, in order:

| Ticker | Company | Mom | Already mapped to |
|---|---|---|---|
| `CIEN` | Ciena | 94 | — |
| `MTSI` | MACOM | 94 | — |
| `MRVL` | Marvell | 92 | **ai** |
| `VIAV` | Viavi | 92 | — |
| `LITE` | Lumentum | 90 | **ai, quantum** |
| `CRDO` | Credo | 85 | **ai** |
| `COHR` | Coherent | 74 | **ai, quantum** |
| `AVGO` | Broadcom | 69 | **ai** |
| `IPGP` | IPG Photonics | 67 | — |
| `AXTI` | AXT | 66 | — |
| `GLW` | Corning | 63 | **ai** |
| `LPTH` | LightPath | 47 | — |
| `LWLG` | Lightwave Logic | 43 | — |
| `LASR` | nLIGHT | 34 | — |
| `POET` | POET Technologies | 29 | — |
| `INFN` | Infinera | — | not in universe |

Two things fall out of that, both against a new theme:

**The median would be 69, not 88.** The industry rollup showed `Communications
Equipment` at 88 because that bucket is a narrower, stronger set. A real
photonics chain has to include the lasers and substrates, and that tail —
`LASR` 34, `POET` 29, `LWLG` 43, `LPTH` 47 — drags it to the middle of the pack.
It would arrive on the board as an unremarkable theme, which is not the story
the original signal suggested.

**Six of the fifteen are already in the AI chain.** A new theme would duplicate
40% of itself against `ai`, and five of those six sit in `ai/optical` or next to
it. The hubs panel handles multi-theme names, but this is not one company
catching two trends — it is the same link written twice.

The honest reading of the original signal is narrower than "photonics is hot".
It is **optical interconnect for AI is hot** — and that is precisely what
`ai/optical` already is, with four members when it should have eight.

### What I would do instead

Add to `ai/optical`: `CIEN` 94, `MTSI` 94, `VIAV` 92 — already in the confident
list above. That takes the link from 4 members to 7 and captures the entire
strong end of the cluster.

Leave `LASR`, `POET`, `LWLG`, `LPTH`, `AXTI`, `IPGP` unmapped. They are
photonics companies, but they are small, weak, and tied to industrial lasers and
telecom capex rather than to any trend we track. Mapping them would add six
names and lower the quality of the map.

If you still want a standalone photonics theme, say so and I will build it — but
you should expect it to show around 69 and overlap AI heavily, and I would rather
tell you that now than after it is on the site.

---

## What this would change

Coverage on the current top 40 goes from 13 mapped to roughly 25. Not 40 — about
ten of the top names genuinely belong to no theme, and that is the right answer
rather than a gap. The Leaders header counts it either way, so the number falls
on its own as the map improves.

## To apply

Reply with which parts you want:

1. **Confident list** (14 names) — my recommendation, apply as-is
2. **Any of the six marginals** — name which, or drop them all
3. **Standalone photonics theme** — against my advice above, but your call

I will write it as a seed script in the same shape as `seed_theme_*.py`, so it is
re-runnable, reviewable in git, and reversible the way the six delisted members
were.
